"""Cortex page store — the ONLY read/write path for Cortex/ pages.

Design invariant (CortexMemory plan §2.1): the LLM never rewrites a
whole page. Machine state lives in frontmatter and is read/written
deterministically here; the body has four fixed sections operated on at
section level. Consolidation code mutates the CortexPage dataclass and
calls save_cortex_page() — nothing else touches the files.

Hard acceptance gate: parse_cortex_page(render) round-trips every field.
PyYAML eagerly converts ISO timestamps/dates into datetime objects, so
parsing coerces them back to strings — timestamps are opaque strings to
this layer.
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from core.parser import parse_markdown_metadata
from core.vault_utils import sanitize_filename

# Stable section headers — the deterministic parser's anchors. Never
# reword without a schema_version bump and a migration.
CORE_CLAIM_HEADER = "## Core Claim"
EVIDENCE_HEADER = "## Evidence"
VARIANTS_HEADER = "## Nuances & Variants"
COUNTERPOINTS_HEADER = "## Counterpoints"
_EMPTY_PLACEHOLDER = "- （尚無）"

_SECTION_SPLIT_RE = re.compile(r"^## ", re.MULTILINE)
_LIST_ITEM_RE = re.compile(r"^-\s+(.*)$")
_FILENAME_SANITIZE_RE = re.compile(r"[^\w一-鿿぀-ヿ\s-]", re.UNICODE)


@dataclass
class CortexPage:
    claim_id: str
    path: Path
    claim: str
    status: str = "active"
    confidence: float = 0.5
    falsifiability: float | None = None
    falsifier: str = ""
    applies_when: str = ""
    S: float = 1.0  # storage strength — float since Phase 3 spacing-effect gains
    last_reinforced_at: str = ""
    created: str = ""
    updated: str = ""
    evidence: list[dict] = field(default_factory=list)
    contradictions: list[str] = field(default_factory=list)
    related: list[str] = field(default_factory=list)
    variants: list[str] = field(default_factory=list)
    counterpoints: list[str] = field(default_factory=list)
    schema_version: int = 1


def make_claim_id(claim: str) -> str:
    return "cortex-" + hashlib.sha256(claim.strip().encode("utf-8")).hexdigest()[:16]


def claim_filename(claim: str, claim_id: str, cortex_dir: Path) -> Path:
    """Human-readable filename from the claim; claim_id suffix on collision."""
    # Reduce LaTeX math first ($\mathcal{L}^2$ → L2) so the char-filter keeps a
    # readable stem instead of leaking the command name (…→ "mathcalL2").
    base = _FILENAME_SANITIZE_RE.sub("", sanitize_filename(claim)).strip()
    base = re.sub(r"\s+", " ", base)[:60].strip() or claim_id
    candidate = cortex_dir / f"{base}.md"
    if candidate.exists():
        candidate = cortex_dir / f"{base} ({claim_id[-6:]}).md"
    return candidate


def _as_str(value) -> str:
    """Timestamps are opaque strings here; undo PyYAML's eager parsing."""
    if isinstance(value, (_dt.datetime, _dt.date)):
        return value.isoformat()
    return "" if value is None else str(value)


def _as_str_list(value) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(v) for v in value if v is not None]


def _coerce_evidence(value) -> list[dict]:
    out = []
    if not isinstance(value, list):
        return out
    for item in value:
        if not isinstance(item, dict):
            continue
        out.append(
            {
                "insight": _as_str(item.get("insight")),
                "sources": _as_str_list(item.get("sources")),
                "date": _as_str(item.get("date")),
                "summary": _as_str(item.get("summary")),
            }
        )
    return out


def render_cortex_page(page: CortexPage) -> str:
    frontmatter = {
        "claim_id": page.claim_id,
        "status": page.status,
        "confidence": round(float(page.confidence), 4),
        "falsifiability": round(float(page.falsifiability), 4)
        if page.falsifiability is not None
        else None,
        "falsifier": page.falsifier,
        "S": round(float(page.S), 4),
        "last_reinforced_at": page.last_reinforced_at,
        "created": page.created,
        "updated": page.updated,
        "evidence": page.evidence,
        "contradictions": page.contradictions,
        "related": page.related,
        "schema_version": page.schema_version,
    }
    yaml_block = yaml.safe_dump(
        frontmatter, allow_unicode=True, sort_keys=False, default_flow_style=False
    ).strip()

    evidence_lines = [
        f"- [[{e['insight']}]]（{e['date']}）：{e['summary']} — 來源："
        + "、".join(f"[[{s}]]" for s in e["sources"])
        for e in page.evidence
    ] or [_EMPTY_PLACEHOLDER]
    variants_lines = [f"- {v}" for v in page.variants] or [_EMPTY_PLACEHOLDER]
    counterpoints_lines = [f"- {c}" for c in page.counterpoints] or [_EMPTY_PLACEHOLDER]

    core_claim_lines = [page.claim]
    if page.applies_when:
        core_claim_lines.append(f"> 適用情境：{page.applies_when}")

    body = "\n".join(
        [
            f"# {page.claim[:60]}",
            "",
            CORE_CLAIM_HEADER,
            *core_claim_lines,
            "",
            EVIDENCE_HEADER,
            *evidence_lines,
            "",
            VARIANTS_HEADER,
            *variants_lines,
            "",
            COUNTERPOINTS_HEADER,
            *counterpoints_lines,
            "",
        ]
    )
    return f"---\n{yaml_block}\n---\n\n{body}"


def _section_items(section_text: str) -> list[str]:
    items = []
    for line in section_text.splitlines():
        line = line.strip()
        if line == _EMPTY_PLACEHOLDER.strip():
            continue
        m = _LIST_ITEM_RE.match(line)
        if m:
            items.append(m.group(1).strip())
    return items


def _section_map(body: str) -> dict[str, str]:
    """Split the body on `## ` headers → {header_title: section_text}."""
    sections: dict[str, str] = {}
    parts = _SECTION_SPLIT_RE.split(body)
    for part in parts[1:]:
        title, _, rest = part.partition("\n")
        sections["## " + title.strip()] = rest
    return sections


def parse_cortex_page(path: Path) -> CortexPage | None:
    """Parse one Cortex page. Returns None (with a warning) on bad files."""
    try:
        text = path.read_text(encoding="utf-8")
    except Exception as e:
        logging.warning(f"CortexStore: cannot read {path.name}: {e}")
        return None

    meta = parse_markdown_metadata(text)
    if not meta.get("claim_id"):
        logging.warning(f"CortexStore: {path.name} has no claim_id; skipping")
        return None

    sections = _section_map(text)
    claim_section = sections.get(CORE_CLAIM_HEADER, "").strip()
    if not claim_section:
        logging.warning(f"CortexStore: {path.name} has no Core Claim section; skipping")
        return None

    claim_text = ""
    applies_when_text = ""
    for line in claim_section.splitlines():
        line_s = line.strip()
        if not line_s:
            continue
        if line_s.startswith("> 適用情境："):
            applies_when_text = line_s[len("> 適用情境：") :].strip()
        elif not claim_text and not line_s.startswith(">"):
            claim_text = line_s

    if not claim_text:
        logging.warning(
            f"CortexStore: {path.name} has no valid claim in Core Claim section; skipping"
        )
        return None

    try:
        f_val = meta.get("falsifiability")
        return CortexPage(
            claim_id=str(meta["claim_id"]),
            path=path,
            claim=claim_text,
            status=str(meta.get("status") or "active"),
            confidence=float(meta.get("confidence", 0.5)),
            falsifiability=float(f_val) if f_val is not None else None,
            falsifier=str(meta.get("falsifier") or ""),
            applies_when=applies_when_text,
            S=float(meta.get("S", 1)),
            last_reinforced_at=_as_str(meta.get("last_reinforced_at")),
            created=_as_str(meta.get("created")),
            updated=_as_str(meta.get("updated")),
            evidence=_coerce_evidence(meta.get("evidence")),
            contradictions=_as_str_list(meta.get("contradictions")),
            related=_as_str_list(meta.get("related")),
            variants=_section_items(sections.get(VARIANTS_HEADER, "")),
            counterpoints=_section_items(sections.get(COUNTERPOINTS_HEADER, "")),
            schema_version=int(meta.get("schema_version", 1)),
        )
    except (TypeError, ValueError) as e:
        logging.warning(f"CortexStore: {path.name} has malformed fields: {e}")
        return None


def save_cortex_page(page: CortexPage) -> None:
    """Atomic write (temp + rename), per engineering conventions."""
    page.path.parent.mkdir(parents=True, exist_ok=True)
    rendered = render_cortex_page(page)
    tmp = page.path.with_name(page.path.name + ".tmp")
    tmp.write_text(rendered, encoding="utf-8")
    tmp.replace(page.path)


def load_all_pages(cortex_dir: Path) -> list[CortexPage]:
    if not cortex_dir.exists():
        return []
    pages = []
    for path in sorted(cortex_dir.glob("*.md")):
        if path.stem.startswith("_"):
            continue
        page = parse_cortex_page(path)
        if page is not None:
            pages.append(page)
    return pages
