"""
E2E failure-mode tests.

Scenarios:
    A. Anthropic API down (503) -> response_engine falls back to local route, UI still gets a suggestion
    B. Audio device disconnects mid-meeting -> graceful re-discovery, no transcript loss for queued clips
    C. Fabric MCP unreachable -> envelopes buffer locally, flush on reconnect
    D. Google STT unavailable -> raised cleanly (no silent corruption) and operator sees an error envelope
    E. Google Translate unavailable + PT input -> graceful degradation (raw PT shown to operator)
"""
from __future__ import annotations

import pytest

from conftest import (
    MockAnthropicClient,
    MockFabricMCP,
    MockGoogleSTT,
    MockGoogleTranslate,
    SyntheticAudioClip,
    WiredPipeline,
    drive_turn,
)


# ---------------------------------------------------------------------------
# A. Anthropic 503 -> local fallback
# ---------------------------------------------------------------------------
@pytest.mark.integration
def test_anthropic_503_triggers_local_fallback(
    pipeline: WiredPipeline,
    clip_en_interviewer: SyntheticAudioClip,
    mock_anthropic: MockAnthropicClient,
    mock_fabric: MockFabricMCP,
) -> None:
    pipeline.zoom_adapter.join("zoom://meet/fail-A")
    mock_anthropic.failure_mode = "503"

    result = drive_turn(pipeline, clip_en_interviewer, "en")

    # Fallback engaged
    assert pipeline.response_engine.fallback_used is True
    assert result["suggestion"]["route"] == "local-fallback"
    assert "LOCAL-FALLBACK" in result["suggestion"]["text"]
    assert "503" in result["suggestion"]["error"]

    # UI still got a suggestion (graceful)
    assert len(pipeline.ui_overlay.panes["suggestion"]) == 1

    # Fabric still sees the chain (suggestion envelope carries route=local-fallback)
    suggestion_env = next(e for e in mock_fabric.emitted if e.kind == "suggestion")
    assert suggestion_env.payload["route"] == "local-fallback"


@pytest.mark.integration
def test_anthropic_timeout_triggers_local_fallback(
    pipeline: WiredPipeline,
    clip_en_interviewer: SyntheticAudioClip,
    mock_anthropic: MockAnthropicClient,
) -> None:
    pipeline.zoom_adapter.join("zoom://meet/fail-A-timeout")
    mock_anthropic.failure_mode = "timeout"

    result = drive_turn(pipeline, clip_en_interviewer, "en")
    assert result["suggestion"]["route"] == "local-fallback"
    assert pipeline.response_engine.fallback_used is True


@pytest.mark.integration
def test_anthropic_rate_limit_triggers_local_fallback(
    pipeline: WiredPipeline,
    clip_en_interviewer: SyntheticAudioClip,
    mock_anthropic: MockAnthropicClient,
) -> None:
    pipeline.zoom_adapter.join("zoom://meet/fail-A-rate")
    mock_anthropic.failure_mode = "rate"

    result = drive_turn(pipeline, clip_en_interviewer, "en")
    assert result["suggestion"]["route"] == "local-fallback"


# ---------------------------------------------------------------------------
# B. Audio device disconnect mid-meeting
# ---------------------------------------------------------------------------
@pytest.mark.integration
def test_audio_disconnect_reconnect_no_transcript_loss(
    pipeline: WiredPipeline,
    clip_en_interviewer: SyntheticAudioClip,
    mock_fabric: MockFabricMCP,
) -> None:
    pipeline.zoom_adapter.join("zoom://meet/fail-B")

    # Turn 1: normal
    drive_turn(pipeline, clip_en_interviewer, "en")
    assert len(pipeline.ui_overlay.panes["interviewer"]) == 1

    # Disconnect
    pipeline.audio_capture.simulate_disconnect()
    assert pipeline.audio_capture.connected is False

    # Pushing during disconnect raises (caller responsible for retry)
    with pytest.raises(RuntimeError, match="audio device not connected"):
        pipeline.audio_capture.push_clip(clip_en_interviewer)

    # Reconnect
    pipeline.audio_capture.reconnect()
    assert pipeline.audio_capture.connected is True

    # Turn 2: post-reconnect — must succeed without loss of prior transcript
    drive_turn(pipeline, clip_en_interviewer, "en")
    assert len(pipeline.ui_overlay.panes["interviewer"]) == 2     # turn 1 preserved + turn 2 added
    assert pipeline.audio_capture.disconnect_count == 1
    # Fabric received turn-2 envelopes intact
    assert mock_fabric.kinds().count("transcript.chunk") == 2


# ---------------------------------------------------------------------------
# C. Fabric MCP unreachable -> buffer + flush
# ---------------------------------------------------------------------------
@pytest.mark.integration
def test_fabric_unreachable_buffers_and_flushes_on_reconnect(
    pipeline: WiredPipeline,
    clip_en_interviewer: SyntheticAudioClip,
    mock_fabric: MockFabricMCP,
) -> None:
    pipeline.zoom_adapter.join("zoom://meet/fail-C")

    # Knock fabric offline before any turn
    mock_fabric.reachable = False

    # Drive a turn — should NOT raise, should buffer
    drive_turn(pipeline, clip_en_interviewer, "en")
    assert mock_fabric.emitted == []
    assert len(pipeline.fabric_emitter.buffer) == 2   # transcript + suggestion (no translation, EN->EN)

    # Drive another turn while still down
    drive_turn(pipeline, clip_en_interviewer, "en")
    assert len(pipeline.fabric_emitter.buffer) == 4

    # Bring fabric back up + flush
    mock_fabric.reachable = True
    flushed = pipeline.fabric_emitter.flush_buffer()
    assert flushed == 4
    assert pipeline.fabric_emitter.buffer == []
    assert len(mock_fabric.emitted) == 4

    # UI was never blocked
    assert len(pipeline.ui_overlay.panes["interviewer"]) == 2
    assert len(pipeline.ui_overlay.panes["suggestion"]) == 2


@pytest.mark.integration
def test_fabric_partial_outage_partial_flush(
    pipeline: WiredPipeline,
    clip_en_interviewer: SyntheticAudioClip,
    mock_fabric: MockFabricMCP,
) -> None:
    """Fabric flips between reachable + unreachable -> buffer holds only the failed ones."""
    pipeline.zoom_adapter.join("zoom://meet/fail-C-partial")

    # Turn 1: succeeds
    drive_turn(pipeline, clip_en_interviewer, "en")
    assert len(mock_fabric.emitted) == 2
    assert len(pipeline.fabric_emitter.buffer) == 0

    # Knock offline
    mock_fabric.reachable = False
    drive_turn(pipeline, clip_en_interviewer, "en")
    assert len(pipeline.fabric_emitter.buffer) == 2

    # Reconnect + flush
    mock_fabric.reachable = True
    pipeline.fabric_emitter.flush_buffer()
    assert len(mock_fabric.emitted) == 4
    assert pipeline.fabric_emitter.buffer == []


# ---------------------------------------------------------------------------
# D. STT unavailable
# ---------------------------------------------------------------------------
@pytest.mark.integration
def test_stt_unavailable_raises_cleanly(
    pipeline: WiredPipeline,
    clip_en_interviewer: SyntheticAudioClip,
    mock_stt: MockGoogleSTT,
) -> None:
    pipeline.zoom_adapter.join("zoom://meet/fail-D")
    mock_stt.failure_mode = "unavailable"

    with pytest.raises(RuntimeError, match="MockSTTError"):
        drive_turn(pipeline, clip_en_interviewer, "en")

    # Switch to local-only — STT routes locally and succeeds
    pipeline.privacy_gate.local_only = True
    result = drive_turn(pipeline, clip_en_interviewer, "en")
    assert result["transcript"]["route"] == "local"
    assert result["transcript"]["text"] == clip_en_interviewer.expected_transcript


# ---------------------------------------------------------------------------
# E. Translate unavailable on PT input
# ---------------------------------------------------------------------------
@pytest.mark.integration
def test_translate_unavailable_on_pt_input_raises_cleanly(
    pipeline: WiredPipeline,
    clip_pt_interviewer: SyntheticAudioClip,
    mock_translate: MockGoogleTranslate,
    mock_fabric: MockFabricMCP,
) -> None:
    pipeline.meet_adapter.join("https://meet.google.com/fail-E")
    mock_translate.failure_mode = "unavailable"

    with pytest.raises(RuntimeError, match="MockTranslateError"):
        drive_turn(pipeline, clip_pt_interviewer, "en")

    # Transcript envelope was still emitted before the translator threw
    assert mock_fabric.kinds() == ["transcript.chunk"]
    # UI got the PT transcript even though translation failed
    assert pipeline.ui_overlay.panes["interviewer"] == [clip_pt_interviewer.expected_transcript]


# ---------------------------------------------------------------------------
# F. Combined: Anthropic down + Fabric down simultaneously
# ---------------------------------------------------------------------------
@pytest.mark.integration
def test_combined_anthropic_and_fabric_outage(
    pipeline: WiredPipeline,
    clip_en_interviewer: SyntheticAudioClip,
    mock_anthropic: MockAnthropicClient,
    mock_fabric: MockFabricMCP,
) -> None:
    """Worst-case: both cloud paths fail. UI must still render a suggestion + fabric buffers."""
    pipeline.zoom_adapter.join("zoom://meet/fail-F-combined")
    mock_anthropic.failure_mode = "503"
    mock_fabric.reachable = False

    drive_turn(pipeline, clip_en_interviewer, "en")

    # UI still rendered
    assert len(pipeline.ui_overlay.panes["interviewer"]) == 1
    assert len(pipeline.ui_overlay.panes["suggestion"]) == 1

    # Fabric buffered
    assert len(pipeline.fabric_emitter.buffer) == 2
    assert mock_fabric.emitted == []

    # Restore both
    mock_anthropic.failure_mode = None
    mock_fabric.reachable = True
    pipeline.fabric_emitter.flush_buffer()
    assert len(mock_fabric.emitted) == 2

    # Next turn fully cloud-routed
    result = drive_turn(pipeline, clip_en_interviewer, "en")
    assert result["suggestion"]["route"] == "cloud"
    assert len(mock_fabric.emitted) == 4
