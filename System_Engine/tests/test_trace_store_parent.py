"""Tests for TraceStore parent_run_id auto-detection (Phase 5C)."""

import os
import sqlite3
from pathlib import Path

os.environ.setdefault("LLM_PROVIDER", "vllm")

import pytest

from services.trace_store import TraceStore


def _trace_store(tmp_path: Path) -> TraceStore:
    return TraceStore(db_path=tmp_path / "trace.sqlite", retention_days=0)


class TestParentRunIdAutoDetect:
    def test_top_level_run_has_no_parent(self, tmp_path):
        ts = _trace_store(tmp_path)
        with ts.run(intent="solo") as run_id:
            pass
        row = (
            sqlite3.connect(str(ts.db_path))
            .execute("SELECT parent_run_id FROM runs WHERE run_id = ?", (run_id,))
            .fetchone()
        )
        assert row[0] is None

    def test_nested_run_inherits_parent_from_contextvar(self, tmp_path):
        ts = _trace_store(tmp_path)
        with ts.run(intent="parent") as parent_id:
            with ts.run(intent="child") as child_id:
                pass
        conn = sqlite3.connect(str(ts.db_path))
        parent_link = conn.execute(
            "SELECT parent_run_id FROM runs WHERE run_id = ?", (child_id,)
        ).fetchone()[0]
        assert parent_link == parent_id

    def test_grandchild_chain(self, tmp_path):
        ts = _trace_store(tmp_path)
        with ts.run(intent="a") as a_id:
            with ts.run(intent="b") as b_id:
                with ts.run(intent="c") as c_id:
                    pass
        conn = sqlite3.connect(str(ts.db_path))
        chain = {row[0]: row[1] for row in conn.execute("SELECT run_id, parent_run_id FROM runs")}
        assert chain[a_id] is None
        assert chain[b_id] == a_id
        assert chain[c_id] == b_id

    def test_explicit_parent_overrides_contextvar(self, tmp_path):
        """When parent_run_id is passed explicitly, it wins over the
        ambient ContextVar — useful for stitching synthetic trace trees."""
        ts = _trace_store(tmp_path)
        explicit_parent = "run_synthetic_parent_id_123"
        with ts.run(intent="ambient") as ambient_id:
            with ts.run(intent="child", parent_run_id=explicit_parent) as child_id:
                pass
        link = (
            sqlite3.connect(str(ts.db_path))
            .execute("SELECT parent_run_id FROM runs WHERE run_id = ?", (child_id,))
            .fetchone()[0]
        )
        assert link == explicit_parent
        assert link != ambient_id

    def test_sibling_children_share_parent(self, tmp_path):
        ts = _trace_store(tmp_path)
        with ts.run(intent="parent") as parent_id:
            with ts.run(intent="child_a") as a_id:
                pass
            with ts.run(intent="child_b") as b_id:
                pass
        conn = sqlite3.connect(str(ts.db_path))
        rows = conn.execute(
            "SELECT run_id, parent_run_id FROM runs WHERE parent_run_id = ?",
            (parent_id,),
        ).fetchall()
        ids = {r[0] for r in rows}
        assert ids == {a_id, b_id}

    def test_failed_child_does_not_corrupt_parent_chain(self, tmp_path):
        ts = _trace_store(tmp_path)
        try:
            with ts.run(intent="parent") as parent_id:
                with ts.run(intent="failing_child") as child_id:
                    raise RuntimeError("boom")
        except RuntimeError:
            pass
        conn = sqlite3.connect(str(ts.db_path))
        # Parent recorded as failed (since the exception escaped the parent
        # context too), child recorded as failed, parent link is intact.
        row_child = conn.execute(
            "SELECT parent_run_id, status, error FROM runs WHERE run_id = ?",
            (child_id,),
        ).fetchone()
        assert row_child[0] == parent_id
        assert row_child[1] == "failed"
        assert "boom" in row_child[2]


class TestSchemaMigration:
    def test_alter_table_is_idempotent(self, tmp_path):
        # First TraceStore creates the table with parent_run_id column.
        ts1 = _trace_store(tmp_path)
        # Second TraceStore on the same file re-runs the init; the
        # ALTER TABLE ADD COLUMN raises OperationalError which we catch.
        ts2 = TraceStore(db_path=ts1.db_path, retention_days=0)
        # If we got this far without exception, the migration is idempotent.
        with ts2.run(intent="x"):
            pass
        # And the column is queryable.
        conn = sqlite3.connect(str(ts2.db_path))
        cols = [r[1] for r in conn.execute("PRAGMA table_info(runs)")]
        assert "parent_run_id" in cols


class TestPruning:
    def test_run_does_not_prune_synchronously(self, tmp_path):
        class NoSyncPruneTraceStore(TraceStore):
            def __init__(self, *args, **kwargs):
                self.prune_calls = 0
                super().__init__(*args, **kwargs)

            def prune_old(self):
                self.prune_calls += 1

        ts = NoSyncPruneTraceStore(db_path=tmp_path / "trace.sqlite", retention_days=0)

        with ts.run(intent="no_sync_prune"):
            pass

        assert ts.prune_calls == 0
        ts.prune_old()
        assert ts.prune_calls == 1


if __name__ == "__main__":
    pytest.main([__file__, "-v"])


# ── R7-E: a finalize-write failure must not mask the body exception ─────


def test_run_body_exception_not_masked_by_finalize_db_error(tmp_path):
    ts = TraceStore(db_path=tmp_path / "trace.sqlite", retention_days=0)

    real_connect = ts._connect
    calls = {"n": 0}

    def flaky_connect(*a, **k):
        # First connect (INSERT runs row) succeeds; the finalize connect raises.
        calls["n"] += 1
        if calls["n"] >= 2:
            raise sqlite3.OperationalError("database is locked")
        return real_connect(*a, **k)

    ts._connect = flaky_connect

    # The body raises; the finalize UPDATE will hit the locked DB. The ORIGINAL
    # ValueError must propagate, not the OperationalError from the finally.
    with pytest.raises(ValueError, match="boom"):
        with ts.run(intent="x"):
            raise ValueError("boom")


# ── R7-F: time-window indexes exist and are used (not full scans) ───────


def test_ts_indexes_present_and_used(tmp_path):
    import sqlite3

    ts = TraceStore(db_path=tmp_path / "trace.sqlite", retention_days=30)
    con = sqlite3.connect(str(ts.db_path))
    names = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='index'")}
    for idx in (
        "idx_artifacts_type_ts",
        "idx_llm_calls_stage_ts",
        "idx_retrieval_events_ts",
        "idx_llm_calls_ts",
        "idx_artifacts_ts",
    ):
        assert idx in names, f"missing {idx}"

    def plan(q, p):
        return " | ".join(r[-1] for r in con.execute("EXPLAIN QUERY PLAN " + q, p))

    # Windowed analytics + prune queries must use an index, not a full scan.
    assert "USING INDEX" in plan(
        "SELECT * FROM artifacts WHERE artifact_type=? AND ts>=? ORDER BY ts DESC",
        ("x", "2026-01-01"),
    )
    assert "USING INDEX" in plan(
        "SELECT * FROM llm_calls WHERE stage=? AND ts>=? ORDER BY ts DESC", ("x", "2026-01-01")
    )
    assert "USING INDEX" in plan(
        "SELECT query_text FROM retrieval_events WHERE ts>=? ORDER BY ts DESC", ("2026-01-01",)
    )
    assert "SCAN" not in plan("DELETE FROM llm_calls WHERE ts<?", ("2026-01-01",))
    con.close()
