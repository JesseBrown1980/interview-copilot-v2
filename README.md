# Interview Copilot v2

**Project:** real-time interview copilot for Zoom/Meet calls (interviewer EN ↔ operator PT) with privacy-mode kill-switch.
**Status (2026-05-27 14:00 UTC):** **scaffold only**. Architecture spec landed (B02), all 12 modules unimplemented. **Not ready for live interview use.** See "Roadmap" + sibling `FINAL-SHIP-PLAN.md` for next steps.
**Owner:** operator (acer / plasmatoid@gmail.com).
**Build wave:** 18-agent interview-copilot-update 2026-05-27; this README authored by B14-FinalSynthesis.

---

## Architecture (ASCII)

```
                              ZOOM / MEET CALL
                                     |
              +----------------------+----------------------+
              | system audio (interviewer)                  | mic (operator)
              v                                             v
       [M01 WASAPI loopback]                       [M01 mic capture]
              | PCM 16kHz mono 16-bit                       |
              v                                             v
       [M02 VAD silero]                            [M02 VAD silero]
              | speech segment (300ms+)                     |
              v                                             v
       +------+---------- [M10 privacy-gate] ---------------+
              |   route decision (cloud | local)
              v
       [M03 STT router]  --cloud-->  GCP STT v2
              |          --local-->  Whisper.cpp (base.en GGML, CPU+OpenBLAS)
              | transcript {speaker, text, lang, t0, t1}
              v
       [M11 session-recorder]  AES-GCM .hbp local-only
              |
              v
       [M04 translator]  --cloud-->  GCP Translate v3
              |          --local-->  Gemma-4 via LM Studio :1234
              | translation {src_lang, tgt_lang, src_text, tgt_text}
              v
       [M09 fabric-emitter] --HBPv1--> asolaria-fabric MCP :4781
              |
              v
       if speaker == interviewer:
              v
       [M05 response-engine] (Claude Sonnet/Opus, prompt-cached)
              | system_prompt = M06 ctx-pack (resume + company doc)
              | user = transcript window (last ~2 min)
              v
       [M07 UI overlay]  Tauri + Svelte, always-on-top, 3 panes
              ├── pane A: interviewer transcript + translation
              ├── pane B: operator transcript + translation
              └── pane C: streamed AI suggestion
              |
       operator decides → [M08 TTS optional] + [M11 log] + [M09 fabric decision-emit]

[All processes supervised by M12 (asyncio); health :4970/health; heartbeat 60s to fabric per LAW v3]
```

---

## Quick-start

### 1. Install (Windows 11, PowerShell 7)

```powershell
cd D:/interview/copilot-v2-2026-05-27
# Python 3.11 venv (use existing nvm4w/python or system Python)
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -e .[dev]
# Tauri/Svelte UI deps
cd src/ui
npm install
cd ../..
# Verify external deps (Whisper.cpp binary, silero-vad.onnx, GCP creds, LM Studio :1234, fabric :4781)
.\scripts\verify-deps.ps1
```

### 2. Configure

Copy `.env.example` → `.env` and fill in:

```env
ANTHROPIC_API_KEY=sk-ant-...                 # operator's Claude Max key
GOOGLE_APPLICATION_CREDENTIALS=C:/...json    # GCP service-account JSON for STT v2 + Translate v3
LM_STUDIO_URL=http://127.0.0.1:1234          # already running per gpu-inventory cascade 2026-05-22
FABRIC_URL=http://127.0.0.1:4781             # asolaria-fabric MCP
SESSION_PASSPHRASE=<operator chooses>        # Argon2id-derives AES-GCM key for M11 logs
PRIVACY_DEFAULT=cloud                        # cloud | local
ACTIVE_COMPANY=Capital Rx                    # one of packs/*.docx — set per interview
```

### 3. Run

```powershell
.\scripts\start-copilot.ps1
# Supervisor binds :4970, Tauri UI window appears (frameless, always-on-top, draggable)
# Hotkeys: Ctrl+Space=ask, Ctrl+M=mute, Ctrl+P=privacy-toggle, Esc=hide
```

### 4. During an interview

1. Open Zoom/Meet **before** starting copilot (WASAPI loopback grabs the system audio device).
2. Move the overlay to your secondary monitor or any non-shared region.
3. Confirm pane A (interviewer) starts populating as the interviewer speaks.
4. Pane C streams Claude suggestions when interviewer questions are detected.
5. Toggle `Ctrl+P` to drop into 100% local mode at any point if topic gets sensitive.

---

## Operator-action queue (BLOCKING for live use)

Items the operator must do that no agent can do:

| # | Action | Why | Effort |
|---|--------|-----|--------|
| OA01 | **Run `pip install -e .[dev]`** once code is generated | install Python deps | 2 min |
| OA02 | **Install LM Studio if not already** + confirm :1234 reachable + Gemma-4 loaded | privacy-mode fallback for M04/M05 | 0 min if already running (per memory `project_gpu_inventory_cascade_wave_2026_05_22.md`) |
| OA03 | **Set ANTHROPIC_API_KEY** in `.env` | M05 suggestion engine | 1 min |
| OA04 | **Get GCP service-account JSON** with STT v2 + Translate v3 scopes; download to disk; set `GOOGLE_APPLICATION_CREDENTIALS` | M03 cloud STT + M04 cloud translate | **15-30 min** (GCP Console: create project → enable APIs → create service account → key.json) |
| OA05 | **Pick SESSION_PASSPHRASE** and remember it | M11 log encryption (Argon2id) | 1 min |
| OA06 | **Choose ACTIVE_COMPANY** per interview, set in `.env` (or pick in UI dropdown once M07 ships) | M06 context-pack selection | 30 sec/interview |
| OA07 | **Run dry-run on ONE company** before live use | acceptance-criterion gate | 15 min |
| OA08 | **Test WASAPI loopback** on operator's Win11 + Realtek/Pascal combo | M01 hardware path verification | 5 min |
| OA09 | **(Optional) install Tauri prereqs** (Rust toolchain + WebView2 runtime) if shipping from source | M07 build | 20 min one-time |
| OA10 | **Operator approval gate**: review `FINAL-SHIP-PLAN.md` + this README, then say go / no-go on rebuilding-vs-resurrecting any legacy v1 (B01 inventory not landed) | prevents rebuild-already-existing waste | 10 min |

**Estimated total operator time before first live use:** ~1h (assuming GCP setup is fresh; less if GCP project already exists).

---

## Privacy threat model (summary)

### Stays 100% local always
- Raw mic + WASAPI loopback PCM (in-memory ring buffer, never written to disk)
- VAD segments
- Encrypted session log (`sessions/*.hbp`, AES-GCM, Argon2id-derived key, **key NOT persisted**)
- Privacy-mode flag (M10)

### Leaves device in `cloud` mode (default; accuracy-first)
| Data | Destination | Sensitivity |
|------|-------------|-------------|
| PCM speech segments | GCP STT v2 (`speech.googleapis.com`) | HIGH (voice + content) |
| Transcript text | GCP Translate v3 (`translation.googleapis.com`) | HIGH (content) |
| Recent transcript window (~2 min) | Anthropic API | HIGH (content) |
| Resume + active company doc | Anthropic API (prompt-cached after first call) | MEDIUM (operator-public-ish) |

### Leaves device in `local` mode (Ctrl+P toggle)
Nothing — Whisper.cpp local + Gemma-4 local + Claude suggestion engine **disabled or degraded-routed to local Gemma-4 with warning UI**.

### Threats covered
- 3rd-party leak → privacy-gate hard-routes local-only.
- Lost laptop → session logs AES-GCM, key not persisted.
- Interviewer notices overlay → frameless + Esc instant-hide + secondary-monitor placement.
- Fabric leak → M09 emits hash+preview-64-chars, not raw content.

### Honest gaps (NOT covered)
- Network MITM (rely on TLS, no cert pinning) — acceptable.
- Local malware with code-exec — out of scope.
- Anthropic content-retention policy — documented in `PRIVACY.md` (to be written), operator chooses informed.

Full detail will land in `PRIVACY.md` once B12 (privacy-gate) ships.

---

## Roadmap

### MVP (target: interview-ready within 2-3 build cycles)
**Must-have for first live interview:**
- M01 audio-capture (WASAPI loopback + mic)
- M02 VAD segmenter (silero ONNX)
- M03 STT router (cloud GCP v2 path **only** for MVP; local Whisper.cpp deferred to v2.1)
- M04 translator (cloud GCP v3 path only for MVP)
- M05 response-engine (Claude Sonnet, prompt-cached, resume+active-company in system prompt)
- M06 context-pack (basic docx/pdf loader; HyperBEHCS quant deferred to v2.1)
- M07 UI overlay (3 panes, hotkeys, always-on-top)
- M10 privacy-gate (toggle UI only; full local-route deferred to v2.1)
- M11 session-recorder (AES-GCM .hbp logs)
- M12 supervisor (asyncio + health + start-stop)

### v2.1 (post-MVP polish)
- Local Whisper.cpp path (M03 fallback)
- Local Gemma-4 path for M04/M05 (full privacy-mode)
- M08 TTS optional (Edge-TTS ear-prompt)
- M09 fabric-emitter (HBPv1 envelopes to asolaria-fabric :4781)
- HyperBEHCS 47D quant in M06
- Tauri MSI installer via `scripts/package-release.ps1`

### Deferred (post-interview-cycle)
- Multi-language beyond PT↔EN
- Speaker diarization (currently relies on physical-channel split: loopback=interviewer, mic=operator)
- Multi-window / multi-monitor auto-positioning
- Settings pane for user-configurable hotkeys
- Mac/Linux port (Windows-only for now — WASAPI is Win-specific; equivalent paths CoreAudio/PulseAudio future work)

### Acceptance criteria (must all be GREEN before "ship")
1. All 10 module unit tests pass (MVP scope).
2. Integration: 30-sec dual-stream synthetic PCM → full transcripts + translations + 1 suggestion in < 2 sec end-to-end.
3. Privacy `local` mode: zero outbound TLS to GCP/Anthropic (verified via netstat or Wireshark in test).
4. UI overlay survives 60-min interview without crash; memory < 400 MB.
5. Operator dry-run on ONE company doc completed successfully.
6. (LAW v3) Heartbeat every 60s reaches asolaria-fabric :4781.

---

## Build wave summary (18 agents, 2026-05-27)

| Agent | Role | Status at this README's write time |
|-------|------|-----------------------------------|
| A01-A04 | inventory / pre-work | not landed |
| B01 | legacy inventory | **not landed** |
| B02 | architecture spec | **LANDED** at `Asolaria/reports/.../copilot/agent-B02-architecture.md` |
| B03 | M01 audio-capture | not landed |
| B04 | M02 VAD | not landed |
| B05 | M03 STT router | not landed |
| B06 | M04 translator | not landed |
| B07 | M05 response-engine | not landed |
| B08 | M06 context-pack | not landed |
| B09 | M07 UI overlay | not landed |
| B10 | M08 TTS | not landed |
| B11 | M09 fabric-emitter | not landed |
| B12 | M10 privacy-gate | not landed |
| B13 | M11 session-recorder | not landed |
| **B14** | **final synthesis (this doc)** | **LANDED** |

**Honest current state:** **scaffold + 1 architecture doc only.** Code modules unimplemented. **Not interview-ready.** See `FINAL-SHIP-PLAN.md` for next-wave plan.

---

## Files of interest

- Architecture spec: `C:/Users/acer/Asolaria/reports/interview-copilot-update-2026-05-27/copilot/agent-B02-architecture.md`
- Final ship plan: `C:/Users/acer/Asolaria/reports/interview-copilot-update-2026-05-27/FINAL-SHIP-PLAN.md`
- Git prep: `D:/interview/copilot-v2-2026-05-27/.git-prep.md`
- Company packs: `D:/interview - resume/` (10 docs, verified present)
- Foundation v1 anchor (read before any agent work): `C:/asolaria-foundation-v1/00-IMMUTABLE-FOUNDATION.md`

— B14-FinalSynthesis, 2026-05-27
