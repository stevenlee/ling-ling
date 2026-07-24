from pathlib import Path
import threading

from services.ingest.artifact_pipeline import (
    ARTIFACT_END,
    ARTIFACT_START_PREFIX,
    apply_artifact_section,
    artifact_section_from_page,
    artifact_slot_status,
    begin_artifact_attempt,
    content_hash,
    defer_artifact_attempt,
    prepare_artifact_slot,
    reset_generated_artifact_slot,
    ArtifactJobDispatcher,
)
from services.ingestion_pipeline import IngestionPipeline


FIXTURES = Path(__file__).parent / "fixtures" / "ingest_artifacts"


def test_legacy_mathematics_shape_is_preserved_and_moved_before_navigation():
    existing = (FIXTURES / "legacy_with_artifacts.md").read_text(encoding="utf-8")
    basis = content_hash("chunk-1")

    prepared = prepare_artifact_slot(existing, existing, basis_hash=basis, enabled=True)

    assert prepared.status == "preserved"
    assert prepared.should_generate is False
    assert prepared.text.index("## 🖼️ 學習輔助") < prepared.text.index("## 🔗 知識導航")
    assert "root((範例))" in prepared.text
    assert prepared.text.count("## 🖼️ 學習輔助") == 1


def test_missing_mathematics_shape_gets_invisible_pending_slot_before_navigation():
    existing = (FIXTURES / "core_without_artifacts.md").read_text(encoding="utf-8")
    basis = content_hash("chunk-44")

    prepared = prepare_artifact_slot(existing, existing, basis_hash=basis, enabled=True)

    assert prepared.should_generate is True
    assert artifact_slot_status(prepared.text, basis) == "pending"
    assert prepared.text.index(ARTIFACT_START_PREFIX) < prepared.text.index("## 🔗 知識導航")
    assert "學習輔助產生中" not in prepared.text


def test_pending_slot_survives_resume_with_attempt_ledger(tmp_path):
    source = (FIXTURES / "core_without_artifacts.md").read_text(encoding="utf-8")
    basis = content_hash("chunk-ledger")
    prepared = prepare_artifact_slot(source, source, basis_hash=basis, enabled=True)
    page = tmp_path / "Part.md"
    page.write_text(prepared.text, encoding="utf-8")
    assert begin_artifact_attempt(page, basis_hash=basis, max_attempts=2).allowed
    defer_artifact_attempt(
        page,
        basis_hash=basis,
        detail="invalid table",
        max_attempts=2,
        quarantine_hours=24,
    )

    current = page.read_text(encoding="utf-8")
    resumed = prepare_artifact_slot(current, current, basis_hash=basis, enabled=True)

    assert resumed.status == "pending"
    assert resumed.should_generate is True
    assert 'attempts="1"' in resumed.text
    assert 'failure_sha256="' in resumed.text


def test_artifact_attempts_quarantine_and_content_change_resets_budget(tmp_path):
    source = (FIXTURES / "core_without_artifacts.md").read_text(encoding="utf-8")
    basis = content_hash("chunk-a")
    prepared = prepare_artifact_slot(source, source, basis_hash=basis, enabled=True)
    page = tmp_path / "Part.md"
    page.write_text(prepared.text, encoding="utf-8")
    for _ in range(2):
        assert begin_artifact_attempt(page, basis_hash=basis, max_attempts=2).allowed
        defer_artifact_attempt(
            page,
            basis_hash=basis,
            detail="invalid output",
            max_attempts=2,
            quarantine_hours=24,
        )

    denied = begin_artifact_attempt(page, basis_hash=basis, max_attempts=2)
    assert denied.allowed is False
    assert denied.status == "quarantined"

    current = page.read_text(encoding="utf-8")
    new_basis = content_hash("chunk-b")
    reset = prepare_artifact_slot(current, current, basis_hash=new_basis, enabled=True)
    page.write_text(reset.text, encoding="utf-8")
    fresh = begin_artifact_attempt(page, basis_hash=new_basis, max_attempts=2)
    assert fresh.allowed is True
    assert fresh.attempts == 1


def test_transient_artifact_outage_rolls_back_attempt(tmp_path):
    source = (FIXTURES / "core_without_artifacts.md").read_text(encoding="utf-8")
    basis = content_hash("chunk")
    prepared = prepare_artifact_slot(source, source, basis_hash=basis, enabled=True)
    page = tmp_path / "Part.md"
    page.write_text(prepared.text, encoding="utf-8")
    begin_artifact_attempt(page, basis_hash=basis, max_attempts=2)

    defer_artifact_attempt(
        page,
        basis_hash=basis,
        detail="provider timeout",
        max_attempts=2,
        quarantine_hours=24,
        transient=True,
    )

    assert 'attempts="' not in page.read_text(encoding="utf-8")
    assert begin_artifact_attempt(page, basis_hash=basis, max_attempts=2).attempts == 1


def test_late_insert_preserves_manual_edits_outside_owned_slot(tmp_path):
    source = (FIXTURES / "core_without_artifacts.md").read_text(encoding="utf-8")
    basis = content_hash("chunk")
    prepared = prepare_artifact_slot(source, source, basis_hash=basis, enabled=True)
    page = tmp_path / "Part.md"
    page.write_text(prepared.text.replace("尚未產生", "人工修訂：尚未產生"), encoding="utf-8")

    result = apply_artifact_section(
        page,
        "## 🖼️ 學習輔助（mindmap）\n\n```mermaid\nmindmap\n  root((完成))\n```",
        basis_hash=basis,
        backup_dir=tmp_path / "backups",
    )

    updated = page.read_text(encoding="utf-8")
    assert result.status == "applied"
    assert "人工修訂" in updated
    assert "root((完成))" in updated
    assert updated.index("## 🖼️ 學習輔助") < updated.index("## 🔗 知識導航")
    assert result.backup_path and result.backup_path.exists()


def test_late_insert_refuses_slot_edited_by_human(tmp_path):
    source = (FIXTURES / "core_without_artifacts.md").read_text(encoding="utf-8")
    basis = content_hash("chunk")
    prepared = prepare_artifact_slot(source, source, basis_hash=basis, enabled=True)
    edited = prepared.text.replace(ARTIFACT_END, "人工放入的內容\n" + ARTIFACT_END)
    page = tmp_path / "Part.md"
    page.write_text(edited, encoding="utf-8")

    result = apply_artifact_section(
        page,
        "## 🖼️ 學習輔助（mindmap）\nnew",
        basis_hash=basis,
        backup_dir=tmp_path / "backups",
    )

    assert result.status == "conflict"
    assert page.read_text(encoding="utf-8") == edited


def test_generated_region_becomes_human_owned_after_manual_edit(tmp_path):
    source = (FIXTURES / "core_without_artifacts.md").read_text(encoding="utf-8")
    basis = content_hash("chunk")
    prepared = prepare_artifact_slot(source, source, basis_hash=basis, enabled=True)
    page = tmp_path / "Part.md"
    page.write_text(prepared.text, encoding="utf-8")
    apply_artifact_section(
        page,
        "## 🖼️ 學習輔助（mindmap）\noriginal",
        basis_hash=basis,
        backup_dir=tmp_path / "backups",
    )
    edited = page.read_text(encoding="utf-8").replace("original", "人工修改")

    rerendered = prepare_artifact_slot(edited, edited, basis_hash=basis, enabled=True)

    assert rerendered.status == "preserved"
    assert rerendered.should_generate is False
    assert "人工修改" in artifact_section_from_page(rerendered.text)


def test_reset_generated_slot_requires_untouched_owned_region(tmp_path):
    source = (FIXTURES / "core_without_artifacts.md").read_text(encoding="utf-8")
    basis = content_hash("chunk")
    prepared = prepare_artifact_slot(source, source, basis_hash=basis, enabled=True)
    page = tmp_path / "Part.md"
    page.write_text(prepared.text, encoding="utf-8")
    apply_artifact_section(
        page,
        "## 🖼️ 學習輔助（comparison_table）\n\ninvalid reasoning",
        basis_hash=basis,
        backup_dir=tmp_path / "backups",
    )

    reset = reset_generated_artifact_slot(
        page, basis_hash=basis, backup_dir=tmp_path / "repair-backups"
    )

    assert reset.status == "applied"
    assert reset.backup_path and reset.backup_path.exists()
    assert artifact_slot_status(page.read_text(encoding="utf-8"), basis) == "pending"


def test_pipeline_preflight_conflict_keeps_page_without_spending_generation(tmp_path, monkeypatch):
    source = (FIXTURES / "core_without_artifacts.md").read_text(encoding="utf-8")
    basis = content_hash("chunk")
    prepared = prepare_artifact_slot(source, source, basis_hash=basis, enabled=True)
    edited = prepared.text.replace(ARTIFACT_END, "人工編輯中的圖\n" + ARTIFACT_END)
    page = tmp_path / "Part.md"
    page.write_text(edited, encoding="utf-8")
    monkeypatch.setattr(
        "services.ingestion_pipeline.INGEST_ARTIFACT_BACKUP_DIR", tmp_path / "backups"
    )
    monkeypatch.setattr(
        "services.ingestion_pipeline.INGEST_ARTIFACT_PENDING_DIR", tmp_path / "pending"
    )
    monkeypatch.setattr(
        "services.learning_artifacts.maybe_artifact_section",
        lambda *args, **kwargs: "## 🖼️ 學習輔助（mindmap）\ngenerated",
    )
    pipeline = IngestionPipeline.__new__(IngestionPipeline)
    pipeline.llm = object()
    pipeline._commit_lock = threading.Lock()

    metric = pipeline._run_artifact_job(page, basis, "正文", "Book", 1)

    assert metric["status"] == "conflict"
    assert page.read_text(encoding="utf-8") == edited
    assert not list((tmp_path / "pending").rglob("*.md"))


def test_pipeline_deferred_artifact_leaves_slot_pending(tmp_path, monkeypatch):
    from services.learning_artifacts import ArtifactSectionOutcome

    source = (FIXTURES / "core_without_artifacts.md").read_text(encoding="utf-8")
    basis = content_hash("chunk")
    prepared = prepare_artifact_slot(source, source, basis_hash=basis, enabled=True)
    page = tmp_path / "Part.md"
    page.write_text(prepared.text, encoding="utf-8")
    monkeypatch.setattr(
        "services.learning_artifacts.maybe_artifact_section",
        lambda *args, **kwargs: ArtifactSectionOutcome("deferred", detail="provider timeout"),
    )
    pipeline = IngestionPipeline.__new__(IngestionPipeline)
    pipeline.llm = object()
    pipeline.trace_store = None
    pipeline._commit_lock = threading.Lock()
    pipeline._record_artifact = lambda *args, **kwargs: None

    metric = pipeline._run_artifact_job(page, basis, "正文", "Book", 1)

    assert metric["status"] == "deferred"
    assert artifact_slot_status(page.read_text(encoding="utf-8"), basis) == "pending"


def test_dispatcher_reports_which_job_blocks_backpressure():
    dispatcher = ArtifactJobDispatcher(workers=1)
    release = threading.Event()
    waits = []
    try:
        dispatcher.submit(
            lambda: release.wait(timeout=2),
            wait_until_running=True,
            job_label="Part 7 learning aids",
        )
        timer = threading.Timer(0.04, release.set)
        timer.start()
        dispatcher.enforce_max_inflight(1, on_wait=waits.append, heartbeat_seconds=0.01)
        timer.cancel()
    finally:
        release.set()
        dispatcher.shutdown()

    assert waits
    assert waits[-1]["label"] == "Part 7 learning aids"
    assert waits[-1]["elapsed_seconds"] > 0
