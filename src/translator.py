"""
translator.py

Real-time interview copilot translator wrapping Google Cloud Translation v3.

Design constraints (from agent-B04 spec):
  - p99 latency < 300ms per chunk (target; the v3 RPC is the dominant cost)
  - Source language auto-detected (Translate v3 detectLanguage)
  - Target language operator-configurable (default en-US)
  - Glossary support backed by data/tech-glossary.yaml
  - In-memory LRU cache, size 1000, keyed by sha256(text|source|target|glossary_version)
  - Reviewable, mockable, NO live API call when imported or under pytest

Authentication is selected at construction time:
  - GOOGLE_APPLICATION_CREDENTIALS=<path/to/sa.json>  (preferred, full v3)
  - GOOGLE_TRANSLATE_API_KEY=<key>                    (limited; falls back to v3 REST)

Operator privacy contract:
  - .translate() refuses to send if config.opt_in is False
  - All sends are accounted in self.audit_log (in-memory; persistence is
    Hookwall / GULP drain's job, not this module's)

This module DOES NOT hit the network at import time. The Google client is
constructed lazily inside _client() so tests can monkeypatch it cleanly.
"""

from __future__ import annotations

import hashlib
import logging
import os
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Optional

try:
    import yaml  # type: ignore
except ImportError:  # pragma: no cover - exercised only without yaml installed
    yaml = None  # sentinel; load_glossary raises a clear error if used

LOG = logging.getLogger(__name__)

DEFAULT_TARGET = "en-US"
DEFAULT_PROJECT_LOCATION = "global"
CACHE_DEFAULT_SIZE = 1000
GLOSSARY_DEFAULT_PATH = Path(__file__).resolve().parent.parent / "data" / "tech-glossary.yaml"


# --------------------------------------------------------------------------- #
# Data classes
# --------------------------------------------------------------------------- #


@dataclass
class TranslatorConfig:
    """Operator-tunable configuration. All fields safe to serialise."""

    project_id: Optional[str] = None
    location: str = DEFAULT_PROJECT_LOCATION
    target_language: str = DEFAULT_TARGET
    source_language: Optional[str] = None  # None => auto-detect
    glossary_path: Path = GLOSSARY_DEFAULT_PATH
    glossary_id: Optional[str] = None  # registered Translate v3 glossary resource id
    api_key: Optional[str] = None  # if set, REST + key auth path is used
    service_account_json: Optional[str] = None  # path; overrides GOOGLE_APPLICATION_CREDENTIALS
    opt_in: bool = False  # operator MUST flip this before any send
    cache_size: int = CACHE_DEFAULT_SIZE
    request_timeout_s: float = 1.5  # network timeout; tighter than perceived 300ms cap
    # Test/observability hook: when set, .translate() routes through this callable
    # instead of constructing a real client. Used by tests and the dry-run CLI.
    rpc_override: Optional[Callable[[str, str, Optional[str]], str]] = field(default=None, repr=False)


@dataclass
class TranslationResult:
    text: str
    source_language: str
    target_language: str
    glossary_used: bool
    cache_hit: bool
    elapsed_ms: float


# --------------------------------------------------------------------------- #
# LRU cache (stdlib-only so it survives in minimal envs)
# --------------------------------------------------------------------------- #


class _LRU:
    """Tiny LRU. functools.lru_cache won't work because keys must be sha-able."""

    def __init__(self, maxsize: int) -> None:
        self.maxsize = maxsize
        self._store: "OrderedDict[str, str]" = OrderedDict()
        self.hits = 0
        self.misses = 0

    def get(self, key: str) -> Optional[str]:
        if key in self._store:
            self._store.move_to_end(key)
            self.hits += 1
            return self._store[key]
        self.misses += 1
        return None

    def put(self, key: str, value: str) -> None:
        if key in self._store:
            self._store.move_to_end(key)
        self._store[key] = value
        if len(self._store) > self.maxsize:
            self._store.popitem(last=False)

    def __len__(self) -> int:
        return len(self._store)


# --------------------------------------------------------------------------- #
# Glossary loader
# --------------------------------------------------------------------------- #


def load_glossary(path: Path) -> dict[str, Any]:
    """Parse tech-glossary.yaml. Returns {} if missing (graceful)."""
    if not path.exists():
        LOG.warning("glossary file not found at %s; running without glossary", path)
        return {"version": 0, "entries": [], "locales": []}
    if yaml is None:
        raise RuntimeError(
            "PyYAML not installed; `pip install pyyaml` or pass an empty glossary"
        )
    with path.open("r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    if "entries" not in data:
        data["entries"] = []
    return data


def glossary_version_tag(glossary: dict[str, Any]) -> str:
    """Stable hash so cache invalidates when the operator edits the glossary."""
    body = repr(sorted([(e.get("term"), tuple(sorted((e.get("translations") or {}).items())))
                        for e in glossary.get("entries", [])]))
    return hashlib.sha256(body.encode("utf-8")).hexdigest()[:12]


# --------------------------------------------------------------------------- #
# Translator
# --------------------------------------------------------------------------- #


class Translator:
    """Real-time translator wrapping Google Cloud Translation v3.

    Lifecycle:
        t = Translator(TranslatorConfig(project_id="...", opt_in=True))
        result = t.translate("Olá, tudo bem?")
        # result.text == "Hello, how are you?"

    Hot path is .translate(); it short-circuits on cache, otherwise dispatches
    to the (lazily-built) Google client OR the rpc_override hook (used in tests).
    """

    def __init__(self, config: TranslatorConfig) -> None:
        self.config = config
        self.glossary = load_glossary(config.glossary_path)
        self._glossary_tag = glossary_version_tag(self.glossary)
        self._cache = _LRU(config.cache_size)
        self._client: Any = None  # lazy
        self.audit_log: list[dict[str, Any]] = []

    # ------------------------------------------------------------------ #
    # Public API
    # ------------------------------------------------------------------ #

    def translate(self, text: str) -> TranslationResult:
        """Translate a single chunk. Hot path."""
        if not self.config.opt_in:
            raise PermissionError(
                "Translator.opt_in is False; operator has not consented to send "
                "text off-device. Set config.opt_in=True to enable."
            )
        text = (text or "").strip()
        if not text:
            return TranslationResult(
                text="",
                source_language=self.config.source_language or "und",
                target_language=self.config.target_language,
                glossary_used=False,
                cache_hit=False,
                elapsed_ms=0.0,
            )

        cache_key = self._cache_key(text)
        cached = self._cache.get(cache_key)
        if cached is not None:
            return TranslationResult(
                text=cached,
                source_language=self.config.source_language or "auto",
                target_language=self.config.target_language,
                glossary_used=bool(self.config.glossary_id),
                cache_hit=True,
                elapsed_ms=0.0,
            )

        t0 = time.perf_counter()
        translated, detected = self._dispatch(text)
        elapsed_ms = (time.perf_counter() - t0) * 1000.0

        self._cache.put(cache_key, translated)
        self.audit_log.append({
            "ts": time.time(),
            "chars": len(text),
            "source": detected,
            "target": self.config.target_language,
            "elapsed_ms": elapsed_ms,
            "glossary_used": bool(self.config.glossary_id),
        })
        return TranslationResult(
            text=translated,
            source_language=detected,
            target_language=self.config.target_language,
            glossary_used=bool(self.config.glossary_id),
            cache_hit=False,
            elapsed_ms=elapsed_ms,
        )

    def translate_batch(self, texts: Iterable[str]) -> list[TranslationResult]:
        """Naive batch: loops .translate(). Real v3 batch API is a follow-up."""
        return [self.translate(t) for t in texts]

    def detect_language(self, text: str) -> str:
        """Return ISO-639-1 of detected language (or 'und' if undetermined)."""
        if not self.config.opt_in:
            raise PermissionError("opt_in required for detect_language")
        if not text.strip():
            return "und"
        if self.config.rpc_override is not None:
            # In tests, the override is sufficient: detect implied by source field.
            _ = self.config.rpc_override(text, self.config.target_language, None)
            return self.config.source_language or "auto"
        client, parent = self._client_and_parent()
        resp = client.detect_language(
            parent=parent,
            content=text,
            mime_type="text/plain",
            timeout=self.config.request_timeout_s,
        )
        langs = list(resp.languages or [])
        return langs[0].language_code if langs else "und"

    def cache_stats(self) -> dict[str, int]:
        return {"size": len(self._cache), "hits": self._cache.hits, "misses": self._cache.misses,
                "maxsize": self.config.cache_size}

    # ------------------------------------------------------------------ #
    # Internals
    # ------------------------------------------------------------------ #

    def _cache_key(self, text: str) -> str:
        h = hashlib.sha256()
        h.update(text.encode("utf-8"))
        h.update(b"|")
        h.update((self.config.source_language or "auto").encode("utf-8"))
        h.update(b"|")
        h.update(self.config.target_language.encode("utf-8"))
        h.update(b"|")
        h.update(self._glossary_tag.encode("utf-8"))
        return h.hexdigest()

    def _dispatch(self, text: str) -> tuple[str, str]:
        """Returns (translated_text, detected_source_language)."""
        if self.config.rpc_override is not None:
            translated = self.config.rpc_override(
                text, self.config.target_language, self.config.source_language
            )
            return translated, self.config.source_language or "auto"

        client, parent = self._client_and_parent()
        request: dict[str, Any] = {
            "parent": parent,
            "contents": [text],
            "mime_type": "text/plain",
            "target_language_code": self.config.target_language,
        }
        if self.config.source_language:
            request["source_language_code"] = self.config.source_language
        if self.config.glossary_id:
            request["glossary_config"] = {"glossary": self.config.glossary_id}

        response = client.translate_text(request=request, timeout=self.config.request_timeout_s)
        if self.config.glossary_id and getattr(response, "glossary_translations", None):
            translation = response.glossary_translations[0]
        else:
            translation = response.translations[0]
        return translation.translated_text, getattr(
            translation, "detected_language_code", self.config.source_language or "auto"
        )

    def _client_and_parent(self) -> tuple[Any, str]:
        if self._client is None:
            self._client = self._build_client()
        if not self.config.project_id:
            raise RuntimeError(
                "TranslatorConfig.project_id is required (Google Cloud project id)"
            )
        parent = f"projects/{self.config.project_id}/locations/{self.config.location}"
        return self._client, parent

    def _build_client(self) -> Any:
        """Lazily import + construct Translate v3 client.

        Two auth paths:
          1. service-account JSON via GOOGLE_APPLICATION_CREDENTIALS or config.service_account_json
          2. API key (limited; uses google.api_core ClientOptions)
        """
        # Resolve credentials env var if a path was passed via config
        if self.config.service_account_json:
            os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = self.config.service_account_json

        try:
            from google.cloud import translate_v3 as translate  # type: ignore
        except ImportError as e:
            raise RuntimeError(
                "google-cloud-translate not installed. "
                "Run: pip install google-cloud-translate"
            ) from e

        if self.config.api_key:
            try:
                from google.api_core.client_options import ClientOptions  # type: ignore
            except ImportError as e:
                raise RuntimeError("google-api-core required for api-key auth") from e
            return translate.TranslationServiceClient(
                client_options=ClientOptions(api_key=self.config.api_key)
            )
        # ADC / service-account JSON path
        return translate.TranslationServiceClient()


# --------------------------------------------------------------------------- #
# CLI entry (manual dry-run only; not used by interview hot path)
# --------------------------------------------------------------------------- #


def _cli() -> int:  # pragma: no cover
    import argparse, json

    p = argparse.ArgumentParser("translator")
    p.add_argument("text", nargs="?", default="")
    p.add_argument("--target", default=DEFAULT_TARGET)
    p.add_argument("--source", default=None)
    p.add_argument("--project", default=os.environ.get("GOOGLE_CLOUD_PROJECT"))
    p.add_argument("--dry-run", action="store_true",
                   help="Skip Google call; echo input with a debug prefix")
    p.add_argument("--opt-in", action="store_true",
                   help="Required to send text off-device")
    args = p.parse_args()

    override = None
    if args.dry_run:
        override = lambda t, tgt, src: f"[dry-run {src or 'auto'}->{tgt}] {t}"

    cfg = TranslatorConfig(
        project_id=args.project,
        target_language=args.target,
        source_language=args.source,
        opt_in=args.opt_in or args.dry_run,
        rpc_override=override,
    )
    t = Translator(cfg)
    res = t.translate(args.text)
    print(json.dumps(res.__dict__, default=str, indent=2))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(_cli())
