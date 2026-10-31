"""Offline reference-case evaluation; never invokes models or maintenance.

Labels describe passage scope, independently of the production A2 JSON schema.
A matching score establishes agreement with the versioned reference labels,
not source independence, semantic truth, or permission to mutate Cortex.
"""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any, NoReturn

LABELS = ("full_support", "partial_support", "analogy", "contradiction", "neutral")


@dataclass(frozen=True)
class EvidenceCase:
    id: str
    claim: str
    falsifier: str
    source_title: str
    passage: str
    expected_label: str
    rationale: str
    tags: tuple[str, ...]


@dataclass(frozen=True)
class EvidenceDataset:
    id: str
    annotation_status: str
    cases: tuple[EvidenceCase, ...]
    sha256: str

    def inputs(self) -> dict:
        """Export only inputs; do not leak reference labels to the judge."""
        return {
            "dataset_id": self.id,
            "dataset_sha256": self.sha256,
            "cases": [
                {
                    key: getattr(case, key)
                    for key in ("id", "claim", "falsifier", "source_title", "passage")
                }
                for case in self.cases
            ],
        }


def _object(pairs: list[tuple[str, Any]]) -> dict:
    result: dict = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"JSON 欄位重複：{key}")
        result[key] = value
    return result


def _invalid_constant(value: str) -> NoReturn:
    raise ValueError(f"JSON 不接受非有限數值：{value}")


def read_json(path: Path) -> Any:
    return json.loads(
        path.read_text(encoding="utf-8"),
        object_pairs_hook=_object,
        parse_constant=_invalid_constant,
    )


def _mapping(value: Any, name: str) -> dict:
    if not isinstance(value, dict):
        raise ValueError(f"{name} 必須是 object")
    return value


def _text(row: dict, key: str) -> str:
    value = row.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{key} 必須是非空字串")
    return value


def load_dataset(path: Path) -> EvidenceDataset:
    raw = _mapping(read_json(path), "dataset")
    if type(raw.get("schema_version")) is not int or raw["schema_version"] != 1:
        raise ValueError("不支援的 dataset schema_version")
    dataset_id = _text(raw, "dataset_id")
    annotation_status = _text(raw, "annotation_status")
    if annotation_status not in ("assistant_authored_pending_review", "reviewed"):
        raise ValueError("不支援的 annotation_status")
    rows = raw.get("cases")
    if not isinstance(rows, list) or not rows:
        raise ValueError("cases 必須是非空陣列")
    cases = []
    ids: set[str] = set()
    for row in rows:
        row = _mapping(row, "case")
        cid = _text(row, "id")
        if cid in ids:
            raise ValueError(f"case id 重複：{cid}")
        ids.add(cid)
        label = _text(row, "expected_label")
        if label not in LABELS:
            raise ValueError(f"未知 expected_label：{label}")
        tags = row.get("tags")
        if (
            not isinstance(tags, list)
            or not tags
            or not all(isinstance(t, str) and t.strip() for t in tags)
        ):
            raise ValueError("tags 必須是非空字串陣列")
        provenance = _mapping(row.get("provenance"), "provenance")
        if provenance.get("kind") != "synthetic":
            raise ValueError("v1 評估集只接受明示 synthetic 的案例")
        passage = _text(row, "passage")
        if len(passage) > 2000:
            raise ValueError(f"{cid} passage 超過目前 A2 的 2000 字輸入預算")
        cases.append(
            EvidenceCase(
                id=cid,
                claim=_text(row, "claim"),
                falsifier=_text(row, "falsifier"),
                source_title=_text(row, "source_title"),
                passage=passage,
                expected_label=label,
                rationale=_text(row, "rationale"),
                tags=tuple(tags),
            )
        )
    if {case.expected_label for case in cases} != set(LABELS):
        raise ValueError("評估集必須包含全部五種分類，避免缺少支持／反證對照")
    basis = json.dumps(raw, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    sha256 = hashlib.sha256(basis.encode("utf-8")).hexdigest()
    return EvidenceDataset(dataset_id, annotation_status, tuple(cases), sha256)


def evaluate_predictions(dataset: EvidenceDataset, payload: Any) -> dict:
    """Score exact labels. Missing/error cases remain failures in the denominator."""
    raw = _mapping(payload, "predictions")
    if raw.get("dataset_id") != dataset.id:
        raise ValueError("predictions dataset_id 與評估集不符")
    if raw.get("dataset_sha256") != dataset.sha256:
        raise ValueError("predictions dataset_sha256 與評估集內容不符")
    rows = raw.get("predictions")
    if not isinstance(rows, list):
        raise ValueError("predictions 必須是陣列")
    ids = {case.id for case in dataset.cases}
    predictions: dict[str, str] = {}
    for row in rows:
        row = _mapping(row, "prediction")
        cid = _text(row, "case_id")
        if cid not in ids:
            raise ValueError(f"未知 case_id：{cid}")
        if cid in predictions:
            raise ValueError(f"prediction case_id 重複：{cid}")
        label = _text(row, "label")
        if label not in (*LABELS, "error"):
            raise ValueError(f"未知 prediction label：{label}")
        predictions[cid] = label

    counts = Counter(case.expected_label for case in dataset.cases)
    matched: Counter[str] = Counter()
    confusion: dict[str, Counter[str]] = {label: Counter() for label in LABELS}
    results = []
    unsafe_full_support = missing = errors = 0
    for case in dataset.cases:
        actual = predictions.get(case.id, "missing")
        correct = actual == case.expected_label
        matched[case.expected_label] += int(correct)
        confusion[case.expected_label][actual] += 1
        unsafe_full_support += int(
            actual == "full_support" and case.expected_label != "full_support"
        )
        missing += int(actual == "missing")
        errors += int(actual == "error")
        results.append(
            {
                "case_id": case.id,
                "expected": case.expected_label,
                "actual": actual,
                "correct": correct,
                "rationale": case.rationale,
            }
        )
    correct_count = sum(matched.values())
    return {
        "dataset_id": dataset.id,
        "dataset_sha256": dataset.sha256,
        "annotation_status": dataset.annotation_status,
        "evaluation_only": True,
        "passed": correct_count == len(dataset.cases),
        "total": len(dataset.cases),
        "correct": correct_count,
        "accuracy": correct_count / len(dataset.cases),
        "missing": missing,
        "errors": errors,
        "unsafe_full_support": unsafe_full_support,
        "by_label": {
            label: {
                "total": counts[label],
                "correct": matched[label],
                "recall": matched[label] / counts[label],
            }
            for label in LABELS
        },
        "confusion": {key: dict(value) for key, value in confusion.items()},
        "cases": results,
    }
