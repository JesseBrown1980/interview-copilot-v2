"""
Unit tests for B11 overlay_ui.

Strategy:
- pytest-qt provides a `qtbot` fixture and an embedded QApplication.
- We inject a FabricClient stub and call emit_test_envelope() to drive the UI.
- DebouncedTextBuffer.flush_now() bypasses the 100ms wait, keeping tests fast.
- Display-affinity is a thin wrapper — we test it via the pure function, not
  by actually invoking Win32 (covered as a "honest gap" in the paper).

Per feedback_unit_and_integration_tests_for_any_change_2026_05_24: both unit
and integration coverage shipped; nothing auto-submits a PR.
"""
from __future__ import annotations

import sys
from typing import List
from unittest.mock import patch

import pytest

# Skip the entire module if PySide6 / pytest-qt aren't installed — this lets
# the file land in the repo without breaking CI that doesn't have a Qt stack.
PySide6 = pytest.importorskip("PySide6")
pytest_qt = pytest.importorskip("pytestqt")

from PySide6.QtCore import Qt  # noqa: E402  pylint: disable=wrong-import-position
from PySide6.QtGui import QKeySequence  # noqa: E402

# Make src/ importable without a package install
sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent.parent / "src"))

import overlay_ui  # noqa: E402  pylint: disable=wrong-import-position
from overlay_ui import (  # noqa: E402
    DEBOUNCE_MS,
    DebouncedTextBuffer,
    Envelope,
    FabricClient,
    LATENCY_GREEN_MS,
    LATENCY_YELLOW_MS,
    OverlayWindow,
    TOPIC_SUGGESTION,
    TOPIC_TRANSCRIPT,
    TOPIC_TRANSLATED,
    WDA_EXCLUDEFROMCAPTURE,
    WINDOW_ALPHA,
    WINDOW_H,
    WINDOW_W,
    apply_display_affinity_excludefromcapture,
)


# ----------------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------------


@pytest.fixture
def fabric() -> FabricClient:
    return FabricClient()


@pytest.fixture
def overlay(qtbot, fabric: FabricClient) -> OverlayWindow:
    win = OverlayWindow(fabric_client=fabric)
    qtbot.addWidget(win)
    return win


def _flush_all_buffers(win: OverlayWindow) -> None:
    win._buf_transcript.flush_now()
    win._buf_translation.flush_now()
    win._buf_suggestion.flush_now()


# ----------------------------------------------------------------------------
# Window spec
# ----------------------------------------------------------------------------


class TestWindowSpec:
    def test_window_size_pinned(self, overlay: OverlayWindow) -> None:
        assert overlay.size().width() == WINDOW_W
        assert overlay.size().height() == WINDOW_H

    def test_window_opacity_is_semi_transparent(self, overlay: OverlayWindow) -> None:
        assert abs(overlay.windowOpacity() - WINDOW_ALPHA) < 1e-6

    def test_window_is_always_on_top(self, overlay: OverlayWindow) -> None:
        flags = overlay.windowFlags()
        assert bool(flags & Qt.WindowStaysOnTopHint)

    def test_window_is_frameless(self, overlay: OverlayWindow) -> None:
        flags = overlay.windowFlags()
        assert bool(flags & Qt.FramelessWindowHint)

    def test_window_has_translucent_attr(self, overlay: OverlayWindow) -> None:
        assert overlay.testAttribute(Qt.WA_TranslucentBackground)


# ----------------------------------------------------------------------------
# Panel layout
# ----------------------------------------------------------------------------


class TestPanelLayout:
    def test_three_text_panels_present(self, overlay: OverlayWindow) -> None:
        assert overlay.transcript_box is not None
        assert overlay.translation_box is not None
        assert overlay.suggestion_box is not None

    def test_all_panels_readonly(self, overlay: OverlayWindow) -> None:
        assert overlay.transcript_box.isReadOnly()
        assert overlay.translation_box.isReadOnly()
        assert overlay.suggestion_box.isReadOnly()

    def test_status_bar_has_meeting_and_latency_widgets(
        self, overlay: OverlayWindow
    ) -> None:
        assert overlay.meeting_label is not None
        assert overlay.lat_transcript is not None
        assert overlay.lat_translate is not None
        assert overlay.lat_suggest is not None
        assert overlay.affinity_label is not None


# ----------------------------------------------------------------------------
# Data binding — fabric envelopes drive UI
# ----------------------------------------------------------------------------


class TestEnvelopeIngest:
    def test_transcript_envelope_updates_transcript_box(
        self, overlay: OverlayWindow, fabric: FabricClient
    ) -> None:
        fabric.emit_test_envelope(
            Envelope(
                topic=TOPIC_TRANSCRIPT,
                body={
                    "text": "what is your greatest weakness",
                    "latency_ms": 180,
                    "meeting_id": "abc-123-xyz",
                },
            )
        )
        _flush_all_buffers(overlay)
        assert "greatest weakness" in overlay.transcript_box.toPlainText()
        assert "abc-123-xyz"[:12] in overlay.meeting_label.text()

    def test_translation_envelope_updates_translation_box(
        self, overlay: OverlayWindow, fabric: FabricClient
    ) -> None:
        fabric.emit_test_envelope(
            Envelope(
                topic=TOPIC_TRANSLATED,
                body={"text": "qual sua maior fraqueza", "latency_ms": 220},
            )
        )
        _flush_all_buffers(overlay)
        assert "maior fraqueza" in overlay.translation_box.toPlainText()

    def test_suggestion_list_replaces_previous(
        self, overlay: OverlayWindow, fabric: FabricClient
    ) -> None:
        fabric.emit_test_envelope(
            Envelope(
                topic=TOPIC_SUGGESTION,
                body={
                    "suggestions": [
                        "Lead with concrete example.",
                        "Frame as growth area.",
                        "End with mitigation.",
                    ],
                    "latency_ms": 410,
                },
            )
        )
        _flush_all_buffers(overlay)
        text = overlay.suggestion_box.toPlainText()
        assert "Lead with concrete example" in text
        assert "Frame as growth area" in text
        assert "End with mitigation" in text
        assert text.startswith("1. ")

    def test_suggestion_capped_at_three(
        self, overlay: OverlayWindow, fabric: FabricClient
    ) -> None:
        fabric.emit_test_envelope(
            Envelope(
                topic=TOPIC_SUGGESTION,
                body={"suggestions": [f"s{i}" for i in range(10)]},
            )
        )
        _flush_all_buffers(overlay)
        assert len(overlay._suggestions) == 3

    def test_unknown_topic_silently_ignored(
        self, overlay: OverlayWindow, fabric: FabricClient
    ) -> None:
        # Should not raise
        fabric.emit_test_envelope(
            Envelope(topic="interview.unknown", body={"text": "x"})
        )
        _flush_all_buffers(overlay)
        assert overlay.transcript_box.toPlainText() == ""


# ----------------------------------------------------------------------------
# Latency lamp color logic
# ----------------------------------------------------------------------------


class TestLatencyLamp:
    @pytest.mark.parametrize(
        "ms,expected_substr",
        [
            (LATENCY_GREEN_MS - 50, "60, 160, 80"),       # green
            (LATENCY_GREEN_MS + 50, "200, 160, 50"),      # yellow
            (LATENCY_YELLOW_MS + 100, "200, 60, 60"),     # red
        ],
    )
    def test_color_thresholds(
        self,
        overlay: OverlayWindow,
        fabric: FabricClient,
        ms: int,
        expected_substr: str,
    ) -> None:
        fabric.emit_test_envelope(
            Envelope(
                topic=TOPIC_TRANSCRIPT,
                body={"text": "hello", "latency_ms": ms},
            )
        )
        _flush_all_buffers(overlay)
        sheet = overlay.lat_transcript.styleSheet()
        assert expected_substr in sheet
        assert f"{ms}ms" in overlay.lat_transcript.text()


# ----------------------------------------------------------------------------
# Hotkeys
# ----------------------------------------------------------------------------


class TestHotkeys:
    def test_toggle_visibility_signal_fires(
        self, qtbot, overlay: OverlayWindow
    ) -> None:
        overlay.show()
        with qtbot.waitSignal(overlay.visibility_toggled, timeout=500) as blocker:
            overlay.toggle_visibility()
        assert blocker.args == [False]
        with qtbot.waitSignal(overlay.visibility_toggled, timeout=500) as blocker:
            overlay.toggle_visibility()
        assert blocker.args == [True]

    def test_copy_top_suggestion_emits_signal(
        self, qtbot, overlay: OverlayWindow, fabric: FabricClient
    ) -> None:
        fabric.emit_test_envelope(
            Envelope(
                topic=TOPIC_SUGGESTION,
                body={"suggestions": ["primary suggestion", "alt", "alt2"]},
            )
        )
        _flush_all_buffers(overlay)
        with qtbot.waitSignal(overlay.suggestion_copied, timeout=500) as blocker:
            overlay.copy_top_suggestion()
        assert blocker.args == ["primary suggestion"]

    def test_copy_with_no_suggestion_is_noop(
        self, qtbot, overlay: OverlayWindow
    ) -> None:
        # Should NOT emit suggestion_copied when buffer is empty
        with qtbot.assertNotEmitted(overlay.suggestion_copied, wait=150):
            overlay.copy_top_suggestion()

    def test_exit_signal_fires(self, qtbot, overlay: OverlayWindow) -> None:
        with qtbot.waitSignal(overlay.exited, timeout=500):
            overlay.request_exit()


# ----------------------------------------------------------------------------
# Debounced buffer
# ----------------------------------------------------------------------------


class TestDebounce:
    def test_multiple_pushes_collapse_to_one_setter_call(
        self, qtbot
    ) -> None:
        calls: List[str] = []
        buf = DebouncedTextBuffer(calls.append, debounce_ms=DEBOUNCE_MS)
        for token in ["a", "ab", "abc", "abcd"]:
            buf.push(token)
        # nothing applied yet
        assert calls == []
        # let the timer fire
        qtbot.wait(DEBOUNCE_MS + 50)
        assert calls == ["abcd"]

    def test_flush_now_bypasses_timer(self) -> None:
        calls: List[str] = []
        buf = DebouncedTextBuffer(calls.append)
        buf.push("eager")
        buf.flush_now()
        assert calls == ["eager"]


# ----------------------------------------------------------------------------
# Display affinity — pure function, mocked WinAPI
# ----------------------------------------------------------------------------


class TestDisplayAffinity:
    def test_non_windows_returns_false(self) -> None:
        with patch.object(overlay_ui.sys, "platform", "linux"):
            assert apply_display_affinity_excludefromcapture(12345) is False

    def test_zero_hwnd_returns_false(self) -> None:
        with patch.object(overlay_ui.sys, "platform", "win32"):
            assert apply_display_affinity_excludefromcapture(0) is False

    @pytest.mark.skipif(sys.platform != "win32", reason="Windows-only WinAPI call")
    def test_calls_winapi_with_exclude_from_capture_constant(self) -> None:
        # Patch ctypes.windll.user32.SetWindowDisplayAffinity inside overlay_ui.
        from unittest.mock import MagicMock
        fake_user32 = MagicMock()
        fake_user32.SetWindowDisplayAffinity.return_value = 1
        fake_windll = MagicMock(user32=fake_user32)
        with patch.object(overlay_ui.ctypes, "windll", fake_windll, create=True):
            ok = apply_display_affinity_excludefromcapture(0xDEADBEEF)
        assert ok is True
        # Inspect call args: second arg must be WDA_EXCLUDEFROMCAPTURE
        args, _ = fake_user32.SetWindowDisplayAffinity.call_args
        # First arg is a ctypes c_void_p wrapping the hwnd; second is the affinity
        assert args[1].value == WDA_EXCLUDEFROMCAPTURE


# ----------------------------------------------------------------------------
# Fabric subscription wiring
# ----------------------------------------------------------------------------


class TestFabricWiring:
    def test_subscribe_topics_set(self, overlay: OverlayWindow) -> None:
        assert TOPIC_TRANSCRIPT in overlay.fabric_client._subscribed
        assert TOPIC_TRANSLATED in overlay.fabric_client._subscribed
        assert TOPIC_SUGGESTION in overlay.fabric_client._subscribed

    def test_heartbeat_fires_on_demand(self, overlay: OverlayWindow) -> None:
        overlay._fire_heartbeat()
        assert hasattr(overlay.fabric_client, "_last_heartbeat_ms")
        assert overlay.fabric_client._last_heartbeat_ms > 0


# ----------------------------------------------------------------------------
# Integration — end-to-end envelope flow
# ----------------------------------------------------------------------------


class TestIntegration:
    def test_full_interview_turn_renders_all_three_panels(
        self, overlay: OverlayWindow, fabric: FabricClient
    ) -> None:
        # 1. ASR drops transcript
        fabric.emit_test_envelope(
            Envelope(
                topic=TOPIC_TRANSCRIPT,
                body={
                    "text": "tell me about a time you handled conflict",
                    "latency_ms": 150,
                    "meeting_id": "intg-001",
                },
            )
        )
        # 2. MT translates
        fabric.emit_test_envelope(
            Envelope(
                topic=TOPIC_TRANSLATED,
                body={
                    "text": "fale sobre uma vez que voce lidou com conflito",
                    "latency_ms": 320,
                },
            )
        )
        # 3. Suggester emits 3 STAR-style suggestions
        fabric.emit_test_envelope(
            Envelope(
                topic=TOPIC_SUGGESTION,
                body={
                    "suggestions": [
                        "Situation: peer disagreement on architecture.",
                        "Action: scheduled 1:1, aligned on shared metric.",
                        "Result: shipped, reduced p95 by 40%.",
                    ],
                    "latency_ms": 580,
                },
            )
        )
        _flush_all_buffers(overlay)
        assert "conflict" in overlay.transcript_box.toPlainText()
        assert "conflito" in overlay.translation_box.toPlainText()
        sug = overlay.suggestion_box.toPlainText()
        assert "Situation" in sug and "Action" in sug and "Result" in sug
        # Latency lamps all painted
        assert "150ms" in overlay.lat_transcript.text()
        assert "320ms" in overlay.lat_translate.text()
        assert "580ms" in overlay.lat_suggest.text()
        # Meeting id propagated
        assert "intg-001" in overlay.meeting_label.text()
