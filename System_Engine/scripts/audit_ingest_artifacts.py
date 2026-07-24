#!/usr/bin/env python3
"""Audit generated learning-aid slots; optionally reset invalid ones."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from core.config import INGEST_ARTIFACT_BACKUP_DIR, PAGES_DIR
from services.ingest.artifact_audit import (
    audit_generated_artifacts,
    repair_invalid_generated_slots,
)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("root", nargs="?", type=Path, default=PAGES_DIR)
    parser.add_argument("--repair", action="store_true")
    parser.add_argument("--backup-dir", type=Path, default=INGEST_ARTIFACT_BACKUP_DIR)
    parser.add_argument("--fail-on-issues", action="store_true")
    args = parser.parse_args()

    issues = audit_generated_artifacts(args.root)
    repairs = (
        repair_invalid_generated_slots(issues, backup_dir=args.backup_dir) if args.repair else []
    )
    print(
        json.dumps(
            {
                "root": str(args.root),
                "issue_count": len(issues),
                "issues": [issue.to_dict() for issue in issues],
                "repairs": repairs,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 1 if issues and args.fail_on_issues and not args.repair else 0


if __name__ == "__main__":
    raise SystemExit(main())
