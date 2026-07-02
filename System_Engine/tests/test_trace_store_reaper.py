"""Startup reaper: orphaned 'running' runs (daemon died mid-run) get retired."""

import os
import sqlite3

os.environ.setdefault("LLM_PROVIDER", "vllm")

from services.trace_store import TraceStore


def _ts(tmp_path):
    return TraceStore(db_path=tmp_path / "trace.sqlite", retention_days=0)


def test_reap_marks_running_as_interrupted_only(tmp_path):
    ts = _ts(tmp_path)
    conn = sqlite3.connect(str(ts.db_path))
    conn.execute(
        "INSERT INTO runs(run_id,intent,status,started_at) "
        "VALUES('zombie','ingest','running','2026-01-01T00:00:00Z')"
    )
    conn.execute(
        "INSERT INTO runs(run_id,intent,status,started_at,ended_at) "
        "VALUES('done','dream','succeeded','2026-01-01T00:00:00Z','2026-01-01T00:01:00Z')"
    )
    conn.commit()
    conn.close()

    assert ts.reap_orphan_runs() == 1

    conn = sqlite3.connect(str(ts.db_path))
    z = conn.execute("SELECT status, ended_at, error FROM runs WHERE run_id='zombie'").fetchone()
    assert z[0] == "interrupted" and z[1] is not None and z[2]  # retired + stamped + reason
    assert conn.execute("SELECT status FROM runs WHERE run_id='done'").fetchone()[0] == "succeeded"
    conn.close()


def test_reap_is_noop_when_no_orphans(tmp_path):
    assert _ts(tmp_path).reap_orphan_runs() == 0
