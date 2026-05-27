"""
E2E: Zoom + English-only happy path.

Scenario:
    1. Operator joins Zoom meeting
    2. Interviewer asks question in EN
    3. Audio capture -> STT -> (no translate, already EN) -> Claude suggestion
    4. UI shows transcript + suggestion
    5. Fabric receives envelope chain: transcript.chunk -> suggestion (no translation kind)
"""
from __future__ import annotations

import pytest

from conftest import (
    SyntheticAudioClip,
    WiredPipeline,
    MockFabricMCP,
    MockAnthropicClient,
    MockGoogleTranslate,
    drive_turn,
)


@pytest.mark.integration
def test_zoom_en_happy_path_renders_transcript_and_suggestion(
    pipeline: WiredPipeline,
    clip_en_interviewer: SyntheticAudioClip,
    mock_fabric: MockFabricMCP,
    mock_anthropic: MockAnthropicClient,
    mock_translate: MockGoogleTranslate,
) -> None:
    # 1. Join Zoom
    join_ack = pipeline.zoom_adapter.join("zoom://meet/abc-defg-hij")
    assert join_ack["status"] == "joined"
    assert pipeline.zoom_adapter.joined

    # 2..4. Drive a full turn
    result = drive_turn(pipeline, clip_en_interviewer, operator_target_lang="en")

    # Transcript matches expected
    assert result["transcript"]["text"] == clip_en_interviewer.expected_transcript
    assert result["transcript"]["lang"] == "en"
    assert result["transcript"]["route"] == "cloud"
    assert pipeline.stt_router.last_route == "gcp-stt-v2"

    # No translation should have happened (EN -> EN)
    assert mock_translate.calls == [], "translator must not be called for same-language turn"
    assert result["translation"] == clip_en_interviewer.expected_transcript

    # Suggestion present and looks like an EN answer (canned reply contains 'distributed')
    assert result["suggestion"] is not None
    assert "distributed" in result["suggestion"]["text"].lower()
    assert result["suggestion"]["route"] == "cloud"
    assert pipeline.response_engine.fallback_used is False

    # UI panes updated
    assert pipeline.ui_overlay.panes["interviewer"] == [clip_en_interviewer.expected_transcript]
    assert pipeline.ui_overlay.panes["operator"] == []
    assert len(pipeline.ui_overlay.panes["suggestion"]) == 1

    # Anthropic was hit exactly once
    assert len(mock_anthropic.calls) == 1

    # 5. Fabric envelope chain
    kinds = mock_fabric.kinds()
    assert kinds == ["transcript.chunk", "suggestion"], (
        f"expected no translation envelope on EN->EN path, got {kinds}"
    )
    # All envelopes share the session pid
    assert all(p == pipeline.session_pid for p in mock_fabric.pids())


@pytest.mark.integration
def test_zoom_en_multi_turn_chain(
    pipeline: WiredPipeline,
    clip_en_interviewer: SyntheticAudioClip,
    clip_en_operator: SyntheticAudioClip,
    mock_fabric: MockFabricMCP,
    mock_anthropic: MockAnthropicClient,
) -> None:
    """Three turns: interviewer, operator answer, interviewer follow-up."""
    pipeline.zoom_adapter.join("zoom://meet/multi-turn")

    drive_turn(pipeline, clip_en_interviewer, "en")
    drive_turn(pipeline, clip_en_operator, "en")           # operator turn -> NO suggestion
    drive_turn(pipeline, clip_en_interviewer, "en")        # follow-up interviewer turn

    kinds = mock_fabric.kinds()
    # Expected envelope sequence:
    # interviewer turn: transcript.chunk, suggestion
    # operator turn:    transcript.chunk
    # interviewer:      transcript.chunk, suggestion
    assert kinds == [
        "transcript.chunk", "suggestion",
        "transcript.chunk",
        "transcript.chunk", "suggestion",
    ], f"got envelope kinds: {kinds}"

    # 2 anthropic calls (operator turn did not call response engine)
    assert len(mock_anthropic.calls) == 2

    # UI has 2 interviewer + 1 operator transcripts, 2 suggestions
    assert len(pipeline.ui_overlay.panes["interviewer"]) == 2
    assert len(pipeline.ui_overlay.panes["operator"]) == 1
    assert len(pipeline.ui_overlay.panes["suggestion"]) == 2


@pytest.mark.integration
def test_zoom_en_session_pid_is_stable(
    pipeline: WiredPipeline,
    clip_en_interviewer: SyntheticAudioClip,
    mock_fabric: MockFabricMCP,
) -> None:
    """All envelopes for one session must carry the same PID (PROF mint stability)."""
    pipeline.zoom_adapter.join("zoom://meet/pid-test")
    for _ in range(3):
        drive_turn(pipeline, clip_en_interviewer, "en")

    pids = mock_fabric.pids()
    assert len(set(pids)) == 1, f"session PID drifted across envelopes: {set(pids)}"
    assert pids[0] == pipeline.session_pid
