"""Private, immutable A2 judgment snapshots; callers hold the daemon busy lock.

Audit files are never automatically expired, including unreferenced preparations.
A file proves that a judgment was recorded, not that a Cortex commit occurred.
The current Cortex evidence/counterpoint reference is the commit ground truth.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from datetime import datetime
from pathlib import Path

from services.cortex_store import parse_cortex_page
from services.evidence_contract import CONTRACT_VERSION, validate_scope_verdict
from services.ingest.atomic_io import atomic_write_text

_HEX = re.compile(r"[0-9a-f]{64}")


def text_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _canonical(value: dict) -> str:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    )


def validate_judge_context(value: object) -> dict:
    if not isinstance(value, dict) or not value:
        raise ValueError("缺少模型與 prompt 稽核資訊")
    for key in ("provider", "model", "prompt_asset", "prompt_sha256", "prompt_body"):
        if not isinstance(value.get(key), str) or not value[key].strip():
            raise ValueError(f"稽核 {key} 必須是非空字串")
    if (
        not _HEX.fullmatch(value["prompt_sha256"])
        or text_hash(value["prompt_body"]) != value["prompt_sha256"]
    ):
        raise ValueError("prompt hash 無效")
    if (
        type(value.get("contract_version")) is not int
        or value["contract_version"] != CONTRACT_VERSION
    ):
        raise ValueError("模型稽核契約版本不符")
    if type(value.get("scope_attempt")) is not int or value["scope_attempt"] not in (1, 2):
        raise ValueError("模型稽核 attempt 無效")
    trace = value.get("trace_run_id")
    if trace is not None and (not isinstance(trace, str) or not trace.strip()):
        raise ValueError("trace run ID 無效")
    if "trace_run_id" not in value:
        raise ValueError("必須明示 trace run ID，無 trace 時為 null")
    return {
        key: value[key]
        for key in (
            "provider",
            "model",
            "prompt_asset",
            "prompt_sha256",
            "prompt_body",
            "contract_version",
            "scope_attempt",
            "trace_run_id",
        )
    }


def validate_payload(payload: object) -> dict:
    if not isinstance(payload, dict):
        raise ValueError("稽核 payload 必須是 object")
    basis = payload.get("basis")
    source = payload.get("source")
    if not isinstance(basis, dict) or not isinstance(source, dict):
        raise ValueError("稽核缺少 basis 或 source")
    snapshot = basis.get("snapshot")
    if not isinstance(snapshot, str) or text_hash(snapshot) != basis.get("sha256"):
        raise ValueError("稽核 basis hash 不符")
    page = parse_cortex_page(Path("audit.md"), text=snapshot)
    if page is None or any(
        getattr(page, key) != basis.get(key) for key in ("claim_id", "claim", "falsifier")
    ):
        raise ValueError("稽核 basis 與主張不符")
    if not isinstance(basis.get("path"), str) or not basis["path"]:
        raise ValueError("缺少 Cortex path")
    passage = source.get("passage")
    if not isinstance(passage, str) or text_hash(passage) != source.get("sha256"):
        raise ValueError("稽核 passage hash 不符")
    for key in ("title", "family", "family_method"):
        if not isinstance(source.get(key), str) or not source[key]:
            raise ValueError(f"缺少來源 {key}")
    if not isinstance(source.get("path"), str):
        raise ValueError("來源 path 必須明示，未知可為空")
    extent = source.get("range")
    if not isinstance(extent, dict) or extent != {
        "coordinate": "retrieved_chunk_prefix",
        "start": 0,
        "end": len(passage),
    }:
        raise ValueError("稽核範圍必須對應實際送審 chunk prefix，不冒充文件 offset")
    validate_scope_verdict(payload.get("verdict"), passage)
    validate_judge_context(payload.get("judge"))
    _canonical(payload)  # rejects non-JSON values and non-finite distances
    return payload


def audit_id(payload: object) -> str:
    return text_hash(_canonical(validate_payload(payload)))


def load_audit(audit_dir: Path, identifier: str) -> dict:
    """Read and verify an immutable record without trusting its filename."""
    if not isinstance(identifier, str) or not _HEX.fullmatch(identifier):
        raise ValueError("audit ID 無效")
    record = json.loads((audit_dir / f"{identifier}.json").read_text(encoding="utf-8"))
    if (
        not isinstance(record, dict)
        or type(record.get("schema_version")) is not int
        or record["schema_version"] != 1
    ):
        raise ValueError("不支援的 audit schema")
    if record.get("audit_id") != identifier or audit_id(record.get("payload")) != identifier:
        raise ValueError("audit 內容與 ID 不符")
    if not isinstance(record.get("recorded_at"), str) or not record["recorded_at"]:
        raise ValueError("audit 缺少記錄時間")
    return record


def _sync_directory(directory: Path) -> None:
    descriptor = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def persist_audit(audit_dir: Path, payload: dict, now: datetime) -> str:
    """Persist before vault commit; corrupt existing slots fail closed, never overwrite."""
    identifier = audit_id(payload)
    slot = audit_dir / f"{identifier}.json"
    if slot.exists():
        load_audit(audit_dir, identifier)
    else:
        record = {
            "schema_version": 1,
            "audit_id": identifier,
            "recorded_at": now.isoformat(timespec="seconds"),
            "payload": payload,
        }
        atomic_write_text(slot, json.dumps(record, ensure_ascii=False, indent=2, allow_nan=False))
    # Atomic writer fsyncs file content; directory sync makes its name durable
    # before the independently committed Cortex page can refer to it.
    _sync_directory(audit_dir)
    _sync_directory(audit_dir.parent)
    return identifier
