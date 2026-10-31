"""Isolated, checkpointed evaluation of the production scope judge on A01 cases."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Callable

from core.config import WIKI_VAULT_DIR, settings
from services.evidence_audit import text_hash, validate_judge_context
from services.evidence_contract import CONTRACT_VERSION, validate_scope_verdict
from services.evidence_evaluation import EvidenceDataset, evaluate_predictions
from services.ingest.atomic_io import atomic_write_text


def validate_output_dir(path: Path) -> Path:
    resolved = path.resolve()
    if resolved == WIKI_VAULT_DIR.resolve() or WIKI_VAULT_DIR.resolve() in resolved.parents:
        raise ValueError("模型評估輸出必須在 live vault 之外")
    return resolved


def _save(path: Path, value: dict) -> None:
    atomic_write_text(path, json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False))


def run_model_evaluation(
    dataset: EvidenceDataset,
    llm,
    output_dir: Path,
    *,
    prompt: str,
    max_cases: int = 24,
    max_errors: int = 1,
    can_start: Callable[[], bool] = lambda: True,
    progress: Callable[[str], None] = lambda message: None,
) -> dict:
    """No caller retries or maintenance/apply calls; errors/missing keep denominator.

    Checkpoint an error-labelled started row BEFORE each expensive call. A crash
    preserves its uncertain outcome. Reusing a run directory is refused; this
    version deliberately has no automatic resume/replay or hidden model spend.
    """
    output_dir = validate_output_dir(output_dir)
    if max_cases < 1 or max_cases > len(dataset.cases) or max_errors < 1:
        raise ValueError("評估案例／失敗上限無效")
    if not isinstance(prompt, str) or not prompt.strip():
        raise ValueError("缺少評估 prompt")
    if (output_dir / "manifest.json").exists():
        raise ValueError("此評估目錄已有 run，不覆寫或自動重跑")
    manifest = {
        "schema_version": 1,
        "dataset_id": dataset.id,
        "dataset_sha256": dataset.sha256,
        "annotation_status": dataset.annotation_status,
        "contract_version": CONTRACT_VERSION,
        "provider": llm.provider,
        "model": llm.model,
        "prompt_body": prompt,
        "prompt_sha256": text_hash(prompt),
        "max_cases": max_cases,
        "max_errors": max_errors,
        "evaluation_only": True,
        "decoding": {
            "temperature": 0.1,
            "max_output_tokens": settings.MAX_OUTPUT,
            "scope_attempt_limit": 2,
            "transport_attempt_limit": 3,
        },
    }
    # The explicit client trace must also live in this run, never the daemon DB.
    trace = getattr(llm, "trace_store", None)
    if trace is not None and Path(trace.db_path).resolve() != output_dir / "trace.sqlite":
        raise ValueError("評估 trace 必須使用本 run 的獨立 trace.sqlite")
    _save(output_dir / "manifest.json", manifest)
    predictions: dict = {
        "dataset_id": dataset.id,
        "dataset_sha256": dataset.sha256,
        "predictions": [],
    }
    details: dict = {"status": "running", "cases": []}
    errors = 0

    def checkpoint() -> dict:
        _save(output_dir / "predictions.json", predictions)
        _save(output_dir / "details.json", details)
        result = evaluate_predictions(dataset, predictions)
        _save(output_dir / "result.json", result)
        return result

    checkpoint()
    try:
        for case in dataset.cases[:max_cases]:
            if not can_start():
                details["status"] = "stopped_daemon_busy_or_unavailable"
                break
            row = {"case_id": case.id, "label": "error"}
            detail: dict = {
                "case_id": case.id,
                "status": "started",
                "error": "呼叫已開始，結果尚未確認",
            }
            predictions["predictions"].append(row)
            details["cases"].append(detail)
            checkpoint()  # persistence failure must stop BEFORE a new request
            started = time.perf_counter()
            try:
                raw = llm.judge_evidence_scope(
                    claim=case.claim,
                    falsifier=case.falsifier,
                    title=case.source_title,
                    passage=case.passage,
                    claim_id=case.id,
                )
                verdict = validate_scope_verdict(raw, case.passage)
                context = validate_judge_context(raw.get("audit_context"))
                if any(
                    context[key] != manifest[key] for key in ("provider", "model", "prompt_sha256")
                ):
                    raise ValueError("模型或 prompt 版本於評估期間改變")
                row["label"] = (
                    "contradiction" if verdict["relation"] == "contradicts" else verdict["relation"]
                )
                detail.update(
                    status="completed",
                    verdict=verdict,
                    context={key: value for key, value in context.items() if key != "prompt_body"},
                )
                detail.pop("error")
            except Exception as error:
                errors += 1
                detail.update(status="error", error=f"{type(error).__name__}: {error}")
            finally:
                detail["elapsed_seconds"] = round(time.perf_counter() - started, 3)
            checkpoint()
            progress(f"{len(predictions['predictions'])}/{max_cases} {case.id}: {row['label']}")
            if errors >= max_errors:
                details["status"] = "stopped_error_budget"
                break
        else:
            details["status"] = "completed" if max_cases == len(dataset.cases) else "limited"
    except BaseException:
        details["status"] = "interrupted"
        raise
    finally:
        result = checkpoint()
    return result
