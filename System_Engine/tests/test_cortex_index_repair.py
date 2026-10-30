"""A2 vault-commit/index-repair recovery; all state and DB substitutes are local."""

import json
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from maintenance import cortex_index_repair as repair
from maintenance import evidence_traceback as traceback
from services.cortex_store import CortexPage, parse_cortex_page, save_cortex_page

_NOW = datetime(2026, 10, 29, 3)


@pytest.fixture
def env(tmp_path, monkeypatch):
    cortex, queue = tmp_path / "Cortex", tmp_path / "repairs"
    cortex.mkdir()
    monkeypatch.setattr(traceback, "CORTEX_INDEX_REPAIR_DIR", queue)
    monkeypatch.setattr(repair, "CORTEX_INDEX_REPAIR_BACKOFF_SECONDS", 60)
    monkeypatch.setattr(repair, "CORTEX_INDEX_REPAIR_MAX_BACKOFF_SECONDS", 600)
    monkeypatch.setattr(repair, "CORTEX_INDEX_REPAIR_BATCH", 5)
    monkeypatch.setattr(traceback.settings, "EVIDENCE_TRACEBACK_APPLY", True)
    return cortex, queue, tmp_path


def _page(env, name="one", status="active", thick=False):
    cortex, _, _ = env
    page = CortexPage(
        claim_id=f"cortex-{name}",
        path=cortex / f"{name}.md",
        claim=f"主張 {name}",
        status=status,
        confidence=0.5,
        S=1,
        last_reinforced_at="2026-01-01T00:00:00",
        evidence=[{"insight": "origin", "sources": [], "summary": "origin", "date": "2026-01-01"}],
    )
    if thick:
        page.evidence.append(
            {"insight": "second", "sources": [], "summary": "s2", "date": "2026-01-02"}
        )
    save_cortex_page(page)
    return page


class FakeRAG:
    def __init__(self, *, fail_doc=False, fail_facets=False, before_doc=None):
        self.fail_doc, self.fail_facets, self.before_doc = fail_doc, fail_facets, before_doc
        self.documents, self.facets, self.removed = [], [], []

    def add_document(self, path, title, text, *, strict=False, force=False):
        assert strict and force  # partial previous upserts must be rebuilt
        self.documents.append((path, title, text))
        if self.before_doc:
            self.before_doc(path)
        if self.fail_doc:
            raise ConnectionError("index offline")

    def add_facets(self, path, title, facets, *, strict=False):
        assert strict
        self.facets.append((path, title, facets))
        return not self.fail_facets

    def remove_facets(self, path, *, strict=False):
        assert strict
        self.removed.append(path)


def _apply(page, rag):
    scan = traceback.ClaimScan(page.claim_id, page.claim, "反例")
    scan.judgments = [traceback.PassageJudgment("Independent Doc", "supports", "佐證")]
    return traceback._apply_scan(page, scan, rag, _NOW)


def _run(env, rag, now=_NOW):
    cortex, queue, root = env
    return traceback.run_evidence_traceback(
        object(),
        rag,
        cortex_dir=cortex,
        repair_dir=queue,
        state_file=root / "cursor.json",
        out_dir=root / "reports",
        now=now,
    )


def test_committed_support_survives_failed_index_then_recovers_with_empty_thin_pool(env):
    cortex, queue, _ = env
    page = _page(env)
    first = _apply(page, FakeRAG(fail_doc=True))
    assert first["added_evidence"] == 1 and first["reinforced"]
    assert first["index_pending"] and "error" not in first  # committed, not rolled back
    committed = page.path.read_bytes()
    assert len(parse_cortex_page(page.path).evidence) == 2
    assert "已生效但待索引" in traceback._applied_action(first)
    slot = next(queue.glob("*.json"))
    record = json.loads(slot.read_text(encoding="utf-8"))
    assert record["attempts"] == 1 and "offline" in record["last_error"]
    # Fresh invocation reloads persisted intent; no LLM is available, and the
    # claim has already left the thin pool.
    healthy = FakeRAG()
    result = _run(env, healthy, _NOW + timedelta(seconds=61))
    assert result.status == "ok" and result.scans == []
    assert result.report_path.exists()
    assert "完成 1" in result.summary
    assert len(healthy.documents) == len(healthy.facets) == 1
    assert page.path.read_bytes() == committed  # no second evidence/reinforcement
    assert not list(queue.glob("*.json"))


def test_intent_failure_prevents_vault_commit(env, monkeypatch):
    page = _page(env)
    original = page.path.read_bytes()
    monkeypatch.setattr(
        repair, "atomic_write_text", lambda *a: (_ for _ in ()).throw(OSError("disk full"))
    )
    rag = FakeRAG()
    outcome = _apply(page, rag)
    assert "disk full" in outcome["error"]
    assert page.path.read_bytes() == original and rag.documents == []


def test_crash_after_intent_before_vault_commit_indexes_existing_file_only(env, monkeypatch):
    cortex, queue, _ = env
    page = _page(env)
    original = page.path.read_bytes()

    def crash(page):
        assert len(list(queue.glob("*.json"))) == 1
        raise KeyboardInterrupt("simulated crash")

    monkeypatch.setattr(traceback, "save_cortex_page", crash)
    with pytest.raises(KeyboardInterrupt):
        _apply(page, FakeRAG())
    assert page.path.read_bytes() == original
    rag = FakeRAG()
    result = repair.repair_pending_indexes(rag, cortex, queue, _NOW)
    assert result.repaired == 1
    assert rag.documents[0][2] == original.decode()
    assert len(parse_cortex_page(page.path).evidence) == 1


def test_attempt_must_persist_before_index_call(env, monkeypatch):
    cortex, queue, _ = env
    page = _page(env)
    real_write = repair.atomic_write_text

    def fail_attempt(path, text):
        if json.loads(text)["attempts"]:
            raise OSError("ledger unavailable")
        real_write(path, text)

    monkeypatch.setattr(repair, "atomic_write_text", fail_attempt)
    rag = FakeRAG()
    outcome = _apply(page, rag)
    assert outcome["added_evidence"] == 1 and outcome["index_pending"]
    assert len(parse_cortex_page(page.path).evidence) == 2
    assert rag.documents == []
    assert len(list(queue.glob("*.json"))) == 1


def test_backoff_survives_restart_but_changed_page_gets_fresh_budget(env):
    cortex, queue, _ = env
    page = _page(env)
    _apply(page, FakeRAG(fail_doc=True))
    rag = FakeRAG()
    waiting = repair.repair_pending_indexes(rag, cortex, queue, _NOW + timedelta(seconds=1))
    assert waiting.pending == 1 and waiting.attempted == 0 and rag.documents == []
    page.path.write_text(
        page.path.read_text(encoding="utf-8") + "\n人工補充：保留最新版。\n", encoding="utf-8"
    )
    latest = page.path.read_bytes()
    done = repair.repair_pending_indexes(rag, cortex, queue, _NOW + timedelta(seconds=2))
    assert done.repaired == 1 and "人工補充" in rag.documents[0][2]
    assert page.path.read_bytes() == latest


def test_false_facet_result_keeps_intent_after_successful_document(env):
    _, queue, _ = env
    page = _page(env)
    outcome = _apply(page, FakeRAG(fail_facets=True))
    assert outcome["added_evidence"] == 1 and outcome["index_pending"]
    assert "facet" in outcome["index_error"]
    assert len(list(queue.glob("*.json"))) == 1
    recovered = _run(env, FakeRAG(), _NOW + timedelta(seconds=61))
    assert recovered.status == "ok" and not list(queue.glob("*.json"))


def test_cleanup_failure_does_not_lose_committed_evidence(env, monkeypatch):
    cortex, queue, _ = env
    page = _page(env)
    real_unlink = Path.unlink

    def fail_cleanup(self, *args, **kwargs):
        if self.parent == queue and self.suffix == ".json":
            raise OSError("cannot clear intent")
        return real_unlink(self, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", fail_cleanup)
    outcome = _apply(page, FakeRAG())
    assert outcome["index_pending"] and outcome["added_evidence"] == 1
    committed = page.path.read_bytes()
    monkeypatch.setattr(Path, "unlink", real_unlink)
    assert (
        repair.repair_pending_indexes(
            FakeRAG(), cortex, queue, _NOW + timedelta(seconds=61)
        ).repaired
        == 1
    )
    assert page.path.read_bytes() == committed


def test_deleted_page_is_not_recreated_from_intent(env):
    cortex, queue, _ = env
    page = _page(env)
    repair.enqueue_index_repair(page, queue, cortex)
    page.path.unlink()
    rag = FakeRAG()
    result = repair.repair_pending_indexes(rag, cortex, queue, _NOW)
    assert result.removed == 1 and result.pending == 0
    assert rag.documents == [] and not page.path.exists()


def test_corrupted_slot_rebuilds_from_current_page(env):
    cortex, queue, _ = env
    page = _page(env)
    slot = repair.enqueue_index_repair(page, queue, cortex)
    slot.write_text("{broken", encoding="utf-8")
    rag = FakeRAG()
    result = repair.repair_pending_indexes(rag, cortex, queue, _NOW)
    assert result.repaired == 1 and result.pending == 0
    assert rag.documents[0][1] == page.claim_id


def test_invalid_slots_do_not_escape_cortex_or_starve_valid_work(env, monkeypatch):
    cortex, queue, root = env
    queue.mkdir()
    outside = root / "outside.md"
    outside.write_text("outside must not be read", encoding="utf-8")
    invalid = queue / "invalid.json"
    invalid.write_text(
        json.dumps(
            {"schema_version": 1, "path": "../outside.md", "claim_id": "x", "content_hash": "bad"}
        ),
        encoding="utf-8",
    )
    page = _page(env)
    repair.enqueue_index_repair(page, queue, cortex)
    real_read = Path.read_text

    def checked_read(self, *args, **kwargs):
        assert self.resolve() != outside.resolve()
        return real_read(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", checked_read)
    monkeypatch.setattr(repair, "CORTEX_INDEX_REPAIR_BATCH", 1)
    rag = FakeRAG()
    result = repair.repair_pending_indexes(rag, cortex, queue, _NOW)
    assert result.repaired == 1 and result.pending == 1
    assert invalid.exists() and len(rag.documents) == 1


def test_latest_dormant_status_removes_facets_instead_of_reviving_them(env):
    cortex, queue, _ = env
    page = _page(env, status="dormant")
    repair.enqueue_index_repair(page, queue, cortex)
    rag = FakeRAG()
    result = repair.repair_pending_indexes(rag, cortex, queue, _NOW)
    assert result.repaired == 1
    assert rag.facets == [] and rag.removed == [page.path]


def test_external_edit_during_index_keeps_intent_and_recovers_latest(env):
    cortex, queue, _ = env
    page = _page(env)
    repair.enqueue_index_repair(page, queue, cortex)

    def edit(path):
        path.write_text(path.read_text(encoding="utf-8") + "\nNew manual text.\n", encoding="utf-8")

    first = repair.repair_pending_indexes(FakeRAG(before_doc=edit), cortex, queue, _NOW)
    assert first.pending == 1 and first.errors
    rag = FakeRAG()
    second = repair.repair_pending_indexes(rag, cortex, queue, _NOW + timedelta(seconds=1))
    assert second.repaired == 1 and "New manual text." in rag.documents[0][2]


def test_dry_run_does_not_process_or_clear_pending_index(env, monkeypatch):
    cortex, queue, _ = env
    page = _page(env, thick=True)
    slot = repair.enqueue_index_repair(page, queue, cortex)
    before = slot.read_bytes(), page.path.read_bytes()
    monkeypatch.setattr(traceback.settings, "EVIDENCE_TRACEBACK_APPLY", False)
    rag = FakeRAG()
    result = _run(env, rag)
    assert result.status == "ok" and rag.documents == []
    assert (slot.read_bytes(), page.path.read_bytes()) == before


def test_daily_task_reports_pending_index_failure_without_any_thin_claim(env):
    page = _page(env, thick=True)
    cortex, queue, _ = env
    repair.enqueue_index_repair(page, queue, cortex)
    result = _run(env, FakeRAG(fail_doc=True))
    assert result.status == "failed" and result.scans == []
    assert "待處理 1" in result.report_path.read_text(encoding="utf-8")


def test_backoff_and_budget_do_not_starve_older_unattempted_jobs(env, monkeypatch):
    import os

    cortex, queue, _ = env
    monkeypatch.setattr(repair, "CORTEX_INDEX_REPAIR_BATCH", 1)
    for i in range(3):
        page = _page(env, str(i))
        slot = repair.enqueue_index_repair(page, queue, cortex)
        os.utime(slot, (i + 1, i + 1))
    rag = FakeRAG(fail_doc=True)
    first = repair.repair_pending_indexes(rag, cortex, queue, _NOW)
    second = repair.repair_pending_indexes(rag, cortex, queue, _NOW + timedelta(days=1))
    assert first.attempted == second.attempted == 1
    assert [item[1] for item in rag.documents] == ["cortex-0", "cortex-1"]


def test_corrupt_future_retry_timestamp_does_not_freeze_queue(env):
    cortex, queue, _ = env
    slot = repair.enqueue_index_repair(_page(env), queue, cortex)
    record = json.loads(slot.read_text(encoding="utf-8"))
    record.update(next_retry_at=10**100, attempts="corrupt")
    slot.write_text(json.dumps(record), encoding="utf-8")
    assert repair.repair_pending_indexes(FakeRAG(), cortex, queue, _NOW).repaired == 1


def test_scheduler_holds_busy_lock_for_repair_and_skips_when_user_is_busy(env, monkeypatch):
    from core.state import BusyState
    from watchers import maintenance_scheduler as scheduler_module

    cortex, queue, root = env
    page = _page(env, thick=True)
    repair.enqueue_index_repair(page, queue, cortex)
    busy = BusyState()
    busy.lock_file = root / "test.lock"
    monkeypatch.setattr(scheduler_module, "global_busy_state", busy)
    rag = FakeRAG(
        before_doc=lambda _: busy.is_busy() or (_ for _ in ()).throw(AssertionError("no lock"))
    )

    def action():
        result = _run(env, rag)
        return scheduler_module.MaintenanceResult(result.status, result.summary)

    task = scheduler_module.MaintenanceTask(name="a2", action=action, daily=True)
    scheduler = scheduler_module.MaintenanceScheduler(
        root,
        object(),
        rag=rag,
        tasks=[task],
        state_file=root / "scheduler.json",
        enabled=True,
    )
    busy.set_busy(True)
    try:
        assert scheduler.run_due_once(_NOW) == []
        assert rag.documents == []
    finally:
        busy.set_busy(False, fire_callbacks=False)
    assert scheduler.run_due_once(_NOW) == ["a2"]
    assert len(rag.documents) == 1 and not busy.is_busy()


class _BrokenCollection:
    def __init__(self, fail):
        self.fail = fail
        self.upserts = []

    def get(self, **kwargs):
        if self.fail == "fetch":
            raise OSError("fetch failed")
        if "title" in (kwargs.get("where") or {}):
            return {"ids": ["legacy"], "metadatas": [{}]}
        return {"ids": ["facet"], "metadatas": [{"role": "facet"}]}

    def delete(self, **kwargs):
        if self.fail == "delete":
            raise OSError("delete failed")

    def upsert(self, **kwargs):
        if self.fail == "upsert":
            raise OSError("upsert failed")
        self.upserts.append(kwargs)


def _backend(fail):
    from services.rag_manager import RAGManager

    class BM25:
        def mark_dirty(self):
            pass

    rag = RAGManager.__new__(RAGManager)
    rag.collection = _BrokenCollection(fail)
    rag._bm25 = BM25()
    return rag


@pytest.mark.parametrize("failure", ["fetch", "delete", "upsert"])
def test_strict_facets_raise_instead_of_silently_finishing(tmp_path, failure):
    rag = _backend(failure)
    with pytest.raises(OSError, match="failed"):
        rag.add_facets(tmp_path / "page.md", "claim", ["facet"], strict=True)
    # Keep the existing fail-soft contract for unrelated callers.
    if failure == "upsert":
        assert rag.add_facets(tmp_path / "page.md", "claim", ["facet"]) is False


@pytest.mark.parametrize("failure", ["fetch", "delete"])
def test_strict_facet_removal_cannot_hide_failure(tmp_path, failure):
    with pytest.raises(OSError, match="failed"):
        _backend(failure).remove_facets(tmp_path / "page.md", strict=True)


@pytest.mark.parametrize("failure", ["fetch", "delete"])
def test_strict_document_legacy_cleanup_propagates_failures(failure):
    rag = _backend(failure)
    with pytest.raises(OSError, match="failed"):
        rag._delete_legacy_title_chunks("claim", strict=True)


def test_strict_document_delete_propagates_and_default_remains_fail_soft():
    rag = _backend("delete")
    with pytest.raises(OSError, match="delete failed"):
        rag.delete_document("a" * 64, strict=True)
    rag.delete_document("a" * 64)


def test_force_document_rebuild_propagates_failed_cleanup(tmp_path):
    # Even if an earlier partial upsert stored the target content_hash, repair
    # must not short-circuit; force=True reaches the strict cleanup boundary.
    rag = _backend("delete")
    with pytest.raises(OSError, match="delete failed"):
        rag.add_document(tmp_path / "page.md", "claim", "content", strict=True, force=True)


def test_vault_replace_failure_preserves_original_and_write_ahead_intent(env, monkeypatch):
    cortex, queue, _ = env
    page = _page(env)
    before = page.path.read_bytes()
    replace = Path.replace

    def fail_vault_replace(self, destination):
        if Path(destination) == page.path:
            assert len(list(queue.glob("*.json"))) == 1  # intent is already durable
            raise OSError("vault replace failed")
        return replace(self, destination)

    monkeypatch.setattr(Path, "replace", fail_vault_replace)
    rag = FakeRAG()
    result = _apply(page, rag)
    assert "vault replace failed" in result["error"]
    assert page.path.read_bytes() == before and rag.documents == []
    assert not list(cortex.glob("*.tmp"))
    assert len(list(queue.glob("*.json"))) == 1


def test_index_snapshot_parser_does_not_reread_different_file(env, monkeypatch):
    page = _page(env)
    snapshot = page.path.read_text(encoding="utf-8")
    read = Path.read_text

    def forbid_cortex_read(self, *args, **kwargs):
        if self == page.path:
            raise AssertionError("must parse the exact snapshot")
        return read(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", forbid_cortex_read)
    assert parse_cortex_page(page.path, text=snapshot).claim_id == page.claim_id
