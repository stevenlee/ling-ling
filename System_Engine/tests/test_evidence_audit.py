"""Immutable private A2 audit records: integrity, durability and retention."""

import json
from datetime import datetime
from pathlib import Path

import pytest

from services import evidence_audit as audit
from services.cortex_store import CortexPage, render_cortex_page

_NOW = datetime(2026, 10, 31, 9)


@pytest.fixture
def payload():
    page = CortexPage("cortex-test", Path("Cortex/test.md"), "測試主張", falsifier="測試反例")
    snapshot = render_cortex_page(page)
    return {
        "basis": {
            "path": str(page.path),
            "snapshot": snapshot,
            "sha256": audit.text_hash(snapshot),
            "claim_id": page.claim_id,
            "claim": page.claim,
            "falsifier": page.falsifier,
        },
        "source": {
            "title": "來源",
            "path": "pages/source.md",
            "family": "pages/source",
            "family_method": "path_stem_v1_unverified",
            "chunk_id": "chunk-1",
            "passage": "可覆核段落",
            "sha256": audit.text_hash("可覆核段落"),
            "distance": 0.2,
            "range": {"coordinate": "retrieved_chunk_prefix", "start": 0, "end": 5},
        },
        "verdict": {
            "contract_version": 2,
            "relation": "full_support",
            "reason": "指定條件下支持",
            "quotes": ["可覆核段落"],
            "unsupported": [],
            "applicability": "測試條件",
        },
        "judge": {
            "provider": "fake",
            "model": "fake-v1",
            "prompt_asset": "Operations/judge_evidence_scope.md",
            "prompt_sha256": audit.text_hash("fake prompt"),
            "prompt_body": "fake prompt",
            "contract_version": 2,
            "scope_attempt": 1,
            "trace_run_id": "run-123",
        },
    }


def test_snapshot_is_replayable_without_source_or_trace_database(tmp_path, payload):
    identifier = audit.persist_audit(tmp_path / "private", payload, _NOW)
    record = audit.load_audit(tmp_path / "private", identifier)
    assert record["payload"] == payload
    assert record["payload"]["source"]["passage"] == "可覆核段落"
    assert record["payload"]["judge"]["trace_run_id"] == "run-123"
    assert not (tmp_path / "private" / ".tmp").exists()


def test_duplicate_content_keeps_first_record_and_timestamp(tmp_path, payload, monkeypatch):
    identifier = audit.persist_audit(tmp_path, payload, _NOW)
    original = (tmp_path / f"{identifier}.json").read_bytes()

    def unexpected(*args):
        pytest.fail("immutable duplicate must not be rewritten")

    monkeypatch.setattr(audit, "atomic_write_text", unexpected)
    assert audit.persist_audit(tmp_path, payload, datetime(2027, 1, 1)) == identifier
    assert (tmp_path / f"{identifier}.json").read_bytes() == original


def test_old_and_unreferenced_preparations_are_retained(tmp_path, payload):
    first = audit.persist_audit(tmp_path, payload, datetime(2000, 1, 1))
    payload["judge"]["prompt_body"] = "new prompt"
    payload["judge"]["prompt_sha256"] = audit.text_hash("new prompt")
    second = audit.persist_audit(tmp_path, payload, datetime(2030, 1, 1))
    assert first != second
    assert audit.load_audit(tmp_path, first)["recorded_at"].startswith("2000")
    assert len(list(tmp_path.glob("*.json"))) == 2


@pytest.mark.parametrize("field", ["claim", "falsifier", "claim_id", "sha256"])
def test_claim_snapshot_tampering_is_rejected(tmp_path, payload, field):
    payload["basis"][field] = "changed"
    with pytest.raises(ValueError):
        audit.persist_audit(tmp_path, payload, _NOW)
    assert not list(tmp_path.glob("*.json"))


@pytest.mark.parametrize(
    "patch",
    [
        {"sha256": "bad"},
        {"passage": "changed"},
        {"range": {"coordinate": "document", "start": 0, "end": 5}},
        {"range": {"coordinate": "retrieved_chunk_prefix", "start": 1, "end": 5}},
        {"distance": float("nan")},
    ],
)
def test_source_integrity_and_extent_are_checked(tmp_path, payload, patch):
    payload["source"].update(patch)
    with pytest.raises(ValueError):
        audit.persist_audit(tmp_path, payload, _NOW)


@pytest.mark.parametrize(
    "patch",
    [
        {"model": ""},
        {"provider": None},
        {"prompt_sha256": "bad"},
        {"contract_version": True},
        {"scope_attempt": 0},
        {"trace_run_id": []},
    ],
)
def test_missing_or_invalid_judge_provenance_is_rejected(tmp_path, payload, patch):
    payload["judge"].update(patch)
    with pytest.raises(ValueError):
        audit.persist_audit(tmp_path, payload, _NOW)


@pytest.mark.parametrize("damage", ["bad-json", "wrong-id", "wrong-schema", "changed-payload"])
def test_corrupt_existing_audit_is_preserved_and_blocks_reuse(tmp_path, payload, damage):
    identifier = audit.persist_audit(tmp_path, payload, _NOW)
    path = tmp_path / f"{identifier}.json"
    record = json.loads(path.read_text(encoding="utf-8"))
    if damage == "wrong-id":
        record["audit_id"] = "b" * 64
    elif damage == "wrong-schema":
        record["schema_version"] = 99
    elif damage == "changed-payload":
        record["payload"]["verdict"]["reason"] = "changed"
    path.write_text("broken" if damage == "bad-json" else json.dumps(record), encoding="utf-8")
    damaged = path.read_bytes()
    with pytest.raises(ValueError):
        audit.persist_audit(tmp_path, payload, _NOW)
    assert path.read_bytes() == damaged


def test_atomic_replace_failure_leaves_no_partial_record(tmp_path, payload, monkeypatch):
    def boom(*args):
        raise OSError("replace failed")

    monkeypatch.setattr(Path, "replace", boom)
    with pytest.raises(OSError, match="replace failed"):
        audit.persist_audit(tmp_path, payload, _NOW)
    assert list(tmp_path.iterdir()) == []


def test_directory_sync_failure_is_reported_and_later_retry_recovers(
    tmp_path, payload, monkeypatch
):
    sync = audit._sync_directory
    monkeypatch.setattr(
        audit, "_sync_directory", lambda path: (_ for _ in ()).throw(OSError("fsync failed"))
    )
    with pytest.raises(OSError, match="fsync failed"):
        audit.persist_audit(tmp_path, payload, _NOW)
    monkeypatch.setattr(audit, "_sync_directory", sync)
    identifier = audit.persist_audit(tmp_path, payload, _NOW)
    assert audit.load_audit(tmp_path, identifier)["payload"] == payload


@pytest.mark.parametrize("identifier", ["../outside", "a" * 63, "A" * 64])
def test_invalid_audit_id_cannot_escape_private_directory(tmp_path, identifier):
    with pytest.raises(ValueError, match="audit ID"):
        audit.load_audit(tmp_path, identifier)
