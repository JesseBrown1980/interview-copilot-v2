"""
zoom_adapter.py — Zoom desktop client integration for interview copilot v2.

Mission: capture Zoom meeting audio for local STT + translation + AI suggestion
without touching the Zoom Bot SDK (no Marketplace app approval needed).

Architecture:
    Zoom desktop client (any version)
        |
        v  (renders audio to default output device)
    Windows audio engine
        |
        v  (per-process or full-system loopback)
    WASAPI loopback capture  <-- this adapter
        |
        v  (16-bit PCM @ 16 kHz mono frames)
    B03 audio_capture.AudioCaptureStream (consumer)
        |
        v
    STT / translation / AI suggestion pipeline (B04 / B06 / B07)

Contract with B03 (audio_capture.py):
    The B03 module is expected to expose an `AudioCaptureStream` class with:
        - `start(source: str, **kwargs) -> None`
        - `stop() -> None`
        - `is_running() -> bool`
        - `flush() -> None`        # forces tail-of-buffer transcript emit
        - `set_meeting_session_id(mid: str) -> None`
    `source` values used here:
        - "system_audio"                      (full loopback)
        - "system_audio:pid=<int>"            (per-process loopback, Win10+ 20H1)
    If B03 ships a different signature, only the small `_drive_capture()` helper
    needs to change — the detection / lifecycle / metadata logic is independent.

Privacy: nothing leaves device. This adapter only routes audio frames to the
local B03 stream. Egress (translate / Claude API) is gated by operator opt-in
elsewhere (B04 / B07).
"""

from __future__ import annotations

import hashlib
import logging
import re
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Iterable, List, Optional, Protocol


log = logging.getLogger("zoom_adapter")


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Process name patterns. Windows Zoom desktop client uses "Zoom.exe" (main UI)
# and "CptHost.exe" (shared content / screen share helper). Older builds also
# spawn "Zoom_launcher.exe". The conferencing audio always lives in Zoom.exe.
ZOOM_PROCESS_PATTERNS: tuple[str, ...] = (
    "Zoom.exe",
    "zoom.exe",
    "CptHost.exe",          # screen-share helper; secondary signal
    "Zoom_launcher.exe",    # legacy
)

# Window title patterns. Zoom uses these substrings:
#   - "Zoom Meeting"                  -- generic meeting window (no topic set)
#   - "Zoom - <topic>"                -- meeting with a topic
#   - "Zoom Webinar"                  -- webinar mode
#   - "Meeting"                       -- some locales / older builds
# We require the process to be a Zoom process AND the title to match one of
# these to avoid false-positives from e.g. a browser tab titled "Zoom".
ZOOM_MEETING_TITLE_PATTERNS: tuple[re.Pattern[str], ...] = (
    # End-of-string anchors are deliberate: we must reject browser-tab titles
    # like "Zoom Meeting on Mozilla Firefox" that contain our keyword but are
    # not Zoom desktop windows. PID-filtering (select_meeting_window) is the
    # primary defense; this is defense-in-depth.
    re.compile(r"^Zoom Meeting\s*$", re.IGNORECASE),
    re.compile(r"^Zoom - .+", re.IGNORECASE),
    re.compile(r"^Zoom Webinar(\s*-\s*.+)?\s*$", re.IGNORECASE),
    re.compile(r"^Meeting$", re.IGNORECASE),  # narrow exact match only
)

# Poll cadence for the watcher thread. 2 s strikes a balance between snappy
# auto-start at meeting-begin and CPU/wakeup cost while idle.
POLL_INTERVAL_SECONDS: float = 2.0

# Grace period after the last Zoom process disappears before we call stop().
# Avoids flapping if Zoom briefly relaunches (e.g. switching meetings).
STOP_GRACE_SECONDS: float = 5.0


# ---------------------------------------------------------------------------
# Protocols (typed shims — let us mock cleanly in tests)
# ---------------------------------------------------------------------------


class ProcessInfo(Protocol):
    """Subset of psutil.Process we care about. Mocked in tests."""

    @property
    def pid(self) -> int: ...

    def name(self) -> str: ...


class WindowInfo(Protocol):
    """Subset of pywin32 / pygetwindow we care about. Mocked in tests."""

    @property
    def title(self) -> str: ...

    @property
    def pid(self) -> int: ...


class AudioCaptureStream(Protocol):
    """B03's AudioCaptureStream contract. We talk to this; B03 owns it."""

    def start(self, source: str, **kwargs) -> None: ...

    def stop(self) -> None: ...

    def is_running(self) -> bool: ...

    def flush(self) -> None: ...

    def set_meeting_session_id(self, mid: str) -> None: ...


# ---------------------------------------------------------------------------
# Process / window enumeration shims
# ---------------------------------------------------------------------------


def _default_list_processes() -> List[ProcessInfo]:
    """Real implementation: psutil. Tests inject a stub instead."""
    try:
        import psutil  # type: ignore
    except ImportError:
        log.warning("psutil not installed; Zoom detection disabled")
        return []
    out: List[ProcessInfo] = []
    for p in psutil.process_iter(attrs=["pid", "name"]):
        try:
            # Cast through info dict to avoid raising on access-denied procs.
            info = p.info
            out.append(_PsutilShim(pid=info["pid"], name_=info.get("name") or ""))
        except Exception:  # noqa: BLE001 -- process may vanish mid-iter
            continue
    return out


def _default_list_windows() -> List[WindowInfo]:
    """Real implementation: pywin32 EnumWindows. Tests inject a stub."""
    try:
        import win32gui  # type: ignore
        import win32process  # type: ignore
    except ImportError:
        log.warning("pywin32 not installed; window-title metadata disabled")
        return []

    found: List[WindowInfo] = []

    def _cb(hwnd: int, _: object) -> bool:
        if not win32gui.IsWindowVisible(hwnd):
            return True
        title = win32gui.GetWindowText(hwnd)
        if not title:
            return True
        try:
            _, pid = win32process.GetWindowThreadProcessId(hwnd)
        except Exception:  # noqa: BLE001
            return True
        found.append(_WindowShim(title=title, pid=pid))
        return True

    try:
        win32gui.EnumWindows(_cb, None)
    except Exception as e:  # noqa: BLE001
        log.warning("EnumWindows failed: %s", e)
    return found


@dataclass(frozen=True)
class _PsutilShim:
    pid: int
    name_: str

    def name(self) -> str:
        return self.name_


@dataclass(frozen=True)
class _WindowShim:
    title: str
    pid: int


# ---------------------------------------------------------------------------
# Pure helpers (heavy unit-test coverage)
# ---------------------------------------------------------------------------


def is_zoom_process(proc: ProcessInfo) -> bool:
    """True if a process object looks like a Zoom desktop client process."""
    name = (proc.name() or "").strip()
    return name in ZOOM_PROCESS_PATTERNS


def find_zoom_processes(procs: Iterable[ProcessInfo]) -> List[ProcessInfo]:
    """Filter a process list down to Zoom-family processes."""
    return [p for p in procs if is_zoom_process(p)]


def is_zoom_meeting_title(title: str) -> bool:
    """True if a window title indicates an active Zoom meeting (not just the
    Zoom home/contacts window)."""
    if not title:
        return False
    return any(p.search(title) for p in ZOOM_MEETING_TITLE_PATTERNS)


def extract_meeting_topic(title: str) -> Optional[str]:
    """Pull the meeting topic out of a Zoom window title, if present.

    Examples:
        "Zoom Meeting"                  -> None    (no topic exposed)
        "Zoom - Daily Standup"          -> "Daily Standup"
        "Zoom Meeting - Daily Standup"  -> "Daily Standup"
        "Zoom Webinar - Q3 All-Hands"   -> "Q3 All-Hands"

    Zoom does NOT expose the meeting ID / passcode in the window title for
    privacy reasons. Topic is the best we get from the OS layer.
    """
    if not title:
        return None
    # Pattern: "Zoom[ Meeting| Webinar] - <topic>"
    m = re.match(
        r"^Zoom(?:\s+Meeting|\s+Webinar)?\s*-\s*(.+?)\s*$",
        title,
        flags=re.IGNORECASE,
    )
    if m:
        topic = m.group(1).strip()
        # Strip trailing window-state suffixes Zoom sometimes appends.
        topic = re.sub(r"\s*\(.*\)\s*$", "", topic).strip()
        return topic or None
    return None


def hash_meeting_session_id(topic: Optional[str], pid: int, start_ts: float) -> str:
    """Stable per-meeting session id for fabric envelopes.

    Inputs:
        topic     -- window-title topic if available, else None
        pid       -- Zoom.exe PID at meeting-start time
        start_ts  -- unix timestamp of detection (seconds, float)

    We hash a normalized string so the same meeting topic across sessions
    yields a different id (privacy: don't make meeting topics linkable).
    pid+start_ts make it unique per session even if topic is identical or
    missing.
    """
    payload = f"{(topic or '').strip().lower()}|{pid}|{int(start_ts)}"
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    # 16 hex chars is plenty for a per-session id and matches the fabric
    # convention used elsewhere (cube ids, tool-citizen PIDs).
    return f"zoom-{digest[:16]}"


def select_meeting_window(
    zoom_pids: Iterable[int],
    windows: Iterable[WindowInfo],
) -> Optional[WindowInfo]:
    """Among visible windows owned by Zoom processes, return the one whose
    title matches a meeting pattern. None if no meeting window is open
    (i.e. Zoom is running but the operator is on the contacts/home screen)."""
    zoom_pid_set = set(zoom_pids)
    for w in windows:
        if w.pid not in zoom_pid_set:
            continue
        if is_zoom_meeting_title(w.title):
            return w
    return None


# ---------------------------------------------------------------------------
# Per-process audio routing
# ---------------------------------------------------------------------------


@dataclass
class CaptureSource:
    """What to ask B03 to capture from.

    mode:
        "per_process"     -- WASAPI process-loopback bound to a PID (Win10 20H1+)
        "system_full"     -- default-render-endpoint loopback (catches ALL audio)
        "none"            -- no capture (Zoom not running / no meeting)

    pid is only meaningful for "per_process".
    """

    mode: str
    pid: Optional[int] = None
    warnings: List[str] = field(default_factory=list)

    def to_source_string(self) -> str:
        if self.mode == "per_process" and self.pid is not None:
            return f"system_audio:pid={self.pid}"
        if self.mode == "system_full":
            return "system_audio"
        return ""


def choose_capture_source(
    zoom_pid: Optional[int],
    process_loopback_available: bool,
) -> CaptureSource:
    """Decide what source string to hand B03.

    Strategy:
        - If we know the Zoom PID AND process-loopback is supported on this
          OS, ask for per-process capture (clean: only Zoom audio).
        - Else fall back to full-system loopback with a warning that other
          audio (Spotify, browser tabs, system sounds) will be in the stream.
        - If Zoom is not running, return mode="none".
    """
    if zoom_pid is None:
        return CaptureSource(mode="none")
    if process_loopback_available:
        return CaptureSource(mode="per_process", pid=zoom_pid)
    return CaptureSource(
        mode="system_full",
        warnings=[
            "Per-process loopback unavailable on this OS build; capturing ALL "
            "system audio. Pause Spotify / other audio sources to keep the "
            "transcript clean."
        ],
    )


def is_process_loopback_supported() -> bool:
    """Win10 build 20H1 (19041) and later expose IAudioClient::InitializeEx
    with PROCESS_LOOPBACK. We probe pycaw / win-version cheaply.

    Returns False on any non-Windows host or on older Win10 builds. Tests
    monkey-patch this to force either branch.
    """
    import platform
    if platform.system() != "Windows":
        return False
    try:
        ver = platform.version()  # e.g. "10.0.19045"
        parts = ver.split(".")
        build = int(parts[2]) if len(parts) >= 3 else 0
        return build >= 19041
    except Exception:  # noqa: BLE001
        return False


# ---------------------------------------------------------------------------
# Lifecycle: watcher thread
# ---------------------------------------------------------------------------


@dataclass
class ZoomAdapterState:
    """Public, inspectable state. Tests assert against this."""

    running: bool = False
    zoom_detected: bool = False
    meeting_active: bool = False
    meeting_topic: Optional[str] = None
    meeting_session_id: Optional[str] = None
    capture_source: Optional[str] = None
    warnings: List[str] = field(default_factory=list)
    last_zoom_seen_ts: float = 0.0


class ZoomAdapter:
    """Watches for Zoom desktop client, drives B03 audio capture on/off.

    Public methods:
        - start_watcher() / stop_watcher()  : run/halt the poll loop in a thread
        - tick()                            : one detection+drive cycle (sync)
        - state                             : current snapshot
    The watcher is optional — tests drive tick() directly for determinism.
    """

    def __init__(
        self,
        capture_stream: AudioCaptureStream,
        list_processes: Callable[[], List[ProcessInfo]] = _default_list_processes,
        list_windows: Callable[[], List[WindowInfo]] = _default_list_windows,
        process_loopback_probe: Callable[[], bool] = is_process_loopback_supported,
        clock: Callable[[], float] = time.time,
        poll_interval: float = POLL_INTERVAL_SECONDS,
        stop_grace_seconds: float = STOP_GRACE_SECONDS,
    ) -> None:
        self._capture = capture_stream
        self._list_processes = list_processes
        self._list_windows = list_windows
        self._probe_loopback = process_loopback_probe
        self._clock = clock
        self._poll = poll_interval
        self._stop_grace = stop_grace_seconds
        self._stop_evt = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self.state = ZoomAdapterState()

    # ---- public lifecycle ----

    def start_watcher(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop_evt.clear()
        self._thread = threading.Thread(
            target=self._run, name="zoom-adapter-watcher", daemon=True
        )
        self._thread.start()

    def stop_watcher(self) -> None:
        self._stop_evt.set()
        if self._thread:
            self._thread.join(timeout=self._poll * 3)
        self._thread = None
        # Ensure audio is released on shutdown.
        if self.state.running:
            self._safe_stop_capture("watcher-stopped")

    # ---- core cycle ----

    def tick(self) -> None:
        """One detection / lifecycle step. Safe to call from tests."""
        now = self._clock()
        procs = self._list_processes()
        zooms = find_zoom_processes(procs)

        if zooms:
            self.state.zoom_detected = True
            self.state.last_zoom_seen_ts = now
            # Pick the *main* Zoom.exe PID (skip CptHost helpers when possible).
            main = next(
                (p for p in zooms if p.name() in ("Zoom.exe", "zoom.exe")),
                zooms[0],
            )
            zoom_pid = main.pid

            # Is the meeting window actually open?
            windows = self._list_windows()
            meeting_win = select_meeting_window(
                [p.pid for p in zooms], windows
            )

            if meeting_win is not None:
                self._ensure_capture_started(zoom_pid, meeting_win, now)
            else:
                # Zoom open but no meeting window -> idle on contacts screen.
                # Stop capture if it was running (operator left the meeting
                # but kept Zoom open).
                if self.state.running:
                    self._safe_stop_capture("meeting-window-closed")
                self.state.meeting_active = False
        else:
            self.state.zoom_detected = False
            # Apply grace period before tearing down audio.
            if self.state.running:
                age = now - self.state.last_zoom_seen_ts
                if age >= self._stop_grace:
                    self._safe_stop_capture("zoom-process-gone")
                    self.state.meeting_active = False

    # ---- helpers ----

    def _ensure_capture_started(
        self, zoom_pid: int, win: WindowInfo, now: float
    ) -> None:
        if self.state.running:
            # Already capturing. Update topic if it changed mid-meeting
            # (rare; happens if host changes the topic).
            topic = extract_meeting_topic(win.title)
            if topic != self.state.meeting_topic:
                self.state.meeting_topic = topic
            return

        loopback_ok = self._probe_loopback()
        source = choose_capture_source(zoom_pid, loopback_ok)
        if source.mode == "none":
            return

        topic = extract_meeting_topic(win.title)
        session_id = hash_meeting_session_id(topic, zoom_pid, now)

        try:
            self._capture.set_meeting_session_id(session_id)
            self._capture.start(source.to_source_string())
        except Exception as e:  # noqa: BLE001
            log.exception("Failed to start B03 capture: %s", e)
            return

        self.state.running = True
        self.state.meeting_active = True
        self.state.meeting_topic = topic
        self.state.meeting_session_id = session_id
        self.state.capture_source = source.to_source_string()
        self.state.warnings = list(source.warnings)
        log.info(
            "Zoom meeting detected pid=%s topic=%r session=%s source=%s",
            zoom_pid,
            topic,
            session_id,
            source.to_source_string(),
        )

    def _safe_stop_capture(self, reason: str) -> None:
        try:
            self._capture.flush()
        except Exception:  # noqa: BLE001
            log.exception("flush() failed during stop (%s)", reason)
        try:
            self._capture.stop()
        except Exception:  # noqa: BLE001
            log.exception("stop() failed (%s)", reason)
        self.state.running = False
        self.state.capture_source = None
        log.info("Zoom capture stopped: %s", reason)

    def _run(self) -> None:
        while not self._stop_evt.is_set():
            try:
                self.tick()
            except Exception:  # noqa: BLE001
                log.exception("zoom-adapter tick failed")
            self._stop_evt.wait(self._poll)


__all__ = [
    "ZoomAdapter",
    "ZoomAdapterState",
    "CaptureSource",
    "is_zoom_process",
    "find_zoom_processes",
    "is_zoom_meeting_title",
    "extract_meeting_topic",
    "hash_meeting_session_id",
    "select_meeting_window",
    "choose_capture_source",
    "is_process_loopback_supported",
    "POLL_INTERVAL_SECONDS",
    "STOP_GRACE_SECONDS",
    "ZOOM_PROCESS_PATTERNS",
]
