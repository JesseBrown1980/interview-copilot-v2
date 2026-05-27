"""
Integration test fixtures for interview-copilot v2.

Per B02 architecture: M01..M12 wired with mocked external services
(Anthropic / Google STT / Google Translate / asolaria-fabric MCP),
real internal Python module wiring.

Fixtures here are session-scoped where stateless, function-scoped where they
must reset per-test (envelope collector, etc.).
"""
from __future__ import annotations

import asyncio
import json
import os
import struct
import sys
import tempfile
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

import pytest

# ---------------------------------------------------------------------------
# Path bootstrap: make src/ importable without installing the package.
# ---------------------------------------------------------------------------
PROJECT_ROOT = Path(__file__).resolve().parents[2]
SRC_ROOT = PROJECT_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))


# ---------------------------------------------------------------------------
# 1. Synthetic audio fixture
# ---------------------------------------------------------------------------
@dataclass
class SyntheticAudioClip:
    """A pre-recorded synthetic WAV blob with known transcript + language.

    The audio itself is silent PCM (16-bit, 16kHz, mono). The "transcript"
    is what the mock STT will return when this clip is pushed through.
    """
    wav_path: Path
    expected_transcript: str
    language: str          # 'en' or 'pt'
    speaker: str           # 'interviewer' or 'operator'
    duration_ms: int


def _write_silent_wav(path: Path, duration_ms: int, sample_rate: int = 16000) -> None:
    """Write a valid silent WAV header + zero-PCM body. No external deps."""
    n_samples = int(sample_rate * duration_ms / 1000)
    n_channels = 1
    bits_per_sample = 16
    byte_rate = sample_rate * n_channels * bits_per_sample // 8
    block_align = n_channels * bits_per_sample // 8
    data_size = n_samples * block_align

    with open(path, "wb") as f:
        # RIFF header
        f.write(b"RIFF")
        f.write(struct.pack("<I", 36 + data_size))
        f.write(b"WAVE")
        # fmt chunk
        f.write(b"fmt ")
        f.write(struct.pack("<I", 16))            # subchunk1 size
        f.write(struct.pack("<H", 1))             # PCM format
        f.write(struct.pack("<H", n_channels))
        f.write(struct.pack("<I", sample_rate))
        f.write(struct.pack("<I", byte_rate))
        f.write(struct.pack("<H", block_align))
        f.write(struct.pack("<H", bits_per_sample))
        # data chunk
        f.write(b"data")
        f.write(struct.pack("<I", data_size))
        f.write(b"\x00" * data_size)


@pytest.fixture(scope="session")
def audio_fixtures_dir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """One-time directory of synthetic WAV files used by all integration tests."""
    d = tmp_path_factory.mktemp("audio_fixtures")
    return d


@pytest.fixture(scope="session")
def clip_en_interviewer(audio_fixtures_dir: Path) -> SyntheticAudioClip:
    p = audio_fixtures_dir / "en_interviewer.wav"
    _write_silent_wav(p, duration_ms=2400)
    return SyntheticAudioClip(
        wav_path=p,
        expected_transcript="What is your experience with distributed systems?",
        language="en",
        speaker="interviewer",
        duration_ms=2400,
    )


@pytest.fixture(scope="session")
def clip_pt_interviewer(audio_fixtures_dir: Path) -> SyntheticAudioClip:
    p = audio_fixtures_dir / "pt_interviewer.wav"
    _write_silent_wav(p, duration_ms=2600)
    return SyntheticAudioClip(
        wav_path=p,
        expected_transcript="Fale sobre sua experiencia com sistemas distribuidos.",
        language="pt",
        speaker="interviewer",
        duration_ms=2600,
    )


@pytest.fixture(scope="session")
def clip_en_operator(audio_fixtures_dir: Path) -> SyntheticAudioClip:
    p = audio_fixtures_dir / "en_operator.wav"
    _write_silent_wav(p, duration_ms=1200)
    return SyntheticAudioClip(
        wav_path=p,
        expected_transcript="Thanks, let me think about that.",
        language="en",
        speaker="operator",
        duration_ms=1200,
    )


# ---------------------------------------------------------------------------
# 2. Mock Anthropic SDK
# ---------------------------------------------------------------------------
@dataclass
class MockAnthropicResponse:
    """Mirrors the minimal shape that response_engine consumes."""
    content: List[Dict[str, str]]
    usage: Dict[str, int] = field(default_factory=lambda: {"input_tokens": 100, "output_tokens": 50})
    model: str = "claude-opus-4-7"
    id: str = "msg_mock_01"
    stop_reason: str = "end_turn"


class MockAnthropicClient:
    """Deterministic Anthropic SDK stand-in. Records every call.

    Set `.failure_mode` to:
        None        -> success
        '503'       -> raise APIStatusError(503)
        'timeout'   -> raise APITimeoutError
        'rate'      -> raise RateLimitError(429)
    """

    def __init__(self) -> None:
        self.failure_mode: Optional[str] = None
        self.calls: List[Dict[str, Any]] = []
        # Map (lang, keyword) -> canned reply for determinism.
        self.canned: Dict[str, str] = {
            "en::distributed":
                "I have 5 years working with distributed systems: Kafka pipelines at scale, eventual-consistency tradeoffs, and CRDT-based collaboration tools.",
            "en::default":
                "I would approach that by clarifying the requirements first, then proposing a tradeoff analysis between latency and consistency.",
            "pt::default":
                "Eu abordaria esse problema esclarecendo os requisitos primeiro, depois propondo uma analise de tradeoffs.",
        }
        # exposed messages namespace mirrors anthropic SDK shape
        self.messages = self  # type: ignore[assignment]

    def _pick_reply(self, prompt_text: str) -> str:
        lower = prompt_text.lower()
        if "distributed" in lower:
            return self.canned["en::distributed"]
        if "portugues" in lower or "sistemas distribuidos" in lower:
            return self.canned["pt::default"]
        return self.canned["en::default"]

    def create(self, *, model: str, messages: List[Dict[str, Any]], **kwargs: Any) -> MockAnthropicResponse:
        self.calls.append({"model": model, "messages": messages, "kwargs": kwargs})
        if self.failure_mode == "503":
            raise RuntimeError("MockAPIStatusError: 503 service_unavailable")
        if self.failure_mode == "timeout":
            raise TimeoutError("MockAPITimeoutError")
        if self.failure_mode == "rate":
            raise RuntimeError("MockRateLimitError: 429 too_many_requests")

        user_text = ""
        for m in messages:
            if m.get("role") == "user":
                content = m.get("content", "")
                if isinstance(content, list):
                    for c in content:
                        if isinstance(c, dict) and c.get("type") == "text":
                            user_text += c.get("text", "")
                else:
                    user_text += str(content)
        reply = self._pick_reply(user_text)
        return MockAnthropicResponse(content=[{"type": "text", "text": reply}])

    def stream(self, **kwargs: Any):
        """Return an iterator of fake delta events for streaming consumers."""
        full = self.create(**kwargs)
        text = full.content[0]["text"]
        chunks = [text[i:i + 32] for i in range(0, len(text), 32)]

        class _Stream:
            def __init__(self, parts: List[str]) -> None:
                self._parts = parts

            def __iter__(self):
                for p in self._parts:
                    yield {"type": "content_block_delta", "delta": {"type": "text_delta", "text": p}}
                yield {"type": "message_stop"}

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

        return _Stream(chunks)


@pytest.fixture
def mock_anthropic() -> MockAnthropicClient:
    return MockAnthropicClient()


# ---------------------------------------------------------------------------
# 3. Mock Google STT v2 client
# ---------------------------------------------------------------------------
@dataclass
class MockSTTHypothesis:
    transcript: str
    confidence: float = 0.92
    language: str = "en"


class MockGoogleSTT:
    """Deterministic STT stand-in keyed by clip name embedded in metadata."""

    def __init__(self) -> None:
        self.failure_mode: Optional[str] = None
        self.calls: List[Dict[str, Any]] = []
        # Map wav-path stem -> hypothesis (so tests can register their clips).
        self.registry: Dict[str, MockSTTHypothesis] = {}

    def register(self, clip: SyntheticAudioClip) -> None:
        self.registry[clip.wav_path.stem] = MockSTTHypothesis(
            transcript=clip.expected_transcript,
            language=clip.language,
        )

    def recognize(self, *, audio_path: str, language_hint: Optional[str] = None) -> MockSTTHypothesis:
        self.calls.append({"audio_path": audio_path, "language_hint": language_hint})
        if self.failure_mode == "unavailable":
            raise RuntimeError("MockSTTError: service unavailable")
        stem = Path(audio_path).stem
        hyp = self.registry.get(stem)
        if hyp is None:
            return MockSTTHypothesis(transcript="<unrecognized>", confidence=0.10, language=language_hint or "en")
        return hyp


@pytest.fixture
def mock_stt(
    clip_en_interviewer: SyntheticAudioClip,
    clip_pt_interviewer: SyntheticAudioClip,
    clip_en_operator: SyntheticAudioClip,
) -> MockGoogleSTT:
    stt = MockGoogleSTT()
    stt.register(clip_en_interviewer)
    stt.register(clip_pt_interviewer)
    stt.register(clip_en_operator)
    return stt


# ---------------------------------------------------------------------------
# 4. Mock Google Translate v3
# ---------------------------------------------------------------------------
class MockGoogleTranslate:
    """Deterministic translator. Maps known PT->EN strings exactly."""

    def __init__(self) -> None:
        self.failure_mode: Optional[str] = None
        self.calls: List[Dict[str, Any]] = []
        self.pt_en: Dict[str, str] = {
            "Fale sobre sua experiencia com sistemas distribuidos.":
                "Tell me about your experience with distributed systems.",
            "Eu abordaria esse problema esclarecendo os requisitos primeiro, depois propondo uma analise de tradeoffs.":
                "I would approach that problem by clarifying the requirements first, then proposing a tradeoff analysis.",
        }
        self.en_pt: Dict[str, str] = {v: k for k, v in self.pt_en.items()}

    def translate(self, *, text: str, source_lang: str, target_lang: str) -> str:
        self.calls.append({"text": text, "source": source_lang, "target": target_lang})
        if self.failure_mode == "unavailable":
            raise RuntimeError("MockTranslateError: service unavailable")
        if source_lang == target_lang:
            return text
        if source_lang == "pt" and target_lang == "en":
            return self.pt_en.get(text, f"[EN of: {text}]")
        if source_lang == "en" and target_lang == "pt":
            return self.en_pt.get(text, f"[PT de: {text}]")
        return text


@pytest.fixture
def mock_translate() -> MockGoogleTranslate:
    return MockGoogleTranslate()


# ---------------------------------------------------------------------------
# 5. Mock asolaria-fabric MCP server (envelope collector)
# ---------------------------------------------------------------------------
REQUIRED_ENVELOPE_FIELDS = ("pid", "kind", "ts", "payload", "sha256", "cube_coord")


@dataclass
class CollectedEnvelope:
    pid: str
    kind: str
    ts: float
    payload: Dict[str, Any]
    sha256: str
    cube_coord: List[float]
    raw: Dict[str, Any]


class MockFabricMCP:
    """Collects emitted envelopes, optionally validates schema, optionally fails."""

    def __init__(self) -> None:
        self.failure_mode: Optional[str] = None  # 'unreachable' | 'partial' | None
        self.emitted: List[CollectedEnvelope] = []
        self.buffered_locally: List[Dict[str, Any]] = []
        self.lock = threading.Lock()
        self.reachable: bool = True

    def emit(self, envelope: Dict[str, Any]) -> Dict[str, Any]:
        """Mirror /api/envelope/emit. Validate, store, return ack."""
        if not self.reachable or self.failure_mode == "unreachable":
            raise ConnectionError("MockFabricMCP unreachable")
        if self.failure_mode == "partial" and len(self.emitted) >= 2:
            raise ConnectionError("MockFabricMCP intermittent fail")

        missing = [f for f in REQUIRED_ENVELOPE_FIELDS if f not in envelope]
        if missing:
            raise ValueError(f"envelope missing fields: {missing}")

        with self.lock:
            self.emitted.append(CollectedEnvelope(
                pid=envelope["pid"],
                kind=envelope["kind"],
                ts=envelope["ts"],
                payload=envelope["payload"],
                sha256=envelope["sha256"],
                cube_coord=envelope["cube_coord"],
                raw=envelope,
            ))
        return {"ack": True, "row_hash": envelope["sha256"][:16]}

    def kinds(self) -> List[str]:
        with self.lock:
            return [e.kind for e in self.emitted]

    def pids(self) -> List[str]:
        with self.lock:
            return [e.pid for e in self.emitted]


@pytest.fixture
def mock_fabric() -> MockFabricMCP:
    return MockFabricMCP()


# ---------------------------------------------------------------------------
# 6. Wired pipeline fixture
# ---------------------------------------------------------------------------
@dataclass
class WiredPipeline:
    """Lightweight harness wiring B03..B11 with mocks injected.

    Intentionally NOT importing real modules yet (B03..B11 may not be landed
    when integration tests are first authored). Implements a thin adapter
    layer here that mirrors the interfaces from B02 architecture, so tests
    are written against stable contracts. When real modules land, replace
    `_PipelineImpl` with imports without touching the test files.
    """
    audio_capture: "_AudioCaptureAdapter"
    stt_router: "_STTRouterAdapter"
    translator: "_TranslatorAdapter"
    zoom_adapter: "_PlatformAdapter"
    meet_adapter: "_PlatformAdapter"
    response_engine: "_ResponseEngineAdapter"
    fabric_emitter: "_FabricEmitterAdapter"
    ui_overlay: "_UIOverlayAdapter"
    privacy_gate: "_PrivacyGateAdapter"
    session_pid: str


# ---- Adapter shims (in-file; real modules will replace) ----

class _PrivacyGateAdapter:
    def __init__(self) -> None:
        self.cloud_enabled = True
        self.local_only = False


class _AudioCaptureAdapter:
    def __init__(self) -> None:
        self.queue: deque = deque()
        self.connected: bool = True
        self.disconnect_count: int = 0

    def push_clip(self, clip: SyntheticAudioClip) -> None:
        if not self.connected:
            raise RuntimeError("audio device not connected")
        self.queue.append(clip)

    def simulate_disconnect(self) -> None:
        self.connected = False
        self.disconnect_count += 1

    def reconnect(self) -> None:
        self.connected = True


class _STTRouterAdapter:
    def __init__(self, gate: _PrivacyGateAdapter, mock_stt: MockGoogleSTT) -> None:
        self.gate = gate
        self.mock_stt = mock_stt
        self.last_route: Optional[str] = None

    def transcribe(self, clip: SyntheticAudioClip) -> Dict[str, Any]:
        if self.gate.local_only:
            self.last_route = "local-whisper"
            # local stub: just echo expected_transcript
            return {"text": clip.expected_transcript, "lang": clip.language, "conf": 0.85, "route": "local"}
        self.last_route = "gcp-stt-v2"
        hyp = self.mock_stt.recognize(audio_path=str(clip.wav_path), language_hint=clip.language)
        return {"text": hyp.transcript, "lang": hyp.language, "conf": hyp.confidence, "route": "cloud"}


class _TranslatorAdapter:
    def __init__(self, gate: _PrivacyGateAdapter, mock_translate: MockGoogleTranslate) -> None:
        self.gate = gate
        self.mock_translate = mock_translate
        self.calls = 0

    def translate(self, text: str, source: str, target: str) -> str:
        if source == target:
            return text
        self.calls += 1
        return self.mock_translate.translate(text=text, source_lang=source, target_lang=target)


class _PlatformAdapter:
    """Shared shape for zoom_adapter and meet_adapter — both produce 'session opened' events."""

    def __init__(self, platform: str) -> None:
        self.platform = platform
        self.joined = False
        self.meeting_id: Optional[str] = None

    def join(self, meeting_id: str) -> Dict[str, Any]:
        self.joined = True
        self.meeting_id = meeting_id
        return {"platform": self.platform, "meeting_id": meeting_id, "status": "joined"}

    def leave(self) -> None:
        self.joined = False


class _ResponseEngineAdapter:
    def __init__(self, mock_anthropic: MockAnthropicClient, gate: _PrivacyGateAdapter) -> None:
        self.mock_anthropic = mock_anthropic
        self.gate = gate
        self.fallback_used = False

    def suggest(self, transcript: str, target_lang: str = "en") -> Dict[str, Any]:
        if self.gate.local_only:
            self.fallback_used = True
            return {"text": "[LOCAL-GEMMA] (stub) Acknowledge and clarify scope before answering.", "route": "local"}
        try:
            resp = self.mock_anthropic.create(
                model="claude-opus-4-7",
                messages=[{"role": "user", "content": transcript}],
            )
            return {"text": resp.content[0]["text"], "route": "cloud", "usage": resp.usage}
        except Exception as e:
            self.fallback_used = True
            return {
                "text": "[LOCAL-FALLBACK] (Gemma-4 stub) I would clarify the scope before answering.",
                "route": "local-fallback",
                "error": str(e),
            }


class _FabricEmitterAdapter:
    def __init__(self, mock_fabric: MockFabricMCP, session_pid: str) -> None:
        self.mock_fabric = mock_fabric
        self.session_pid = session_pid
        self.buffer: List[Dict[str, Any]] = []
        self.flushed: int = 0

    def emit(self, kind: str, payload: Dict[str, Any]) -> bool:
        envelope = {
            "pid": self.session_pid,
            "kind": kind,
            "ts": time.time(),
            "payload": payload,
            "sha256": "0" * 64,                   # tests don't validate the digest contents
            "cube_coord": [0.0] * 50,            # 50D placeholder
        }
        try:
            self.mock_fabric.emit(envelope)
            return True
        except Exception:
            self.buffer.append(envelope)
            return False

    def flush_buffer(self) -> int:
        flushed = 0
        still_buffered: List[Dict[str, Any]] = []
        for env in self.buffer:
            try:
                self.mock_fabric.emit(env)
                flushed += 1
            except Exception:
                still_buffered.append(env)
        self.buffer = still_buffered
        self.flushed += flushed
        return flushed


class _UIOverlayAdapter:
    def __init__(self) -> None:
        self.panes: Dict[str, List[str]] = {"interviewer": [], "operator": [], "suggestion": []}
        self.last_render_ts: Optional[float] = None

    def render_transcript(self, speaker: str, text: str) -> None:
        if speaker not in self.panes:
            speaker = "interviewer"
        self.panes[speaker].append(text)
        self.last_render_ts = time.time()

    def render_suggestion(self, text: str) -> None:
        self.panes["suggestion"].append(text)
        self.last_render_ts = time.time()


# ---- Composite fixture: full wired pipeline ----

@pytest.fixture
def pipeline(
    mock_anthropic: MockAnthropicClient,
    mock_stt: MockGoogleSTT,
    mock_translate: MockGoogleTranslate,
    mock_fabric: MockFabricMCP,
) -> WiredPipeline:
    gate = _PrivacyGateAdapter()
    audio = _AudioCaptureAdapter()
    stt = _STTRouterAdapter(gate, mock_stt)
    tr = _TranslatorAdapter(gate, mock_translate)
    zoom = _PlatformAdapter("zoom")
    meet = _PlatformAdapter("meet")
    resp = _ResponseEngineAdapter(mock_anthropic, gate)
    session_pid = "PID-SESSION-INTEG-TEST-0001"
    fabric = _FabricEmitterAdapter(mock_fabric, session_pid)
    ui = _UIOverlayAdapter()
    return WiredPipeline(
        audio_capture=audio,
        stt_router=stt,
        translator=tr,
        zoom_adapter=zoom,
        meet_adapter=meet,
        response_engine=resp,
        fabric_emitter=fabric,
        ui_overlay=ui,
        privacy_gate=gate,
        session_pid=session_pid,
    )


# ---------------------------------------------------------------------------
# 7. Helper: drive a single interviewer-turn through the pipeline
# ---------------------------------------------------------------------------
def drive_turn(
    pipeline: WiredPipeline,
    clip: SyntheticAudioClip,
    operator_target_lang: str = "en",
) -> Dict[str, Any]:
    """End-to-end single-turn driver used by E2E tests."""
    pipeline.audio_capture.push_clip(clip)

    # STT
    stt_out = pipeline.stt_router.transcribe(clip)
    pipeline.ui_overlay.render_transcript(clip.speaker, stt_out["text"])
    pipeline.fabric_emitter.emit("transcript.chunk", {
        "speaker": clip.speaker, "text": stt_out["text"], "lang": stt_out["lang"],
    })

    # Translate (only when language differs from operator target)
    translated_text = stt_out["text"]
    if stt_out["lang"] != operator_target_lang:
        translated_text = pipeline.translator.translate(stt_out["text"], stt_out["lang"], operator_target_lang)
        pipeline.fabric_emitter.emit("translation", {
            "src_lang": stt_out["lang"], "tgt_lang": operator_target_lang,
            "src_text": stt_out["text"], "tgt_text": translated_text,
        })

    # Suggestion (only for interviewer turns)
    suggestion = None
    if clip.speaker == "interviewer":
        suggestion = pipeline.response_engine.suggest(translated_text, target_lang=operator_target_lang)
        pipeline.ui_overlay.render_suggestion(suggestion["text"])
        pipeline.fabric_emitter.emit("suggestion", {
            "in": translated_text, "out": suggestion["text"], "route": suggestion["route"],
        })

    return {"transcript": stt_out, "translation": translated_text, "suggestion": suggestion}
