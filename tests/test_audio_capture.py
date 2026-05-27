"""Unit tests for audio_capture.py — no real audio hardware required."""

from __future__ import annotations

import asyncio
import os
import struct
import sys
import time
import unittest

# Make src/ importable without installing.
SRC = os.path.join(os.path.dirname(__file__), "..", "src")
sys.path.insert(0, os.path.abspath(SRC))

from audio_capture import (  # noqa: E402
    AudioChunk,
    CaptureConfig,
    CaptureSession,
    EnergyVAD,
    StreamTag,
    UtteranceAssembler,
    VADAdapter,
    WebRTCVAD,
    build_vad,
    downsample_to_16k,
)


# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------

class AlwaysSpeechVAD(VADAdapter):
    def is_speech(self, frame_pcm: bytes, sample_rate: int) -> bool:
        return True


class AlwaysSilenceVAD(VADAdapter):
    def is_speech(self, frame_pcm: bytes, sample_rate: int) -> bool:
        return False


class ScriptedVAD(VADAdapter):
    """Returns a sequence of booleans, repeating last value when exhausted."""
    def __init__(self, script: list[bool]):
        self.script = list(script)
        self.idx = 0

    def is_speech(self, frame_pcm: bytes, sample_rate: int) -> bool:
        if self.idx < len(self.script):
            v = self.script[self.idx]
            self.idx += 1
        else:
            v = self.script[-1]
        return v


def silence_frame(cfg: CaptureConfig) -> bytes:
    n = cfg.sample_rate * cfg.frame_ms // 1000
    return b"\x00\x00" * n


def tone_frame(cfg: CaptureConfig, amplitude: int = 8000) -> bytes:
    n = cfg.sample_rate * cfg.frame_ms // 1000
    # not a real sine — just nonzero so energy VAD trips
    return struct.pack(f"<{n}h", *([amplitude, -amplitude] * (n // 2)))


# ---------------------------------------------------------------------------
# UtteranceAssembler tests
# ---------------------------------------------------------------------------

class UtteranceAssemblerTests(unittest.TestCase):
    def setUp(self):
        self.cfg = CaptureConfig(
            sample_rate=16000,
            frame_ms=30,
            min_utterance_ms=90,
            max_utterance_ms=300,
            silence_close_ms=60,
        )

    def test_silence_only_emits_nothing(self):
        asm = UtteranceAssembler(StreamTag.MIC, self.cfg, AlwaysSilenceVAD())
        for _ in range(20):
            self.assertIsNone(asm.push_frame(silence_frame(self.cfg), time.monotonic()))

    def test_speech_then_silence_emits_silence_close(self):
        # 4 speech frames (120ms) then 3 silence frames (90ms) — over silence_close_ms.
        script = [True] * 4 + [False] * 3
        asm = UtteranceAssembler(StreamTag.MIC, self.cfg, ScriptedVAD(script))
        emitted: list[AudioChunk] = []
        for _ in script:
            ch = asm.push_frame(tone_frame(self.cfg), time.monotonic())
            if ch:
                emitted.append(ch)
        self.assertEqual(len(emitted), 1)
        self.assertEqual(emitted[0].vad_decision, "silence_close")
        self.assertEqual(emitted[0].stream, StreamTag.MIC)
        self.assertGreaterEqual(emitted[0].duration_ms, self.cfg.min_utterance_ms)

    def test_continuous_speech_hits_max_buffer(self):
        asm = UtteranceAssembler(StreamTag.MIC, self.cfg, AlwaysSpeechVAD())
        emitted: list[AudioChunk] = []
        # 15 frames * 30ms = 450ms > max_utterance_ms (300ms)
        for _ in range(15):
            ch = asm.push_frame(tone_frame(self.cfg), time.monotonic())
            if ch:
                emitted.append(ch)
        self.assertGreaterEqual(len(emitted), 1)
        self.assertEqual(emitted[0].vad_decision, "max_buffer")

    def test_short_speech_below_min_does_not_emit_on_silence(self):
        # only 60ms of speech (2 frames) — below 90ms min_utterance_ms
        script = [True, True, False, False, False, False]
        asm = UtteranceAssembler(StreamTag.MIC, self.cfg, ScriptedVAD(script))
        emitted: list[AudioChunk] = []
        for _ in script:
            ch = asm.push_frame(tone_frame(self.cfg), time.monotonic())
            if ch:
                emitted.append(ch)
        self.assertEqual(emitted, [])

    def test_seq_increments_per_emit(self):
        asm = UtteranceAssembler(StreamTag.SYSTEM, self.cfg, AlwaysSpeechVAD())
        seqs = []
        for _ in range(40):
            ch = asm.push_frame(tone_frame(self.cfg), time.monotonic())
            if ch:
                seqs.append(ch.seq)
        self.assertEqual(seqs, list(range(len(seqs))))

    def test_flush_returns_partial_buffer_if_above_min(self):
        # 4 speech frames (120ms) without closing silence.
        asm = UtteranceAssembler(StreamTag.MIC, self.cfg, AlwaysSpeechVAD())
        for _ in range(4):
            asm.push_frame(tone_frame(self.cfg), time.monotonic())
        ch = asm.flush(time.monotonic())
        self.assertIsNotNone(ch)
        self.assertEqual(ch.vad_decision, "manual_flush")

    def test_flush_returns_none_if_below_min(self):
        asm = UtteranceAssembler(StreamTag.MIC, self.cfg, AlwaysSpeechVAD())
        # only 1 speech frame (30ms) — below 90ms min
        asm.push_frame(tone_frame(self.cfg), time.monotonic())
        self.assertIsNone(asm.flush(time.monotonic()))


# ---------------------------------------------------------------------------
# VAD tests
# ---------------------------------------------------------------------------

class EnergyVADTests(unittest.TestCase):
    def test_silence_classified_as_non_speech(self):
        vad = EnergyVAD(rms_threshold=500.0)
        cfg = CaptureConfig()
        self.assertFalse(vad.is_speech(silence_frame(cfg), cfg.sample_rate))

    def test_tone_classified_as_speech(self):
        vad = EnergyVAD(rms_threshold=500.0)
        cfg = CaptureConfig()
        self.assertTrue(vad.is_speech(tone_frame(cfg, amplitude=8000), cfg.sample_rate))


class BuildVADTests(unittest.TestCase):
    def test_energy_backend_explicit(self):
        cfg = CaptureConfig(vad_backend="energy")
        self.assertIsInstance(build_vad(cfg), EnergyVAD)

    def test_unknown_backend_raises(self):
        cfg = CaptureConfig(vad_backend="nonsense")
        with self.assertRaises(ValueError):
            build_vad(cfg)


# ---------------------------------------------------------------------------
# Resampling
# ---------------------------------------------------------------------------

class DownsampleTests(unittest.TestCase):
    def test_16k_passthrough(self):
        cfg = CaptureConfig()
        frame = tone_frame(cfg)
        self.assertEqual(downsample_to_16k(frame, 16000), frame)

    def test_48k_decimation_by_3(self):
        # 30ms @ 48k = 1440 samples = 2880 bytes. Output should be 480 samples.
        n = 1440
        samples = list(range(n))
        pcm = struct.pack(f"<{n}h", *samples)
        out = downsample_to_16k(pcm, 48000)
        self.assertEqual(len(out), 480 * 2)

    def test_arbitrary_rate_interp_length(self):
        n = 320  # 20ms @ 16k... but lie and say it's 8000 src
        samples = [1000] * n
        pcm = struct.pack(f"<{n}h", *samples)
        out = downsample_to_16k(pcm, 8000)
        # ratio = 2 -> expect ~640 samples
        self.assertEqual(len(out), 640 * 2)


# ---------------------------------------------------------------------------
# CaptureSession integration with fake StreamSources
# ---------------------------------------------------------------------------

class FakeStream:
    """Synchronous fake — yields a fixed sequence of frames then blocks."""
    def __init__(self, frames: list[bytes]):
        self.frames = list(frames)
        self.opened = False
        self.closed = False
        self._idx = 0
        self.native_rate = 16000

    def open(self):
        self.opened = True

    def read_frame(self) -> bytes:
        if self._idx < len(self.frames):
            f = self.frames[self._idx]
            self._idx += 1
            return f
        # block forever to simulate live stream — session stop will cancel
        time.sleep(10)
        return b""

    def close(self):
        self.closed = True


class CaptureSessionTests(unittest.IsolatedAsyncioTestCase):
    async def test_session_emits_chunks_via_callback(self):
        cfg = CaptureConfig(
            sample_rate=16000,
            frame_ms=30,
            min_utterance_ms=60,
            max_utterance_ms=150,
            silence_close_ms=60,
            vad_backend="energy",
            enable_mic=True,
            enable_system=False,
        )
        # 5 tone frames (150ms) -> should trigger max_buffer
        frames = [tone_frame(cfg) for _ in range(5)]
        fake_mic = FakeStream(frames)
        received: list[AudioChunk] = []

        async def cb(c: AudioChunk):
            received.append(c)

        session = CaptureSession(cfg, cb, system_source=None, mic_source=fake_mic)
        async with session:
            # give the worker enough loop ticks to consume frames
            for _ in range(20):
                await asyncio.sleep(0.01)
                if received:
                    break

        self.assertTrue(fake_mic.opened)
        self.assertTrue(fake_mic.closed)
        self.assertGreaterEqual(len(received), 1)
        self.assertEqual(received[0].stream, StreamTag.MIC)
        self.assertIn(received[0].vad_decision, {"max_buffer", "silence_close", "manual_flush"})


if __name__ == "__main__":
    unittest.main()
