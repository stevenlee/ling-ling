"""Read-only views into the daemon's state for the TUI.

Everything here is strictly read-only and never opens ChromaDB. The trace DB is
opened in SQLite read-only URI mode (the daemon is the sole writer; WAL allows
concurrent readers). State files are plain JSON written atomically by the daemon.
"""

from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path

from core.config import (
    DAEMON_STATUS_FILE,
    DATABASE_DIR,
    DAYDREAM_STATE_FILE,
    FACET_BACKFILL_STATE_FILE,
    FROM_LLM_DIR,
    LLM_PROVIDER,
    MAINTENANCE_LOG_FILE,
    MAINTENANCE_STATE_FILE,
    PID_FILE,
    PROJECT_ROOT,
    settings,
)

TRACE_DB = DATABASE_DIR / "llm_trace.sqlite"
LOCK_FILE = PROJECT_ROOT / ".kb_lock"


def daemon_alive() -> bool:
    """Is the daemon process actually running? (PID file + liveness probe.)
    Lets us ignore a stale status file / zombie 'running' trace rows."""
    try:
        pid = int(Path(PID_FILE).read_text(encoding="utf-8").strip())
    except Exception:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True   # exists but owned by another user — still alive
    except OSError:
        return False
    return True


def daemon_status() -> dict:
    """Live busy flag + activity message written by the daemon's ui.set_status."""
    return _read_json(DAEMON_STATUS_FILE)


def is_busy() -> bool:
    """True busy state: the daemon's own status flag, or a hard .kb_lock."""
    return bool(daemon_status().get("busy")) or LOCK_FILE.exists()


def _read_json(path: Path) -> dict:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def recent_runs(limit: int = 20) -> list[dict]:
    """Most-recent runs from the trace DB (read-only). Empty on any error."""
    if not TRACE_DB.exists():
        return []
    try:
        con = sqlite3.connect(f"file:{TRACE_DB}?mode=ro", uri=True, timeout=2.0)
        con.row_factory = sqlite3.Row
        try:
            rows = con.execute(
                "SELECT run_id, intent, agent, status, started_at, ended_at "
                "FROM runs ORDER BY started_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        finally:
            con.close()
        return [dict(r) for r in rows]
    except sqlite3.Error:
        return []


def current_run() -> dict | None:
    runs = recent_runs(1)
    run = runs[0] if runs else None
    if run and run.get("status") == "running":
        return run
    return None


def scheduler_state() -> dict:
    return _read_json(MAINTENANCE_STATE_FILE)


def daydream_state() -> dict:
    return _read_json(DAYDREAM_STATE_FILE)


def facet_state() -> dict:
    return _read_json(FACET_BACKFILL_STATE_FILE)


def tail_maintenance_log(n: int = 20) -> list[str]:
    try:
        lines = Path(MAINTENANCE_LOG_FILE).read_text(encoding="utf-8").splitlines()
        return [ln for ln in lines if ln.strip().startswith("## [")][-n:]
    except Exception:
        return []


def recent_results(n: int = 15) -> list[dict]:
    """Newest files in fromLingLing/ (name + mtime), newest first."""
    try:
        files = [
            p for p in Path(FROM_LLM_DIR).iterdir()
            if p.is_file() and not p.name.startswith(".")
        ]
    except Exception:
        return []
    files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return [{"name": p.name, "path": str(p), "mtime": p.stat().st_mtime} for p in files[:n]]


def status_summary() -> dict:
    """One-glance header data. Reloads Scripture so role/dreaming reflect edits.

    Busy/activity come from the daemon's own status file (the in-memory flag is
    invisible cross-process; .kb_lock only marks hard locks). Everything is
    gated on the daemon actually being alive, so a stale status file or zombie
    'running' trace row never shows as live activity.
    """
    try:
        settings.reload()
    except Exception:
        pass
    alive = daemon_alive()
    ds = daemon_status() if alive else {}
    busy = bool(ds.get("busy")) or (alive and LOCK_FILE.exists())
    runs = recent_runs(1)
    last = runs[0] if runs else None
    return {
        "alive": alive,
        "busy": busy,
        "message": ds.get("message") if busy else None,
        "last": last,
        "provider": LLM_PROVIDER,
        "role": getattr(settings, "AGENT_ROLE", "?"),
        "dreaming": f"{getattr(settings, 'DREAMING_FROM', '?')}-{getattr(settings, 'DREAMING_TO', '?')}",
        "daydream": getattr(settings, "DAYDREAM_ENABLED", None),
    }
