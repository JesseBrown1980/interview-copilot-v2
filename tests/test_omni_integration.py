"""
test_omni_integration.py — Unit tests for omni_integration.py.

Agent B10-OmniMapping, 2026-05-27 wave.

Run with: pytest tests/test_omni_integration.py -v
"""

import sys
import pathlib
import re

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

import omni_integration as oi


# ---------------------------------------------------------------------------
# Primitive descriptor table
# ---------------------------------------------------------------------------

def test_primitives_table_complete():
    """All 8 primitives present with required species fields."""
    expected = {
        "omnimets", "omnischeduler", "omni_router", "omniquant_engine",
        "omni_file_rotator", "omni_spindles", "omni_flywheels", "omnidispatcher",
    }
    assert set(oi.PRIMITIVES) == expected
    for name, spec in oi.PRIMITIVES.items():
        assert {"hilbert", "band", "behcs256", "mint"} <= set(spec)
        assert 704 <= spec["hilbert"] <= 711, f"{name} hilbert out of v40 atlas"


def test_primitives_hilbert_unique():
    coords = [s["hilbert"] for s in oi.PRIMITIVES.values()]
    assert len(coords) == len(set(coords)), "duplicate hilbert coords"


def test_primitives_mint_classes_valid():
    valid = {"tool", "skill", "pattern", "genius", "ability", "mistake"}
    for spec in oi.PRIMITIVES.values():
        assert spec["mint"] in valid


# ---------------------------------------------------------------------------
# HBPv1 row format
# ---------------------------------------------------------------------------

def test_hbpv1_row_format():
    row = oi.hbpv1_row("test", {"a": 1, "b": "x"})
    # kind|a=1|b=x|sha=<16 hex>
    assert row.startswith("test|")
    assert "|a=1|" in row
    assert "|b=x|" in row
    m = re.search(r"\|sha=([0-9a-f]{16})$", row)
    assert m, f"missing sha suffix: {row}"


def test_hbpv1_row_deterministic():
    row1 = oi.hbpv1_row("k", {"x": 1, "y": 2})
    row2 = oi.hbpv1_row("k", {"y": 2, "x": 1})  # different insert order
    assert row1 == row2, "row must be deterministic regardless of key order"


# ---------------------------------------------------------------------------
# Metric and TickRate enums
# ---------------------------------------------------------------------------

def test_all_metrics_defined():
    expected_metrics = {
        "AUDIO_CHUNK_COUNT_30S",
        "STT_LATENCY_P50_MS", "STT_LATENCY_P99_MS",
        "TRANSLATE_LATENCY_P50_MS", "TRANSLATE_LATENCY_P99_MS",
        "RESPONSE_LATENCY_P50_MS", "RESPONSE_LATENCY_P99_MS",
        "WORDS_TRANSCRIBED_TOTAL", "WORDS_TRANSLATED_TOTAL",
        "SUGGESTIONS_EMITTED_TOTAL",
        "MEETING_DURATION_S",
        "API_COST_ANTHROPIC_USD", "API_COST_TRANSLATE_USD",
    }
    assert {m.name for m in oi.Metric} == expected_metrics


def test_tick_rates():
    assert oi.TickRate.AUDIO_POLL_MS == 100
    assert oi.TickRate.METRICS_FLUSH_MS == 30_000
    assert oi.TickRate.HEARTBEAT_MS == 60_000          # Foundation v3 LAW
    assert oi.TickRate.LIFECYCLE_PROBE_MS == 5_000


# ---------------------------------------------------------------------------
# Quadruple-quant compression
# ---------------------------------------------------------------------------

def test_quad_quant_deterministic():
    a = oi.quad_quant_compress("hello world")
    b = oi.quad_quant_compress("hello world")
    assert a == b


def test_quad_quant_different_inputs():
    a = oi.quad_quant_compress("alpha")
    b = oi.quad_quant_compress("beta")
    assert a.q1_sha != b.q1_sha


def test_quad_quant_components():
    q = oi.quad_quant_compress("some text")
    # Q1 — full sha hex
    assert len(q.q1_sha) == 64
    # Q2 — polar (theta in [0, 2π), r in [0, 1])
    theta, r = q.q2_polar
    assert 0 <= theta < 6.29
    assert 0 <= r <= 1
    # Q3 — single byte 0-255
    assert 0 <= q.q3_turbo <= 255
    # Q4 — non-empty single glyph
    assert isinstance(q.q4_glyph, str) and len(q.q4_glyph) >= 1


def test_quad_quant_as_row():
    q = oi.quad_quant_compress("payload")
    row = q.as_row()
    assert row.startswith("quad_quant|")
    assert "q1=" in row and "q2t=" in row and "q2r=" in row
    assert "q3=" in row and "q4=" in row


# ---------------------------------------------------------------------------
# Conversation buffer (8-turn verbatim window)
# ---------------------------------------------------------------------------

def test_conv_buffer_under_window():
    buf = oi.ConversationBuffer()
    for i in range(5):
        buf.append(f"turn {i}")
    assert len(buf.turns_verbatim) == 5
    assert len(buf.turns_compressed) == 0


def test_conv_buffer_exact_window():
    buf = oi.ConversationBuffer()
    for i in range(8):
        buf.append(f"turn {i}")
    assert len(buf.turns_verbatim) == 8
    assert len(buf.turns_compressed) == 0


def test_conv_buffer_overflow_compresses_oldest():
    buf = oi.ConversationBuffer()
    for i in range(12):
        buf.append(f"turn {i}")
    # window=8 → 12 in, 4 compressed, 8 verbatim
    assert len(buf.turns_verbatim) == 8
    assert len(buf.turns_compressed) == 4
    # Verbatim window holds turns 4..11
    assert buf.turns_verbatim[0] == "turn 4"
    assert buf.turns_verbatim[-1] == "turn 11"


def test_conv_buffer_prompt_renders_both_sections():
    buf = oi.ConversationBuffer()
    for i in range(12):
        buf.append(f"turn {i}")
    out = buf.as_prompt_context()
    assert "# compressed_history" in out
    assert "# recent_verbatim" in out
    assert "turn 11" in out  # most recent


def test_conv_buffer_no_compressed_section_when_empty():
    buf = oi.ConversationBuffer()
    buf.append("only one")
    out = buf.as_prompt_context()
    assert "# compressed_history" not in out
    assert "# recent_verbatim" in out


# ---------------------------------------------------------------------------
# Resume pre-quantization (one-shot reuse)
# ---------------------------------------------------------------------------

def test_prequantize_resume_descriptor():
    d = oi.prequantize_resume("Resume body here.", company="Capital Rx")
    assert d.company == "Capital Rx"
    assert len(d.pack_sha) == 64
    assert len(d.coord_50d) == 50
    for c in d.coord_50d:
        assert -1.0 <= c <= 1.0


def test_prequantize_resume_deterministic():
    a = oi.prequantize_resume("same text", "X")
    b = oi.prequantize_resume("same text", "X")
    assert a == b


def test_prequantize_resume_as_row():
    d = oi.prequantize_resume("body", "Eshyft")
    row = d.as_row()
    assert row.startswith("resume_ctx|")
    assert "company=Eshyft" in row
    assert "dim=50" in row


# ---------------------------------------------------------------------------
# OmniIntegration wrapper end-to-end
# ---------------------------------------------------------------------------

def test_wrapper_emits_metric_row():
    sink = []
    o = oi.OmniIntegration(session_pid="S-TEST-1", emit_sink=sink.append)
    o.emit_metric(oi.Metric.STT_LATENCY_P50_MS, 247.3)
    assert len(sink) == 1
    assert sink[0].startswith("omnimets|")
    assert "metric=copilot.stt.latency_p50_ms" in sink[0]
    assert "value=247.3" in sink[0]


def test_wrapper_meeting_start_emits_lifecycle_and_ticks():
    sink = []
    o = oi.OmniIntegration(session_pid="S-TEST-2", emit_sink=sink.append)
    o.on_meeting_start(company="Capital Rx", pack_text="resume body")
    # Expect: 1 hook row + 1 resume_ctx row + 4 tick rows = 6 rows
    assert len(sink) == 6
    assert any("hook=on_meeting_start" in r for r in sink)
    assert any("resume_ctx|" in r for r in sink)
    assert sum(1 for r in sink if r.startswith("omnischeduler|tick=") or "tick=" in r) >= 4


def test_wrapper_meeting_end_records_duration():
    sink = []
    o = oi.OmniIntegration(session_pid="S-TEST-3", emit_sink=sink.append)
    o.on_meeting_end()
    # First row is omnimets duration metric; then omnischeduler end hook
    kinds = [r.split("|")[0] for r in sink]
    assert "omnimets" in kinds
    assert "omnischeduler" in kinds


def test_wrapper_edge_routing():
    sink = []
    o = oi.OmniIntegration(session_pid="S-TEST-4", emit_sink=sink.append)
    o.route_edge("stt", "translator", "deadbeef" * 8)
    assert sink[0].startswith("omni_router|")
    assert "src=stt" in sink[0]
    assert "dst=translator" in sink[0]


def test_wrapper_quant_turn_appends_to_buffer():
    sink = []
    o = oi.OmniIntegration(session_pid="S-TEST-5", emit_sink=sink.append)
    o.quant_turn("first interviewer line")
    o.quant_turn("operator reply")
    assert len(o.conv_buffer.turns_verbatim) == 2
    # quant rows emitted
    assert all(r.startswith("quad_quant|") for r in sink)


def test_wrapper_build_prompt_context_includes_resume_and_buffer():
    sink = []
    o = oi.OmniIntegration(session_pid="S-TEST-6", emit_sink=sink.append)
    o.on_meeting_start(company="Eshyft", pack_text="resume")
    o.quant_turn("turn A")
    o.quant_turn("turn B")
    ctx = o.build_prompt_context()
    assert "resume_ctx|" in ctx
    assert "turn A" in ctx and "turn B" in ctx


def test_wrapper_flywheel_llm_call():
    sink = []
    o = oi.OmniIntegration(session_pid="S-TEST-7", emit_sink=sink.append)
    o.route_llm_call("claude-sonnet-4.7", "suggestion", 1200, 180, 450)
    row = sink[0]
    assert row.startswith("omni_flywheels|")
    assert "model=claude-sonnet-4.7" in row
    assert "latency_ms=450" in row


def test_wrapper_dispatcher_envelope():
    sink = []
    o = oi.OmniIntegration(session_pid="S-TEST-8", emit_sink=sink.append)
    o.dispatch_envelope("interview-copilot/v2/suggestion", "0" * 64)
    assert sink[0].startswith("omnidispatcher|")
    assert "kind=interview-copilot/v2/suggestion" in sink[0]


def test_wrapper_spindle_and_rotator():
    sink = []
    o = oi.OmniIntegration(session_pid="S-TEST-9", emit_sink=sink.append)
    o.spawn_subprocess("whisper_local", 12345)
    o.rotate_session_log("D:/interview/sessions/x.hbp", reason="size")
    assert any(r.startswith("omni_spindles|") for r in sink)
    assert any(r.startswith("omni_file_rotator|") for r in sink)


# ---------------------------------------------------------------------------
# Event→primitive routing table
# ---------------------------------------------------------------------------

def test_event_to_primitive_mapping_covers_all_kinds():
    samples = [
        ("audio_chunk",   "omnimets"),
        ("stt_latency",   "omnimets"),
        ("meeting_start", "omnischeduler"),
        ("meeting_end",   "omnischeduler"),
        ("edge_hop",      "omni_router"),
        ("turn_compress", "omniquant_engine"),
        ("log_rotate",    "omni_file_rotator"),
        ("spawn",         "omni_spindles"),
        ("llm_call",      "omni_flywheels"),
        ("envelope",      "omnidispatcher"),
    ]
    for kind, expected in samples:
        assert oi.primitive_for_event(kind) == expected, f"miss for {kind}"


def test_event_unknown_returns_none():
    assert oi.primitive_for_event("totally_unknown_kind_xyz") is None


# ---------------------------------------------------------------------------
# Discipline checks
# ---------------------------------------------------------------------------

def test_no_runtime_json_in_emission():
    """Per AGENT.md L20-26: hot-path is HBPv1, no JSON."""
    sink = []
    o = oi.OmniIntegration(session_pid="S-TEST-DISC", emit_sink=sink.append)
    o.emit_metric(oi.Metric.WORDS_TRANSCRIBED_TOTAL, 42)
    o.quant_turn("hello")
    o.route_edge("vad", "stt", "a" * 16)
    for row in sink:
        assert not row.lstrip().startswith("{"), f"JSON leaked: {row}"
        assert "|" in row, f"missing pipe-delim HBPv1 marker: {row}"
        assert "sha=" in row, f"missing sha suffix: {row}"


def test_edges_topology_complete():
    """Edge list covers the canonical hops audio→vad→stt→translator/response→ui."""
    edges = set(oi.EDGES)
    must_have = {
        ("audio_capture", "vad"),
        ("vad", "stt"),
        ("stt", "translator"),
        ("stt", "response_engine"),
        ("translator", "ui"),
        ("response_engine", "ui"),
    }
    assert must_have <= edges
