"""Durable A2 index intents. Caller must hold the daemon's global busy lock.

An intent is durable BEFORE the vault commit. Recovery reads current files,
never writes Cortex or repeats reinforcement, and force-rebuilds partial RAG
writes. The existing daily A2 task owns recovery; no extra thread or LLM call.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from core.config import (
    CORTEX_INDEX_REPAIR_BACKOFF_SECONDS,
    CORTEX_INDEX_REPAIR_BATCH,
    CORTEX_INDEX_REPAIR_MAX_BACKOFF_SECONDS,
)
from services.cortex_store import CortexPage, load_all_pages, parse_cortex_page, render_cortex_page
from services.ingest.atomic_io import atomic_write_text


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _slot(repair_dir: Path, path: Path, cortex_dir: Path) -> Path:
    relative = path.resolve().relative_to(cortex_dir.resolve()).as_posix()
    return repair_dir / f"{_hash(relative)}.json"


def _record(page: CortexPage, cortex_dir: Path, content: str) -> dict:
    return {
        "schema_version": 1,
        "path": page.path.resolve().relative_to(cortex_dir.resolve()).as_posix(),
        "claim_id": page.claim_id,
        "content_hash": _hash(content),
        "attempts": 0,
        "next_retry_at": 0,
        "last_error": "",
    }


def _save(slot: Path, record: dict) -> None:
    atomic_write_text(slot, json.dumps(record, ensure_ascii=False, indent=2))


def enqueue_index_repair(page: CortexPage, repair_dir: Path, cortex_dir: Path) -> Path:
    """Persist write-ahead intent; failure must prevent the vault commit."""
    slot = _slot(repair_dir, page.path, cortex_dir)
    _save(slot, _record(page, cortex_dir, render_cortex_page(page)))
    return slot


@dataclass
class RepairResult:
    attempted: int = 0
    repaired: int = 0
    removed: int = 0
    pending: int = 0
    errors: list[str] = field(default_factory=list)

    @property
    def summary(self) -> str:
        return f"索引追補：完成 {self.repaired}、已刪頁取消 {self.removed}、待處理 {self.pending}"


def _valid_record(value, slot: Path, cortex_dir: Path) -> bool:
    if not isinstance(value, dict) or value.get("schema_version") != 1:
        return False
    if not all(
        isinstance(value.get(k), str) and value[k] for k in ("path", "claim_id", "content_hash")
    ):
        return False
    try:
        path = cortex_dir / value["path"]
        return path.suffix == ".md" and _slot(slot.parent, path, cortex_dir) == slot
    except (OSError, ValueError, RuntimeError):
        return False


def _number(value, default=0):
    try:
        return (
            value
            if type(value) in (int, float) and math.isfinite(value) and value >= 0
            else default
        )
    except OverflowError:
        return default


def repair_index_slot(slot: Path, rag, cortex_dir: Path, now: datetime) -> tuple[str, str]:
    """Return repaired/removed/waiting/error; preserve every failed intent."""
    started = False
    try:
        try:
            record = json.loads(slot.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            record = None
        if not _valid_record(record, slot, cortex_dir):
            # A corrupted intent can be reconstructed from its path-addressed
            # slot and ground truth. Unmatched slots remain visible for review.
            page = next(
                (
                    p
                    for p in load_all_pages(cortex_dir)
                    if _slot(slot.parent, p.path, cortex_dir) == slot
                ),
                None,
            )
            if page is None:
                raise ValueError("索引意圖損壞且找不到對應頁面")
            record = _record(page, cortex_dir, render_cortex_page(page))
        path = cortex_dir / record["path"]
        try:
            snapshot = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            # VaultWatcher/orphan sweep owns deletion from RAG. Never recreate
            # a removed page from an intent or its former content.
            slot.unlink()
            return "removed", ""
        page = parse_cortex_page(path, text=snapshot)
        if page is None or page.claim_id != record["claim_id"]:
            raise ValueError("Cortex 頁面無法解析或 claim_id 已變更")
        basis = _hash(snapshot)
        if basis != record["content_hash"]:
            record = _record(page, cortex_dir, snapshot)
        retry_at = _number(record.get("next_retry_at"))
        if retry_at > now.timestamp() + CORTEX_INDEX_REPAIR_MAX_BACKOFF_SECONDS:
            retry_at = 0  # corrupted distant-future timestamps must not freeze recovery
        if now.timestamp() < retry_at:
            return "waiting", ""
        record["attempts"] = min(int(_number(record.get("attempts"))), 1_000_000) + 1
        delay = min(
            CORTEX_INDEX_REPAIR_MAX_BACKOFF_SECONDS,
            CORTEX_INDEX_REPAIR_BACKOFF_SECONDS * 2 ** min(record["attempts"] - 1, 16),
        )
        record["next_retry_at"] = now.timestamp() + delay
        _save(slot, record)  # attempt/backoff durable BEFORE embedding/DB calls
        started = True
        try:
            result = rag.add_document(path, page.claim_id, snapshot, strict=True, force=True)
            if result is False:
                raise RuntimeError("正文索引未成功")
            if page.status in ("dormant", "falsified"):
                result = rag.remove_facets(path, strict=True)
            else:
                result = rag.add_facets(path, page.claim_id, [page.claim], strict=True)
            if result is False:
                raise RuntimeError("facet 索引未成功")
            if _hash(path.read_text(encoding="utf-8")) != basis:
                raise RuntimeError("索引期間頁面已變更，保留意圖以追補最新版")
            slot.unlink()  # only after both indexes and snapshot validation
            return "repaired", ""
        except Exception as e:
            record["last_error"] = str(e)[:300]
            _save(slot, record)
            raise
    except Exception as e:
        logging.warning("Cortex index repair pending (%s): %s", slot.name, e)
        return "error" if started else "invalid", str(e)[:300]


def repair_pending_indexes(rag, cortex_dir: Path, repair_dir: Path, now: datetime) -> RepairResult:
    """Bound each daily pass; retry even when no thin claims remain."""
    result = RepairResult()
    # Failed attempts rewrite their slot, so older untouched work goes first.
    # Malformed slots do not consume the embedding budget or starve valid work.
    for slot in sorted(repair_dir.glob("*.json"), key=lambda p: p.stat().st_mtime):
        if result.attempted >= CORTEX_INDEX_REPAIR_BATCH:
            break
        status, error = repair_index_slot(slot, rag, cortex_dir, now)
        if status not in ("waiting", "invalid"):
            result.attempted += 1
        if status == "repaired":
            result.repaired += 1
        elif status == "removed":
            result.removed += 1
        elif status in ("error", "invalid"):
            result.errors.append(error)
    result.pending = len(list(repair_dir.glob("*.json")))
    return result
