"""
stt_engine.py — Interview Copilot v2 speech-to-text.

Primary engine: whisper.cpp via `pywhispercpp` (local, CPU, ~0.3-1.0s latency
on a base.en model for 3-5s utterances).

Fallback engine: Google Cloud Speech-to-Text v2 (cloud, requires API key +
operator opt-in for privacy — interviewer audio MUST NOT be uploaded without
explicit consent).

This module subscribes to AudioChunk events from audio_capture.CaptureSession
and emits TranscriptEvent objects via callback. It does not persist or
analyze — that's the LLM-prompter agent's job (B04).
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Awaitable, Callable, Optional, Protocol

from audio_capture import AudioChunk, StreamTag

logger = logging.getLogger("copilot.stt")


# ---------------------------------------------------------------------------
# Public types
# ---------------------------------------------------------------------------

class STTBackend(str, Enum):
    WHISPER_LOCAL = "whisper_local"
    GOOGLE_CLOUD = "google_cloud"
    MOCK = "mock"   # for tests


@dataclass(frozen=True)
class TranscriptEvent:
    stream: StreamTag
    text: str
    language: str
    started_at: float
    duration_ms: int
    backend: STTBackend
    chunk_seq: int
    confidence: Optional[float] = None
    latency_ms: Optional[int] = None


TranscriptCallback = Callable[[TranscriptEvent], Awaitable[None]]


@dataclass
class STTConfig:
    backend: STTBackend = STTBackend.WHISPER_LOCAL
    whisper_model: str = "base.en"      # tiny | base | small | medium (+ .en variants)
    whisper_model_path: Optional[str] = None  # if None, pywhispercpp downloads to cache
    whisper_threads: int = 4
    language: Optional[str] = None       # None = autodetect (Whisper)
    google_credentials_json: Optional[str] = None  # path to service-account JSON
    google_recognizer_path: Optional[str] = None   # projects/<p>/locations/global/recognizers/_
    fallback_on_error: bool = True
    fallback_backend: STTBackend = STTBackend.GOOGLE_CLOUD
    # Operator-gated cloud egress. interviewer audio MUST NOT leave the box
    # unless this flag is True. Default False = privacy-first.
    allow_cloud_for_system_audio: bool = False


# ---------------------------------------------------------------------------
# Engine protocol + implementations
# ---------------------------------------------------------------------------

class STTEngine(Protocol):
    backend_id: STTBackend
    async def transcribe(self, chunk: AudioChunk) -> TranscriptEvent: ...
    def close(self) -> None: ...


class WhisperLocalEngine:
    """whisper.cpp via pywhispercpp.

    pywhispercpp models accept either a model name (e.g. "base.en") with auto
    download, or an explicit path to a ggml-format .bin file. We prefer the
    explicit path when the model has been vendored under D:/models/whisper/.
    """

    backend_id = STTBackend.WHISPER_LOCAL

    def __init__(self, cfg: STTConfig):
        self.cfg = cfg
        self._model = None  # lazy

    def _ensure_model(self):
        if self._model is not None:
            return
        try:
            from pywhispercpp.model import Model
        except ImportError as e:  # pragma: no cover
            raise RuntimeError(
                "pywhispercpp not installed; pip install pywhispercpp"
            ) from e
        model_id = self.cfg.whisper_model_path or self.cfg.whisper_model
        logger.info("loading whisper model: %s (threads=%d)", model_id, self.cfg.whisper_threads)
        self._model = Model(
            model_id,
            n_threads=self.cfg.whisper_threads,
            print_realtime=False,
            print_progress=False,
        )

    async def transcribe(self, chunk: AudioChunk) -> TranscriptEvent:
        self._ensure_model()
        # whisper.cpp expects float32 [-1,1] at 16 kHz mono. Convert in a thread.
        t0 = time.monotonic()
        text, lang, conf = await asyncio.to_thread(self._sync_transcribe, chunk)
        latency_ms = int((time.monotonic() - t0) * 1000)
        return TranscriptEvent(
            stream=chunk.stream,
            text=text,
            language=lang,
            started_at=chunk.started_at,
            duration_ms=chunk.duration_ms,
            backend=self.backend_id,
            chunk_seq=chunk.seq,
            confidence=conf,
            latency_ms=latency_ms,
        )

    def _sync_transcribe(self, chunk: AudioChunk) -> tuple[str, str, Optional[float]]:
        import numpy as np
        samples = np.frombuffer(chunk.pcm, dtype=np.int16).astype(np.float32) / 32768.0
        kwargs = {}
        if self.cfg.language:
            kwargs["language"] = self.cfg.language
        segments = self._model.transcribe(samples, **kwargs)
        # pywhispercpp returns a list of Segment objects with .text and .t0/.t1
        text = " ".join(s.text.strip() for s in segments).strip()
        # pywhispercpp doesn't expose per-segment confidence; we leave None.
        lang = self.cfg.language or "auto"
        return text, lang, None

    def close(self) -> None:
        # pywhispercpp Model has no explicit close; rely on GC.
        self._model = None


class GoogleCloudEngine:
    """Google Cloud Speech-to-Text v2 streaming-or-batch fallback.

    We use the SYNCHRONOUS Recognize API for chunks <= 60s (which all our
    utterance chunks are). Streaming API is overkill for our 3-5s chunks
    and adds connection-management complexity.
    """

    backend_id = STTBackend.GOOGLE_CLOUD

    def __init__(self, cfg: STTConfig):
        self.cfg = cfg
        self._client = None
        if cfg.google_credentials_json:
            os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = cfg.google_credentials_json

    def _ensure_client(self):
        if self._client is not None:
            return
        try:
            from google.cloud import speech_v2
        except ImportError as e:  # pragma: no cover
            raise RuntimeError(
                "google-cloud-speech not installed; pip install google-cloud-speech"
            ) from e
        self._client = speech_v2.SpeechClient()

    async def transcribe(self, chunk: AudioChunk) -> TranscriptEvent:
        # Privacy gate: refuse to upload system audio unless explicitly opted in.
        if chunk.stream == StreamTag.SYSTEM and not self.cfg.allow_cloud_for_system_audio:
            raise PermissionError(
                "cloud STT refused for system_audio stream "
                "(set STTConfig.allow_cloud_for_system_audio=True to override)"
            )
        self._ensure_client()
        t0 = time.monotonic()
        text, lang, conf = await asyncio.to_thread(self._sync_recognize, chunk)
        latency_ms = int((time.monotonic() - t0) * 1000)
        return TranscriptEvent(
            stream=chunk.stream,
            text=text,
            language=lang,
            started_at=chunk.started_at,
            duration_ms=chunk.duration_ms,
            backend=self.backend_id,
            chunk_seq=chunk.seq,
            confidence=conf,
            latency_ms=latency_ms,
        )

    def _sync_recognize(self, chunk: AudioChunk) -> tuple[str, str, Optional[float]]:
        from google.cloud.speech_v2 import types
        recognizer = self.cfg.google_recognizer_path or "projects/_/locations/global/recognizers/_"
        config = types.RecognitionConfig(
            auto_decoding_config=types.AutoDetectDecodingConfig(),
            language_codes=[self.cfg.language or "en-US"],
            model="long",
            features=types.RecognitionFeatures(enable_automatic_punctuation=True),
        )
        request = types.RecognizeRequest(
            recognizer=recognizer,
            config=config,
            content=chunk.pcm,
        )
        resp = self._client.recognize(request=request)
        if not resp.results:
            return "", self.cfg.language or "en-US", None
        best = resp.results[0].alternatives[0]
        return best.transcript, self.cfg.language or "en-US", float(best.confidence or 0.0)

    def close(self) -> None:
        self._client = None


class MockEngine:
    """Deterministic echo engine for unit tests."""

    backend_id = STTBackend.MOCK

    def __init__(self, cfg: STTConfig, canned_text: str = "mock transcription"):
        self.cfg = cfg
        self.canned_text = canned_text
        self.calls: list[AudioChunk] = []

    async def transcribe(self, chunk: AudioChunk) -> TranscriptEvent:
        self.calls.append(chunk)
        return TranscriptEvent(
            stream=chunk.stream,
            text=self.canned_text,
            language="en",
            started_at=chunk.started_at,
            duration_ms=chunk.duration_ms,
            backend=self.backend_id,
            chunk_seq=chunk.seq,
            confidence=1.0,
            latency_ms=1,
        )

    def close(self) -> None:
        pass


def build_engine(cfg: STTConfig) -> STTEngine:
    if cfg.backend == STTBackend.WHISPER_LOCAL:
        return WhisperLocalEngine(cfg)
    if cfg.backend == STTBackend.GOOGLE_CLOUD:
        return GoogleCloudEngine(cfg)
    if cfg.backend == STTBackend.MOCK:
        return MockEngine(cfg)
    raise ValueError(f"unknown backend: {cfg.backend}")


# ---------------------------------------------------------------------------
# STTPipeline — wraps primary + fallback + callback dispatch
# ---------------------------------------------------------------------------

class STTPipeline:
    """Owns a primary engine and an optional fallback engine.

    Wires into CaptureSession as the on_chunk callback. On primary failure,
    falls back if cfg.fallback_on_error is True AND the fallback isn't blocked
    by the cloud-egress gate.
    """

    def __init__(self, cfg: STTConfig, on_transcript: TranscriptCallback):
        self.cfg = cfg
        self.on_transcript = on_transcript
        self.primary: STTEngine = build_engine(cfg)
        self.fallback: Optional[STTEngine] = None
        if cfg.fallback_on_error and cfg.fallback_backend != cfg.backend:
            fb_cfg = STTConfig(**{**cfg.__dict__, "backend": cfg.fallback_backend})
            self.fallback = build_engine(fb_cfg)

    async def handle_chunk(self, chunk: AudioChunk) -> None:
        """Use this as the on_chunk callback for CaptureSession."""
        try:
            event = await self.primary.transcribe(chunk)
        except Exception as primary_err:
            logger.warning("primary STT failed (seq=%d): %s", chunk.seq, primary_err)
            if not self.fallback:
                logger.error("no fallback configured; dropping chunk seq=%d", chunk.seq)
                return
            try:
                event = await self.fallback.transcribe(chunk)
            except Exception as fb_err:
                logger.exception("fallback STT also failed (seq=%d): %s", chunk.seq, fb_err)
                return
        if not event.text:
            logger.debug("empty transcript for seq=%d, skipping emit", chunk.seq)
            return
        try:
            await self.on_transcript(event)
        except Exception:
            logger.exception("on_transcript callback failed for seq=%d", chunk.seq)

    def close(self) -> None:
        self.primary.close()
        if self.fallback:
            self.fallback.close()


__all__ = [
    "STTBackend",
    "STTConfig",
    "TranscriptEvent",
    "STTEngine",
    "STTPipeline",
    "WhisperLocalEngine",
    "GoogleCloudEngine",
    "MockEngine",
    "build_engine",
]
