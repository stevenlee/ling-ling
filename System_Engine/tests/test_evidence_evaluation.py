"""A01 offline fixture/scorer contract, not model-accuracy measurements."""

import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from services.evidence_evaluation import evaluate_predictions, load_dataset, read_json

_CASES = Path(__file__).parent / "fixtures/evidence_evaluation/cases.json"


def _predictions(dataset, label=None):
    return {
        "dataset_id": dataset.id,
        "dataset_sha256": dataset.sha256,
        "predictions": [
            {"case_id": case.id, "label": label or case.expected_label} for case in dataset.cases
        ],
    }


def test_reference_cases_cover_audit_and_positive_controls():
    dataset = load_dataset(_CASES)
    assert len(dataset.cases) == 24
    assert dataset.annotation_status == "assistant_authored_pending_review"
    assert sum("audit_regression" in case.tags for case in dataset.cases) == 8
    assert sum(case.expected_label == "full_support" for case in dataset.cases) == 6
    assert sum(case.expected_label == "contradiction" for case in dataset.cases) == 4
    assert {"compound_claim", "normative_claim", "prompt_injection", "quoted_claim"} <= {
        tag for case in dataset.cases for tag in case.tags
    }


def test_export_does_not_leak_reference_labels_or_rationales():
    exported = load_dataset(_CASES).inputs()
    assert set(exported) == {"dataset_id", "dataset_sha256", "cases"}
    for case in exported["cases"]:
        assert set(case) == {"id", "claim", "falsifier", "source_title", "passage"}


def test_scorer_reference_roundtrip_is_not_a_model_evaluation():
    dataset = load_dataset(_CASES)
    result = evaluate_predictions(dataset, _predictions(dataset))
    assert result["passed"] is True
    assert result["evaluation_only"] is True
    assert result["annotation_status"] == "assistant_authored_pending_review"
    assert result["correct"] == 24 and result["unsafe_full_support"] == 0


def test_all_neutral_cannot_pass_by_rejecting_everything():
    dataset = load_dataset(_CASES)
    result = evaluate_predictions(dataset, _predictions(dataset, "neutral"))
    assert result["passed"] is False
    assert result["unsafe_full_support"] == 0
    assert result["by_label"]["full_support"]["recall"] == 0
    assert result["by_label"]["contradiction"]["recall"] == 0
    assert result["accuracy"] < 0.5


def test_all_support_detects_partial_and_analogy_overreach():
    dataset = load_dataset(_CASES)
    result = evaluate_predictions(dataset, _predictions(dataset, "full_support"))
    assert result["passed"] is False
    assert result["unsafe_full_support"] == 18
    assert result["confusion"]["analogy"]["full_support"] == 2
    assert result["confusion"]["partial_support"]["full_support"] == 6


def test_missing_and_provider_errors_are_not_neutral_or_removed_from_denominator():
    dataset = load_dataset(_CASES)
    payload = _predictions(dataset)
    omitted = payload["predictions"].pop()
    payload["predictions"][0]["label"] = "error"
    result = evaluate_predictions(dataset, payload)
    assert result["total"] == 24 and result["correct"] == 22
    assert result["missing"] == 1 and result["errors"] == 1
    assert result["confusion"]["partial_support"]["error"] == 1
    assert result["cases"][-1]["case_id"] == omitted["case_id"]
    assert result["cases"][-1]["actual"] == "missing"
    assert result["passed"] is False


@pytest.mark.parametrize(
    "mutation",
    [
        lambda p: p.update(dataset_id="other-version"),
        lambda p: p.pop("dataset_sha256"),
        lambda p: p.update(predictions={}),
        lambda p: p["predictions"].append(dict(p["predictions"][0])),
        lambda p: p["predictions"][0].update(case_id="unknown"),
        lambda p: p["predictions"][0].update(label="supports"),
        lambda p: p["predictions"][0].update(label=None),
        lambda p: p["predictions"].append("not-an-object"),
    ],
)
def test_invalid_predictions_are_rejected_instead_of_silently_skipped(mutation):
    dataset = load_dataset(_CASES)
    payload = _predictions(dataset)
    mutation(payload)
    with pytest.raises(ValueError):
        evaluate_predictions(dataset, payload)


@pytest.mark.parametrize(
    "mutation",
    [
        lambda p: p.update(schema_version=True),
        lambda p: p.update(schema_version=1.0),
        lambda p: p.update(annotation_status="claimed_verified"),
        lambda p: p.update(cases=[]),
        lambda p: p["cases"].append(p["cases"][0]),
        lambda p: p["cases"][0].update(passage=""),
        lambda p: p["cases"][0].update(passage="x" * 2001),
        lambda p: p["cases"][0].update(expected_label="supports"),
        lambda p: p["cases"][0].update(tags=[42]),
        lambda p: p["cases"][0].update(provenance={"kind": "private_original"}),
        lambda p: p.update(cases=[c for c in p["cases"] if c["expected_label"] != "full_support"]),
    ],
)
def test_malformed_or_unbalanced_dataset_is_not_accepted(tmp_path, mutation):
    payload = json.loads(_CASES.read_text())
    mutation(payload)
    path = tmp_path / "cases.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError):
        load_dataset(path)


@pytest.mark.parametrize("text", ['{"key": 1, "key": 2}', '{"value": NaN}', '{"value": Infinity}'])
def test_nonstandard_or_duplicate_json_fields_fail_validation(tmp_path, text):
    path = tmp_path / "bad.json"
    path.write_text(text, encoding="utf-8")
    with pytest.raises(ValueError):
        read_json(path)


def _cli(tmp_path, *args):
    # -S proves this CLI needs no third-party provider/DB stack or live config.
    engine = Path(__file__).resolve().parents[1]
    return subprocess.run(
        [sys.executable, "-S", "-m", "scripts.evaluate_evidence_cases", *args],
        cwd=tmp_path,
        env={**os.environ, "PYTHONPATH": str(engine), "PYTHONDONTWRITEBYTECODE": "1"},
        text=True,
        capture_output=True,
        check=False,
    )


def test_cli_export_runs_without_provider_dependencies_or_answers(tmp_path):
    result = _cli(tmp_path, "--inputs")
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert len(payload["cases"]) == 24
    assert "expected_label" not in result.stdout and "rationale" not in result.stdout


def test_cli_exit_codes_distinguish_scoring_failure_and_bad_input(tmp_path):
    dataset = load_dataset(_CASES)
    path = tmp_path / "predictions.json"
    path.write_text(json.dumps(_predictions(dataset, "neutral")), encoding="utf-8")
    result = _cli(tmp_path, "--predictions", str(path))
    assert result.returncode == 1, result.stderr
    assert json.loads(result.stdout)["passed"] is False
    invalid = _cli(tmp_path, "--predictions", str(tmp_path / "absent.json"))
    assert invalid.returncode == 2
    assert "評估輸入錯誤" in invalid.stderr and invalid.stdout == ""


def test_modified_reference_content_invalidates_saved_predictions(tmp_path):
    old = load_dataset(_CASES)
    payload = _predictions(old)
    raw = json.loads(_CASES.read_text())
    raw["cases"][0]["passage"] += " 新增測試觀察。"
    path = tmp_path / "changed.json"
    path.write_text(json.dumps(raw), encoding="utf-8")
    changed = load_dataset(path)
    assert changed.id == old.id and changed.sha256 != old.sha256
    with pytest.raises(ValueError, match="dataset_sha256"):
        evaluate_predictions(changed, payload)
