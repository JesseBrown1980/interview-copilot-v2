"""
test_zoom_adapter.py — unit tests for the Zoom desktop client adapter.

Strategy:
    - All OS interactions (process list, window list, OS-version probe, clock)
      are dependency-injected, so these tests never touch a real Zoom client.
    - We assert against ZoomAdapter.state after driving tick() cycles.
    - Pure helpers (title parsing, hashing, source selection) get direct
      table-driven coverage.

Run:
    cd D:/interview/copilot-v2-2026-05-27
    python -m pytest tests/test_zoom_adapter.py -v
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

import pytest


# Make src/ importable without a setup.py.
SRC = Path(__file__).resolve().parent.parent / "src"
sys.path.insert(0, str(SRC))

from zoom_adapter import (  # noqa: E402
    CaptureSource,
    ZoomAdapter,
    ZoomAdapterState,
    choose_capture_source,
    extract_meeting_topic,
    find_zoom_processes,
    hash_meeting_session_id,
    is_zoom_meeting_title,
    is_zoom_process,
    select_meeting_window,
)


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FakeProc:
    pid: int
    _name: str

    def name(self) -> str:
        return self._name


@dataclass(frozen=True)
class FakeWindow:
    title: str
    pid: int


@dataclass
class FakeCaptureStream:
    """Records all calls B03 would receive."""

    started: List[str] = field(default_factory=list)
    stopped: int = 0
    flushed: int = 0
    session_ids: List[str] = field(default_factory=list)
    running: bool = False

    def start(self, source: str, **kwargs) -> None:
        self.started.append(source)
        self.running = True

    def stop(self) -> None:
        self.stopped += 1
        self.running = False

    def is_running(self) -> bool:
        return self.running

    def flush(self) -> None:
        self.flushed += 1

    def set_meeting_session_id(self, mid: str) -> None:
        self.session_ids.append(mid)


# ---------------------------------------------------------------------------
# is_zoom_process / find_zoom_processes
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "name,expected",
    [
        ("Zoom.exe", True),
        ("zoom.exe", True),
        ("CptHost.exe", True),
        ("Zoom_launcher.exe", True),
        ("chrome.exe", False),
        ("Teams.exe", False),
        ("ZoomOutlookPlugin.exe", False),  # plugin != client
        ("", False),
    ],
)
def test_is_zoom_process(name: str, expected: bool) -> None:
    assert is_zoom_process(FakeProc(pid=1, _name=name)) is expected


def test_find_zoom_processes_filters_correctly() -> None:
    procs = [
        FakeProc(1, "Zoom.exe"),
        FakeProc(2, "chrome.exe"),
        FakeProc(3, "CptHost.exe"),
        FakeProc(4, "notepad.exe"),
    ]
    result = find_zoom_processes(procs)
    assert {p.pid for p in result} == {1, 3}


# ---------------------------------------------------------------------------
# Title parsing
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "title,expected",
    [
        ("Zoom Meeting", True),
        ("Zoom Meeting ", True),
        ("Zoom - Daily Standup", True),
        ("Zoom Webinar", True),
        ("Meeting", True),
        ("Zoom", False),  # contacts/home window
        ("Zoom Workplace", False),
        ("Zoom Meeting on Mozilla Firefox", False),  # browser tab
        ("", False),
    ],
)
def test_is_zoom_meeting_title(title: str, expected: bool) -> None:
    assert is_zoom_meeting_title(title) is expected


@pytest.mark.parametrize(
    "title,expected",
    [
        ("Zoom Meeting", None),
        ("Zoom - Daily Standup", "Daily Standup"),
        ("Zoom Meeting - Daily Standup", "Daily Standup"),
        ("Zoom Webinar - Q3 All-Hands", "Q3 All-Hands"),
        ("Zoom - 1:1 with Jesse", "1:1 with Jesse"),
        ("Zoom - Daily Standup (Recording)", "Daily Standup"),
        ("", None),
        ("Random window", None),
    ],
)
def test_extract_meeting_topic(title: str, expected: Optional[str]) -> None:
    assert extract_meeting_topic(title) == expected


# ---------------------------------------------------------------------------
# Hashing
# ---------------------------------------------------------------------------


def test_hash_meeting_session_id_stable_for_same_inputs() -> None:
    h1 = hash_meeting_session_id("Daily Standup", 1234, 1716800000.0)
    h2 = hash_meeting_session_id("Daily Standup", 1234, 1716800000.0)
    assert h1 == h2
    assert h1.startswith("zoom-")
    assert len(h1) == len("zoom-") + 16


def test_hash_meeting_session_id_changes_with_pid() -> None:
    h1 = hash_meeting_session_id("X", 1, 1716800000.0)
    h2 = hash_meeting_session_id("X", 2, 1716800000.0)
    assert h1 != h2


def test_hash_meeting_session_id_changes_with_time() -> None:
    h1 = hash_meeting_session_id("X", 1, 1716800000.0)
    h2 = hash_meeting_session_id("X", 1, 1716800001.0)
    assert h1 != h2


def test_hash_meeting_session_id_normalizes_topic() -> None:
    # Same logical topic in different casing/whitespace -> same hash
    h1 = hash_meeting_session_id("Daily Standup", 7, 1716800000.0)
    h2 = hash_meeting_session_id("  DAILY STANDUP  ", 7, 1716800000.0)
    assert h1 == h2


def test_hash_meeting_session_id_handles_no_topic() -> None:
    # Should not crash; should produce a deterministic id.
    h = hash_meeting_session_id(None, 99, 1716800000.0)
    assert h.startswith("zoom-")


# ---------------------------------------------------------------------------
# Window selection
# ---------------------------------------------------------------------------


def test_select_meeting_window_finds_meeting() -> None:
    windows = [
        FakeWindow("Some browser", pid=99),
        FakeWindow("Zoom", pid=10),  # contacts window
        FakeWindow("Zoom Meeting", pid=10),  # the meeting
    ]
    w = select_meeting_window([10], windows)
    assert w is not None
    assert w.title == "Zoom Meeting"


def test_select_meeting_window_ignores_non_zoom_pids() -> None:
    windows = [FakeWindow("Zoom Meeting", pid=42)]
    assert select_meeting_window([10], windows) is None


def test_select_meeting_window_none_when_no_meeting() -> None:
    windows = [FakeWindow("Zoom", pid=10)]
    assert select_meeting_window([10], windows) is None


# ---------------------------------------------------------------------------
# Source selection
# ---------------------------------------------------------------------------


def test_choose_capture_source_per_process_when_supported() -> None:
    s = choose_capture_source(zoom_pid=1234, process_loopback_available=True)
    assert s.mode == "per_process"
    assert s.pid == 1234
    assert s.to_source_string() == "system_audio:pid=1234"
    assert s.warnings == []


def test_choose_capture_source_full_with_warning_when_unsupported() -> None:
    s = choose_capture_source(zoom_pid=1234, process_loopback_available=False)
    assert s.mode == "system_full"
    assert s.to_source_string() == "system_audio"
    assert len(s.warnings) == 1
    assert "Spotify" in s.warnings[0] or "Per-process" in s.warnings[0]


def test_choose_capture_source_none_when_no_zoom() -> None:
    s = choose_capture_source(zoom_pid=None, process_loopback_available=True)
    assert s.mode == "none"
    assert s.to_source_string() == ""


# ---------------------------------------------------------------------------
# ZoomAdapter integration (with full fakes)
# ---------------------------------------------------------------------------


def _make_adapter(
    procs: List[FakeProc],
    windows: List[FakeWindow],
    *,
    loopback: bool = True,
    clock_value: float = 1716800000.0,
):
    cap = FakeCaptureStream()
    adapter = ZoomAdapter(
        capture_stream=cap,
        list_processes=lambda: list(procs),
        list_windows=lambda: list(windows),
        process_loopback_probe=lambda: loopback,
        clock=lambda: clock_value,
        poll_interval=0.01,
        stop_grace_seconds=1.0,
    )
    return adapter, cap


def test_adapter_no_zoom_does_nothing() -> None:
    adapter, cap = _make_adapter(
        procs=[FakeProc(1, "chrome.exe")],
        windows=[FakeWindow("Browser", 1)],
    )
    adapter.tick()
    assert adapter.state.zoom_detected is False
    assert adapter.state.running is False
    assert cap.started == []
    assert cap.stopped == 0


def test_adapter_zoom_open_but_no_meeting_does_not_capture() -> None:
    adapter, cap = _make_adapter(
        procs=[FakeProc(100, "Zoom.exe")],
        windows=[FakeWindow("Zoom", 100)],  # contacts screen
    )
    adapter.tick()
    assert adapter.state.zoom_detected is True
    assert adapter.state.meeting_active is False
    assert adapter.state.running is False
    assert cap.started == []


def test_adapter_zoom_meeting_starts_capture_per_process() -> None:
    adapter, cap = _make_adapter(
        procs=[FakeProc(100, "Zoom.exe")],
        windows=[FakeWindow("Zoom - Daily Standup", 100)],
        loopback=True,
    )
    adapter.tick()
    assert adapter.state.running is True
    assert adapter.state.meeting_active is True
    assert adapter.state.meeting_topic == "Daily Standup"
    assert adapter.state.capture_source == "system_audio:pid=100"
    assert cap.started == ["system_audio:pid=100"]
    assert len(cap.session_ids) == 1
    assert cap.session_ids[0].startswith("zoom-")
    assert adapter.state.warnings == []


def test_adapter_fallback_to_full_loopback_warns_operator() -> None:
    adapter, cap = _make_adapter(
        procs=[FakeProc(100, "Zoom.exe")],
        windows=[FakeWindow("Zoom Meeting", 100)],
        loopback=False,
    )
    adapter.tick()
    assert adapter.state.running is True
    assert adapter.state.capture_source == "system_audio"
    assert cap.started == ["system_audio"]
    assert len(adapter.state.warnings) == 1


def test_adapter_idempotent_while_meeting_active() -> None:
    adapter, cap = _make_adapter(
        procs=[FakeProc(100, "Zoom.exe")],
        windows=[FakeWindow("Zoom - Standup", 100)],
    )
    adapter.tick()
    adapter.tick()
    adapter.tick()
    # Only one start; no spurious stops.
    assert cap.started == ["system_audio:pid=100"]
    assert cap.stopped == 0


def test_adapter_stops_when_meeting_window_closes_but_zoom_running() -> None:
    cap = FakeCaptureStream()
    procs = [FakeProc(100, "Zoom.exe")]
    windows = [FakeWindow("Zoom - Standup", 100)]

    adapter = ZoomAdapter(
        capture_stream=cap,
        list_processes=lambda: list(procs),
        list_windows=lambda: list(windows),
        process_loopback_probe=lambda: True,
        clock=lambda: 1716800000.0,
        poll_interval=0.01,
        stop_grace_seconds=1.0,
    )

    adapter.tick()
    assert cap.started == ["system_audio:pid=100"]

    # Operator left the meeting; Zoom still running, but on contacts screen.
    windows.clear()
    windows.append(FakeWindow("Zoom", 100))
    adapter.tick()
    assert cap.stopped == 1
    assert cap.flushed == 1
    assert adapter.state.meeting_active is False


def test_adapter_stop_grace_prevents_flapping() -> None:
    cap = FakeCaptureStream()
    procs = [FakeProc(100, "Zoom.exe")]
    windows = [FakeWindow("Zoom Meeting", 100)]
    now = {"t": 1000.0}

    adapter = ZoomAdapter(
        capture_stream=cap,
        list_processes=lambda: list(procs),
        list_windows=lambda: list(windows),
        process_loopback_probe=lambda: True,
        clock=lambda: now["t"],
        poll_interval=0.01,
        stop_grace_seconds=5.0,
    )

    adapter.tick()
    assert cap.started

    # Zoom briefly disappears (e.g. switching meetings).
    procs.clear()
    windows.clear()
    now["t"] = 1002.0  # 2s after last sighting -- within 5s grace
    adapter.tick()
    assert cap.stopped == 0, "should not stop within grace window"

    # Now exceed the grace.
    now["t"] = 1006.0  # 6s after last sighting
    adapter.tick()
    assert cap.stopped == 1
    assert cap.flushed == 1


def test_adapter_picks_main_zoom_over_cpthost_helper() -> None:
    """If both Zoom.exe and CptHost.exe are running, capture should bind to
    the main client PID, not the screen-share helper."""
    adapter, cap = _make_adapter(
        procs=[
            FakeProc(200, "CptHost.exe"),
            FakeProc(100, "Zoom.exe"),
        ],
        windows=[FakeWindow("Zoom Meeting", 100)],
    )
    adapter.tick()
    assert cap.started == ["system_audio:pid=100"]


def test_adapter_topic_updates_mid_meeting() -> None:
    cap = FakeCaptureStream()
    procs = [FakeProc(100, "Zoom.exe")]
    windows = [FakeWindow("Zoom - Standup", 100)]

    adapter = ZoomAdapter(
        capture_stream=cap,
        list_processes=lambda: list(procs),
        list_windows=lambda: list(windows),
        process_loopback_probe=lambda: True,
        clock=lambda: 1716800000.0,
        poll_interval=0.01,
        stop_grace_seconds=1.0,
    )
    adapter.tick()
    assert adapter.state.meeting_topic == "Standup"

    # Host renames the meeting mid-call.
    windows[0] = FakeWindow("Zoom - Standup (Recording)", 100)
    adapter.tick()
    # parentheses are stripped; topic still "Standup"
    assert adapter.state.meeting_topic == "Standup"
    # Still only one capture session.
    assert len(cap.started) == 1


def test_adapter_capture_start_exception_does_not_corrupt_state() -> None:
    class BadCap(FakeCaptureStream):
        def start(self, source: str, **kwargs) -> None:
            raise RuntimeError("audio device busy")

    cap = BadCap()
    adapter = ZoomAdapter(
        capture_stream=cap,
        list_processes=lambda: [FakeProc(100, "Zoom.exe")],
        list_windows=lambda: [FakeWindow("Zoom Meeting", 100)],
        process_loopback_probe=lambda: True,
        clock=lambda: 1.0,
    )
    adapter.tick()
    # Capture failed but state stays clean (will retry next tick).
    assert adapter.state.running is False
    assert adapter.state.capture_source is None


def test_state_dataclass_defaults() -> None:
    s = ZoomAdapterState()
    assert s.running is False
    assert s.zoom_detected is False
    assert s.meeting_active is False
    assert s.meeting_topic is None
    assert s.warnings == []


def test_capture_source_to_source_string() -> None:
    assert CaptureSource("per_process", pid=42).to_source_string() == (
        "system_audio:pid=42"
    )
    assert CaptureSource("system_full").to_source_string() == "system_audio"
    assert CaptureSource("none").to_source_string() == ""
