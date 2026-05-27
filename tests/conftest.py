"""
Shared pytest fixtures for interview-copilot-v2 test suite.

Fixtures cover the 12-module surface defined in agent-B02-architecture.md:
  M01 audio-capture, M02 vad-segmenter, M03 stt-router, M04 translator,
  M05 response-engine, M06 context-pack, M07 ui-overlay, M08 tts-optional,
  M09 fabric-emitter, M10 privacy-gate, M11 session-recorder, M12 supervisor

All external dependencies (Anthropic, Google STT/Translate, asolaria-fabric MCP,
LM Studio, WASAPI loopback, ONNX silero-vad, edge-tts) are mocked so the suite
runs without network, GPU, or hardware.

Authored by B12-UnitTests, 2026-05-27.
"""
from __future__ import annotations

import asyncio
import io
import json
import sys
import struct
import types
import wave
from pathlib import Path
from typing import Any, Iterator
from unittest.mock import AsyncMock, MagicMock

import pytest


# ---------------------------------------------------------------------------
# Path setup — make src/ importable as `from audio_capture import ...` etc.
# ---------------------------------------------------------------------------

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


# ---------------------------------------------------------------------------
# Synthetic audio fixtures (M01 / M02 / M03)
# ---------------------------------------------------------------------------

SAMPLE_RATE = 16000  # canonical 16 kHz mono per B02 spec


@pytest.fixture
def silent_pcm_chunk() -> bytes:
    """20 ms of silence at 16 kHz mono 16-bit = 320 samples = 640 bytes."""
    return b"\x00\x00" * 320


@pytest.fixture
def speech_pcm_chunk() -> bytes:
    """20 ms of synthetic 'speech': low-frequency tone above VAD threshold.

    Returns 640 bytes of int16 PCM at ~440 Hz, amplitude high enough that any
    energy-based or silero VAD will treat it as speech-likely.
    """
    import math

    samples = []
    for i in range(320):
        # 440 Hz sine at ~70% full-scale int16
        v = int(0.7 * 32767 * math.sin(2 * math.pi * 440 * i / SAMPLE_RATE))
        samples.append(v)
    return struct.pack("<" + "h" * 320, *samples)


@pytest.fixture
def speech_pcm_segment(speech_pcm_chunk) -> bytes:
    """300 ms of synthetic speech = 15 chunks of 20 ms each."""
    return speech_pcm_chunk * 15


@pytest.fixture
def silent_pcm_segment(silent_pcm_chunk) -> bytes:
    """300 ms of silence."""
    return silent_pcm_chunk * 15


@pytest.fixture
def wav_bytes_speech(speech_pcm_segment) -> bytes:
    """A complete WAV file (header+PCM) wrapping the speech segment."""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SAMPLE_RATE)
        w.writeframes(speech_pcm_segment)
    return buf.getvalue()


# ---------------------------------------------------------------------------
# Mock Anthropic client (M05)
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_anthropic_client(monkeypatch):
    """Mocks `anthropic.Anthropic` and its messages.create / stream APIs."""
    fake_module = types.ModuleType("anthropic")

    class _FakeMessages:
        def __init__(self):
            self.create = MagicMock(
                return_value=MagicMock(
                    content=[MagicMock(text="Sample suggestion from mock Claude.")],
                    usage=MagicMock(
                        input_tokens=120,
                        output_tokens=30,
                        cache_read_input_tokens=80,
                        cache_creation_input_tokens=40,
                    ),
                    model="claude-opus-4-7",
                    stop_reason="end_turn",
                )
            )

            class _Streamer:
                def __enter__(self_inner):
                    return self_inner

                def __exit__(self_inner, *exc):
                    return False

                @property
                def text_stream(self_inner):
                    return iter(["Sample ", "suggestion ", "from mock Claude."])

                def get_final_message(self_inner):
                    return MagicMock(
                        content=[MagicMock(text="Sample suggestion from mock Claude.")],
                        usage=MagicMock(input_tokens=120, output_tokens=30),
                    )

            self.stream = MagicMock(return_value=_Streamer())

    class FakeAnthropic:
        def __init__(self, *args, **kwargs):
            self.messages = _FakeMessages()
            self._init_args = args
            self._init_kwargs = kwargs

    fake_module.Anthropic = FakeAnthropic
    fake_module.AsyncAnthropic = FakeAnthropic
    fake_module.APIError = type("APIError", (Exception,), {})
    fake_module.APIConnectionError = type("APIConnectionError", (Exception,), {})
    fake_module.RateLimitError = type("RateLimitError", (Exception,), {})

    monkeypatch.setitem(sys.modules, "anthropic", fake_module)
    return fake_module


# ---------------------------------------------------------------------------
# Mock Google Cloud STT v2 + Translate v3 (M03 / M04)
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_gcp_stt(monkeypatch):
    """Mocks google.cloud.speech_v2.SpeechClient streaming + batch."""
    pkg = types.ModuleType("google")
    cloud = types.ModuleType("google.cloud")
    speech_v2 = types.ModuleType("google.cloud.speech_v2")

    class _FakeResult:
        def __init__(self, transcript: str):
            self.alternatives = [MagicMock(transcript=transcript, confidence=0.97)]
            self.is_final = True
            self.language_code = "en-US"

    class FakeSpeechClient:
        def __init__(self, *a, **k):
            pass

        def recognize(self, request=None, **kwargs):
            return MagicMock(results=[_FakeResult("hello world")])

        def streaming_recognize(self, requests=None, **kwargs):
            return iter(
                [
                    MagicMock(results=[_FakeResult("hello")]),
                    MagicMock(results=[_FakeResult("hello world")]),
                ]
            )

    speech_v2.SpeechClient = FakeSpeechClient
    speech_v2.types = types.SimpleNamespace(
        RecognitionConfig=MagicMock,
        StreamingRecognitionConfig=MagicMock,
        StreamingRecognizeRequest=MagicMock,
        RecognizeRequest=MagicMock,
    )

    monkeypatch.setitem(sys.modules, "google", pkg)
    monkeypatch.setitem(sys.modules, "google.cloud", cloud)
    monkeypatch.setitem(sys.modules, "google.cloud.speech_v2", speech_v2)
    return speech_v2


@pytest.fixture
def mock_gcp_translate(monkeypatch):
    """Mocks google.cloud.translate_v3.TranslationServiceClient."""
    pkg = sys.modules.setdefault("google", types.ModuleType("google"))
    cloud = sys.modules.setdefault("google.cloud", types.ModuleType("google.cloud"))
    translate_v3 = types.ModuleType("google.cloud.translate_v3")

    class FakeTranslateClient:
        def __init__(self, *a, **k):
            pass

        def translate_text(self, request=None, **kwargs):
            src = (request or {}).get("contents", ["hello"])[0] if isinstance(
                request, dict
            ) else "hello"
            target = (
                (request or {}).get("target_language_code", "pt")
                if isinstance(request, dict)
                else "pt"
            )
            # primitive faux translation
            faux = {
                ("hello world", "pt"): "olá mundo",
                ("olá mundo", "en"): "hello world",
            }.get((src, target), f"[{target}]{src}")
            return MagicMock(
                translations=[
                    MagicMock(translated_text=faux, detected_language_code="en")
                ]
            )

        def detect_language(self, request=None, **kwargs):
            return MagicMock(languages=[MagicMock(language_code="en", confidence=0.99)])

    translate_v3.TranslationServiceClient = FakeTranslateClient
    monkeypatch.setitem(sys.modules, "google.cloud.translate_v3", translate_v3)
    return translate_v3


# ---------------------------------------------------------------------------
# Mock asolaria-fabric MCP HTTP client (M09 / M12 heartbeat)
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_fabric_http(monkeypatch):
    """Mocks httpx.AsyncClient + httpx.Client used by M09 fabric-emitter."""
    fake_httpx = types.ModuleType("httpx")

    posted_envelopes: list[dict[str, Any]] = []

    class _Resp:
        def __init__(self, status_code=200, json_data=None):
            self.status_code = status_code
            self._json = json_data or {"ok": True, "row_hash": "deadbeef" * 8}

        def json(self):
            return self._json

        def raise_for_status(self):
            if self.status_code >= 400:
                raise fake_httpx.HTTPStatusError(
                    f"{self.status_code}", request=None, response=self
                )

    class FakeAsyncClient:
        def __init__(self, *a, **k):
            self.posted = posted_envelopes

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def post(self, url, json=None, **kwargs):
            posted_envelopes.append({"url": url, "json": json})
            return _Resp(200)

        async def get(self, url, **kwargs):
            return _Resp(200, {"status": "ok"})

        async def aclose(self):
            return None

    class FakeClient:
        def __init__(self, *a, **k):
            self.posted = posted_envelopes

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def post(self, url, json=None, **kwargs):
            posted_envelopes.append({"url": url, "json": json})
            return _Resp(200)

        def get(self, url, **kwargs):
            return _Resp(200, {"status": "ok"})

    fake_httpx.AsyncClient = FakeAsyncClient
    fake_httpx.Client = FakeClient
    fake_httpx.HTTPStatusError = type("HTTPStatusError", (Exception,), {})
    fake_httpx.ConnectError = type("ConnectError", (Exception,), {})
    fake_httpx.TimeoutException = type("TimeoutException", (Exception,), {})
    fake_httpx.Response = _Resp

    monkeypatch.setitem(sys.modules, "httpx", fake_httpx)
    return {"httpx": fake_httpx, "posted": posted_envelopes}


# ---------------------------------------------------------------------------
# Mock LM Studio (Gemma-4 local) — privacy-mode fallback for M04 / M05
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_lmstudio(mock_fabric_http):
    """Reuses the mock httpx to also stand in for LM Studio :1234 calls."""
    # When the SUT POSTs to 127.0.0.1:1234/v1/chat/completions, the same
    # FakeAsyncClient/FakeClient returns 200; tests can inspect posted_envelopes
    # and filter by URL.
    return mock_fabric_http


# ---------------------------------------------------------------------------
# Mock silero-vad (M02) / onnxruntime
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_onnxruntime(monkeypatch):
    fake = types.ModuleType("onnxruntime")

    class FakeSession:
        def __init__(self, *a, **k):
            pass

        def run(self, output_names, input_feed):
            # silero returns [speech_prob (float32)] per call
            import numpy as np  # numpy will be available even on minimal install

            return [np.array([[0.92]], dtype="float32")]

        def get_inputs(self):
            return [MagicMock(name="input", shape=[1, 512])]

    fake.InferenceSession = FakeSession
    fake.get_available_providers = lambda: ["DmlExecutionProvider", "CPUExecutionProvider"]
    fake.SessionOptions = MagicMock
    monkeypatch.setitem(sys.modules, "onnxruntime", fake)
    monkeypatch.setitem(sys.modules, "onnxruntime.capi", fake)
    return fake


# ---------------------------------------------------------------------------
# Mock WASAPI loopback / soundcard / pyaudio (M01)
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_soundcard(monkeypatch, speech_pcm_chunk):
    fake_sc = types.ModuleType("soundcard")

    class FakeMic:
        name = "Mock Loopback"

        def recorder(self, samplerate, channels=1, blocksize=320):
            import numpy as np

            class _Rec:
                def __enter__(self_inner):
                    return self_inner

                def __exit__(self_inner, *exc):
                    return False

                def record(self_inner, numframes):
                    return np.zeros((numframes, 1), dtype="int16")

            return _Rec()

    fake_sc.all_microphones = lambda include_loopback=True: [FakeMic()]
    fake_sc.default_microphone = lambda: FakeMic()
    fake_sc.default_speaker = lambda: MagicMock()
    monkeypatch.setitem(sys.modules, "soundcard", fake_sc)

    # Also stub pyaudiowpatch for the WASAPI-specific path
    fake_pa = types.ModuleType("pyaudiowpatch")

    class FakePa:
        paInt16 = 8

        def __init__(self):
            pass

        def get_default_wasapi_loopback(self):
            return {"index": 0, "name": "Mock Loopback", "defaultSampleRate": 16000}

        def open(self, **kwargs):
            class _Stream:
                def read(self_inner, n, exception_on_overflow=False):
                    return speech_pcm_chunk

                def close(self_inner):
                    return None

                def stop_stream(self_inner):
                    return None

                def is_active(self_inner):
                    return True

            return _Stream()

        def terminate(self):
            return None

    fake_pa.PyAudio = FakePa
    fake_pa.paInt16 = 8
    monkeypatch.setitem(sys.modules, "pyaudiowpatch", fake_pa)
    monkeypatch.setitem(sys.modules, "pyaudio", fake_pa)
    return fake_sc


# ---------------------------------------------------------------------------
# Mock python-docx / pypdf for M06 context-pack
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_docx(monkeypatch):
    fake = types.ModuleType("docx")

    class FakeParagraph:
        def __init__(self, text):
            self.text = text

    class FakeDocument:
        def __init__(self, path=None):
            self.path = path
            self.paragraphs = [
                FakeParagraph("Jesse Daniel Brown"),
                FakeParagraph("Senior Engineer"),
                FakeParagraph("Experience: federation architecture, MCP, BEHCS."),
                FakeParagraph(""),
                FakeParagraph("Skills: Python, Rust, TypeScript."),
            ]

    fake.Document = FakeDocument
    monkeypatch.setitem(sys.modules, "docx", fake)
    return fake


@pytest.fixture
def mock_pypdf(monkeypatch):
    fake = types.ModuleType("pypdf")

    class FakePage:
        def extract_text(self):
            return "Sample PDF page text for company pack."

    class FakeReader:
        def __init__(self, source):
            self.pages = [FakePage(), FakePage()]
            self.metadata = {"/Title": "Sample Company"}

    fake.PdfReader = FakeReader
    monkeypatch.setitem(sys.modules, "pypdf", fake)
    return fake


# ---------------------------------------------------------------------------
# Mock cryptography for M11 session-recorder AES-GCM
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_cryptography(monkeypatch):
    fake = types.ModuleType("cryptography")
    hazmat = types.ModuleType("cryptography.hazmat")
    primitives = types.ModuleType("cryptography.hazmat.primitives")
    ciphers = types.ModuleType("cryptography.hazmat.primitives.ciphers")
    aead = types.ModuleType("cryptography.hazmat.primitives.ciphers.aead")
    kdf = types.ModuleType("cryptography.hazmat.primitives.kdf")
    argon2 = types.ModuleType("cryptography.hazmat.primitives.kdf.argon2")

    class FakeAESGCM:
        def __init__(self, key):
            self.key = key

        @staticmethod
        def generate_key(bit_length):
            return b"\x01" * (bit_length // 8)

        def encrypt(self, nonce, data, associated_data=None):
            return b"ENC[" + data + b"]"

        def decrypt(self, nonce, ciphertext, associated_data=None):
            if ciphertext.startswith(b"ENC[") and ciphertext.endswith(b"]"):
                return ciphertext[4:-1]
            raise ValueError("invalid ciphertext")

    aead.AESGCM = FakeAESGCM

    class FakeArgon2id:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

        def derive(self, passphrase: bytes) -> bytes:
            return (b"K" + passphrase)[:32].ljust(32, b"\x00")

    argon2.Argon2id = FakeArgon2id

    monkeypatch.setitem(sys.modules, "cryptography", fake)
    monkeypatch.setitem(sys.modules, "cryptography.hazmat", hazmat)
    monkeypatch.setitem(sys.modules, "cryptography.hazmat.primitives", primitives)
    monkeypatch.setitem(sys.modules, "cryptography.hazmat.primitives.ciphers", ciphers)
    monkeypatch.setitem(sys.modules, "cryptography.hazmat.primitives.ciphers.aead", aead)
    monkeypatch.setitem(sys.modules, "cryptography.hazmat.primitives.kdf", kdf)
    monkeypatch.setitem(sys.modules, "cryptography.hazmat.primitives.kdf.argon2", argon2)
    return {"AESGCM": FakeAESGCM, "Argon2id": FakeArgon2id}


# ---------------------------------------------------------------------------
# Mock edge-tts (M08)
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_edge_tts(monkeypatch):
    fake = types.ModuleType("edge_tts")

    class FakeCommunicate:
        def __init__(self, text, voice="en-US-AriaNeural", **kwargs):
            self.text = text
            self.voice = voice

        async def stream(self):
            yield {"type": "audio", "data": b"\x00\x01" * 256}

        async def save(self, path):
            Path(path).write_bytes(b"FAKEMP3" + self.text.encode())

    fake.Communicate = FakeCommunicate
    fake.list_voices = AsyncMock(return_value=[{"Name": "en-US-AriaNeural"}])
    monkeypatch.setitem(sys.modules, "edge_tts", fake)
    return fake


# ---------------------------------------------------------------------------
# Mock blake3 for HBPv1 packer (M09)
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_blake3(monkeypatch):
    import hashlib

    fake = types.ModuleType("blake3")

    class FakeBlake3:
        def __init__(self, data=b""):
            self._h = hashlib.blake2b(data, digest_size=32)

        def update(self, data):
            self._h.update(data)
            return self

        def digest(self):
            return self._h.digest()

        def hexdigest(self):
            return self._h.hexdigest()

    fake.blake3 = FakeBlake3
    monkeypatch.setitem(sys.modules, "blake3", fake)
    return fake


# ---------------------------------------------------------------------------
# Sample resume + company pack content
# ---------------------------------------------------------------------------


@pytest.fixture
def sample_resume_text() -> str:
    return (
        "Jesse Daniel Brown\n"
        "Senior Software Engineer — federation architecture, MCP, "
        "Brown-Hilbert/BEHCS, Python/Rust/TypeScript.\n"
        "10+ years building distributed runtimes.\n"
    )


@pytest.fixture
def sample_company_pack() -> dict:
    return {
        "name": "Capital Rx",
        "industry": "PBM / Healthcare",
        "summary": "Pharmacy Benefits Manager, building Single Ledger backend.",
        "stack_keywords": ["Python", "PostgreSQL", "AWS", "PBM"],
    }


# ---------------------------------------------------------------------------
# Sample HBPv1 envelope
# ---------------------------------------------------------------------------


@pytest.fixture
def sample_envelope() -> dict:
    return {
        "kind": "interview-copilot/v2/transcript",
        "session_pid": "session-test-0001",
        "company": "Capital Rx",
        "speaker": "interviewer",
        "t_utc": "2026-05-27T13:42:00Z",
        "payload_sha256": "a" * 64,
        "payload_preview": "Tell me about your experience with PBM systems.",
        "lang": "en",
        "privacy_mode": "cloud",
    }


# ---------------------------------------------------------------------------
# Event loop fixture for pytest-asyncio (M12 supervisor / async paths)
# ---------------------------------------------------------------------------


@pytest.fixture
def event_loop():
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()


# ---------------------------------------------------------------------------
# tmp session dir
# ---------------------------------------------------------------------------


@pytest.fixture
def tmp_session_dir(tmp_path) -> Path:
    p = tmp_path / "sessions"
    p.mkdir()
    return p


# ---------------------------------------------------------------------------
# Privacy gate fixture — both modes
# ---------------------------------------------------------------------------


@pytest.fixture(params=["cloud", "local"])
def privacy_mode(request) -> str:
    return request.param
