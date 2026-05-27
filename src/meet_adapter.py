"""
meet_adapter.py — Google Meet integration for interview-copilot-v2

Agent B06-MeetIntegration · 2026-05-27 wave

Two capture paths:
  Path A (primary):   WASAPI loopback (system audio capture) → STT.
                      Works regardless of whether Meet captions are on.
                      Identical contract to b05_zoom_adapter.py's audio path.
  Path B (optional):  Windows UI Automation scrape of Chrome's accessibility
                      tree → Meet's live-caption DOM element. Faster (~150 ms
                      latency vs ~700 ms STT) but brittle to DOM changes.

Path C (deferred):    Google Meet Captions REST API — requires Google
                      Workspace admin + OAuth scope
                      `https://www.googleapis.com/auth/meetings.space.created`.
                      Not viable for a personal-account interview-copilot.

Usage (intended; this module exposes pure detection + scrape; the orchestrator
pipes its output into the STT/router that B05+B07 wire up):

    from meet_adapter import MeetAdapter
    a = MeetAdapter()
    if a.is_meet_active():
        a.start_path_a()                 # audio loopback
        # optionally:
        a.start_path_b(poll_hz=4)        # UIA caption scrape
        for ev in a.events():
            ...                          # ev is {source, text, ts, conf}

This file is import-safe on systems without Windows / uiautomation / psutil
installed — it defers those imports behind a capability probe so unit tests
can mock cleanly.
"""

from __future__ import annotations

import logging
import platform
import queue
import re
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Iterator, List, Optional

log = logging.getLogger("meet_adapter")

# ----------------------------------------------------------------------------
# Capability probes — fail soft, never at import time
# ----------------------------------------------------------------------------

IS_WINDOWS = platform.system() == "Windows"


def _try_import(name: str):
    try:
        return __import__(name)
    except ImportError:
        return None


_psutil = _try_import("psutil")
_uia = _try_import("uiautomation") if IS_WINDOWS else None
_win32gui = _try_import("win32gui") if IS_WINDOWS else None


# ----------------------------------------------------------------------------
# Data model
# ----------------------------------------------------------------------------


@dataclass
class MeetCaptionEvent:
    """A single caption / transcript line emitted by either path."""

    source: str                  # "path_a_audio_stt" | "path_b_uia_scrape"
    text: str
    ts: float                    # monotonic seconds
    speaker: Optional[str] = None
    conf: float = 0.0            # 0..1 (UIA scrape pins at 1.0; STT real)
    raw: dict = field(default_factory=dict)


@dataclass
class MeetWindow:
    """A Chrome/Edge window that appears to host a Meet call."""

    pid: int
    process_name: str
    hwnd: int                    # Windows handle (0 if cross-platform stub)
    title: str
    has_meet_url: bool = False   # True if `meet.google.com` in cmdline / title
    is_focused: bool = False


# ----------------------------------------------------------------------------
# Browser detection
# ----------------------------------------------------------------------------


CHROMIUM_PROCESS_NAMES = {"chrome.exe", "msedge.exe", "brave.exe"}
MEET_URL_PATTERN = re.compile(r"meet\.google\.com/[a-z]{3}-[a-z]{4}-[a-z]{3}", re.I)
MEET_TITLE_PATTERN = re.compile(r"(meet\.google\.com|google meet)", re.I)


def enumerate_chromium_processes(psutil_mod=_psutil) -> List[MeetWindow]:
    """List Chromium-family processes with their command lines.

    Returns one MeetWindow per *process* (not tab — Chromium uses multi-process
    per tab, so this overcounts; the caller dedupes by `meet.google.com`
    occurrence in cmdline).

    `psutil_mod` is injected for testability.
    """
    if psutil_mod is None:
        log.debug("psutil unavailable; cannot enumerate processes")
        return []
    out: List[MeetWindow] = []
    for proc in psutil_mod.process_iter(["pid", "name", "cmdline"]):
        try:
            name = (proc.info.get("name") or "").lower()
            if name not in CHROMIUM_PROCESS_NAMES:
                continue
            cmdline = " ".join(proc.info.get("cmdline") or [])
            has_meet = bool(MEET_URL_PATTERN.search(cmdline))
            out.append(
                MeetWindow(
                    pid=proc.info["pid"],
                    process_name=name,
                    hwnd=0,
                    title="",
                    has_meet_url=has_meet,
                )
            )
        except Exception:           # noqa: BLE001 — psutil raises many subclasses
            continue
    return out


def enumerate_chromium_windows(win32gui_mod=_win32gui) -> List[MeetWindow]:
    """Enumerate top-level Windows by title; match Meet patterns.

    Window title only reflects the *active* tab — this misses background-tab
    Meet calls. Hybrid with `enumerate_chromium_processes` for full coverage.
    """
    if win32gui_mod is None:
        return []
    out: List[MeetWindow] = []

    def _cb(hwnd, _):
        if not win32gui_mod.IsWindowVisible(hwnd):
            return
        title = win32gui_mod.GetWindowText(hwnd)
        if not title:
            return
        if MEET_TITLE_PATTERN.search(title):
            out.append(
                MeetWindow(
                    pid=0,
                    process_name="chrome.exe-or-edge.exe",
                    hwnd=hwnd,
                    title=title,
                    has_meet_url=True,
                    is_focused=(hwnd == win32gui_mod.GetForegroundWindow()),
                )
            )

    win32gui_mod.EnumWindows(_cb, None)
    return out


def detect_meet(
    psutil_mod=_psutil,
    win32gui_mod=_win32gui,
) -> Optional[MeetWindow]:
    """Return a single MeetWindow if a call appears active, else None.

    Prefers window-title match (active tab, highest confidence). Falls back to
    process cmdline (may match a backgrounded tab — still useful for Path A
    audio because audio plays regardless of tab focus).
    """
    by_title = enumerate_chromium_windows(win32gui_mod)
    if by_title:
        return by_title[0]
    by_proc = [w for w in enumerate_chromium_processes(psutil_mod) if w.has_meet_url]
    if by_proc:
        return by_proc[0]
    return None


# ----------------------------------------------------------------------------
# Path B: UI Automation caption scrape
# ----------------------------------------------------------------------------

# Meet's live-caption container has historically used:
#   - role: "region" with aria-label "Captions"
#   - inside, a series of speaker-tagged spans
# The class names (jsname, c-wiz Material classes) churn frequently across Meet
# releases, so we anchor on ARIA semantics, not CSS classes. Worst-case the
# script returns empty list and Path A continues unaffected.

UIA_CAPTION_ARIA_LABELS = ("Captions", "Subtitles", "Live captions")


def find_meet_caption_root(window_hwnd: int, uia_mod=_uia):
    """Walk the Chrome accessibility tree from a window handle to the caption
    region. Returns the UIA element or None.

    This is the brittle bit. If Meet ships a DOM rewrite, this needs to be
    re-anchored on the new ARIA structure. Failing soft — never raise — is
    intentional so Path A keeps running.
    """
    if uia_mod is None or window_hwnd == 0:
        return None
    try:
        # ControlFromHandle gives the top-level Chrome window control
        top = uia_mod.ControlFromHandle(window_hwnd)
        if not top:
            return None
        # Document control is the rendered web page
        doc = top.DocumentControl(searchDepth=10)
        if not doc.Exists(2):
            return None
        # Find the caption region by aria-label
        for label in UIA_CAPTION_ARIA_LABELS:
            region = doc.GroupControl(Name=label, searchDepth=20)
            if region.Exists(0.5):
                return region
            region = doc.PaneControl(Name=label, searchDepth=20)
            if region.Exists(0.5):
                return region
        return None
    except Exception as e:           # noqa: BLE001
        log.warning("find_meet_caption_root failed: %s", e)
        return None


def extract_caption_text(caption_root) -> List[tuple]:
    """Walk children of the caption region and pull (speaker, text) tuples.

    Meet renders each speaker's running utterance as a row containing their
    display name + a transcribed paragraph. The structure roughly is:

        <region aria-label="Captions">
          <div>
            <span>{speaker_name}</span>
            <span>{transcribed_text}</span>
          </div>
          ...
        </region>

    UIA exposes these as TextControl descendants. We return them in DOM order;
    the caller dedupes against last-seen text per speaker.
    """
    if caption_root is None:
        return []
    out: List[tuple] = []
    try:
        children = caption_root.GetChildren()
        for child in children:
            texts = []
            speaker = None
            try:
                # Each row has a name (speaker) attribute or first TextControl
                row_name = (child.Name or "").strip()
                if row_name:
                    speaker = row_name
                # Walk descendant text
                for desc in child.GetChildren():
                    t = (desc.Name or "").strip()
                    if t and t != speaker:
                        texts.append(t)
            except Exception:        # noqa: BLE001
                continue
            text = " ".join(texts).strip()
            if text:
                out.append((speaker, text))
    except Exception as e:           # noqa: BLE001
        log.warning("extract_caption_text failed: %s", e)
    return out


# ----------------------------------------------------------------------------
# Path A: audio loopback hook
# ----------------------------------------------------------------------------
#
# We do NOT reimplement WASAPI loopback here — that lives in the shared
# `audio_capture.py` module that B05 (Zoom adapter) also calls. This adapter
# just signals "Meet is active; please start capturing" and forwards STT
# events through with `source="path_a_audio_stt"`.


class PathAStub:
    """Placeholder that the real audio_capture module will replace."""

    def __init__(self, on_transcript: Callable[[MeetCaptionEvent], None]):
        self.on_transcript = on_transcript
        self._running = False

    def start(self):
        self._running = True
        log.info("PathA audio loopback START requested (handled by audio_capture)")

    def stop(self):
        self._running = False
        log.info("PathA audio loopback STOP requested")

    @property
    def running(self) -> bool:
        return self._running


# ----------------------------------------------------------------------------
# Adapter
# ----------------------------------------------------------------------------


class MeetAdapter:
    """High-level glue. Two-path capture with a single output queue."""

    def __init__(
        self,
        psutil_mod=_psutil,
        win32gui_mod=_win32gui,
        uia_mod=_uia,
        audio_class=PathAStub,
    ):
        self._psutil = psutil_mod
        self._win32gui = win32gui_mod
        self._uia = uia_mod
        self._audio_cls = audio_class
        self._events: "queue.Queue[MeetCaptionEvent]" = queue.Queue()
        self._path_a: Optional[PathAStub] = None
        self._path_b_thread: Optional[threading.Thread] = None
        self._path_b_stop = threading.Event()
        self._last_per_speaker: dict = {}

    # ---- detection ----

    def is_meet_active(self) -> bool:
        return detect_meet(self._psutil, self._win32gui) is not None

    def current_window(self) -> Optional[MeetWindow]:
        return detect_meet(self._psutil, self._win32gui)

    # ---- path A ----

    def start_path_a(self) -> bool:
        if self._path_a and self._path_a.running:
            return True
        self._path_a = self._audio_cls(on_transcript=self._events.put)
        self._path_a.start()
        return True

    def stop_path_a(self):
        if self._path_a:
            self._path_a.stop()

    # ---- path B ----

    def start_path_b(self, poll_hz: float = 4.0) -> bool:
        if self._path_b_thread and self._path_b_thread.is_alive():
            return True
        if self._uia is None:
            log.info("Path B unavailable (no uiautomation); Path A still works")
            return False
        win = self.current_window()
        if not win or win.hwnd == 0:
            log.info("Path B needs a window hwnd; no Meet window found")
            return False
        self._path_b_stop.clear()
        self._path_b_thread = threading.Thread(
            target=self._path_b_loop,
            args=(win.hwnd, poll_hz),
            daemon=True,
            name="meet-path-b-uia",
        )
        self._path_b_thread.start()
        return True

    def stop_path_b(self):
        self._path_b_stop.set()
        if self._path_b_thread:
            self._path_b_thread.join(timeout=2.0)

    def _path_b_loop(self, hwnd: int, poll_hz: float):
        interval = 1.0 / max(poll_hz, 0.5)
        while not self._path_b_stop.is_set():
            try:
                root = find_meet_caption_root(hwnd, self._uia)
                rows = extract_caption_text(root)
                for speaker, text in rows:
                    prev = self._last_per_speaker.get(speaker or "_unk", "")
                    # Only emit when the text has actually advanced
                    if text and text != prev and not prev.endswith(text):
                        self._last_per_speaker[speaker or "_unk"] = text
                        self._events.put(
                            MeetCaptionEvent(
                                source="path_b_uia_scrape",
                                text=text,
                                ts=time.monotonic(),
                                speaker=speaker,
                                conf=1.0,
                            )
                        )
            except Exception as e:   # noqa: BLE001
                log.warning("path_b loop error: %s", e)
            self._path_b_stop.wait(interval)

    # ---- consumption ----

    def events(self, timeout: float = 0.25) -> Iterator[MeetCaptionEvent]:
        """Generator yielding events as they arrive. Non-blocking-ish."""
        while True:
            try:
                yield self._events.get(timeout=timeout)
            except queue.Empty:
                return

    def drain(self) -> List[MeetCaptionEvent]:
        out: List[MeetCaptionEvent] = []
        while True:
            try:
                out.append(self._events.get_nowait())
            except queue.Empty:
                return out

    # ---- shutdown ----

    def close(self):
        self.stop_path_a()
        self.stop_path_b()


# ----------------------------------------------------------------------------
# CLI smoke (not run in tests; manual probe)
# ----------------------------------------------------------------------------

if __name__ == "__main__":
    import sys

    logging.basicConfig(level=logging.INFO)
    a = MeetAdapter()
    if a.is_meet_active():
        print("Meet detected:", a.current_window())
        sys.exit(0)
    print("No Meet window detected.")
    sys.exit(1)
