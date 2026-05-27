# Interview Copilot v2 — Omni-Primitive Map

**Agent:** B10-OmniMapping
**Wave:** 18-agent interview-copilot-update 2026-05-27
**Atlas reference:** v40+v41 omni-primitive comprehensive atlas (cosign seq=246+248)

This document is the visual + textual map of where the interview copilot
lives in each of the 8 omni-primitives, what events flow where, and how
the quadruple-quant compression keeps Claude prompts cache-friendly.

---

## 1. Primitive grid (v40 atlas, frozen)

| Primitive          | H    | Band   | BEHCS-256 | Mint    | Copilot's footprint                                                         |
|--------------------|------|--------|-----------|---------|-----------------------------------------------------------------------------|
| OMNIMETS           | 704  | helm   | 67        | tool    | 13 metrics: audio_chunk, latencies, words, suggestions, cost                |
| OMNISCHEDULER      | 705  | helm   | 66        | skill   | 4 ticks (100ms/30s/60s/5s) + meeting-start/end hooks                        |
| OMNI_ROUTER        | 706  | helm   | 175       | pattern | 7 edges audio→vad→stt→(translator,response)→ui→recorder                     |
| OMNIQUANT_ENGINE   | 707  | vector | 7         | genius  | 4-quant convo compress (Q1 sha + Q2 polar + Q3 turbo8 + Q4 glyph)           |
| OMNI_FILE_ROTATOR  | 708  | helm   | 131       | pattern | session-log rotation by size/time                                           |
| OMNI_SPINDLES      | 709  | forge  | 14        | ability | spawn whisper.cpp, gemma-local subprocess                                   |
| OMNI_FLYWHEELS     | 710  | vector | 169       | skill   | Claude/Gemma LLM call routing + token/latency record                        |
| OMNIDISPATCHER     | 711  | helm   | 8         | tool    | top-level envelope dispatch (transcript/translation/suggestion/decision)   |

---

## 2. Where each copilot module lives

```
   M01 audio_capture ─┐
   M02 vad           ─┼─► OMNIMETS  (chunk count, latencies)
   M03 stt           ─┤   OMNI_ROUTER (edge hops)
   M04 translator    ─┤   OMNI_SPINDLES (process spawn whisper/gemma)
   M05 response_eng  ─┤   OMNI_FLYWHEELS (LLM router record)
   M06 context_pack  ─┴─► OMNIQUANT_ENGINE (4-quant + 50D resume pre-quant)
   M07 ui_overlay    ───► OMNIMETS (suggestion_emitted)
   M08 tts           ───► OMNI_SPINDLES (edge-tts subprocess)
   M09 fabric_emit   ───► OMNIDISPATCHER (envelope emission)
   M10 privacy_gate  ───► OMNI_ROUTER (route decision tag)
   M11 session_rec   ───► OMNI_FILE_ROTATOR (log rotation)
   M12 supervisor    ───► OMNISCHEDULER (ticks + lifecycle) + OMNIMETS (heartbeat)
```

---

## 3. Metrics catalog (OMNIMETS contract)

Every metric is HBPv1 pipe-delim, sha-chained, default-observed by
hookwall+GNN per v38 L13 universal-route primitive.

| Metric key                              | Source module | Cadence                       |
|-----------------------------------------|---------------|-------------------------------|
| `copilot.audio.chunk_count_30s`         | M01           | rolling 30s window            |
| `copilot.stt.latency_p50_ms`            | M03           | per segment close             |
| `copilot.stt.latency_p99_ms`            | M03           | per segment close             |
| `copilot.translate.latency_p50_ms`      | M04           | per translation               |
| `copilot.translate.latency_p99_ms`      | M04           | per translation               |
| `copilot.response.latency_p50_ms`       | M05           | per Claude call (first token) |
| `copilot.response.latency_p99_ms`       | M05           | per Claude call (first token) |
| `copilot.words.transcribed_total`       | M03           | cumulative                    |
| `copilot.words.translated_total`        | M04           | cumulative                    |
| `copilot.suggestions.emitted_total`     | M05           | cumulative                    |
| `copilot.meeting.duration_s`            | M12           | on meeting end                |
| `copilot.cost.anthropic_usd`            | M05           | per call estimate             |
| `copilot.cost.translate_usd`            | M04           | per call estimate             |

Cost estimation rule (rough, conservative):
- Claude Sonnet 4.7: ~$3 in / $15 out per MTok → embedded constants in
  `omni_flywheels` row tag `usd_est`.
- GCP Translate v3: $20 per MChar after free tier.

These are descriptors only — operator-side budget cap is a separate B12
privacy-gate concern.

---

## 4. Scheduling cadence (OMNISCHEDULER contract)

| Tick               | Period       | Justification                                          |
|--------------------|--------------|--------------------------------------------------------|
| `audio_poll_ms`    | 100 ms       | Ring-buffer drain, matches WASAPI 20ms × 5 frames     |
| `metrics_flush_ms` | 30 s         | Push window for cumulative counters → OMNIMETS         |
| `heartbeat_ms`     | 60 s         | Foundation v3 LAW internal heartbeat (canonical)       |
| `lifecycle_ms`     | 5 s          | Micro-agent THINK/PLAN/MEMORY/RESPOND/SELF_REFLECT     |

Lifecycle hooks:

```
on_meeting_start(company, pack_text):
  - pre-quantize resume+company doc → 50D coord, cached for session
  - register 4 standing ticks
  - emit omnischeduler hook row

on_meeting_end():
  - emit meeting_duration_s metric
  - flush conversation buffer
  - emit omnischeduler hook row
```

---

## 5. Quadruple-quant context compression (OMNIQUANT contract)

```
ConversationBuffer:
  ┌────────────────────────────────────┐
  │ Last 8 turns kept VERBATIM         │
  │ (full text passed to Claude)       │
  └────────────────────────────────────┘
              │ overflow
              v
  ┌────────────────────────────────────┐
  │ Older turns COMPRESSED:            │
  │   Q1: sha256[16] fingerprint       │
  │   Q2: polar (theta, r) ∈ unit disk │
  │   Q3: Turbo float8 (0-255)         │
  │   Q4: zeta-tier glyph (BEHCS-256)  │
  └────────────────────────────────────┘
```

Why 8 turns verbatim? Empirically (per round-1 A02 GNN paper) the
2000-msg gulp / 1600 minimum-effect canon implies a much larger
saturation point, but the **Claude prompt-cache breakpoint** is governed
by suffix stability — keep the latest 8 turns mutable, the rest stable
behind sha fingerprints so the cache prefix re-hits on each new turn.

Resume pre-quantization is a **one-shot** operation at meeting start:

```
resume.docx + company.docx
        │
        v
prequantize_resume() ──► ResumeContextDescriptor:
                          pack_sha (sha256)
                          coord_50d (deterministic placeholder)
                          company
                          .as_row() → emitted once per session
                          .reused on every Claude call
```

Real 50D embedding handoff: B08 context_pack module owns the live
sentence-transformer feed. B10 wrapper provides the descriptor stamp
and emission plumbing only.

---

## 6. Edge routing (OMNI_ROUTER contract)

```
audio_capture ─► vad ─► stt ─┬─► translator ─► ui ─► session_recorder
                             │
                             └─► response_engine ─► ui
```

Each hop emits an `omni_router` HBPv1 row:

```
omni_router|session=<pid>|src=stt|dst=translator|sha=<payload_sha[:16]>|sha=<row_sha>
```

This satisfies the **DUAL_EMIT_GATE** (v38) — destination delivery
PLUS observation row, so a silent edge failure surfaces in the
hookwall/GNN trio downstream.

---

## 7. Test coverage

`tests/test_omni_integration.py` covers:

- 8-primitive table completeness (Hilbert/band/BEHCS/mint)
- HBPv1 row format determinism (key-order-invariant, sha-suffixed)
- All 13 metrics defined
- All 4 tick rates wired with canonical values
- QuadQuant fingerprint determinism + uniqueness
- ConversationBuffer overflow semantics (8 verbatim, rest compressed)
- ResumeContextDescriptor stamping + reuse
- OmniIntegration end-to-end: meeting_start, meeting_end, edge routing,
  LLM call recording, envelope dispatch, spindle spawn, rotator
- Event→primitive routing table
- Discipline: no JSON leakage, every emission HBPv1

---

## 8. Honest gaps

1. **Descriptor-only, not wired live** — this module produces HBPv1
   rows; the actual network emission to running omni-primitives
   (`:4950` omnidispatcher, omnimets daemon, etc.) is B14's
   responsibility at integration time. B10 outputs are CANON shape but
   not yet flowing on the wire.
2. **50D coord is deterministic from sha**, not from a real embedder.
   B08 owns the live embedding pipe; B10 only stamps identity.
3. **Cost estimation is constants-only.** No real metering against
   Anthropic billing endpoint or GCP cost API — those require operator
   creds and are out of scope for B10.
4. **OMNIDISPATCHER has 2 voxels** (H631 v29 + H711 v40). B10 uses
   the H711 name; cp ceremony will resolve to single canonical PID.
5. **OMNI_FILE_ROTATOR rotation policy** (size threshold? time
   threshold?) deferred to B13 session_recorder which owns the actual
   file format and retention rules.
6. **Q3 turbo float8** is a single byte 0-255 here; real Turbo-quant
   in HyperBEHCS uses learned codebooks. Placeholder is honest, not
   load-bearing.
7. **Q4 glyph alphabet uses 16 sample glyphs** (`·∘○●◐◑◒◓◔◕◖◗◘◙◚◛`),
   not the full 256-element BEHCS-256 glyph set. Real glyph index
   is in `omniquant_engine` runtime which B10 does not boot.

---

## 9. Wiring checklist for B14 supervisor

- [ ] Inject real `emit_sink` callback that POSTs to fabric MCP
      `/api/envelope/emit` or writes to local `.hbp` ledger.
- [ ] Wire the 100ms audio_poll tick to M01.ring_buffer.drain().
- [ ] Wire the 30s metrics flush to push accumulated counters via
      `emit_metric`.
- [ ] Wire the 60s heartbeat to `/api/internal-heartbeat` per LAW v3.
- [ ] Wire the 5s lifecycle probe to micro-agent THINK/PLAN/MEMORY/
      RESPOND/SELF_REFLECT/EXECUTE state (per A05 round-1 finding).
- [ ] Pass real resume+company doc text into `on_meeting_start`.
- [ ] Hand `build_prompt_context()` output as the `messages[]` prefix
      to Claude SDK call inside M05.
- [ ] Hand B08 `context_pack` 50D vector into `coord_50d` of the
      `ResumeContextDescriptor` (replace the deterministic placeholder).

— B10-OmniMapping, 2026-05-27
