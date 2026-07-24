#!/usr/bin/env python3
"""Read-only audit for generated long-document Part pages."""

from __future__ import annotations

import argparse
import json

from core.config import PAGES_DIR
from services.ingest.entity_audit import audit_part_tree


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pages-dir", type=str, default=str(PAGES_DIR))
    parser.add_argument("--all", action="store_true", help="Include clean rows in output")
    parser.add_argument("--fail-on-issues", action="store_true")
    args = parser.parse_args()

    from pathlib import Path

    report = audit_part_tree(Path(args.pages_dir))
    if not args.all:
        report["pages"] = [
            row for row in report["pages"] if row["status"] not in {"complete", "legacy_clean"}
        ]
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 1 if args.fail_on_issues and (report["needs_reprocess"] or report["unreadable"]) else 0


if __name__ == "__main__":
    raise SystemExit(main())
