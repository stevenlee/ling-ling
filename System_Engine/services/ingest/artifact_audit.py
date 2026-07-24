"""Read-only validation and recoverable repair for generated artifact slots."""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from pathlib import Path

from services.ingest.artifact_pipeline import (
    artifact_slot_snapshot,
    reset_generated_artifact_slot,
)
from services.learning_artifacts import validate_markdown_table

_SECTION_RE = re.compile(
    r"(?ms)^## 🖼️ 學習輔助（(?P<kind>[^）]+)）\s*\n+"
    r"(?P<body>.*?)(?=^## 🖼️ 學習輔助（|\Z)"
)


@dataclass(frozen=True)
class ArtifactAuditIssue:
    path: str
    basis_hash: str
    artifact_type: str
    reason: str

    def to_dict(self) -> dict:
        return asdict(self)


def audit_generated_artifact_page(path: Path) -> list[ArtifactAuditIssue]:
    text = path.read_text(encoding="utf-8")
    slot = artifact_slot_snapshot(text)
    if not slot or slot.ownership != "generated" or not slot.intact:
        return []
    issues: list[ArtifactAuditIssue] = []
    for match in _SECTION_RE.finditer(slot.body):
        kind = match.group("kind").strip()
        body = match.group("body").strip()
        if kind == "comparison_table" and not validate_markdown_table(body):
            issues.append(
                ArtifactAuditIssue(
                    str(path),
                    slot.basis_hash,
                    kind,
                    "generated comparison_table failed structural validation",
                )
            )
    return issues


def audit_generated_artifacts(root: Path) -> list[ArtifactAuditIssue]:
    issues: list[ArtifactAuditIssue] = []
    for path in sorted(root.rglob("*.md")):
        try:
            issues.extend(audit_generated_artifact_page(path))
        except OSError:
            continue
    return issues


def repair_invalid_generated_slots(
    issues: list[ArtifactAuditIssue], *, backup_dir: Path
) -> list[dict]:
    results: list[dict] = []
    seen: set[str] = set()
    for issue in issues:
        if issue.path in seen:
            continue
        seen.add(issue.path)
        result = reset_generated_artifact_slot(
            Path(issue.path), basis_hash=issue.basis_hash, backup_dir=backup_dir
        )
        results.append(
            {
                "path": issue.path,
                "status": result.status,
                "detail": result.detail,
                "backup_path": str(result.backup_path) if result.backup_path else "",
            }
        )
    return results
