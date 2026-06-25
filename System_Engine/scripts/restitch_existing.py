#!/usr/bin/env python3
"""Backfill: re-stitch existing (Stitched) notes so they carry the per-part
learning artifacts (comparison_table / mindmap / flowchart).

Why this exists
---------------
``IngestionPipeline._extract_stitchable_body`` used to cut each part at the
navigation marker, which dropped the learning-artifact block that lives just
below it. The pipeline is now fixed, so any *new* ingest stitches artifacts in
automatically. This script applies the same fix to documents that were already
stitched under the old logic — **without re-running the LLM**.

It is purely deterministic markdown surgery:
  - reads the existing ``(Stitched).md`` frontmatter and **preserves it**
    verbatim (trace_ids, run_id, model, char counts, dates) — provenance intact
  - rebuilds the body from the on-disk ``(Part N).md`` files via the (fixed)
    ``_extract_stitchable_body``
  - bumps ``stitch_pipeline`` to ``part-note-stitch-v2`` so the format change is
    auditable

It does NOT touch the RAG index (no ChromaDB writes). Re-index separately if you
want search to reflect the new artifact content.

Usage
-----
  python scripts/restitch_existing.py "<base title>"   # one document
  python scripts/restitch_existing.py --all            # every stitched document
  python scripts/restitch_existing.py --all --dry-run  # report only, no writes
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.absolute()))

from core.config import PAGES_DIR  # noqa: E402
from core.parser import (  # noqa: E402
    dump_markdown_with_metadata,
    parse_markdown_metadata,
    run_markdown_quality_checks,
)
from services.ingestion_pipeline import IngestionPipeline  # noqa: E402

_PART_NUM_RE = re.compile(r"\(Part (\d+)\)\.md$")


def _part_files(folder: Path, base_title: str) -> list[Path]:
    """Part files for a document, ordered by part number."""
    parts = []
    for p in folder.glob(f"{base_title} (Part *).md"):
        m = _PART_NUM_RE.search(p.name)
        if m:
            parts.append((int(m.group(1)), p))
    return [p for _, p in sorted(parts)]


def restitch(base_title: str, *, dry_run: bool = False) -> bool:
    """Rebuild one document's (Stitched).md body in place. Returns True if written."""
    folder = PAGES_DIR / base_title
    stitched = folder / f"{base_title} (Stitched).md"
    if not stitched.exists():
        print(f"  ✗ no Stitched note: {stitched.name}")
        return False

    part_paths = _part_files(folder, base_title)
    if not part_paths:
        print(f"  ✗ no Part files for: {base_title}")
        return False

    # Preserve the existing frontmatter (provenance), only rebuild the body.
    existing = stitched.read_text(encoding="utf-8")
    metadata = parse_markdown_metadata(existing)

    pipeline = IngestionPipeline(llm_client=None, rag_manager=None)
    sections: list[str] = []
    for index, part_path in enumerate(part_paths, 1):
        content = part_path.read_text(encoding="utf-8")
        part_meta = parse_markdown_metadata(content)
        body = pipeline._extract_stitchable_body(content)
        if not body:
            continue
        source_range = pipeline._format_source_range(part_meta)
        sections.append(
            f"## Part {index}\n\n"
            f"Source note: [[{part_path.stem}]]\n\n"
            f"{source_range}"
            f"<!-- source: {part_path.name} -->\n\n"
            f"{body}"
        )

    if not sections:
        print(f"  ✗ nothing stitchable for: {base_title}")
        return False

    body = (
        f"# {base_title} (Stitched)\n\n"
        "> 忠實接合版：保留各 Part note 的主要內容，移除每篇的 navigation、metadata 與 digest appendix。"
        "這份文件偏向完整閱讀，不等同於洞察型 Synthesis。\n\n"
        "## 🔗 Navigation\n"
        f"- [[{base_title} (Synthesis)|查看洞察總結 (Synthesis)]]\n"
        f"- [[{base_title}|查看完整原始檔 (Original)]]\n\n"
        "---\n\n"
        + "\n".join(f"{section}\n" for section in sections)
    )
    body, _ = run_markdown_quality_checks(body)
    metadata["stitch_pipeline"] = "part-note-stitch-v2"

    artifact_count = body.count("🖼️ 學習輔助")
    if dry_run:
        print(f"  • {base_title}: {len(sections)} parts, {artifact_count} artifacts (dry-run)")
        return False

    stitched.write_text(dump_markdown_with_metadata(metadata, body), encoding="utf-8")
    print(f"  ✓ {base_title}: {len(sections)} parts, {artifact_count} artifacts")
    return True


def _all_stitched_titles() -> list[str]:
    titles = []
    for f in PAGES_DIR.glob("*/* (Stitched).md"):
        titles.append(f.name[: -len(" (Stitched).md")])
    return sorted(titles)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("title", nargs="?", help="Base title of the document to re-stitch")
    ap.add_argument("--all", action="store_true", help="Re-stitch every stitched document")
    ap.add_argument("--dry-run", action="store_true", help="Report only; do not write")
    args = ap.parse_args()

    if args.all:
        titles = _all_stitched_titles()
    elif args.title:
        titles = [args.title]
    else:
        ap.error("provide a title or --all")

    print(f"Re-stitching {len(titles)} document(s):")
    written = sum(restitch(t, dry_run=args.dry_run) for t in titles)
    print(f"\nDone. {written} file(s) rewritten.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
