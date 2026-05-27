"""
test_response_engine.py - agent B07-ResponseEngine - 2026-05-27
================================================================

Unit tests for resume_extractor + response_engine.

Run from repo root:
    python -m pytest D:/interview/copilot-v2-2026-05-27/tests/ -v

We mock the Anthropic client entirely - these tests must NEVER make real
network calls. The fake client records each call so we can assert prompt
structure, caching headers, and message ordering.
"""

from __future__ import annotations

import json
import sys
import types
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

# Add src/ to sys.path so we can `import src.response_engine as ...`
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src import resume_extractor as rx  # noqa: E402
from src.response_engine import (  # noqa: E402
    DEFAULT_MODEL,
    MAX_OUTPUT_TOKENS,
    MeetingContext,
    ResponseEngine,
    build_resume_block,
)


# ---------------------------------------------------------------------------
# Fake Anthropic client - records every call, yields scripted chunks.
# ---------------------------------------------------------------------------


@dataclass
class _FakeStream:
    chunks: list[str]

    def __enter__(self) -> "_FakeStream":
        return self

    def __exit__(self, *a: Any) -> None:
        return None

    def text_stream(self):
        for c in self.chunks:
            yield c


@dataclass
class _FakeMessages:
    calls: list[dict] = field(default_factory=list)
    scripted_chunks: list[str] = field(
        default_factory=lambda: ["- bullet 1\n", "- bullet 2"]
    )

    def stream(self, **kwargs: Any) -> _FakeStream:
        self.calls.append(kwargs)
        return _FakeStream(self.scripted_chunks)


@dataclass
class _FakeClient:
    messages: _FakeMessages = field(default_factory=_FakeMessages)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def mini_cache() -> dict:
    return {
        "extracted_at": "2026-05-27T00:00:00Z",
        "schema_version": 1,
        "source_dir": "/fake",
        "resumes": {
            "capital-rx": {
                "source_file": "Capital Rx.docx",
                "company_display": "Capital Rx",
                "char_count": 100,
                "paragraph_count": 5,
                "text": "JESSE BROWN\nSenior Full-Stack Developer\nPython, Flask, PostgreSQL",
            }
        },
        "skipped": [],
        "missing_from_mission": [],
    }


@pytest.fixture()
def engine(mini_cache) -> ResponseEngine:
    return ResponseEngine(resume_cache=mini_cache, client=_FakeClient())


# ---------------------------------------------------------------------------
# resume_extractor
# ---------------------------------------------------------------------------


class TestResumeExtractor:
    def test_slugify_basic(self) -> None:
        assert rx._slugify("Capital Rx") == "capital-rx"
        assert rx._slugify("Inductive Automation ") == "inductive-automation"
        assert rx._slugify("HappyCo") == "happyco"

    def test_truncate_to_budget_under_limit(self) -> None:
        from src.response_engine import _truncate_to_budget

        assert _truncate_to_budget("short", 100) == "short"

    def test_truncate_to_budget_over_limit(self) -> None:
        from src.response_engine import _truncate_to_budget

        text = "line a\nline b\nline c\nline d\nline e"
        out = _truncate_to_budget(text, 15)
        # Result must be within budget and a prefix of the original.
        assert len(out) <= 15
        assert text.startswith(out)
        # Must NOT end mid-line (last char before the cut is a \n boundary
        # OR the result has no \n at all because we cut before the first).
        assert not out.endswith("\n")

    def test_discover_docx_filters_by_company(self, tmp_path: Path) -> None:
        (tmp_path / "Capital Rx.docx").touch()
        (tmp_path / "Eshyft.docx").touch()
        (tmp_path / "RandomOther.docx").touch()
        out = rx.discover_docx(tmp_path, ["Capital Rx", "Eshyft"])
        names = sorted(p.name for p in out)
        assert names == ["Capital Rx.docx", "Eshyft.docx"]

    def test_discover_docx_handles_trailing_space(self, tmp_path: Path) -> None:
        (tmp_path / "Inductive Automation .docx").touch()
        out = rx.discover_docx(tmp_path, ["Inductive Automation"])
        assert len(out) == 1
        assert out[0].name == "Inductive Automation .docx"

    def test_discover_docx_no_filter_returns_all(self, tmp_path: Path) -> None:
        (tmp_path / "A.docx").touch()
        (tmp_path / "B.docx").touch()
        out = rx.discover_docx(tmp_path, None)
        assert len(out) == 2

    def test_discover_docx_missing_dir_returns_empty(self) -> None:
        assert rx.discover_docx(Path("/nonexistent/path/zzz"), None) == []

    def test_load_cache_missing_returns_none(self, tmp_path: Path) -> None:
        assert rx.load_cache(tmp_path / "nope.json") is None

    def test_load_cache_corrupt_returns_none(self, tmp_path: Path) -> None:
        p = tmp_path / "bad.json"
        p.write_text("{not json")
        assert rx.load_cache(p) is None

    def test_load_cache_schema_mismatch_returns_none(self, tmp_path: Path) -> None:
        p = tmp_path / "old.json"
        p.write_text(json.dumps({"schema_version": 999, "resumes": {}}))
        assert rx.load_cache(p) is None

    def test_write_then_load_roundtrip(self, tmp_path: Path, mini_cache) -> None:
        out = tmp_path / "cache.json"
        rx.write_cache(mini_cache, out)
        loaded = rx.load_cache(out)
        assert loaded is not None
        assert loaded["resumes"]["capital-rx"]["company_display"] == "Capital Rx"

    def test_build_cache_empty_dir(self, tmp_path: Path) -> None:
        cache = rx.build_cache(source_dir=tmp_path, company_filter=["X"])
        assert cache["resumes"] == {}
        assert "X" in cache["missing_from_mission"]


# ---------------------------------------------------------------------------
# build_resume_block (pure function)
# ---------------------------------------------------------------------------


class TestResumeBlock:
    def test_no_cache_returns_empty(self) -> None:
        assert build_resume_block(None, "capital-rx") == ""

    def test_no_company_returns_empty(self, mini_cache) -> None:
        assert build_resume_block(mini_cache, None) == ""

    def test_unknown_company_returns_empty(self, mini_cache) -> None:
        assert build_resume_block(mini_cache, "no-such-company") == ""

    def test_known_company_includes_display_and_text(self, mini_cache) -> None:
        out = build_resume_block(mini_cache, "capital-rx")
        assert "OPERATOR RESUME (tailored for Capital Rx)" in out
        assert "JESSE BROWN" in out
        assert "PostgreSQL" in out


# ---------------------------------------------------------------------------
# ResponseEngine - prompt assembly
# ---------------------------------------------------------------------------


class TestPromptAssembly:
    def test_system_blocks_role_first(self, engine, mini_cache) -> None:
        ctx = MeetingContext(company_key="capital-rx")
        blocks = engine._build_system_blocks(ctx)
        assert len(blocks) == 2
        assert "interview coach" in blocks[0]["text"]
        assert blocks[0]["cache_control"] == {"type": "ephemeral"}
        assert "Capital Rx" in blocks[1]["text"]
        assert blocks[1]["cache_control"] == {"type": "ephemeral"}

    def test_system_blocks_no_resume_when_unknown_company(self, engine) -> None:
        ctx = MeetingContext(company_key=None)
        blocks = engine._build_system_blocks(ctx)
        assert len(blocks) == 1  # role only
        assert "interview coach" in blocks[0]["text"]

    def test_messages_includes_current_question(self, engine) -> None:
        ctx = MeetingContext()
        msgs = engine._build_messages("what's your favorite db?", ctx)
        assert msgs[-1] == {"role": "user", "content": "what's your favorite db?"}

    def test_messages_includes_history_in_order(self, engine) -> None:
        ctx = MeetingContext(history=[("Q1?", "A1"), ("Q2?", "A2")])
        msgs = engine._build_messages("Q3?", ctx)
        assert msgs == [
            {"role": "user", "content": "Q1?"},
            {"role": "assistant", "content": "A1"},
            {"role": "user", "content": "Q2?"},
            {"role": "assistant", "content": "A2"},
            {"role": "user", "content": "Q3?"},
        ]

    def test_messages_truncates_to_max_history_turns(self, mini_cache) -> None:
        engine = ResponseEngine(
            resume_cache=mini_cache,
            client=_FakeClient(),
            max_history_turns=2,
        )
        ctx = MeetingContext(
            history=[
                ("Q1?", "A1"),
                ("Q2?", "A2"),
                ("Q3?", "A3"),
                ("Q4?", "A4"),
            ]
        )
        msgs = engine._build_messages("Q5?", ctx)
        # Should only keep last 2 history turns + current Q = 5 messages
        assert len(msgs) == 5
        assert msgs[0]["content"] == "Q3?"
        assert msgs[-1]["content"] == "Q5?"

    def test_messages_skips_assistant_for_empty_answer(self, engine) -> None:
        ctx = MeetingContext(history=[("Q1?", "")])
        msgs = engine._build_messages("Q2?", ctx)
        # empty assistant turn dropped
        assert msgs == [
            {"role": "user", "content": "Q1?"},
            {"role": "user", "content": "Q2?"},
        ]


# ---------------------------------------------------------------------------
# ResponseEngine - streaming integration (with fake client)
# ---------------------------------------------------------------------------


class TestStreaming:
    def test_stream_yields_chunks(self, engine) -> None:
        ctx = MeetingContext(company_key="capital-rx")
        out = list(engine.stream_suggestion("Tell me about Python.", ctx))
        assert out == ["- bullet 1\n", "- bullet 2"]

    def test_stream_passes_model_and_max_tokens(self, engine) -> None:
        list(engine.stream_suggestion("Q?", MeetingContext()))
        call = engine._client.messages.calls[0]
        assert call["model"] == DEFAULT_MODEL
        assert call["max_tokens"] == MAX_OUTPUT_TOKENS

    def test_stream_passes_system_blocks_with_cache_control(self, engine) -> None:
        list(
            engine.stream_suggestion(
                "Q?", MeetingContext(company_key="capital-rx")
            )
        )
        call = engine._client.messages.calls[0]
        system = call["system"]
        assert isinstance(system, list)
        assert all("cache_control" in b for b in system)
        assert all(b["cache_control"]["type"] == "ephemeral" for b in system)

    def test_stream_passes_messages_in_order(self, engine) -> None:
        ctx = MeetingContext(history=[("hi", "hello")])
        list(engine.stream_suggestion("how are you?", ctx))
        call = engine._client.messages.calls[0]
        assert call["messages"][0]["content"] == "hi"
        assert call["messages"][-1]["content"] == "how are you?"

    def test_collect_returns_joined_text_and_elapsed(self, engine) -> None:
        text, elapsed = engine.collect_suggestion("Q?", MeetingContext())
        assert text == "- bullet 1\n- bullet 2"
        assert elapsed >= 0.0

    def test_engine_raises_without_api_key(
        self, mini_cache, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        eng = ResponseEngine(resume_cache=mini_cache)  # no client injected
        # Stub the late import so we don't even try to construct a real client
        fake_anthropic = types.ModuleType("anthropic")

        class _StubAnthropic:
            def __init__(self, *a: Any, **kw: Any) -> None:
                raise AssertionError("should not be reached - env-check first")

        fake_anthropic.Anthropic = _StubAnthropic  # type: ignore[attr-defined]
        monkeypatch.setitem(sys.modules, "anthropic", fake_anthropic)
        with pytest.raises(RuntimeError, match="ANTHROPIC_API_KEY"):
            list(eng.stream_suggestion("Q?", MeetingContext()))


# ---------------------------------------------------------------------------
# Factory + cache loading
# ---------------------------------------------------------------------------


class TestFactory:
    def test_from_default_cache_with_missing_file(
        self, tmp_path: Path
    ) -> None:
        eng = ResponseEngine.from_default_cache(
            cache_path=tmp_path / "nope.json", client=_FakeClient()
        )
        assert eng.resume_cache is None  # graceful no-cache
        # Should still produce a system block (role only)
        blocks = eng._build_system_blocks(MeetingContext(company_key="x"))
        assert len(blocks) == 1
