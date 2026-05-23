"""DB migration runner.

Discovers migration modules in `maintenance.migrations`, tracks which
have run in `<DATABASE_DIR>/migrations.json`, and applies whatever is
pending. Idempotent — safe to call from daemon startup.

CLI (run from System_Engine/):

    python -m maintenance.migrate              # apply pending
    python -m maintenance.migrate --list       # show all migrations + status
    python -m maintenance.migrate --dry-run    # report pending without applying
    python -m maintenance.migrate --force ID   # rerun a specific migration

When adding a new migration, drop a `NNN_short_name.py` file in
`maintenance/migrations/` that exposes `MIGRATION_ID`, `DESCRIPTION`,
and `run(rag_manager) -> dict`. See `001_normalize_chroma_tags.py`.
"""

from __future__ import annotations

import argparse
import importlib
import json
import logging
import pkgutil
import sys
import time
from datetime import datetime
from pathlib import Path
from types import ModuleType


# Allow `python System_Engine/maintenance/migrate.py` direct invocation
# (mirrors `repair_db.py`'s convention).
sys.path.append(str(Path(__file__).parent.parent.absolute()))

from core.config import DATABASE_DIR  # noqa: E402


STATE_FILE = DATABASE_DIR / "migrations.json"


# ── State persistence ────────────────────────────────────────────────────

def _load_state() -> dict:
    if not STATE_FILE.exists():
        return {"applied": []}
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception as e:
        logging.error(f"migrate: failed to read {STATE_FILE.name}: {e}")
        return {"applied": []}


def _save_state(state: dict) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")


def _applied_ids(state: dict) -> set[str]:
    return {entry["id"] for entry in state.get("applied", [])}


# ── Migration discovery ──────────────────────────────────────────────────

def _discover() -> list[ModuleType]:
    """Return migration modules sorted by filename (= MIGRATION_ID prefix)."""
    from maintenance import migrations as pkg

    mods: list[ModuleType] = []
    for info in pkgutil.iter_modules(pkg.__path__):
        if info.name.startswith("_"):
            continue
        mod = importlib.import_module(f"maintenance.migrations.{info.name}")
        if not all(hasattr(mod, attr) for attr in ("MIGRATION_ID", "DESCRIPTION", "run")):
            logging.warning(f"migrate: skipping {info.name} (missing required attrs)")
            continue
        mods.append(mod)
    mods.sort(key=lambda m: m.MIGRATION_ID)
    return mods


# ── Apply ────────────────────────────────────────────────────────────────

def _record(state: dict, mig_id: str, stats: dict, duration_s: float, forced: bool) -> None:
    state.setdefault("applied", []).append({
        "id": mig_id,
        "ts": datetime.now().isoformat(timespec="seconds"),
        "duration_s": round(duration_s, 2),
        "stats": stats,
        "forced": forced,
    })


def _run_one(mod: ModuleType, rag_manager, state: dict, forced: bool) -> dict:
    logging.info(f"migrate: applying {mod.MIGRATION_ID} — {mod.DESCRIPTION}")
    t0 = time.monotonic()
    stats = mod.run(rag_manager) or {}
    _record(state, mod.MIGRATION_ID, stats, time.monotonic() - t0, forced)
    _save_state(state)
    logging.info(f"migrate: done {mod.MIGRATION_ID} — {stats}")
    return stats


def apply_pending(rag_manager) -> list[dict]:
    """Apply every not-yet-applied migration. Returns per-migration stats.

    Safe to call from daemon startup: catches per-migration exceptions so
    one bad migration doesn't block boot. The exception is logged and the
    migration stays in 'pending' so it'll be retried next launch.
    """
    state = _load_state()
    done = _applied_ids(state)
    out: list[dict] = []
    for mod in _discover():
        if mod.MIGRATION_ID in done:
            continue
        try:
            stats = _run_one(mod, rag_manager, state, forced=False)
            out.append({"id": mod.MIGRATION_ID, "stats": stats})
        except Exception as e:
            logging.exception(f"migrate: {mod.MIGRATION_ID} failed: {e}")
            # Stop the chain — later migrations may depend on this one.
            break
    return out


def force_run(rag_manager, mig_id: str) -> dict:
    state = _load_state()
    for mod in _discover():
        if mod.MIGRATION_ID == mig_id:
            return _run_one(mod, rag_manager, state, forced=True)
    raise SystemExit(f"migrate: no such migration: {mig_id}")


# ── CLI ──────────────────────────────────────────────────────────────────

def _cmd_list(args) -> int:
    state = _load_state()
    done = _applied_ids(state)
    last_ts = {entry["id"]: entry["ts"] for entry in state.get("applied", [])}
    print(f"{'STATUS':10}  {'ID':40}  {'LAST_TS':20}  DESCRIPTION")
    print("-" * 100)
    for mod in _discover():
        status = "applied" if mod.MIGRATION_ID in done else "pending"
        ts = last_ts.get(mod.MIGRATION_ID, "—")
        print(f"{status:10}  {mod.MIGRATION_ID:40}  {ts:20}  {mod.DESCRIPTION}")
    return 0


def _cmd_dry_run(args) -> int:
    state = _load_state()
    done = _applied_ids(state)
    pending = [m for m in _discover() if m.MIGRATION_ID not in done]
    if not pending:
        print("migrate: nothing pending.")
        return 0
    print(f"migrate: {len(pending)} pending migration(s):")
    for mod in pending:
        print(f"  • {mod.MIGRATION_ID} — {mod.DESCRIPTION}")
    return 0


def _cmd_apply(args) -> int:
    from services.rag_manager import RAGManager
    rag = RAGManager()
    results = apply_pending(rag)
    if not results:
        print("migrate: nothing to do.")
    else:
        for r in results:
            print(f"  ✓ {r['id']}  {r['stats']}")
    return 0


def _cmd_force(args) -> int:
    from services.rag_manager import RAGManager
    rag = RAGManager()
    stats = force_run(rag, args.force)
    print(f"  ✓ {args.force}  {stats}")
    return 0


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    p = argparse.ArgumentParser(description="ChromaDB migration runner.")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--list", action="store_true", help="List all migrations + status")
    g.add_argument("--dry-run", action="store_true", help="Show what would run, don't apply")
    g.add_argument("--force", metavar="ID", help="Re-run a specific migration by id")
    args = p.parse_args(argv)

    if args.list:
        return _cmd_list(args)
    if args.dry_run:
        return _cmd_dry_run(args)
    if args.force:
        return _cmd_force(args)
    return _cmd_apply(args)


if __name__ == "__main__":
    raise SystemExit(main())
