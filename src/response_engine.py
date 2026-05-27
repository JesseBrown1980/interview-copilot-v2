"""
response_engine.py - agent B07-ResponseEngine - 2026-05-27
============================================================

Streaming Claude API wrapper that turns a transcribed interview question
into 1-3 short suggestion bullets for the operator.

Design pillars:
  1. STREAMING by default - first token target <2s, full response <5s.
  2. PROMPT CACHING on the heavy system prompt (resume + role) so the
     2nd+ call in a session pays only for the user-turn delta.
  3. BUDGETED context - resume text truncated to ~5K tokens to keep
     cache footprint cheap and TTFT fast.
  4. NO interview-text persistence by this module - the caller decides
     whether to log the question. Privacy is a router concern.

Usage:
    from response_engine import ResponseEngine, MeetingContext

    engine = ResponseEngine.from_default_cache()
    ctx = MeetingContext(company_key="capital-rx",
                         history=[("Tell me about yourself", "...")])
    for chunk in engine.stream_suggestion(
            question="What's your experience with PostgreSQL?", ctx=ctx):
        print(chunk, end="", flush=True)

Honest gap: requires ANTHROPIC_API_KEY in environment. Operator-action
required before first run.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator, Protocol

from .resume_extractor import load_cache, DEFAULT_OUTPUT


# Mission default: claude-sonnet-4-6 (fast + smart enough for short bullets).
# If the published id differs (e.g. claude-sonnet-4-5 is current public),
# swap the constant here - it's the only place model id is hardcoded.
DEFAULT_MODEL = os.environ.get("COPILOT_MODEL", "claude-sonnet-4-6")

# Approximate token budgets. We don't tokenize here; use a 4-char ~ 1-token
# heuristic which is fine for English resume text. Anthropic's tokenizer
# is closer to 3.7 chars/token but the headroom absorbs that drift.
RESUME_TOKEN_BUDGET = 5_000
RESUME_CHAR_BUDGET = RESUME_TOKEN_BUDGET * 4  # ~20K chars

# Cap on output to keep responses short and latency low.
MAX_OUTPUT_TOKENS = 256

# How many prior (question, answer) turns to feed back as conversation
# history. Larger -> richer context but worse latency + larger non-cached
# user message payload.
DEFAULT_HISTORY_TURNS = 4


# ---------------------------------------------------------------------------
# System prompt template
# ---------------------------------------------------------------------------

# The system prompt is split into TWO blocks for prompt caching:
#   [0] role/instructions  -- almost never changes -> cached
#   [1] resume context     -- changes only when operator switches company
#                              -> cached, gets invalidated cheaply on switch
# Both blocks marked cache_control:ephemeral so a single hash covers them.
ROLE_INSTRUCTIONS = """You are an interview coach assisting the operator
during a LIVE job interview. The operator's interviewer just asked a
question. Your job: suggest 1-3 SHORT bullet answers the operator can
deliver in their own voice.

Rules:
- Output 1-3 bullets. NEVER more.
- Each bullet <= 30 words.
- Concrete, resume-anchored, no fluff.
- If the operator's resume has a directly relevant project/skill/metric,
  lead with it.
- If the question is behavioral, prefer STAR-style one-liners.
- Never invent achievements or numbers not in the resume.
- Output ONLY the bullets - no preamble, no "Here are some suggestions",
  no closing remarks.

Format exactly:
- <bullet 1>
- <bullet 2>
- <bullet 3>
""".strip()


def _truncate_to_budget(text: str, budget: int = RESUME_CHAR_BUDGET) -> str:
    """Trim text from the end so total length <= budget.

    Keeps the resume header/summary which is more relevant for first-pass
    answers than the deep job-history tail. If the resume is shorter than
    the budget it's returned unchanged.
    """
    if len(text) <= budget:
        return text
    return text[:budget].rsplit("\n", 1)[0]  # cut at paragraph boundary


def build_resume_block(cache: dict | None, company_key: str | None) -> str:
    """Render the resume context block for the active company.

    Returns an empty string if cache is missing or the company is unknown
    - the caller will still get a sensible (generic) suggestion.
    """
    if not cache or not company_key:
        return ""
    resume = cache.get("resumes", {}).get(company_key)
    if not resume:
        return ""
    body = _truncate_to_budget(resume.get("text", ""))
    return (
        f"OPERATOR RESUME (tailored for {resume['company_display']}):\n"
        f"-----\n{body}\n-----"
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


@dataclass
class MeetingContext:
    """Per-call meeting state. Cheap to construct; pass new instance each Q."""

    company_key: str | None = None
    history: list[tuple[str, str]] = field(default_factory=list)
    # history is [(question_text, prior_suggestion_text), ...] in
    # chronological order. The engine truncates to DEFAULT_HISTORY_TURNS.


class _Streamable(Protocol):
    """Minimal anthropic stream surface we depend on, for mocking in tests."""

    def text_stream(self) -> Iterator[str]: ...
    def __enter__(self) -> "_Streamable": ...
    def __exit__(self, *a: Any) -> None: ...


class _ClientLike(Protocol):
    """Subset of anthropic.Anthropic we use - lets tests inject a fake."""

    @property
    def messages(self) -> Any: ...


class ResponseEngine:
    """Stateless wrapper around the Anthropic client.

    Construct once per process. All per-call state lives in MeetingContext.
    """

    def __init__(
        self,
        resume_cache: dict | None,
        client: _ClientLike | None = None,
        model: str = DEFAULT_MODEL,
        max_history_turns: int = DEFAULT_HISTORY_TURNS,
    ) -> None:
        self.resume_cache = resume_cache
        self.model = model
        self.max_history_turns = max_history_turns
        self._client = client  # lazy-init at first call if None

    # ----- factory --------------------------------------------------------
    @classmethod
    def from_default_cache(
        cls, cache_path: Path = DEFAULT_OUTPUT, **kwargs: Any
    ) -> "ResponseEngine":
        """Load resume JSON from the canonical path. Cache-miss is OK."""
        return cls(resume_cache=load_cache(cache_path), **kwargs)

    # ----- prompt assembly ------------------------------------------------
    def _build_system_blocks(self, ctx: MeetingContext) -> list[dict]:
        """Two cache-able blocks: role instructions + resume context.

        Both marked cache_control:ephemeral. Anthropic prompt caching uses
        the *prefix* of the system list as the cache key, so role-then-resume
        ordering means the role block stays cached even when the operator
        switches company (only the resume tail gets re-hashed).
        """
        blocks: list[dict] = [
            {
                "type": "text",
                "text": ROLE_INSTRUCTIONS,
                "cache_control": {"type": "ephemeral"},
            }
        ]
        resume_block = build_resume_block(self.resume_cache, ctx.company_key)
        if resume_block:
            blocks.append(
                {
                    "type": "text",
                    "text": resume_block,
                    "cache_control": {"type": "ephemeral"},
                }
            )
        return blocks

    def _build_messages(self, question: str, ctx: MeetingContext) -> list[dict]:
        """Conversation history + current question as user/assistant turns.

        We use real role turns (not a flattened user message) so the model
        treats prior suggestions as its own output. This improves coherence
        when the interviewer asks follow-ups ("can you elaborate on that?").
        """
        msgs: list[dict] = []
        for q, a in ctx.history[-self.max_history_turns :]:
            msgs.append({"role": "user", "content": q})
            if a:
                msgs.append({"role": "assistant", "content": a})
        msgs.append({"role": "user", "content": question})
        return msgs

    # ----- streaming call -------------------------------------------------
    def _ensure_client(self) -> _ClientLike:
        if self._client is not None:
            return self._client
        # Late import so tests can run without the package side-effecting.
        from anthropic import Anthropic  # type: ignore

        if not os.environ.get("ANTHROPIC_API_KEY"):
            raise RuntimeError(
                "ANTHROPIC_API_KEY not set - operator action required"
            )
        self._client = Anthropic()
        return self._client

    def stream_suggestion(
        self,
        question: str,
        ctx: MeetingContext | None = None,
    ) -> Iterator[str]:
        """Yield text chunks as they arrive from the model.

        Caller is responsible for surfacing them to the operator UI as fast
        as possible. Total wall-clock for first chunk should be sub-2s on
        a cache-warm call.
        """
        ctx = ctx or MeetingContext()
        client = self._ensure_client()
        system_blocks = self._build_system_blocks(ctx)
        messages = self._build_messages(question, ctx)

        with client.messages.stream(
            model=self.model,
            max_tokens=MAX_OUTPUT_TOKENS,
            system=system_blocks,
            messages=messages,
        ) as stream:
            for chunk in stream.text_stream():
                yield chunk

    def collect_suggestion(
        self,
        question: str,
        ctx: MeetingContext | None = None,
    ) -> tuple[str, float]:
        """Convenience wrapper - returns (full_text, elapsed_seconds)."""
        t0 = time.monotonic()
        parts = list(self.stream_suggestion(question, ctx))
        return "".join(parts), time.monotonic() - t0


__all__ = [
    "DEFAULT_MODEL",
    "MAX_OUTPUT_TOKENS",
    "MeetingContext",
    "ResponseEngine",
    "build_resume_block",
]
