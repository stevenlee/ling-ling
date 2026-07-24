from services.ingest.artifact_audit import (
    audit_generated_artifact_page,
    repair_invalid_generated_slots,
)
from services.ingest.artifact_pipeline import (
    apply_artifact_section,
    artifact_slot_status,
    content_hash,
    prepare_artifact_slot,
)


def _generated_page(tmp_path, section: str):
    tmp_path.mkdir(parents=True, exist_ok=True)
    basis = content_hash("chunk")
    source = "# Part\n\nBody\n\n## 🔗 知識導航\n"
    prepared = prepare_artifact_slot(source, source, basis_hash=basis, enabled=True)
    path = tmp_path / "Part.md"
    path.write_text(prepared.text, encoding="utf-8")
    apply_artifact_section(path, section, basis_hash=basis, backup_dir=tmp_path / "initial-backup")
    return path, basis


def test_audit_flags_reasoning_scratchpad_but_not_valid_table(tmp_path):
    invalid, _ = _generated_page(
        tmp_path / "invalid",
        "## 🖼️ 學習輔助（comparison_table）\n\n* Input: compare\n* Goal: table",
    )
    valid, _ = _generated_page(
        tmp_path / "valid",
        "## 🖼️ 學習輔助（comparison_table）\n\n| A | B |\n|---|---|\n| 1 | 2 |",
    )

    assert len(audit_generated_artifact_page(invalid)) == 1
    assert audit_generated_artifact_page(valid) == []


def test_repair_resets_only_intact_generated_slot(tmp_path):
    path, basis = _generated_page(
        tmp_path,
        "## 🖼️ 學習輔助（comparison_table）\n\n* Input: leaked reasoning",
    )
    issues = audit_generated_artifact_page(path)

    results = repair_invalid_generated_slots(issues, backup_dir=tmp_path / "repair")

    assert results[0]["status"] == "applied"
    assert artifact_slot_status(path.read_text(encoding="utf-8"), basis) == "pending"
    assert results[0]["backup_path"]
