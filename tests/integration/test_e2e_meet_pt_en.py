"""
E2E: Google Meet + PT->EN translation flow.

Scenario:
    1. Operator joins Meet meeting
    2. Interviewer speaks Portuguese (PT)
    3. Audio capture -> STT (returns PT) -> Translate to EN -> Claude suggests in EN
    4. UI shows PT transcript + EN translation + EN suggestion
    5. Fabric receives: transcript.chunk -> translation -> suggestion
"""
from __future__ import annotations

import pytest

from conftest import (
    MockAnthropicClient,
    MockFabricMCP,
    MockGoogleTranslate,
    SyntheticAudioClip,
    WiredPipeline,
    drive_turn,
)


@pytest.mark.integration
def test_meet_pt_to_en_happy_path(
    pipeline: WiredPipeline,
    clip_pt_interviewer: SyntheticAudioClip,
    mock_fabric: MockFabricMCP,
    mock_translate: MockGoogleTranslate,
    mock_anthropic: MockAnthropicClient,
) -> None:
    # 1. Join Meet
    join_ack = pipeline.meet_adapter.join("https://meet.google.com/abc-defg-hij")
    assert join_ack["platform"] == "meet"
    assert join_ack["status"] == "joined"
    assert pipeline.meet_adapter.joined

    # 2..4. Drive a PT interviewer turn with EN operator target
    result = drive_turn(pipeline, clip_pt_interviewer, operator_target_lang="en")

    # Transcript is PT
    assert result["transcript"]["text"] == clip_pt_interviewer.expected_transcript
    assert result["transcript"]["lang"] == "pt"

    # Translation triggered exactly once: PT -> EN
    assert len(mock_translate.calls) == 1
    call = mock_translate.calls[0]
    assert call["source"] == "pt"
    assert call["target"] == "en"
    assert "distributed systems" in result["translation"].lower()

    # Suggestion is EN-flavored (Claude received the EN translation)
    assert result["suggestion"] is not None
    assert result["suggestion"]["route"] == "cloud"

    # UI panes
    assert pipeline.ui_overlay.panes["interviewer"] == [clip_pt_interviewer.expected_transcript]
    assert len(pipeline.ui_overlay.panes["suggestion"]) == 1

    # 5. Fabric envelope sequence
    kinds = mock_fabric.kinds()
    assert kinds == ["transcript.chunk", "translation", "suggestion"], (
        f"PT->EN flow must emit translation envelope; got {kinds}"
    )

    # Anthropic was called once with the EN translation
    assert len(mock_anthropic.calls) == 1
    user_msg = mock_anthropic.calls[0]["messages"][0]
    assert "distributed systems" in str(user_msg["content"]).lower()


@pytest.mark.integration
def test_meet_pt_translation_payload_carries_both_languages(
    pipeline: WiredPipeline,
    clip_pt_interviewer: SyntheticAudioClip,
    mock_fabric: MockFabricMCP,
) -> None:
    pipeline.meet_adapter.join("https://meet.google.com/payload-test")
    drive_turn(pipeline, clip_pt_interviewer, operator_target_lang="en")

    translation_env = next(e for e in mock_fabric.emitted if e.kind == "translation")
    assert translation_env.payload["src_lang"] == "pt"
    assert translation_env.payload["tgt_lang"] == "en"
    assert translation_env.payload["src_text"] == clip_pt_interviewer.expected_transcript
    assert "distributed systems" in translation_env.payload["tgt_text"].lower()


@pytest.mark.integration
def test_meet_pt_repeated_translation_is_consistent(
    pipeline: WiredPipeline,
    clip_pt_interviewer: SyntheticAudioClip,
    mock_translate: MockGoogleTranslate,
) -> None:
    """Same input -> same output across turns (deterministic mock)."""
    pipeline.meet_adapter.join("https://meet.google.com/det-test")

    outputs = []
    for _ in range(3):
        r = drive_turn(pipeline, clip_pt_interviewer, "en")
        outputs.append(r["translation"])

    assert len(set(outputs)) == 1, f"translation drifted: {outputs}"
    assert len(mock_translate.calls) == 3
