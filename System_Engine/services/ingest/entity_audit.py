"""Read-only audit of persisted Part notes against the entity contract."""

from __future__ import annotations

from pathlib import Path

from core.parser import parse_markdown_metadata, strip_body_frontmatter
from services.ingest.entity_quality import assess_entity_body


def audit_part_page(path: Path) -> dict:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        return {"path": str(path), "status": "unreadable", "issues": [type(exc).__name__]}
    metadata = parse_markdown_metadata(text)
    body, _ = strip_body_frontmatter(text)
    quality = assess_entity_body(body)
    issues = [*quality.hard_issues, *quality.suspect_issues]
    ingest_status = metadata.get("ingest_status")
    if ingest_status == "pending_index":
        issues.insert(0, "pending_index")
    elif ingest_status not in (None, "complete"):
        issues.insert(0, "unknown_ingest_status")
    status = "needs_reprocess" if issues else ("complete" if ingest_status else "legacy_clean")
    return {
        "path": str(path),
        "status": status,
        "issues": issues,
        "ingest_status": ingest_status,
    }


def audit_part_tree(pages_dir: Path) -> dict:
    rows = [audit_part_page(path) for path in sorted(pages_dir.rglob("*(Part *).md"))]
    return {
        "scanned": len(rows),
        "needs_reprocess": sum(row["status"] == "needs_reprocess" for row in rows),
        "unreadable": sum(row["status"] == "unreadable" for row in rows),
        "pages": rows,
    }
