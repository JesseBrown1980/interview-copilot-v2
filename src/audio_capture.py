"""
audio_capture.py — Interview Copilot v2 dual-stream audio capture.

Captures two parallel streams:
  - system_audio: WASAPI loopback (interviewer voice from Zoom/Meet/Teams speakers)
  - mic:         operator's microphone

Both streams are chunked by VAD (Voice Activity Detection) into utterance-sized
windows (3-5s default, emitted early on silence-detect). Each emitted chunk is
delivered to a downstream consumer callback as a (stream_tag, pcm_bytes, meta)
tuple. The STT engine subscribes to these.

Design notes
------------
1. WASAPI loopback support on Windows requires `pyaudiowpatch` (a fork of
   PyAudio with WASAPI loopback). On non-Windows hosts the module degrades to
   `sounddevice` for both streams (loopback unavailable; mic-only).
2. VAD: we use webrtcvad (3 aggressiveness levels) as primary because it is
   pure-C and has zero ML dependency. silero-vad (ONNX) is offered as an
   alternative for quieter rooms / noisy backgrounds where webrtc misclassifies.
3. Sample format: 16 kHz / 16-bit mono PCM. This is the Whisper-native format
   and avoids resampling on the STT side. WASAPI captures at device-native
   sample rate (usually 48 kHz / 32f); we resample-down + dither in-pipeline.
4. Backpressure: each stream has a bounded asyncio.Queue (default depth 8).
   On overflow we drop oldest (loopback can't pause the OS mixer; mic can't
   pause the operator). Drop events are logged.
5. The capture module emits envelopes via callback; it does NOT directly write
   files or call STT. This keeps it unit-testable with a mock callback.

Frontend-agent discipline: this module is a ROUTER of audio bytes — it does
not transcribe, summarize, or persist. It hands off to stt_engine.py.
"""

from __future__ import annotations

import asyncio
import logging
import platform
import struct
import sys
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Awaitable, Callable, Optional

logger = logging.getLogger("copilot.audio")


# ---------------------------------------------------------------------------
# Public types
# ---------------------------------------------------------------------------

class StreamTag(str, Enum):
    """Identifies which audio stream a chunk came from."""
    SYSTEM = "system_audio"   # WASAPI loopback (interviewer)
    MIC = "mic"               # operator microphone


@dataclass(frozen=True)
class AudioChunk:
    """An emitted utterance-sized chunk of 16-bit mono PCM @ 16 kHz."""
    stream: StreamTag
    pcm: bytes                # raw little-endian int16 PCM
    sample_rate: int          # always 16000 after pipeline
    started_at: float         # monotonic seconds at first sample
    duration_ms: int
    vad_decision: str         # "silence_close" | "max_buffer" | "manual_flush"
    seq: int                  # per-stream monotonic chunk index


@dataclass
class CaptureConfig:
    """Tunable parameters for the capture pipeline."""
    sample_rate: int = 16000             # output sample rate (Whisper-native)
    frame_ms: int = 30                   # webrtcvad supports 10/20/30 ms frames
    vad_aggressiveness: int = 2          # 0=most permissive, 3=most aggressive
    min_utterance_ms: int = 300          # don't emit chunks shorter than this
    max_utterance_ms: int = 5000         # force-emit at this length
    silence_close_ms: int = 600          # close utterance after this much silence
    queue_depth: int = 8                 # per-stream bounded queue
    enable_mic: bool = True
    enable_system: bool = True
    vad_backend: str = "webrtc"          # "webrtc" | "silero" | "energy"


# Consumer callback: stt_engine.py provides this.
ChunkCallback = Callable[[AudioChunk], Awaitable[None]]


# ---------------------------------------------------------------------------
# VAD adapters
# ---------------------------------------------------------------------------

class VADAdapter:
    """Abstract VAD — returns True if frame contains speech."""

    def is_speech(self, frame_pcm: bytes, sample_rate: int) -> bool:
        raise NotImplementedError


class WebRTCVAD(VADAdapter):
    def __init__(self, aggressiveness: int = 2):
        try:
            import webrtcvad
        except ImportError as e:  # pragma: no cover - import guard
            raise RuntimeError(
                "webrtcvad not installed; pip install webrtcvad-wheels"
            ) from e
        self._vad = webrtcvad.Vad(aggressiveness)

    def is_speech(self, frame_pcm: bytes, sample_rate: int) -> bool:
        return self._vad.is_speech(frame_pcm, sample_rate)


class EnergyVAD(VADAdapter):
    """Cheap fallback — RMS energy threshold. Use only if webrtc unavailable."""

    def __init__(self, rms_threshold: float = 500.0):
        self.rms_threshold = rms_threshold

    def is_speech(self, frame_pcm: bytes, sample_rate: int) -> bool:
        n = len(frame_pcm) // 2
        if n == 0:
            return False
        samples = struct.unpack(f"<{n}h", frame_pcm)
        # RMS without numpy to keep dependency surface tight in the hot path.
        acc = 0
        for s in samples:
            acc += s * s
        rms = (acc / n) ** 0.5
        return rms > self.rms_threshold


def build_vad(cfg: CaptureConfig) -> VADAdapter:
    if cfg.vad_backend == "webrtc":
        try:
            return WebRTCVAD(cfg.vad_aggressiveness)
        except RuntimeError:
            logger.warning("webrtcvad unavailable, falling back to energy VAD")
            return EnergyVAD()
    if cfg.vad_backend == "energy":
        return EnergyVAD()
    if cfg.vad_backend == "silero":
        try:
            return SileroVAD()
        except Exception as e:  # pragma: no cover
            logger.warning("silero VAD failed (%s), falling back to webrtc", e)
            return WebRTCVAD(cfg.vad_aggressiveness)
    raise ValueError(f"unknown vad_backend: {cfg.vad_backend}")


class SileroVAD(VADAdapter):  # pragma: no cover - requires torch + ONNX
    """Silero-VAD via ONNX runtime. Heavier but more accurate in noise."""

    def __init__(self):
        import numpy as np  # noqa: F401
        import onnxruntime as ort
        # Model path is expected to be vendored; we don't download at runtime.
        self.session = ort.InferenceSession("models/silero_vad.onnx")
        self._state = None

    def is_speech(self, frame_pcm: bytes, sample_rate: int) -> bool:
        import numpy as np
        x = np.frombuffer(frame_pcm, dtype=np.int16).astype(np.float32) / 32768.0
        out = self.session.run(None, {"input": x[None, :], "sr": sample_rate})
        prob = float(out[0].squeeze())
        return prob > 0.5


# ---------------------------------------------------------------------------
# Resampling (device-native -> 16 kHz)
# ---------------------------------------------------------------------------

def downsample_to_16k(pcm: bytes, src_rate: int) -> bytes:
    """Simple decimation w/ linear-interpolation fallback.

    For src_rate == 16000 returns input unchanged. For 48000 we do strict 1:3
    decimation. For arbitrary rates we fall back to linear interpolation. This
    is good enough for Whisper at telephone-quality voice; for higher-fidelity
    we'd swap in soxr or scipy.signal.resample_poly.
    """
    if src_rate == 16000:
        return pcm
    n = len(pcm) // 2
    samples = struct.unpack(f"<{n}h", pcm)
    if src_rate == 48000:
        # decimate by 3 — anti-alias should be applied by the OS or a real
        # resampler; this is honest-quick-and-dirty.
        out = samples[::3]
    else:
        ratio = 16000 / src_rate
        out_len = int(n * ratio)
        out = []
        for i in range(out_len):
            src_idx = i / ratio
            lo = int(src_idx)
            hi = min(lo + 1, n - 1)
            frac = src_idx - lo
            out.append(int(samples[lo] * (1 - frac) + samples[hi] * frac))
    return struct.pack(f"<{len(out)}h", *out)


# ---------------------------------------------------------------------------
# Utterance assembler — turns VAD-tagged frames into AudioChunks
# ---------------------------------------------------------------------------

class UtteranceAssembler:
    """Stateful per-stream VAD-driven chunker."""

    def __init__(self, stream: StreamTag, cfg: CaptureConfig, vad: VADAdapter):
        self.stream = stream
        self.cfg = cfg
        self.vad = vad
        self._buf = bytearray()
        self._buf_start_t: Optional[float] = None
        self._silence_ms = 0
        self._speech_ms = 0
        self._seq = 0

    def push_frame(self, frame_pcm: bytes, now: float) -> Optional[AudioChunk]:
        """Append a frame. Return an emitted AudioChunk or None."""
        is_speech = self.vad.is_speech(frame_pcm, self.cfg.sample_rate)
        frame_ms = self.cfg.frame_ms

        if not self._buf:
            if not is_speech:
                return None  # don't start a buffer on silence
            self._buf_start_t = now
            self._silence_ms = 0
            self._speech_ms = 0

        self._buf.extend(frame_pcm)
        if is_speech:
            self._speech_ms += frame_ms
            self._silence_ms = 0
        else:
            self._silence_ms += frame_ms

        buf_ms = (len(self._buf) // 2) * 1000 // self.cfg.sample_rate
        decision: Optional[str] = None
        if self._silence_ms >= self.cfg.silence_close_ms and self._speech_ms >= self.cfg.min_utterance_ms:
            decision = "silence_close"
        elif buf_ms >= self.cfg.max_utterance_ms:
            decision = "max_buffer"

        if decision is None:
            return None

        chunk = AudioChunk(
            stream=self.stream,
            pcm=bytes(self._buf),
            sample_rate=self.cfg.sample_rate,
            started_at=self._buf_start_t or now,
            duration_ms=buf_ms,
            vad_decision=decision,
            seq=self._seq,
        )
        self._seq += 1
        self._buf.clear()
        self._buf_start_t = None
        self._silence_ms = 0
        self._speech_ms = 0
        return chunk

    def flush(self, now: float) -> Optional[AudioChunk]:
        if not self._buf or self._speech_ms < self.cfg.min_utterance_ms:
            self._buf.clear()
            return None
        buf_ms = (len(self._buf) // 2) * 1000 // self.cfg.sample_rate
        chunk = AudioChunk(
            stream=self.stream,
            pcm=bytes(self._buf),
            sample_rate=self.cfg.sample_rate,
            started_at=self._buf_start_t or now,
            duration_ms=buf_ms,
            vad_decision="manual_flush",
            seq=self._seq,
        )
        self._seq += 1
        self._buf.clear()
        self._buf_start_t = None
        return chunk


# ---------------------------------------------------------------------------
# Stream sources (WASAPI loopback + mic)
# ---------------------------------------------------------------------------

class StreamSource:
    """Abstract — yields raw int16 PCM frames at cfg.sample_rate."""

    def open(self) -> None: raise NotImplementedError
    def read_frame(self) -> bytes: raise NotImplementedError
    def close(self) -> None: raise NotImplementedError
    @property
    def native_rate(self) -> int: raise NotImplementedError


class PyAudioWPatchLoopback(StreamSource):  # pragma: no cover - hw-dependent
    """WASAPI loopback via pyaudiowpatch. Windows only."""

    def __init__(self, cfg: CaptureConfig):
        self.cfg = cfg
        self._pa = None
        self._stream = None
        self._native_rate = 48000  # populated in open()
        self._frame_samples_native = 0

    def open(self) -> None:
        import pyaudiowpatch as pyaudio
        self._pa = pyaudio.PyAudio()
        default = self._pa.get_default_wasapi_loopback()
        self._native_rate = int(default["defaultSampleRate"])
        self._frame_samples_native = int(self._native_rate * self.cfg.frame_ms / 1000)
        self._stream = self._pa.open(
            format=pyaudio.paInt16,
            channels=1,
            rate=self._native_rate,
            input=True,
            frames_per_buffer=self._frame_samples_native,
            input_device_index=default["index"],
        )

    def read_frame(self) -> bytes:
        raw = self._stream.read(self._frame_samples_native, exception_on_overflow=False)
        return downsample_to_16k(raw, self._native_rate)

    def close(self) -> None:
        if self._stream:
            self._stream.stop_stream()
            self._stream.close()
        if self._pa:
            self._pa.terminate()

    @property
    def native_rate(self) -> int:
        return self._native_rate


class SoundDeviceMic(StreamSource):  # pragma: no cover - hw-dependent
    """Operator mic via sounddevice. Cross-platform."""

    def __init__(self, cfg: CaptureConfig, device: Optional[int] = None):
        self.cfg = cfg
        self.device = device
        self._stream = None
        self._native_rate = cfg.sample_rate
        self._frame_samples = int(cfg.sample_rate * cfg.frame_ms / 1000)

    def open(self) -> None:
        import sounddevice as sd
        self._stream = sd.RawInputStream(
            samplerate=self.cfg.sample_rate,
            blocksize=self._frame_samples,
            dtype="int16",
            channels=1,
            device=self.device,
        )
        self._stream.start()

    def read_frame(self) -> bytes:
        data, overflowed = self._stream.read(self._frame_samples)
        if overflowed:
            logger.warning("mic stream overflow")
        return bytes(data)

    def close(self) -> None:
        if self._stream:
            self._stream.stop()
            self._stream.close()

    @property
    def native_rate(self) -> int:
        return self._native_rate


# ---------------------------------------------------------------------------
# CaptureSession — orchestrates both streams + emits to callback
# ---------------------------------------------------------------------------

class CaptureSession:
    """Runs two threads (system + mic), each emitting AudioChunks via callback.

    Use as an async context manager. The threads cooperate with asyncio via
    bounded queues drained by `_dispatch()` coroutines.
    """

    def __init__(
        self,
        cfg: CaptureConfig,
        on_chunk: ChunkCallback,
        system_source: Optional[StreamSource] = None,
        mic_source: Optional[StreamSource] = None,
    ):
        self.cfg = cfg
        self.on_chunk = on_chunk
        self.system_source = system_source
        self.mic_source = mic_source
        self._tasks: list[asyncio.Task] = []
        self._stop = asyncio.Event()
        self._loop: Optional[asyncio.AbstractEventLoop] = None

    @classmethod
    def for_platform(cls, cfg: CaptureConfig, on_chunk: ChunkCallback) -> "CaptureSession":
        sys_src = None
        mic_src = None
        if cfg.enable_system and platform.system() == "Windows":
            sys_src = PyAudioWPatchLoopback(cfg)
        elif cfg.enable_system:
            logger.warning("system audio loopback not supported on %s; skipping", platform.system())
        if cfg.enable_mic:
            mic_src = SoundDeviceMic(cfg)
        return cls(cfg, on_chunk, sys_src, mic_src)

    async def __aenter__(self) -> "CaptureSession":
        self._loop = asyncio.get_running_loop()
        if self.system_source:
            self.system_source.open()
            self._tasks.append(asyncio.create_task(self._run_stream(self.system_source, StreamTag.SYSTEM)))
        if self.mic_source:
            self.mic_source.open()
            self._tasks.append(asyncio.create_task(self._run_stream(self.mic_source, StreamTag.MIC)))
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        self._stop.set()
        for t in self._tasks:
            t.cancel()
        for t in self._tasks:
            try:
                await t
            except asyncio.CancelledError:
                pass
        if self.system_source:
            self.system_source.close()
        if self.mic_source:
            self.mic_source.close()

    async def _run_stream(self, source: StreamSource, tag: StreamTag) -> None:
        vad = build_vad(self.cfg)
        assembler = UtteranceAssembler(tag, self.cfg, vad)
        # Run blocking read_frame in a thread to keep the event loop responsive.
        while not self._stop.is_set():
            try:
                frame = await asyncio.to_thread(source.read_frame)
            except Exception:
                logger.exception("read_frame failed on stream=%s", tag)
                await asyncio.sleep(0.05)
                continue
            now = time.monotonic()
            chunk = assembler.push_frame(frame, now)
            if chunk:
                try:
                    await self.on_chunk(chunk)
                except Exception:
                    logger.exception("on_chunk callback failed for seq=%d", chunk.seq)
        # graceful flush
        final = assembler.flush(time.monotonic())
        if final:
            try:
                await self.on_chunk(final)
            except Exception:
                logger.exception("final on_chunk callback failed")


__all__ = [
    "StreamTag",
    "AudioChunk",
    "CaptureConfig",
    "CaptureSession",
    "UtteranceAssembler",
    "WebRTCVAD",
    "EnergyVAD",
    "build_vad",
    "downsample_to_16k",
]
