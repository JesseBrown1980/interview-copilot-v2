"""Unit tests for stt_engine.py — Whisper + Google clients fully mocked."""

from __future__ import annotations

import asyncio
import os
import struct
import sys
import time
import unittest
from unittest.mock import MagicMock, patch

SRC = os.path.join(os.path.dirname(__file__), "..", "src")
sys.path.insert(0, os.path.abspath(SRC))

from audio_capture import AudioChunk, StreamTag  # noqa: E402
from stt_engine import (  # noqa: E402
    GoogleCloudEngine,
    MockEngine,
    STTBackend,
    STTConfig,
    STTPipeline,
    TranscriptEvent,
    WhisperLocalEngine,
    build_engine,
)


def make_chunk(stream: StreamTag = StreamTag.MIC, seq: int = 0) -> AudioChunk:
    pcm = struct.pack("<160h", *([1000] * 160))  # 10ms-ish placeholder
    return AudioChunk(
        stream=stream,
        pcm=pcm,
        sample_rate=16000,
        started_at=time.monotonic(),
        duration_ms=10,
        vad_decision="manual_flush",
        seq=seq,
    )


# ---------------------------------------------------------------------------
# MockEngine
# ---------------------------------------------------------------------------

class MockEngineTests(unittest.IsolatedAsyncioTestCase):
    async def test_mock_emits_canned_text(self):
        cfg = STTConfig(backend=STTBackend.MOCK)
        eng = MockEngine(cfg, canned_text="hello world")
        ev = await eng.transcribe(make_chunk(seq=7))
        self.assertEqual(ev.text, "hello world")
        self.assertEqual(ev.backend, STTBackend.MOCK)
        self.assertEqual(ev.chunk_seq, 7)
        self.assertEqual(eng.calls[0].seq, 7)


# ---------------------------------------------------------------------------
# WhisperLocalEngine — mock pywhispercpp
# ---------------------------------------------------------------------------

class WhisperLocalEngineTests(unittest.IsolatedAsyncioTestCase):
    async def test_whisper_calls_model_and_returns_event(self):
        cfg = STTConfig(backend=STTBackend.WHISPER_LOCAL, whisper_model="base.en")
        eng = WhisperLocalEngine(cfg)

        fake_segment = MagicMock()
        fake_segment.text = " hello whisper "
        fake_model = MagicMock()
        fake_model.transcribe.return_value = [fake_segment]

        fake_module = MagicMock()
        fake_module.Model = MagicMock(return_value=fake_model)
        with patch.dict(sys.modules, {"pywhispercpp.model": fake_module}):
            ev = await eng.transcribe(make_chunk(seq=3))

        self.assertEqual(ev.text, "hello whisper")
        self.assertEqual(ev.backend, STTBackend.WHISPER_LOCAL)
        self.assertEqual(ev.chunk_seq, 3)
        self.assertIsNotNone(ev.latency_ms)
        fake_module.Model.assert_called_once()

    async def test_whisper_passes_language_when_set(self):
        cfg = STTConfig(backend=STTBackend.WHISPER_LOCAL, language="pt")
        eng = WhisperLocalEngine(cfg)
        fake_segment = MagicMock()
        fake_segment.text = "ola"
        fake_model = MagicMock()
        fake_model.transcribe.return_value = [fake_segment]
        fake_module = MagicMock()
        fake_module.Model = MagicMock(return_value=fake_model)
        with patch.dict(sys.modules, {"pywhispercpp.model": fake_module}):
            await eng.transcribe(make_chunk())
        # Validate language kwarg was forwarded
        _, kwargs = fake_model.transcribe.call_args
        self.assertEqual(kwargs.get("language"), "pt")


# ---------------------------------------------------------------------------
# GoogleCloudEngine — mock google.cloud.speech_v2
# ---------------------------------------------------------------------------

class GoogleCloudEngineTests(unittest.IsolatedAsyncioTestCase):
    async def test_cloud_refused_for_system_audio_by_default(self):
        cfg = STTConfig(backend=STTBackend.GOOGLE_CLOUD)
        eng = GoogleCloudEngine(cfg)
        with self.assertRaises(PermissionError):
            await eng.transcribe(make_chunk(stream=StreamTag.SYSTEM))

    async def test_cloud_allowed_when_optin_set(self):
        cfg = STTConfig(
            backend=STTBackend.GOOGLE_CLOUD,
            allow_cloud_for_system_audio=True,
        )
        eng = GoogleCloudEngine(cfg)

        fake_alt = MagicMock(transcript="cloud says hi", confidence=0.87)
        fake_result = MagicMock(alternatives=[fake_alt])
        fake_response = MagicMock(results=[fake_result])
        fake_client = MagicMock()
        fake_client.recognize.return_value = fake_response

        fake_types = MagicMock()
        fake_types.RecognitionConfig = MagicMock()
        fake_types.AutoDetectDecodingConfig = MagicMock()
        fake_types.RecognitionFeatures = MagicMock()
        fake_types.RecognizeRequest = MagicMock()

        fake_speech_v2 = MagicMock()
        fake_speech_v2.SpeechClient = MagicMock(return_value=fake_client)
        fake_speech_v2.types = fake_types
        fake_gc = MagicMock(speech_v2=fake_speech_v2)

        with patch.dict(sys.modules, {
            "google.cloud": fake_gc,
            "google.cloud.speech_v2": fake_speech_v2,
            "google.cloud.speech_v2.types": fake_types,
        }):
            ev = await eng.transcribe(make_chunk(stream=StreamTag.SYSTEM, seq=11))

        self.assertEqual(ev.text, "cloud says hi")
        self.assertAlmostEqual(ev.confidence or 0.0, 0.87, places=2)
        self.assertEqual(ev.chunk_seq, 11)


# ---------------------------------------------------------------------------
# STTPipeline — fallback + callback behavior
# ---------------------------------------------------------------------------

class FailingEngine:
    backend_id = STTBackend.WHISPER_LOCAL
    def __init__(self, *_a, **_k): pass
    async def transcribe(self, chunk):
        raise RuntimeError("primary blew up")
    def close(self): pass


class STTPipelineTests(unittest.IsolatedAsyncioTestCase):
    async def test_pipeline_emits_transcript_via_callback(self):
        cfg = STTConfig(backend=STTBackend.MOCK, fallback_on_error=False)
        events: list[TranscriptEvent] = []

        async def cb(ev: TranscriptEvent):
            events.append(ev)

        pipe = STTPipeline(cfg, cb)
        await pipe.handle_chunk(make_chunk(seq=1))
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].chunk_seq, 1)
        self.assertEqual(events[0].backend, STTBackend.MOCK)

    async def test_pipeline_skips_empty_text(self):
        cfg = STTConfig(backend=STTBackend.MOCK, fallback_on_error=False)
        events: list[TranscriptEvent] = []

        async def cb(ev: TranscriptEvent):
            events.append(ev)

        pipe = STTPipeline(cfg, cb)
        # swap engine for one that returns empty text
        pipe.primary = MockEngine(cfg, canned_text="")
        await pipe.handle_chunk(make_chunk())
        self.assertEqual(events, [])

    async def test_pipeline_falls_back_on_primary_failure(self):
        cfg = STTConfig(
            backend=STTBackend.WHISPER_LOCAL,
            fallback_on_error=True,
            fallback_backend=STTBackend.MOCK,
        )
        events: list[TranscriptEvent] = []

        async def cb(ev: TranscriptEvent):
            events.append(ev)

        pipe = STTPipeline.__new__(STTPipeline)
        pipe.cfg = cfg
        pipe.on_transcript = cb
        pipe.primary = FailingEngine()
        pipe.fallback = MockEngine(cfg, canned_text="fallback wins")

        await pipe.handle_chunk(make_chunk(seq=42))
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].text, "fallback wins")
        self.assertEqual(events[0].chunk_seq, 42)

    async def test_pipeline_drops_chunk_when_no_fallback_and_primary_fails(self):
        cfg = STTConfig(backend=STTBackend.WHISPER_LOCAL, fallback_on_error=False)
        events: list[TranscriptEvent] = []

        async def cb(ev: TranscriptEvent):
            events.append(ev)

        pipe = STTPipeline.__new__(STTPipeline)
        pipe.cfg = cfg
        pipe.on_transcript = cb
        pipe.primary = FailingEngine()
        pipe.fallback = None

        await pipe.handle_chunk(make_chunk())
        self.assertEqual(events, [])


class BuildEngineTests(unittest.TestCase):
    def test_build_mock(self):
        eng = build_engine(STTConfig(backend=STTBackend.MOCK))
        self.assertIsInstance(eng, MockEngine)

    def test_build_unknown_raises(self):
        cfg = STTConfig()
        cfg.backend = "wrong"  # type: ignore
        with self.assertRaises(ValueError):
            build_engine(cfg)


if __name__ == "__main__":
    unittest.main()
