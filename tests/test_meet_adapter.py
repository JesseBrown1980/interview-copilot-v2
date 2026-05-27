"""
test_meet_adapter.py — unit tests for B06-MeetIntegration

We mock:
  - psutil.process_iter (browser detection)
  - win32gui.EnumWindows / GetWindowText / GetForegroundWindow / IsWindowVisible
  - uiautomation control tree (caption scrape)
  - audio loopback class (Path A signaling)

No real Chrome / Meet / audio device is touched.
"""

from __future__ import annotations

import sys
import time
import types
import unittest
from unittest.mock import MagicMock

# Make src/ importable for `python -m unittest discover tests`
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import meet_adapter as M       # noqa: E402


# ----------------------------------------------------------------------------
# psutil mock factory
# ----------------------------------------------------------------------------


def _fake_psutil(procs):
    """`procs` is a list of dicts with keys pid/name/cmdline."""
    mod = types.SimpleNamespace()

    def _iter(attrs=None):
        for p in procs:
            yield types.SimpleNamespace(info=p)

    mod.process_iter = _iter
    return mod


# ----------------------------------------------------------------------------
# win32gui mock factory
# ----------------------------------------------------------------------------


def _fake_win32gui(windows, foreground_hwnd=None):
    """`windows` is a list of (hwnd, title, visible) tuples."""
    mod = types.SimpleNamespace()

    def _enum(cb, _):
        for hwnd, _title, _vis in windows:
            cb(hwnd, None)

    mod.EnumWindows = _enum
    mod.IsWindowVisible = lambda hwnd: next(
        (v for h, _t, v in windows if h == hwnd), False
    )
    mod.GetWindowText = lambda hwnd: next(
        (t for h, t, _v in windows if h == hwnd), ""
    )
    mod.GetForegroundWindow = lambda: foreground_hwnd if foreground_hwnd else 0
    return mod


# ----------------------------------------------------------------------------
# uiautomation mock factory
# ----------------------------------------------------------------------------


class _FakeUIAControl:
    """Tree-walking stub used for both caption-root finding and child reading."""

    def __init__(self, name="", children=None, exists=True):
        self.Name = name
        self._children = children or []
        self._exists = exists

    def Exists(self, _timeout):
        return self._exists

    def DocumentControl(self, **_kwargs):
        # The first child is the document
        if self._children:
            return self._children[0]
        return _FakeUIAControl(exists=False)

    def GroupControl(self, Name=None, **_kwargs):
        for c in self._children:
            if c.Name == Name:
                return c
        return _FakeUIAControl(exists=False)

    def PaneControl(self, Name=None, **_kwargs):
        return self.GroupControl(Name=Name)

    def GetChildren(self):
        return self._children


def _fake_uia(caption_rows):
    """Build a UIA mock that returns a Captions region with the given rows.

    `caption_rows` is a list of (speaker, text) tuples.
    """
    mod = types.SimpleNamespace()
    rows = []
    for speaker, text in caption_rows:
        # row = container with two text children
        text_child = _FakeUIAControl(name=text)
        rows.append(_FakeUIAControl(name=speaker, children=[text_child]))
    caption_region = _FakeUIAControl(name="Captions", children=rows)
    doc = _FakeUIAControl(name="doc", children=[caption_region])
    top = _FakeUIAControl(name="chrome-top", children=[doc])
    mod.ControlFromHandle = lambda _hwnd: top
    return mod


# ----------------------------------------------------------------------------
# Tests: browser detection
# ----------------------------------------------------------------------------


class TestProcessEnumeration(unittest.TestCase):

    def test_finds_chrome_with_meet_url_in_cmdline(self):
        ps = _fake_psutil([
            {"pid": 1, "name": "chrome.exe",
             "cmdline": ["chrome.exe", "https://meet.google.com/abc-defg-hij"]},
            {"pid": 2, "name": "chrome.exe",
             "cmdline": ["chrome.exe", "--type=renderer"]},
            {"pid": 3, "name": "explorer.exe", "cmdline": ["explorer.exe"]},
        ])
        windows = M.enumerate_chromium_processes(ps)
        self.assertEqual(len(windows), 2)            # both chrome procs
        meet_hits = [w for w in windows if w.has_meet_url]
        self.assertEqual(len(meet_hits), 1)
        self.assertEqual(meet_hits[0].pid, 1)

    def test_finds_edge(self):
        ps = _fake_psutil([
            {"pid": 11, "name": "msedge.exe",
             "cmdline": ["msedge.exe", "https://meet.google.com/xyz-abcd-efg"]},
        ])
        windows = M.enumerate_chromium_processes(ps)
        self.assertEqual(len(windows), 1)
        self.assertTrue(windows[0].has_meet_url)

    def test_ignores_non_chromium(self):
        ps = _fake_psutil([
            {"pid": 1, "name": "firefox.exe",
             "cmdline": ["firefox.exe", "https://meet.google.com/abc-defg-hij"]},
        ])
        self.assertEqual(M.enumerate_chromium_processes(ps), [])

    def test_no_psutil_returns_empty(self):
        self.assertEqual(M.enumerate_chromium_processes(None), [])


class TestWindowEnumeration(unittest.TestCase):

    def test_finds_meet_in_window_title(self):
        w = _fake_win32gui([
            (101, "Google Meet — abc-defg-hij — Google Chrome", True),
            (102, "GitHub — Mozilla Firefox", True),
        ], foreground_hwnd=101)
        windows = M.enumerate_chromium_windows(w)
        self.assertEqual(len(windows), 1)
        self.assertEqual(windows[0].hwnd, 101)
        self.assertTrue(windows[0].is_focused)

    def test_matches_meet_url_in_title(self):
        w = _fake_win32gui([
            (200, "meet.google.com/abc — Microsoft Edge", True),
        ])
        windows = M.enumerate_chromium_windows(w)
        self.assertEqual(len(windows), 1)

    def test_ignores_invisible(self):
        w = _fake_win32gui([(1, "Google Meet", False)])
        self.assertEqual(M.enumerate_chromium_windows(w), [])

    def test_no_win32gui_returns_empty(self):
        self.assertEqual(M.enumerate_chromium_windows(None), [])


class TestDetectMeet(unittest.TestCase):

    def test_prefers_window_title_when_available(self):
        ps = _fake_psutil([])  # no procs
        w = _fake_win32gui([(50, "Google Meet — Chrome", True)])
        win = M.detect_meet(ps, w)
        self.assertIsNotNone(win)
        self.assertEqual(win.hwnd, 50)

    def test_falls_back_to_process_when_no_active_window(self):
        ps = _fake_psutil([
            {"pid": 9, "name": "chrome.exe",
             "cmdline": ["chrome.exe", "https://meet.google.com/aaa-bbbb-ccc"]},
        ])
        w = _fake_win32gui([])     # no matching window (backgrounded tab)
        win = M.detect_meet(ps, w)
        self.assertIsNotNone(win)
        self.assertEqual(win.pid, 9)
        self.assertTrue(win.has_meet_url)

    def test_returns_none_when_nothing(self):
        self.assertIsNone(M.detect_meet(_fake_psutil([]), _fake_win32gui([])))


# ----------------------------------------------------------------------------
# Tests: UIA caption scrape
# ----------------------------------------------------------------------------


class TestUIACaptionScrape(unittest.TestCase):

    def test_finds_caption_root_by_aria_label(self):
        uia = _fake_uia([("Jesse", "Hello there"), ("Rayssa", "Hi Jesse")])
        root = M.find_meet_caption_root(window_hwnd=1234, uia_mod=uia)
        self.assertIsNotNone(root)
        self.assertEqual(root.Name, "Captions")

    def test_returns_none_when_no_uia(self):
        self.assertIsNone(M.find_meet_caption_root(1234, uia_mod=None))

    def test_returns_none_when_no_hwnd(self):
        uia = _fake_uia([])
        self.assertIsNone(M.find_meet_caption_root(0, uia_mod=uia))

    def test_extracts_speaker_and_text(self):
        uia = _fake_uia([
            ("Jesse Brown", "What is your experience with distributed systems"),
            ("Candidate", "I built three sharded databases at scale"),
        ])
        root = M.find_meet_caption_root(1234, uia)
        rows = M.extract_caption_text(root)
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0][0], "Jesse Brown")
        self.assertIn("distributed systems", rows[0][1])
        self.assertEqual(rows[1][0], "Candidate")

    def test_extract_with_none_root(self):
        self.assertEqual(M.extract_caption_text(None), [])


# ----------------------------------------------------------------------------
# Tests: MeetAdapter (integration of the pieces)
# ----------------------------------------------------------------------------


class FakeAudio:
    """Stand-in for the real WASAPI loopback class B05 will provide."""

    def __init__(self, on_transcript):
        self.on_transcript = on_transcript
        self.started = False
        self.stopped = False

    def start(self):
        self.started = True

    def stop(self):
        self.stopped = True

    @property
    def running(self):
        return self.started and not self.stopped


class TestMeetAdapter(unittest.TestCase):

    def _adapter(self, procs=(), windows=(), caption_rows=(), foreground=None):
        return M.MeetAdapter(
            psutil_mod=_fake_psutil(list(procs)),
            win32gui_mod=_fake_win32gui(list(windows), foreground_hwnd=foreground),
            uia_mod=_fake_uia(list(caption_rows)) if caption_rows else None,
            audio_class=FakeAudio,
        )

    def test_is_meet_active_true_when_window_matches(self):
        a = self._adapter(windows=[(77, "Google Meet — Chrome", True)])
        self.assertTrue(a.is_meet_active())

    def test_is_meet_active_false_when_nothing(self):
        a = self._adapter()
        self.assertFalse(a.is_meet_active())

    def test_start_path_a_signals_audio(self):
        a = self._adapter(windows=[(7, "Google Meet — Chrome", True)])
        ok = a.start_path_a()
        self.assertTrue(ok)
        self.assertTrue(a._path_a.running)
        a.stop_path_a()
        self.assertFalse(a._path_a.running)

    def test_start_path_a_idempotent(self):
        a = self._adapter(windows=[(7, "Google Meet — Chrome", True)])
        a.start_path_a()
        first = a._path_a
        a.start_path_a()
        self.assertIs(a._path_a, first)

    def test_start_path_b_emits_caption_events(self):
        a = self._adapter(
            windows=[(7, "Google Meet — Chrome", True)],
            caption_rows=[
                ("Jesse", "What is bigpickle"),
                ("Candidate", "A clean room rebuild"),
            ],
            foreground=7,
        )
        ok = a.start_path_b(poll_hz=20)
        self.assertTrue(ok)
        time.sleep(0.25)            # let loop run a few ticks
        a.stop_path_b()
        events = a.drain()
        texts = [e.text for e in events]
        self.assertTrue(any("bigpickle" in t for t in texts))
        self.assertTrue(any("clean room" in t for t in texts))
        # all should be tagged path_b
        for e in events:
            self.assertEqual(e.source, "path_b_uia_scrape")
            self.assertEqual(e.conf, 1.0)
            self.assertIsNotNone(e.speaker)

    def test_path_b_deduplicates_repeat_emission(self):
        a = self._adapter(
            windows=[(7, "Google Meet — Chrome", True)],
            caption_rows=[("Jesse", "Same line")],
            foreground=7,
        )
        a.start_path_b(poll_hz=50)
        time.sleep(0.15)
        a.stop_path_b()
        events = a.drain()
        # multiple poll ticks but only the first should emit
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].text, "Same line")

    def test_path_b_returns_false_without_uia(self):
        a = M.MeetAdapter(
            psutil_mod=_fake_psutil([]),
            win32gui_mod=_fake_win32gui([(7, "Google Meet", True)]),
            uia_mod=None,
            audio_class=FakeAudio,
        )
        self.assertFalse(a.start_path_b())

    def test_path_b_returns_false_without_window(self):
        a = self._adapter()  # no windows
        self.assertFalse(a.start_path_b())

    def test_close_stops_everything(self):
        a = self._adapter(
            windows=[(7, "Google Meet", True)],
            caption_rows=[("X", "hello")],
            foreground=7,
        )
        a.start_path_a()
        a.start_path_b(poll_hz=20)
        time.sleep(0.05)
        a.close()
        self.assertTrue(a._path_a.stopped)
        # path_b_thread should have exited
        if a._path_b_thread:
            self.assertFalse(a._path_b_thread.is_alive())


# ----------------------------------------------------------------------------
# Tests: regex anchors
# ----------------------------------------------------------------------------


class TestPatterns(unittest.TestCase):

    def test_meet_url_pattern_matches_canonical(self):
        self.assertIsNotNone(M.MEET_URL_PATTERN.search(
            "https://meet.google.com/abc-defg-hij"))

    def test_meet_url_pattern_case_insensitive(self):
        self.assertIsNotNone(M.MEET_URL_PATTERN.search(
            "MEET.GOOGLE.COM/xyz-uvwx-rst"))

    def test_meet_url_pattern_rejects_garbage(self):
        self.assertIsNone(M.MEET_URL_PATTERN.search(
            "https://meet.google.com/lookup/abc"))   # /lookup/ is the redirect path
        self.assertIsNone(M.MEET_URL_PATTERN.search(
            "google.com"))

    def test_title_pattern_matches(self):
        self.assertIsNotNone(M.MEET_TITLE_PATTERN.search("Google Meet — Chrome"))
        self.assertIsNotNone(M.MEET_TITLE_PATTERN.search("meet.google.com/foo"))


# ----------------------------------------------------------------------------

if __name__ == "__main__":
    unittest.main(verbosity=2)
