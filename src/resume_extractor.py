"""
resume_extractor.py - agent B07-ResponseEngine - 2026-05-27
============================================================

One-time extraction of operator's tailored resume DOCX files into a JSON
cache consumed by response_engine.py.

Why a cache:
- docx parsing is slow (~50-200ms per file with python-docx) and we want
  sub-2s first-token latency end-to-end.
- The interview copilot runs *during* a live meeting; we cannot afford a
  cold-start parse on the hot path.
- Resumes change rarely (per-company tailoring before the interview),
  so re-extraction is a deliberate operator action, not an automatic one.

Inputs:
  - DOCX files at D:/interview - resume/<Company>.docx
    (one tailored resume per target company)

Output:
  - JSON cache at D:/interview/copilot-v2-2026-05-27/data/resume-context.json
    {
      "extracted_at": <iso8601>,
      "schema_version": 1,
      "source_dir": "D:/interview - resume",
      "resumes": {
        "<company_slug>": {
          "source_file": "<filename>",
          "company_display": "<display name>",
          "char_count": <int>,
          "paragraph_count": <int>,
          "text": "<full concatenated text>"
        },
        ...
      }
    }

Run as a one-shot:
    python -m resume_extractor

Honest gaps:
- PDF resumes (Enzo Tech, X4Life Sciences, persistent) are NOT covered by
  this extractor. python-docx cannot read PDF. The mission scope listed
  Enzo Tech as docx but on disk it is .pdf — surfaced in the report.
- Tables inside docx are extracted via .tables iter (most resumes are
  flat paragraphs, but Wipfli/Tangible sometimes use tables for layout).
"""

from __future__ import annotations

import json
import re
import sys
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

try:
    import docx  # python-docx
except ImportError:  # pragma: no cover - dependency check only
    print("python-docx not installed. pip install python-docx", file=sys.stderr)
    sys.exit(2)


SCHEMA_VERSION = 1

DEFAULT_SOURCE_DIR = Path("D:/interview - resume")
DEFAULT_OUTPUT = Path(
    "D:/interview/copilot-v2-2026-05-27/data/resume-context.json"
)

# Mission-listed docx resumes. PDF entries are skipped (logged as gaps).
MISSION_COMPANIES = [
    "Capital Rx",
    "Enzo Tech",       # actually .pdf on disk - surfaced as gap
    "Eshyft",
    "HappyCo",
    "Inductive Automation",
    "Tangible",
    "Wipfli",
]


@dataclass
class ResumeRecord:
    source_file: str
    company_display: str
    char_count: int
    paragraph_count: int
    text: str


def _slugify(name: str) -> str:
    """Lowercase, dash-separated, alnum-only company key."""
    s = name.strip().lower()
    s = re.sub(r"[^a-z0-9]+", "-", s)
    return s.strip("-")


def _iter_paragraph_texts(doc) -> Iterable[str]:
    """Yield non-empty paragraph + table-cell texts in document order.

    Tables are flattened cell-by-cell. We don't try to preserve table
    structure — the response engine only needs searchable text, not
    layout fidelity.
    """
    for para in doc.paragraphs:
        text = (para.text or "").strip()
        if text:
            yield text
    for table in getattr(doc, "tables", []):
        for row in table.rows:
            for cell in row.cells:
                cell_text = (cell.text or "").strip()
                if cell_text:
                    yield cell_text


def extract_one(docx_path: Path) -> ResumeRecord:
    """Parse a single DOCX into a ResumeRecord.

    Raises FileNotFoundError / docx.opc.exceptions.PackageNotFoundError
    on malformed input. Caller decides how to recover.
    """
    if not docx_path.is_file():
        raise FileNotFoundError(str(docx_path))
    doc = docx.Document(str(docx_path))
    paragraphs = list(_iter_paragraph_texts(doc))
    text = "\n".join(paragraphs)
    company_display = docx_path.stem.strip()
    return ResumeRecord(
        source_file=docx_path.name,
        company_display=company_display,
        char_count=len(text),
        paragraph_count=len(paragraphs),
        text=text,
    )


def discover_docx(source_dir: Path, company_filter: list[str] | None = None) -> list[Path]:
    """Return DOCX paths under source_dir, optionally filtered by display name.

    Filter is case-insensitive prefix match on the stem (handles the trailing
    space in 'Inductive Automation .docx').
    """
    if not source_dir.is_dir():
        return []
    all_docx = sorted(source_dir.glob("*.docx"))
    if not company_filter:
        return all_docx
    keys = [c.lower().strip() for c in company_filter]
    out = []
    for p in all_docx:
        stem_lc = p.stem.lower().strip()
        if any(stem_lc.startswith(k) for k in keys):
            out.append(p)
    return out


def build_cache(
    source_dir: Path = DEFAULT_SOURCE_DIR,
    company_filter: list[str] | None = MISSION_COMPANIES,
) -> dict:
    """Read all resumes and return the in-memory cache dict.

    Does NOT write to disk — caller handles persistence so tests can
    operate purely in memory.
    """
    docx_files = discover_docx(source_dir, company_filter)
    resumes: dict[str, dict] = {}
    skipped: list[dict] = []
    for path in docx_files:
        try:
            record = extract_one(path)
        except Exception as exc:  # noqa: BLE001 - top-level extractor must not crash
            skipped.append({"file": path.name, "error": str(exc)})
            continue
        key = _slugify(record.company_display)
        resumes[key] = asdict(record)

    # Detect missing companies (mission says docx but disk has pdf, etc.)
    missing: list[str] = []
    if company_filter:
        found_keys = {k.lower().strip() for k in resumes.keys()}
        found_stems = {p.stem.lower().strip() for p in docx_files}
        for c in company_filter:
            cl = c.lower().strip()
            if not any(s.startswith(cl) for s in found_stems):
                missing.append(c)

    return {
        "extracted_at": datetime.now(timezone.utc).isoformat(),
        "schema_version": SCHEMA_VERSION,
        "source_dir": str(source_dir),
        "resumes": resumes,
        "skipped": skipped,
        "missing_from_mission": missing,
    }


def write_cache(cache: dict, output_path: Path = DEFAULT_OUTPUT) -> Path:
    """Atomically write cache to disk. Creates parent dirs as needed."""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = output_path.with_suffix(output_path.suffix + ".tmp")
    tmp.write_text(json.dumps(cache, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(output_path)
    return output_path


def load_cache(path: Path = DEFAULT_OUTPUT) -> dict | None:
    """Return cache dict or None if missing/corrupt.

    response_engine consumers should treat None as 'no resume context'
    and fall back to a generic system prompt.
    """
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if data.get("schema_version") != SCHEMA_VERSION:
            return None
        return data
    except (OSError, json.JSONDecodeError):
        return None


def main(argv: list[str] | None = None) -> int:
    argv = argv or sys.argv[1:]
    source = DEFAULT_SOURCE_DIR
    output = DEFAULT_OUTPUT
    if argv:
        if argv[0] in ("-h", "--help"):
            print(__doc__)
            return 0
        source = Path(argv[0])
    if len(argv) > 1:
        output = Path(argv[1])

    cache = build_cache(source_dir=source)
    write_cache(cache, output)

    print(f"extracted {len(cache['resumes'])} resumes -> {output}")
    if cache["skipped"]:
        print(f"skipped {len(cache['skipped'])} (errors):")
        for s in cache["skipped"]:
            print(f"  - {s['file']}: {s['error']}")
    if cache["missing_from_mission"]:
        print("missing from mission list (not on disk as .docx):")
        for m in cache["missing_from_mission"]:
            print(f"  - {m}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
