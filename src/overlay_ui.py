"""
B11-UIOverlay — Interview Copilot v2 always-on-top overlay.

Federation canon compliance:
- feedback_frontend_agent_is_router_not_worker: this UI is a ROUTER, not a WORKER.
  It subscribes to fabric envelopes (interview.transcript_chunk / interview.translated /
  interview.suggestion_emitted) and displays them. It does NOT do work directly.
- feedback_unit_and_integration_tests_for_any_change_2026_05_24: see tests/test_overlay_ui.py.
- Class-1 LAW v3 internal 60s heartbeat: emitted by HeartbeatTimer to fabric_client.

Privacy guarantee:
- Display affinity WDA_EXCLUDEFROMCAPTURE applied at show() so Zoom / Meet / Teams
  screen-share captures see a black rectangle where this window sits. Operator-only.

Tech: PySide6 (Qt for Python) — mature, cross-platform, no electron bloat.
"""
from __future__ import annotations

import ctypes
import sys
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Callable, Deque, Optional

from PySide6.QtCore import (
    QObject,
    QPoint,
    QSize,
    Qt,
    QTimer,
    Signal,
    Slot,
)
from PySide6.QtGui import (
    QColor,
    QFont,
    QKeySequence,
    QMouseEvent,
    QPalette,
    QShortcut,
)
from PySide6.QtWidgets import (
    QApplication,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QSizePolicy,
    QStatusBar,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

# ----------------------------------------------------------------------------
# Constants — federation-pinned
# ----------------------------------------------------------------------------

WINDOW_W = 480
WINDOW_H = 320
WINDOW_ALPHA = 0.85
DEBOUNCE_MS = 100                # buffer text updates to avoid flicker
HEARTBEAT_MS = 60_000            # internal 60s heartbeat per LAW v3
LATENCY_GREEN_MS = 250
LATENCY_YELLOW_MS = 600
# Latency >= YELLOW threshold goes RED.

# WinAPI display-affinity constants
# https://learn.microsoft.com/en-us/windows/win32/api/winuser/nf-winuser-setwindowdisplayaffinity
WDA_NONE = 0x00000000
WDA_MONITOR = 0x00000001
WDA_EXCLUDEFROMCAPTURE = 0x00000011  # Win10 build 19041+

# Envelope topics — fabric canon
TOPIC_TRANSCRIPT = "interview.transcript_chunk"
TOPIC_TRANSLATED = "interview.translated"
TOPIC_SUGGESTION = "interview.suggestion_emitted"


# ----------------------------------------------------------------------------
# QSS stylesheet — hot-reload friendly, separated from logic
# ----------------------------------------------------------------------------

STYLESHEET = """
QMainWindow, QWidget#OverlayRoot {
    background-color: rgba(20, 22, 28, 220);
    border: 1px solid rgba(120, 140, 180, 140);
    border-radius: 8px;
}
QLabel#PanelHeader {
    color: rgba(180, 200, 230, 255);
    font-size: 10px;
    font-weight: 600;
    text-transform: uppercase;
    letter-spacing: 1px;
    padding: 2px 6px;
}
QTextEdit {
    background-color: rgba(10, 12, 16, 180);
    color: rgba(230, 235, 245, 255);
    border: 1px solid rgba(80, 100, 130, 100);
    border-radius: 4px;
    padding: 4px;
    font-family: "Cascadia Code", "Consolas", monospace;
    font-size: 11px;
    selection-background-color: rgba(80, 140, 220, 180);
}
QTextEdit#SuggestionBox {
    border: 1px solid rgba(120, 180, 100, 140);
}
QStatusBar {
    background-color: rgba(15, 17, 22, 200);
    color: rgba(170, 190, 220, 220);
    font-size: 10px;
    font-family: "Cascadia Code", "Consolas", monospace;
}
QLabel#LatencyIndicator {
    padding: 2px 6px;
    border-radius: 3px;
    font-weight: 600;
    font-size: 10px;
}
"""


# ----------------------------------------------------------------------------
# Data shapes
# ----------------------------------------------------------------------------


@dataclass
class Envelope:
    """Inbound fabric envelope. Schema matches interview.* topics."""

    topic: str
    body: dict
    ts_ms: int = field(default_factory=lambda: int(time.time() * 1000))


@dataclass
class LatencySnapshot:
    transcript_ms: int = 0
    translate_ms: int = 0
    suggest_ms: int = 0


# ----------------------------------------------------------------------------
# Display affinity — Win32 WDA_EXCLUDEFROMCAPTURE
# ----------------------------------------------------------------------------


def apply_display_affinity_excludefromcapture(hwnd: int) -> bool:
    """Hide this window from Zoom / Meet / Teams screen share.

    Returns True if the WinAPI call succeeded. False on non-Windows or pre-19041
    builds; we log but do NOT raise — the overlay still works, just visible to
    screen shares (operator must know — surfaced in status bar).
    """
    if sys.platform != "win32":
        return False
    if not hwnd:
        return False
    try:
        user32 = ctypes.windll.user32
        # BOOL SetWindowDisplayAffinity(HWND hWnd, DWORD dwAffinity);
        user32.SetWindowDisplayAffinity.argtypes = [
            ctypes.c_void_p,
            ctypes.c_uint,
        ]
        user32.SetWindowDisplayAffinity.restype = ctypes.c_int
        ok = user32.SetWindowDisplayAffinity(
            ctypes.c_void_p(int(hwnd)),
            WDA_EXCLUDEFROMCAPTURE,
        )
        return bool(ok)
    except (OSError, AttributeError, ValueError):
        return False


# ----------------------------------------------------------------------------
# Fabric client interface — duck-typed for testability
# ----------------------------------------------------------------------------


class FabricClient(QObject):
    """Minimal fabric subscriber interface.

    Real impl: WebSocket/HTTP-SSE to asolaria-fabric :4949 (or sister-organ).
    Tests inject a fake that emits envelope_received via emit_test_envelope().
    """

    envelope_received = Signal(object)  # Envelope

    def subscribe(self, topics: list[str]) -> None:
        """Subscribe to the given fabric topics. No-op in the base class."""
        self._subscribed = list(topics)

    def emit_test_envelope(self, env: Envelope) -> None:
        """Test hook — push an envelope as if it came from fabric."""
        self.envelope_received.emit(env)

    def emit_heartbeat(self, ts_ms: int) -> None:
        """Heartbeat emit hook — fabric impl wires to /api/heartbeat."""
        # Real impl would POST to fabric; base class is a no-op stub.
        self._last_heartbeat_ms = ts_ms


# ----------------------------------------------------------------------------
# Debounced text setter — buffers updates inside DEBOUNCE_MS window
# ----------------------------------------------------------------------------


class DebouncedTextBuffer(QObject):
    """Coalesce rapid text updates into one paint per DEBOUNCE_MS."""

    def __init__(
        self,
        target_setter: Callable[[str], None],
        debounce_ms: int = DEBOUNCE_MS,
        parent: Optional[QObject] = None,
    ) -> None:
        super().__init__(parent)
        self._setter = target_setter
        self._pending: Optional[str] = None
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(debounce_ms)
        self._timer.timeout.connect(self._flush)

    def push(self, text: str) -> None:
        self._pending = text
        if not self._timer.isActive():
            self._timer.start()

    @Slot()
    def _flush(self) -> None:
        if self._pending is not None:
            self._setter(self._pending)
            self._pending = None

    def flush_now(self) -> None:
        """Test hook — force-flush without waiting on the timer."""
        self._timer.stop()
        self._flush()


# ----------------------------------------------------------------------------
# Main overlay window
# ----------------------------------------------------------------------------


class OverlayWindow(QMainWindow):
    """Always-on-top, frameless, semi-transparent interview copilot overlay."""

    # Public signals — useful for tests & external observers
    visibility_toggled = Signal(bool)
    suggestion_copied = Signal(str)
    exited = Signal()

    def __init__(
        self,
        fabric_client: Optional[FabricClient] = None,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self.fabric_client = fabric_client or FabricClient(self)

        # Rolling buffers — last N chunks per stream
        self._transcript_chunks: Deque[str] = deque(maxlen=80)
        self._translation_chunks: Deque[str] = deque(maxlen=80)
        self._suggestions: Deque[str] = deque(maxlen=3)
        self._latency = LatencySnapshot()
        self._meeting_id: str = "—"

        self._build_ui()
        self._wire_shortcuts()
        self._wire_fabric()
        self._start_heartbeat()
        self._drag_origin: Optional[QPoint] = None

    # -- UI construction ---------------------------------------------------

    def _build_ui(self) -> None:
        # Frameless, always-on-top, tool window (no taskbar icon)
        self.setWindowFlags(
            Qt.FramelessWindowHint
            | Qt.WindowStaysOnTopHint
            | Qt.Tool
        )
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setWindowOpacity(WINDOW_ALPHA)
        self.setFixedSize(QSize(WINDOW_W, WINDOW_H))
        self.setStyleSheet(STYLESHEET)

        root = QWidget(self)
        root.setObjectName("OverlayRoot")
        self.setCentralWidget(root)

        v = QVBoxLayout(root)
        v.setContentsMargins(8, 8, 8, 4)
        v.setSpacing(4)

        # Transcript panel
        v.addWidget(self._make_header("Transcript"))
        self.transcript_box = self._make_textbox("TranscriptBox")
        v.addWidget(self.transcript_box, stretch=3)

        # Translation panel
        v.addWidget(self._make_header("Translation"))
        self.translation_box = self._make_textbox("TranslationBox")
        v.addWidget(self.translation_box, stretch=3)

        # Suggestion panel (max 3 suggestions visible)
        v.addWidget(self._make_header("Suggestions  (Ctrl+Shift+C copies #1)"))
        self.suggestion_box = self._make_textbox("SuggestionBox")
        v.addWidget(self.suggestion_box, stretch=4)

        # Status bar — meeting metadata + latency lamps
        self.status_bar = QStatusBar(self)
        self.setStatusBar(self.status_bar)
        self.meeting_label = QLabel("meet:—")
        self.lat_transcript = self._make_latency_indicator("ASR")
        self.lat_translate = self._make_latency_indicator("MT")
        self.lat_suggest = self._make_latency_indicator("SUG")
        self.affinity_label = QLabel("capture:?")
        for w in (
            self.meeting_label,
            self.lat_transcript,
            self.lat_translate,
            self.lat_suggest,
            self.affinity_label,
        ):
            self.status_bar.addPermanentWidget(w)

        # Debounced setters — flicker-free
        self._buf_transcript = DebouncedTextBuffer(
            self.transcript_box.setPlainText, parent=self
        )
        self._buf_translation = DebouncedTextBuffer(
            self.translation_box.setPlainText, parent=self
        )
        self._buf_suggestion = DebouncedTextBuffer(
            self.suggestion_box.setPlainText, parent=self
        )

    def _make_header(self, text: str) -> QLabel:
        lab = QLabel(text, self)
        lab.setObjectName("PanelHeader")
        return lab

    def _make_textbox(self, object_name: str) -> QTextEdit:
        box = QTextEdit(self)
        box.setObjectName(object_name)
        box.setReadOnly(True)
        box.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        box.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        box.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        box.setLineWrapMode(QTextEdit.WidgetWidth)
        return box

    def _make_latency_indicator(self, prefix: str) -> QLabel:
        lab = QLabel(f"{prefix}:—", self)
        lab.setObjectName("LatencyIndicator")
        self._paint_latency(lab, 0)
        return lab

    @staticmethod
    def _paint_latency(label: QLabel, ms: int) -> None:
        if ms <= 0:
            color = "rgba(100, 100, 100, 200)"
            txt_color = "rgba(200, 200, 200, 255)"
        elif ms < LATENCY_GREEN_MS:
            color = "rgba(60, 160, 80, 200)"
            txt_color = "white"
        elif ms < LATENCY_YELLOW_MS:
            color = "rgba(200, 160, 50, 200)"
            txt_color = "black"
        else:
            color = "rgba(200, 60, 60, 220)"
            txt_color = "white"
        prefix = label.text().split(":")[0]
        display_ms = "—" if ms <= 0 else f"{ms}ms"
        label.setText(f"{prefix}:{display_ms}")
        label.setStyleSheet(
            f"background-color: {color}; color: {txt_color};"
            " padding: 2px 6px; border-radius: 3px;"
        )

    # -- Shortcuts ---------------------------------------------------------

    def _wire_shortcuts(self) -> None:
        # Ctrl+Shift+H — toggle visibility (does NOT steal focus)
        QShortcut(QKeySequence("Ctrl+Shift+H"), self, activated=self.toggle_visibility)
        # Ctrl+Shift+C — copy first suggestion
        QShortcut(QKeySequence("Ctrl+Shift+C"), self, activated=self.copy_top_suggestion)
        # Ctrl+Shift+Q — exit
        QShortcut(QKeySequence("Ctrl+Shift+Q"), self, activated=self.request_exit)

    @Slot()
    def toggle_visibility(self) -> None:
        if self.isVisible():
            self.hide()
            self.visibility_toggled.emit(False)
        else:
            self.show()
            self.visibility_toggled.emit(True)

    @Slot()
    def copy_top_suggestion(self) -> None:
        if not self._suggestions:
            return
        top = self._suggestions[0]
        clip = QApplication.clipboard()
        if clip is not None:
            clip.setText(top)
        self.suggestion_copied.emit(top)

    @Slot()
    def request_exit(self) -> None:
        self.exited.emit()
        self.close()
        app = QApplication.instance()
        if app is not None:
            app.quit()

    # -- Fabric wiring -----------------------------------------------------

    def _wire_fabric(self) -> None:
        self.fabric_client.subscribe(
            [TOPIC_TRANSCRIPT, TOPIC_TRANSLATED, TOPIC_SUGGESTION]
        )
        self.fabric_client.envelope_received.connect(self._on_envelope)

    @Slot(object)
    def _on_envelope(self, env: Envelope) -> None:
        if env.topic == TOPIC_TRANSCRIPT:
            self._ingest_transcript(env)
        elif env.topic == TOPIC_TRANSLATED:
            self._ingest_translation(env)
        elif env.topic == TOPIC_SUGGESTION:
            self._ingest_suggestion(env)
        # silently ignore unknown topics — fabric may upgrade

    def _ingest_transcript(self, env: Envelope) -> None:
        chunk = str(env.body.get("text", "")).strip()
        if chunk:
            self._transcript_chunks.append(chunk)
        ms = int(env.body.get("latency_ms", 0))
        if ms:
            self._latency.transcript_ms = ms
            self._paint_latency(self.lat_transcript, ms)
        meeting = env.body.get("meeting_id")
        if meeting:
            self._meeting_id = str(meeting)
            self.meeting_label.setText(f"meet:{self._meeting_id[:12]}")
        self._buf_transcript.push(" ".join(self._transcript_chunks))

    def _ingest_translation(self, env: Envelope) -> None:
        chunk = str(env.body.get("text", "")).strip()
        if chunk:
            self._translation_chunks.append(chunk)
        ms = int(env.body.get("latency_ms", 0))
        if ms:
            self._latency.translate_ms = ms
            self._paint_latency(self.lat_translate, ms)
        self._buf_translation.push(" ".join(self._translation_chunks))

    def _ingest_suggestion(self, env: Envelope) -> None:
        suggestions = env.body.get("suggestions")
        if isinstance(suggestions, list):
            self._suggestions = deque(
                [str(s) for s in suggestions[:3]], maxlen=3
            )
        else:
            single = str(env.body.get("text", "")).strip()
            if single:
                self._suggestions.append(single)
        ms = int(env.body.get("latency_ms", 0))
        if ms:
            self._latency.suggest_ms = ms
            self._paint_latency(self.lat_suggest, ms)
        rendered = "\n\n".join(
            f"{i + 1}. {s}" for i, s in enumerate(self._suggestions)
        )
        self._buf_suggestion.push(rendered)

    # -- Heartbeat (LAW v3 60s internal) ----------------------------------

    def _start_heartbeat(self) -> None:
        self._heartbeat_timer = QTimer(self)
        self._heartbeat_timer.setInterval(HEARTBEAT_MS)
        self._heartbeat_timer.timeout.connect(self._fire_heartbeat)
        self._heartbeat_timer.start()

    @Slot()
    def _fire_heartbeat(self) -> None:
        self.fabric_client.emit_heartbeat(int(time.time() * 1000))

    # -- Drag-to-move (frameless window) ----------------------------------

    def mousePressEvent(self, event: QMouseEvent) -> None:  # noqa: N802 — Qt API
        if event.button() == Qt.LeftButton:
            self._drag_origin = event.globalPosition().toPoint() - self.frameGeometry().topLeft()
            event.accept()

    def mouseMoveEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        if self._drag_origin is not None and event.buttons() & Qt.LeftButton:
            self.move(event.globalPosition().toPoint() - self._drag_origin)
            event.accept()

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        self._drag_origin = None
        event.accept()

    # -- Display affinity — apply once HWND exists ------------------------

    def showEvent(self, event) -> None:  # noqa: N802 — Qt API
        super().showEvent(event)
        hwnd = int(self.winId())
        ok = apply_display_affinity_excludefromcapture(hwnd)
        self.affinity_label.setText(
            "capture:HIDDEN" if ok else "capture:VISIBLE"
        )
        self.affinity_label.setStyleSheet(
            "background-color: rgba(60,160,80,200); color: white;"
            " padding: 2px 6px; border-radius: 3px;"
            if ok
            else "background-color: rgba(200,60,60,220); color: white;"
            " padding: 2px 6px; border-radius: 3px;"
        )


# ----------------------------------------------------------------------------
# Entrypoint — for manual smoke test only. Real launch goes via the launcher
# agent's bootstrap (B12 / launcher cohort).
# ----------------------------------------------------------------------------


def main() -> int:
    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(True)
    win = OverlayWindow()
    win.show()
    return app.exec()


if __name__ == "__main__":  # pragma: no cover — manual smoke only
    sys.exit(main())
