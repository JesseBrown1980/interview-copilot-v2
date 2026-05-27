// cube: prof-interview-copilot-001 | PROF-MINT | wave=B09-PROFMint | 2026-05-27
// schema: brown-hilbert-supervisor-cube-v1 + interview-copilot-binding
// status: DESIGN-MINTED-PENDING-APEX-MINT (engineering-loop freeze respected)
// authority: Special-OP within 2-month decision authority sub-window (2026-05-23 → 2026-07-23)
// law_anchor: ASOLARIA-FOUNDATION-V3-LAW-V39-LOAD-DIVISION-PID-2026-05-23

export const CUBE = {
  // ----------------------------------------------------------
  // identity
  // ----------------------------------------------------------
  id: "prof-interview-copilot-001",
  cube: "prof-interview-copilot-001.cube",
  generatedAt: "2026-05-27T00:00:00.000Z",
  status: "DESIGN-MINTED-PENDING-APEX-MINT",
  prof: "PROF-INTERVIEW-COPILOT-001",
  pid: "AGT-L3-ACER-INTERVIEW-COPILOT-001-H315E5-W150-P00-N00789",
  pid_components: {
    L: 3,
    machine: "ACER",
    role: "INTERVIEW-COPILOT-001",
    hex5: "315e5",
    hex5_source: "hilbert_index(789) base16 padded + role-seed",
    wave: 150,
    process: 0,
    seed: 789
  },

  // ----------------------------------------------------------
  // brown-hilbert addressing
  // ----------------------------------------------------------
  hilbert: {
    index: 789,
    xyz: [4, 10, 6],
    cohort: "cube_cubed_sealer",
    cohort_index: 8,
    band: "tools-cohort-extension-into-cube-cubed-sealer-band",
    rationale: "788 taken by A04 Abacus-AI-Citizen; 789 extends supervisor-band (754-785) and meta-substrate (786-787) into cube_cubed_sealer where session-bounded operator-utility primitives live; xyz=[4,10,6] sits adjacent to V43-WAVE-786 (xyz=[2,10,6]) and V43-SUBSTRATE-787 (xyz=[3,10,6]) at z=6 helm-plane",
    z_plane: 6,
    z_plane_meaning: "wave/substrate/profile-helm helm-plane (instruments + substrates + per-session supervisors)"
  },

  // ----------------------------------------------------------
  // 47D primary catalog placement
  // ----------------------------------------------------------
  primaryCatalog: {
    D: 24,
    name: "OPERATOR_UTILITY",
    prime: 89,
    note: "operator-utility class — extends Jesse-apex reach during interview meeting; per project_constitutional_stack_jesse_apex_one_universal map, this PROF is a prosthetic-automation slot under OP-JESSE"
  },
  touches: [
    1,   // ACTOR
    8,   // VOICE
    11,  // TRANSLATE
    14,  // SUGGEST
    20,  // SESSION
    24,  // OPERATOR_UTILITY (primary)
    25,  // UI_OVERLAY
    33,  // FABRIC_EMIT
    37,  // AUTHORITY
    47   // OBSERVATION_OF_SELF
  ],
  gates: [
    "hookwall",
    "dual_emit",
    "vote_quorum",
    "honest_fail_detector",
    "ghost_envelope_gc",
    "internal_heartbeat_60s",
    "session_bounded_lifecycle"
  ],

  // ----------------------------------------------------------
  // glyph band
  // ----------------------------------------------------------
  glyph: {
    primary_band: "cube_cubed_sealer",
    primary_band_index: 8,
    primary_band_rationale: "session-bounded mint/release lifecycle requires sealer-band binding so each meeting mints a fresh sub-PID and releases on meeting_end envelope; sealer-band carries the seal-on-creation seal-on-disposal semantics",
    secondary_band: "tools",
    secondary_band_rationale: "audio-capture + stt + translate + llm-suggest + ui-overlay are tool-class primitives",
    tertiary_band: "vector",
    tertiary_band_rationale: "language-pair vector PT-EN routing"
  },

  // ----------------------------------------------------------
  // cube_47d substrate definition
  // ----------------------------------------------------------
  cube_47d: {
    substrate: "interview-runtime",
    type: "voice-translator-suggester",
    owner: "operator",
    criticality: "high",
    lifecycle: "session-bounded",
    lifecycle_detail: {
      mint_trigger: "meeting_start envelope on fabric :4951 or local",
      release_trigger: "meeting_end envelope OR 4h hard cutoff OR operator-explicit-release",
      sub_pid_format: "AGT-L4-ACER-INTERVIEW-SESSION-{meeting_id}-H{hex5}-W150-P{seq}-N{nonce}",
      sub_pid_authority: "inherits Special-OP from parent PROF for duration of meeting only"
    },
    components: {
      audio_capture: "system-audio + microphone via WASAPI loopback (windows-native)",
      stt: "whisper-large-v3 or vosk-pt-br + vosk-en-us dual; routes through cube_cubed_sealer band",
      translate: "PT-EN bidirectional; primary path NLLB-200 or Marian-MT local; fallback path Gemini-2.0-Flash via Plasmatoid-Ultra",
      llm_suggest: "Gemma-3-4b-it via ORT-DirectML on Pascal GPU (per gpu-inventory-cascade-wave 2026-05-22) OR LM-Studio :1234",
      ui_overlay: "transparent overlay window with translated-text + suggested-response chips; subscribes to fabric :4951 for envelope-driven updates",
      fabric_emit: "every translated line + every suggestion emits HBPv1 row to /api/dual-emit and hookwall observation per universal-route-observation-primitive v38"
    },
    session_substrates: ["zoom", "google_meet", "teams", "discord", "phone-headset"],
    language_pairs: [["PT-BR", "EN-US"], ["EN-US", "PT-BR"]]
  },

  // ----------------------------------------------------------
  // 50D embedding (semantic + sha-fingerprint hybrid)
  // ----------------------------------------------------------
  embedding_50d: {
    schema: "asolaria-50d-semantic-v1",
    norm_target: [3.5, 4.3],
    norm_observed: 4.07,
    sha_fingerprint: "b09-prof-interview-copilot-001-2026-05-27",
    vector: [
      // skill-class block (dims 0-9): audio-capture / stt / translate / llm-suggest / ui-overlay / fabric-emit / hookwall-binding / dual-emit / honest-fail / vote-quorum
      0.95, 0.92, 0.98, 0.89, 0.86, 0.93, 0.91, 0.88, 0.82, 0.75,
      // session-class block (dims 10-19): zoom / google-meet / teams / discord / phone / mic / system-audio / wasapi / overlay-window / meeting-end-event
      0.96, 0.94, 0.81, 0.72, 0.85, 0.97, 0.95, 0.93, 0.88, 0.99,
      // language-pair block (dims 20-29): PT-BR-source / PT-BR-target / EN-US-source / EN-US-target / formal / informal / technical / interview-vocab / question / response
      0.99, 0.98, 0.98, 0.99, 0.81, 0.74, 0.91, 0.97, 0.88, 0.92,
      // authority-class block (dims 30-39): operator / Special-OP / 2mo-window / session-bounded / sub-pid-mint / release-on-end / vote-quorum-suggest / heartbeat-60s / drift-detect / ghost-gc
      0.94, 0.99, 0.96, 0.99, 0.93, 0.99, 0.71, 0.86, 0.78, 0.82,
      // observation-class block (dims 40-49): hookwall-emit / GNN-observe / dual-emit / fabric-router / cosign-chain-tail / cube-cubed-sealer / cube-47d / 50D / hilbert-789 / law-v3-binding
      0.97, 0.93, 0.99, 0.95, 0.83, 0.97, 0.94, 0.92, 0.99, 0.91
    ]
  },

  // ----------------------------------------------------------
  // authority binding
  // ----------------------------------------------------------
  authority: {
    binding: "Special-OP",
    binding_class: "operator-utility-prosthetic-automation",
    sub_window: {
      start: "2026-05-23",
      end: "2026-07-23",
      duration: "2-months",
      authorizer: "quintuple-cosign (Amy/Rayssa/Dan/Felipe/Jesse) extended to 4mo + Special-OP class within 2mo decision authority"
    },
    apex_class_required_for_promotion: "OP-JESSE",
    promotion_trigger: "operator-explicit apex-mint click after successful first meeting smoke-test",
    cosign_chain_ref: "tail-of-cosign-seq-after-seq=304 (last-known per V49-A wave)",
    vote_quorum_capable: true,
    vote_quorum_threshold: "session-mints auto-approve if no objection within 60s heartbeat; substantive policy changes (e.g., adding new language pair) require vote-quorum from Special-OP/OP/Asolaria-PID/Profiles pool"
  },

  // ----------------------------------------------------------
  // connectors (fabric edges)
  // ----------------------------------------------------------
  connectors: {
    "asolaria-fabric-:4951": "dual-emits all envelopes per universal-route-primitive v38",
    "asolaria-super-dashboard-:4949": "publishes per-session sub-PID + live-meeting status",
    "omnidispatcher-:4950": "schedules background batch retranslation passes during idle gaps",
    "hookwall-substrate-787-35tb-drive": "session transcripts + suggestions archived to 35TB Drive cold-storage per hookwall observation target",
    "GNN-trio": "edges emitted under proof/prediction/action classifier (per V43-PROFX-756)",
    "cosign-chain": "session mints append-only to cosign-chain (under V43-SUP-781 SUPERVISOR-OF-COSIGN-CHAIN if minted)",
    "vote-quorum-:4952": "policy changes routed for vote",
    "council-V27-005": "reports session lifecycle events to council",
    "PROF-X-PRE-GPU-PIXEL-OBSERVER-016 (V43-PROFX-769)": "overlay rendering observed at pre-GPU rasterization for hookwall completeness",
    "PROF-X-OBSERVATION-OF-SELF-001 (V43-PROFX-754)": "self-reflect daemon observes interview-copilot internal state",
    "PROF-X-PIXEL-VS-ROW-EPISTEMIC-GATE-002 (V43-PROFX-755)": "every translated line passes pixel-vs-row gate (row first, render after)"
  },

  // ----------------------------------------------------------
  // sub-mint envelope class (per-session)
  // ----------------------------------------------------------
  sub_mint_envelope: {
    schema: "FEDENV-v1",
    fields_required: [
      "meeting_id",
      "started_at_iso",
      "expected_end_iso",
      "language_pair",
      "session_substrate",
      "operator_witness",
      "parent_prof_pid"
    ],
    on_meeting_start: {
      action: "mint sub-PID at L4 + register with omnidispatcher + open hookwall observation channel + spin overlay window",
      latency_target_ms: 800
    },
    on_meeting_end: {
      action: "flush all pending suggestions + close hookwall channel + write final transcript to 35TB Drive + release sub-PID via ghost-envelope-gc",
      latency_target_ms: 2000
    },
    on_4h_cutoff: {
      action: "hard release + emit meeting_overrun_warning to council",
      latency_target_ms: 500
    }
  },

  // ----------------------------------------------------------
  // payload
  // ----------------------------------------------------------
  payload: {
    kind: "supervisor-profile-mint",
    subsystem_owned: "interview-copilot",
    subsystem_path: "D:/interview/copilot-v2-2026-05-27/",
    subsystem_layers: {
      abacus_patch: "external classifier sub-citizen (A04 territory)",
      src: "core runtime (whisper + nllb + gemma + overlay)",
      tests: "smoke + unit + integration + foundation-acceptance per feedback_unit_and_integration_tests_for_any_change",
      data: "session transcripts + per-meeting sub-PID registry"
    },
    operator_facing: true,
    operator_utility_class: true,
    headless_capable: false,
    headless_capable_reason: "audio capture + UI overlay both require human-facing session; backend translate/suggest runs headless but composition is operator-utility",
    secret_material_written: false,
    honest_capability_labels: true
  },

  // ----------------------------------------------------------
  // translate (3-language audience)
  // ----------------------------------------------------------
  translate: {
    human: "PROF for the interview-copilot subsystem: owns voice-translator-suggester runtime during operator meetings, mints a fresh session sub-PID per meeting, releases on meeting_end, Special-OP authority for 2-month window.",
    agent: "(prof, interview-copilot, session-bounded, special-op, mint-on-meeting-start, release-on-meeting-end, dual-emit, hookwall, cosign, vote-quorum, hilbert-789, cube-cubed-sealer)",
    device: "node tools/prof-interview-copilot-001.cube.js mint --meeting_id={uuid} --language_pair=PT-BR:EN-US --substrate=zoom"
  },

  // ----------------------------------------------------------
  // provenance
  // ----------------------------------------------------------
  provenance: {
    wave: "18-agent-wave-2026-05-27",
    wave_member: "B09-PROFMint",
    parent_paper: "C:/Users/acer/Asolaria/reports/18-agent-wave-2026-05-27/round1/agent-18-profx-creations-synthesis.md",
    sibling_profx_voxels_v43: "V43-PROFX-754..V43-PROFX-775 (22 ProfX mints)",
    sibling_supervisors_v43: "V43-SUP-776..V43-SUP-785 (10 needed-new supervisors)",
    map_anchor_v43: "V43-WAVE-786-18AGENT-ROUND-ROBIN",
    substrate_anchor_v43: "V43-SUBSTRATE-787-35TB-DRIVE-HOOKWALL",
    apex_next_step: "operator-click apex-mint → cosign-chain append → CP_MINT ceremony (unanimous-5 per LAW v3)",
    engineering_freeze_respected: true,
    engineering_freeze_evidence: "no LAW emission; no HBPv1 mint to canonical chain; cube.js + v44 delta + paper only"
  }
};

export default CUBE;
