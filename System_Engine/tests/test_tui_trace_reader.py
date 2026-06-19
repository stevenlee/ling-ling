"""The TUI reads daemon state strictly read-only (no ChromaDB, no writes)."""
import os
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.absolute()))
os.environ.setdefault("LLM_PROVIDER", "vllm")

import tui.trace_reader as tr


def _make_db(path: Path):
    con = sqlite3.connect(path)
    con.execute(
        "CREATE TABLE runs (run_id TEXT, intent TEXT, agent TEXT, status TEXT, "
        "started_at TEXT, ended_at TEXT)"
    )
    con.executemany(
        "INSERT INTO runs VALUES (?,?,?,?,?,?)",
        [
            ("r1", "insight", "InsightAgent", "succeeded", "2026-06-19T01:00:00", "2026-06-19T01:01:00"),
            ("r2", "maintenance.cortex_consolidation", "CortexConsolidation", "running", "2026-06-19T02:00:00", None),
        ],
    )
    con.commit()
    con.close()


def test_recent_runs_reads_readonly(tmp_path, monkeypatch):
    db = tmp_path / "llm_trace.sqlite"
    _make_db(db)
    monkeypatch.setattr(tr, "TRACE_DB", db)
    runs = tr.recent_runs(5)
    assert [r["run_id"] for r in runs] == ["r2", "r1"]   # newest first
    assert runs[0]["status"] == "running"


def test_current_run_only_when_running(tmp_path, monkeypatch):
    db = tmp_path / "llm_trace.sqlite"
    _make_db(db)
    monkeypatch.setattr(tr, "TRACE_DB", db)
    cur = tr.current_run()
    assert cur and cur["run_id"] == "r2"


def test_missing_db_is_empty(tmp_path, monkeypatch):
    monkeypatch.setattr(tr, "TRACE_DB", tmp_path / "nope.sqlite")
    assert tr.recent_runs() == []
    assert tr.current_run() is None


def test_is_busy_reflects_lock_file(tmp_path, monkeypatch):
    lock = tmp_path / ".kb_lock"
    monkeypatch.setattr(tr, "LOCK_FILE", lock)
    monkeypatch.setattr(tr, "DAEMON_STATUS_FILE", tmp_path / "none.json")
    assert tr.is_busy() is False
    lock.write_text("", encoding="utf-8")
    assert tr.is_busy() is True


def test_is_busy_reflects_daemon_status_file(tmp_path, monkeypatch):
    status = tmp_path / "daemon_status.json"
    monkeypatch.setattr(tr, "LOCK_FILE", tmp_path / "no.lock")
    monkeypatch.setattr(tr, "DAEMON_STATUS_FILE", status)
    status.write_text('{"busy": true, "message": "Maintenance: insight"}', encoding="utf-8")
    assert tr.is_busy() is True
    status.write_text('{"busy": false, "message": "idle"}', encoding="utf-8")
    assert tr.is_busy() is False


def test_daemon_alive_false_for_dead_pid(tmp_path, monkeypatch):
    pid_file = tmp_path / "daemon.pid"
    pid_file.write_text("999999", encoding="utf-8")  # almost certainly not a live PID
    monkeypatch.setattr(tr, "PID_FILE", pid_file)
    assert tr.daemon_alive() is False


def test_daemon_alive_true_for_self(tmp_path, monkeypatch):
    import os
    pid_file = tmp_path / "daemon.pid"
    pid_file.write_text(str(os.getpid()), encoding="utf-8")
    monkeypatch.setattr(tr, "PID_FILE", pid_file)
    assert tr.daemon_alive() is True


def test_status_summary_busy_only_when_alive(tmp_path, monkeypatch):
    import os
    pid_file = tmp_path / "daemon.pid"
    status = tmp_path / "daemon_status.json"
    status.write_text('{"busy": true, "message": "Maintenance: dream"}', encoding="utf-8")
    monkeypatch.setattr(tr, "DAEMON_STATUS_FILE", status)
    monkeypatch.setattr(tr, "LOCK_FILE", tmp_path / "no.lock")
    monkeypatch.setattr(tr, "TRACE_DB", tmp_path / "no.sqlite")
    # Daemon alive → busy reflects the status file.
    pid_file.write_text(str(os.getpid()), encoding="utf-8")
    monkeypatch.setattr(tr, "PID_FILE", pid_file)
    s = tr.status_summary()
    assert s["alive"] is True and s["busy"] is True
    assert s["message"] == "Maintenance: dream"
    # Daemon dead → never busy, even with a stale status file saying busy.
    pid_file.write_text("999999", encoding="utf-8")
    s = tr.status_summary()
    assert s["alive"] is False and s["busy"] is False and s["message"] is None
