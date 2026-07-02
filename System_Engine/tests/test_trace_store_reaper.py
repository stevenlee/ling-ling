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


# ── Periodic (age-gated) reaper — safe while the daemon is live (P4) ──


def test_stale_reap_retires_only_old_running_runs(tmp_path):
    from datetime import datetime, timedelta, timezone

    ts = _ts(tmp_path)
    old = (datetime.now(timezone.utc) - timedelta(hours=48)).isoformat()
    fresh = datetime.now(timezone.utc).isoformat()
    conn = sqlite3.connect(str(ts.db_path))
    conn.execute(
        "INSERT INTO runs(run_id,intent,status,started_at) VALUES('stale','ingest','running',?)",
        (old,),
    )
    conn.execute(
        "INSERT INTO runs(run_id,intent,status,started_at) VALUES('live','dream','running',?)",
        (fresh,),
    )
    conn.commit()
    conn.close()

    assert ts.reap_stale_runs(max_age_hours=24.0) == 1

    conn = sqlite3.connect(str(ts.db_path))
    assert (
        conn.execute("SELECT status FROM runs WHERE run_id='stale'").fetchone()[0] == "interrupted"
    )
    # A genuinely live run is NEVER touched by the periodic reaper.
    assert conn.execute("SELECT status FROM runs WHERE run_id='live'").fetchone()[0] == "running"
    conn.close()


def test_stale_reap_noop_on_fresh_db(tmp_path):
    assert _ts(tmp_path).reap_stale_runs() == 0
