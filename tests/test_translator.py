"""
test_translator.py

Unit tests for src/translator.py. No live Google calls.

Strategy:
  - Inject TranslatorConfig.rpc_override (a stub callable) to bypass network.
  - Test cache, opt-in gate, glossary version tag invalidation, batch, audit log.
  - One test exercises detect_language via override.

Run with:  pytest tests/test_translator.py -v
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

# Make src/ importable when running pytest from project root
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import translator as tr  # noqa: E402


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #


@pytest.fixture
def stub_rpc():
    """Echo-style stub: returns a deterministic 'translation' for a text."""
    calls: list[tuple[str, str, str | None]] = []

    def _rpc(text: str, target: str, source: str | None) -> str:
        calls.append((text, target, source))
        # Deterministic 'translation' so tests can assert on it
        return f"[{source or 'auto'}->{target}] {text}"

    _rpc.calls = calls  # type: ignore[attr-defined]
    return _rpc


@pytest.fixture
def cfg(tmp_path, stub_rpc):
    glossary = tmp_path / "g.yaml"
    glossary.write_text(
        "version: 1\nentries: []\n", encoding="utf-8"
    )
    return tr.TranslatorConfig(
        project_id="test-proj",
        target_language="en-US",
        source_language="pt-BR",
        opt_in=True,
        rpc_override=stub_rpc,
        glossary_path=glossary,
        cache_size=4,  # small so we can test eviction
    )


# --------------------------------------------------------------------------- #
# Opt-in gate (privacy)
# --------------------------------------------------------------------------- #


def test_translate_blocks_without_opt_in(cfg, stub_rpc):
    cfg.opt_in = False
    t = tr.Translator(cfg)
    with pytest.raises(PermissionError):
        t.translate("Olá")
    assert stub_rpc.calls == []  # nothing leaked


def test_detect_language_blocks_without_opt_in(cfg):
    cfg.opt_in = False
    t = tr.Translator(cfg)
    with pytest.raises(PermissionError):
        t.detect_language("Olá")


# --------------------------------------------------------------------------- #
# Happy path
# --------------------------------------------------------------------------- #


def test_translate_happy_path(cfg, stub_rpc):
    t = tr.Translator(cfg)
    r = t.translate("Olá, tudo bem?")
    assert r.text == "[pt-BR->en-US] Olá, tudo bem?"
    assert r.source_language == "pt-BR"
    assert r.target_language == "en-US"
    assert r.cache_hit is False
    assert r.elapsed_ms >= 0.0
    assert len(stub_rpc.calls) == 1


def test_translate_empty_string_returns_empty(cfg, stub_rpc):
    t = tr.Translator(cfg)
    r = t.translate("")
    assert r.text == ""
    assert r.cache_hit is False
    assert stub_rpc.calls == []


def test_translate_whitespace_only_returns_empty(cfg, stub_rpc):
    t = tr.Translator(cfg)
    r = t.translate("   \t\n  ")
    assert r.text == ""
    assert stub_rpc.calls == []


# --------------------------------------------------------------------------- #
# Cache
# --------------------------------------------------------------------------- #


def test_cache_hit_second_call(cfg, stub_rpc):
    t = tr.Translator(cfg)
    t.translate("Olá")
    r2 = t.translate("Olá")
    assert r2.cache_hit is True
    assert r2.text == "[pt-BR->en-US] Olá"
    assert len(stub_rpc.calls) == 1  # second call did not hit the RPC


def test_cache_evicts_lru(cfg, stub_rpc):
    t = tr.Translator(cfg)
    # cache_size=4, push 5 distinct entries; first one must evict
    for i in range(5):
        t.translate(f"frase {i}")
    stats = t.cache_stats()
    assert stats["size"] == 4
    # The very first phrase is gone; re-translating it should miss
    before = len(stub_rpc.calls)
    t.translate("frase 0")
    assert len(stub_rpc.calls) == before + 1


def test_cache_key_distinguishes_target_language(cfg, stub_rpc):
    t = tr.Translator(cfg)
    t.translate("Olá")
    t.config.target_language = "es-ES"
    r = t.translate("Olá")
    assert r.cache_hit is False  # different target -> different key
    assert len(stub_rpc.calls) == 2


def test_cache_invalidates_on_glossary_change(tmp_path, stub_rpc):
    g = tmp_path / "g.yaml"
    g.write_text("version: 1\nentries: []\n", encoding="utf-8")
    cfg = tr.TranslatorConfig(
        project_id="p",
        opt_in=True,
        rpc_override=stub_rpc,
        glossary_path=g,
    )
    t1 = tr.Translator(cfg)
    t1.translate("Hookwall")

    # Operator edits glossary -> rebuild Translator; cache key must differ
    g.write_text(
        "version: 2\n"
        "entries:\n"
        "  - term: Hookwall\n"
        "    translations: { en-US: noop }\n",
        encoding="utf-8",
    )
    t2 = tr.Translator(cfg)
    r = t2.translate("Hookwall")
    assert r.cache_hit is False  # fresh instance + different glossary tag


# --------------------------------------------------------------------------- #
# Glossary
# --------------------------------------------------------------------------- #


def test_load_glossary_missing_file_is_graceful(tmp_path):
    p = tmp_path / "nonexistent.yaml"
    g = tr.load_glossary(p)
    assert g == {"version": 0, "entries": [], "locales": []}


def test_load_glossary_parses_seed_file():
    # Real seed file ships with the project; load it
    p = Path(__file__).resolve().parent.parent / "data" / "tech-glossary.yaml"
    if not p.exists():
        pytest.skip("seed glossary missing in this checkout")
    g = tr.load_glossary(p)
    assert g["version"] == 1
    terms = {e["term"] for e in g["entries"]}
    assert "Hookwall" in terms
    assert "BEHCS" in terms


def test_glossary_version_tag_changes_with_content(tmp_path):
    g1 = {"entries": [{"term": "A", "translations": {"en-US": "A"}}]}
    g2 = {"entries": [{"term": "A", "translations": {"en-US": "B"}}]}
    assert tr.glossary_version_tag(g1) != tr.glossary_version_tag(g2)


# --------------------------------------------------------------------------- #
# Batch + audit
# --------------------------------------------------------------------------- #


def test_translate_batch(cfg, stub_rpc):
    t = tr.Translator(cfg)
    rs = t.translate_batch(["um", "dois", "três"])
    assert [r.text for r in rs] == [
        "[pt-BR->en-US] um",
        "[pt-BR->en-US] dois",
        "[pt-BR->en-US] três",
    ]
    assert len(stub_rpc.calls) == 3


def test_audit_log_records_each_send(cfg, stub_rpc):
    t = tr.Translator(cfg)
    t.translate("Olá")
    t.translate("Mundo")
    t.translate("Olá")  # cache hit; should NOT add audit row
    assert len(t.audit_log) == 2
    assert all("ts" in row and "chars" in row for row in t.audit_log)


# --------------------------------------------------------------------------- #
# detect_language
# --------------------------------------------------------------------------- #


def test_detect_language_uses_override(cfg, stub_rpc):
    t = tr.Translator(cfg)
    lang = t.detect_language("Olá")
    # Because we set source_language='pt-BR' on cfg, that wins in stub-mode
    assert lang == "pt-BR"


def test_detect_language_empty_returns_und(cfg):
    t = tr.Translator(cfg)
    assert t.detect_language("   ") == "und"


# --------------------------------------------------------------------------- #
# Client construction (no network; just verifies error path)
# --------------------------------------------------------------------------- #


def test_build_client_without_lib_raises(monkeypatch, cfg):
    """If google-cloud-translate isn't installed, surface a clean RuntimeError."""
    cfg.rpc_override = None  # force real path

    real_import = __import__

    def _fake_import(name, *args, **kwargs):
        if name == "google.cloud" or name.startswith("google.cloud"):
            raise ImportError("simulated missing dep")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr("builtins.__import__", _fake_import)
    t = tr.Translator(cfg)
    with pytest.raises(RuntimeError, match="google-cloud-translate not installed"):
        t.translate("Olá")


def test_translate_requires_project_id(cfg):
    cfg.rpc_override = None
    cfg.project_id = None
    t = tr.Translator(cfg)
    # Fake the lazy import so we get past it and trip the project_id check
    t._client = SimpleNamespace(translate_text=lambda **kw: None)
    with pytest.raises(RuntimeError, match="project_id is required"):
        t.translate("Olá")
