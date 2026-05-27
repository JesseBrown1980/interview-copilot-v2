"""
omni_integration.py — Interview Copilot v2 ↔ 8 omni-primitive wrapper.

Agent: B10-OmniMapping (2026-05-27 18-agent wave)
Mission: Wire copilot events to omni-primitives (mets/scheduler/router/quant/
         file-rotator/spindles/flywheels/dispatcher) per v40+v41 atlas.

Discipline:
- HBPv1 hot-path emission ONLY (no runtime JSON per AGENT.md L20-26).
- DUAL_EMIT to (destination, hookwall+GNN observation) per v38 L13.
- 60s internal heartbeat per Foundation v3 LAW.
- Read/Write only — NO actual emission to running omni-primitives in this
  wrapper. Emission is descriptor-only; live wiring is deferred to B14
  supervisor at integration time.

Primitive map (v40 cosign seq=246):
  OMNIMETS         H704 helm     BEHCS-256=67   metrics-collector
  OMNISCHEDULER    H705 helm     BEHCS-256=66   timer/task ledger
  OMNI_ROUTER      H706 helm     BEHCS-256=175  edge router
  OMNIQUANT_ENGINE H707 vector   BEHCS-256=7    quadruple-quant runtime
  OMNI_FILE_ROTATR H708 helm     BEHCS-256=131  hot-file rotator
  OMNI_SPINDLES    H709 forge    BEHCS-256=14   process spawner
  OMNI_FLYWHEELS   H710 vector   BEHCS-256=169  LLM router
  OMNIDISPATCHER   H711 helm     BEHCS-256=8    single-parent dispatcher
"""

from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, List, Optional, Tuple

# ---------------------------------------------------------------------------
# 1. Primitive descriptor table (v40 atlas, frozen)
# ---------------------------------------------------------------------------

PRIMITIVES: Dict[str, Dict[str, Any]] = {
    "omnimets":         {"hilbert": 704, "band": "helm",   "behcs256": 67,  "mint": "tool"},
    "omnischeduler":    {"hilbert": 705, "band": "helm",   "behcs256": 66,  "mint": "skill"},
    "omni_router":      {"hilbert": 706, "band": "helm",   "behcs256": 175, "mint": "pattern"},
    "omniquant_engine": {"hilbert": 707, "band": "vector", "behcs256": 7,   "mint": "genius"},
    "omni_file_rotator":{"hilbert": 708, "band": "helm",   "behcs256": 131, "mint": "pattern"},
    "omni_spindles":    {"hilbert": 709, "band": "forge",  "behcs256": 14,  "mint": "ability"},
    "omni_flywheels":   {"hilbert": 710, "band": "vector", "behcs256": 169, "mint": "skill"},
    "omnidispatcher":   {"hilbert": 711, "band": "helm",   "behcs256": 8,   "mint": "tool"},
}


# ---------------------------------------------------------------------------
# 2. Metric definitions (omnimets contract)
# ---------------------------------------------------------------------------

class Metric(str, Enum):
    AUDIO_CHUNK_COUNT_30S       = "copilot.audio.chunk_count_30s"
    STT_LATENCY_P50_MS          = "copilot.stt.latency_p50_ms"
    STT_LATENCY_P99_MS          = "copilot.stt.latency_p99_ms"
    TRANSLATE_LATENCY_P50_MS    = "copilot.translate.latency_p50_ms"
    TRANSLATE_LATENCY_P99_MS    = "copilot.translate.latency_p99_ms"
    RESPONSE_LATENCY_P50_MS     = "copilot.response.latency_p50_ms"
    RESPONSE_LATENCY_P99_MS     = "copilot.response.latency_p99_ms"
    WORDS_TRANSCRIBED_TOTAL     = "copilot.words.transcribed_total"
    WORDS_TRANSLATED_TOTAL      = "copilot.words.translated_total"
    SUGGESTIONS_EMITTED_TOTAL   = "copilot.suggestions.emitted_total"
    MEETING_DURATION_S          = "copilot.meeting.duration_s"
    API_COST_ANTHROPIC_USD      = "copilot.cost.anthropic_usd"
    API_COST_TRANSLATE_USD      = "copilot.cost.translate_usd"


# ---------------------------------------------------------------------------
# 3. Tick rates (omnischeduler contract)
# ---------------------------------------------------------------------------

class TickRate(int, Enum):
    AUDIO_POLL_MS      = 100      # ring-buffer drain cadence
    METRICS_FLUSH_MS   = 30_000   # omnimets push window
    HEARTBEAT_MS       = 60_000   # Foundation v3 LAW
    LIFECYCLE_PROBE_MS = 5_000    # micro-agent THINK/PLAN/MEMORY/etc supervision


# ---------------------------------------------------------------------------
# 4. Edge-routing topology (omni_router contract)
# ---------------------------------------------------------------------------

EDGES: List[Tuple[str, str]] = [
    ("audio_capture", "vad"),
    ("vad",           "stt"),
    ("stt",           "translator"),
    ("stt",           "response_engine"),
    ("translator",    "ui"),
    ("response_engine","ui"),
    ("ui",            "session_recorder"),
]


# ---------------------------------------------------------------------------
# 5. HBPv1 envelope helpers (hot-path, no runtime JSON)
# ---------------------------------------------------------------------------

def _sha256_hex(payload: str) -> str:
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def hbpv1_row(kind: str, fields: Dict[str, Any]) -> str:
    """Build a pipe-delimited HBPv1 row, sha-suffixed.

    Format: kind|k1=v1|k2=v2|...|sha=<hex16>
    Truncates sha to 16 chars per HyperBEHCS hex convention.
    """
    parts = [kind]
    for k in sorted(fields):
        v = fields[k]
        parts.append(f"{k}={v}")
    body = "|".join(parts)
    return f"{body}|sha={_sha256_hex(body)[:16]}"


# ---------------------------------------------------------------------------
# 6. Quadruple-quant context compression (omniquant contract)
# ---------------------------------------------------------------------------

@dataclass
class QuadQuant:
    """Q1+Q2+Q3+Q4 fingerprint of a compressed conversation turn.

    Q1: sha256 fingerprint (deduplication + integrity)
    Q2: polar coordinates (theta, r) — angle + magnitude on unit circle
    Q3: Turbo float8 (8-bit normalized magnitude)
    Q4: zeta-tier glyph (5-glyph block, BEHCS-256 slot)
    """
    q1_sha:   str
    q2_polar: Tuple[float, float]
    q3_turbo: int
    q4_glyph: str

    def as_row(self) -> str:
        return hbpv1_row("quad_quant", {
            "q1":  self.q1_sha[:16],
            "q2t": round(self.q2_polar[0], 4),
            "q2r": round(self.q2_polar[1], 4),
            "q3":  self.q3_turbo,
            "q4":  self.q4_glyph,
        })


def quad_quant_compress(text: str) -> QuadQuant:
    """Deterministic 4-quant fingerprint. No external embedder dep.

    For real similarity-search use B08 context_pack HyperBEHCS quant; this
    wrapper is for telemetry/observability fingerprints only.
    """
    h = hashlib.sha256(text.encode("utf-8")).digest()
    # Q1 — full sha hex
    q1 = h.hex()
    # Q2 — theta from first 2 bytes, r from next 2 bytes, normalised
    theta = (int.from_bytes(h[0:2], "big") / 0xFFFF) * 6.28318  # 2π
    r     = int.from_bytes(h[2:4], "big") / 0xFFFF
    # Q3 — turbo-float8 from byte 4 (single byte 0-255)
    q3 = h[4]
    # Q4 — zeta-tier glyph from byte 5 mod 256 → BEHCS-256 slot label
    glyphs = "·∘○●◐◑◒◓◔◕◖◗◘◙◚◛"  # 16 sample glyphs; real set is 256
    q4 = glyphs[h[5] % len(glyphs)]
    return QuadQuant(q1_sha=q1, q2_polar=(theta, r), q3_turbo=q3, q4_glyph=q4)


@dataclass
class ConversationBuffer:
    """Last 8 turns verbatim; older turns compressed to QuadQuant rows.

    Keeps Claude API call prompt under cache-friendly size while preserving
    sha-chain observability for every dropped turn.
    """
    verbatim_window: int = 8
    turns_verbatim: List[str] = field(default_factory=list)
    turns_compressed: List[QuadQuant] = field(default_factory=list)

    def append(self, text: str) -> None:
        self.turns_verbatim.append(text)
        if len(self.turns_verbatim) > self.verbatim_window:
            evicted = self.turns_verbatim.pop(0)
            self.turns_compressed.append(quad_quant_compress(evicted))

    def as_prompt_context(self) -> str:
        """Render compressed prefix + verbatim suffix as prompt context."""
        compressed = "\n".join(q.as_row() for q in self.turns_compressed)
        verbatim   = "\n".join(self.turns_verbatim)
        if compressed:
            return f"# compressed_history\n{compressed}\n# recent_verbatim\n{verbatim}"
        return f"# recent_verbatim\n{verbatim}"


# ---------------------------------------------------------------------------
# 7. Pre-quantized resume context (one-shot, reused)
# ---------------------------------------------------------------------------

@dataclass
class ResumeContextDescriptor:
    """50D embedding placeholder + sha + atlas coord for the resume pack.

    Computed once at session start; reused for every Claude API call so the
    prompt cache hit-rate stays high. Real 50D embedding comes from B08
    context_pack HyperBEHCS quant; this descriptor only stamps the identity.
    """
    pack_sha:   str
    coord_50d:  Tuple[float, ...]
    company:    str

    def as_row(self) -> str:
        return hbpv1_row("resume_ctx", {
            "company": self.company,
            "sha":     self.pack_sha[:16],
            "dim":     len(self.coord_50d),
            "norm":    round(sum(c*c for c in self.coord_50d) ** 0.5, 4),
        })


def prequantize_resume(pack_text: str, company: str) -> ResumeContextDescriptor:
    """Stamp a descriptor for a resume+company pack. Deterministic from text.

    Uses sha256 for pack_sha identity; uses shake_256 to draw 50 bytes for
    the 50D coord placeholder. Real embedding handed off to B08
    sentence-transformer feed at integration time.
    """
    pack_sha = hashlib.sha256(pack_text.encode("utf-8")).hexdigest()
    raw50 = hashlib.shake_256(pack_text.encode("utf-8")).digest(50)
    coord = tuple(((b / 127.5) - 1.0) for b in raw50)
    return ResumeContextDescriptor(
        pack_sha=pack_sha,
        coord_50d=coord,
        company=company,
    )


# ---------------------------------------------------------------------------
# 8. Wrapper class — wires copilot events to all 8 primitives
# ---------------------------------------------------------------------------

@dataclass
class OmniIntegration:
    """Single wire-point for copilot → omni-primitive plumbing.

    Emits descriptor-only HBPv1 rows. The real network emission to running
    daemons is performed by B14 supervisor at integration time (this class
    only PRODUCES rows; it does NOT send them over the wire).
    """
    session_pid: str
    emit_sink: Callable[[str], None] = lambda row: None  # injectable
    conv_buffer: ConversationBuffer = field(default_factory=ConversationBuffer)
    resume_ctx: Optional[ResumeContextDescriptor] = None
    _t_session_start: float = field(default_factory=time.time)

    # ---- omnimets ---------------------------------------------------------
    def emit_metric(self, metric: Metric, value: float, **tags: Any) -> str:
        row = hbpv1_row("omnimets", {
            "session": self.session_pid,
            "metric":  metric.value,
            "value":   value,
            **{f"t_{k}": v for k, v in tags.items()},
        })
        self.emit_sink(row)
        return row

    # ---- omnischeduler ----------------------------------------------------
    def schedule_tick(self, name: str, period_ms: int) -> str:
        row = hbpv1_row("omnischeduler", {
            "session":   self.session_pid,
            "tick":      name,
            "period_ms": period_ms,
        })
        self.emit_sink(row)
        return row

    def on_meeting_start(self, company: str, pack_text: str) -> str:
        self.resume_ctx = prequantize_resume(pack_text, company)
        row = hbpv1_row("omnischeduler", {
            "session":  self.session_pid,
            "hook":     "on_meeting_start",
            "company":  company,
            "pack_sha": self.resume_ctx.pack_sha[:16],
        })
        self.emit_sink(row)
        self.emit_sink(self.resume_ctx.as_row())
        # Register all four standing tick rates
        for tr in (TickRate.AUDIO_POLL_MS, TickRate.METRICS_FLUSH_MS,
                   TickRate.HEARTBEAT_MS, TickRate.LIFECYCLE_PROBE_MS):
            self.schedule_tick(tr.name.lower(), int(tr.value))
        return row

    def on_meeting_end(self) -> str:
        dur = time.time() - self._t_session_start
        self.emit_metric(Metric.MEETING_DURATION_S, dur)
        row = hbpv1_row("omnischeduler", {
            "session":     self.session_pid,
            "hook":        "on_meeting_end",
            "duration_s":  round(dur, 2),
            "compressed":  len(self.conv_buffer.turns_compressed),
            "verbatim":    len(self.conv_buffer.turns_verbatim),
        })
        self.emit_sink(row)
        return row

    # ---- omni_router ------------------------------------------------------
    def route_edge(self, src: str, dst: str, payload_sha: str) -> str:
        row = hbpv1_row("omni_router", {
            "session": self.session_pid,
            "src":     src,
            "dst":     dst,
            "sha":     payload_sha[:16],
        })
        self.emit_sink(row)
        return row

    # ---- omniquant_engine -------------------------------------------------
    def quant_turn(self, text: str) -> str:
        self.conv_buffer.append(text)
        # Emit a per-turn quant fingerprint regardless of compress/verbatim
        qq = quad_quant_compress(text)
        row = qq.as_row()
        self.emit_sink(row)
        return row

    def build_prompt_context(self) -> str:
        """Returns compressed+verbatim context ready to drop into Claude prompt."""
        parts = []
        if self.resume_ctx:
            parts.append(self.resume_ctx.as_row())
        parts.append(self.conv_buffer.as_prompt_context())
        return "\n".join(parts)

    # ---- omni_file_rotator -----------------------------------------------
    def rotate_session_log(self, log_path: str, reason: str = "size") -> str:
        row = hbpv1_row("omni_file_rotator", {
            "session": self.session_pid,
            "path":    log_path,
            "reason":  reason,
            "t_utc":   int(time.time()),
        })
        self.emit_sink(row)
        return row

    # ---- omni_spindles ----------------------------------------------------
    def spawn_subprocess(self, name: str, pid: int) -> str:
        row = hbpv1_row("omni_spindles", {
            "session":   self.session_pid,
            "proc":      name,
            "os_pid":    pid,
            "t_utc":     int(time.time()),
        })
        self.emit_sink(row)
        return row

    # ---- omni_flywheels ---------------------------------------------------
    def route_llm_call(self, model: str, role: str, tokens_in: int,
                        tokens_out: int, latency_ms: int) -> str:
        row = hbpv1_row("omni_flywheels", {
            "session":    self.session_pid,
            "model":      model,
            "role":       role,
            "tokens_in":  tokens_in,
            "tokens_out": tokens_out,
            "latency_ms": latency_ms,
        })
        self.emit_sink(row)
        return row

    # ---- omnidispatcher ---------------------------------------------------
    def dispatch_envelope(self, kind: str, body_sha: str) -> str:
        row = hbpv1_row("omnidispatcher", {
            "session":  self.session_pid,
            "kind":     kind,
            "body_sha": body_sha[:16],
            "t_utc":    int(time.time()),
        })
        self.emit_sink(row)
        return row


# ---------------------------------------------------------------------------
# 9. Diagnostic helpers
# ---------------------------------------------------------------------------

def primitive_for_event(event_kind: str) -> Optional[str]:
    """Map a copilot event kind to its canonical primitive home."""
    return {
        "audio_chunk":    "omnimets",
        "stt_latency":    "omnimets",
        "translate_done": "omnimets",
        "suggestion":     "omnimets",
        "meeting_start":  "omnischeduler",
        "meeting_end":    "omnischeduler",
        "tick":           "omnischeduler",
        "edge_hop":       "omni_router",
        "turn_compress":  "omniquant_engine",
        "log_rotate":     "omni_file_rotator",
        "spawn":          "omni_spindles",
        "llm_call":       "omni_flywheels",
        "envelope":       "omnidispatcher",
    }.get(event_kind)


__all__ = [
    "PRIMITIVES", "Metric", "TickRate", "EDGES",
    "QuadQuant", "quad_quant_compress",
    "ConversationBuffer",
    "ResumeContextDescriptor", "prequantize_resume",
    "OmniIntegration",
    "primitive_for_event",
    "hbpv1_row",
]
