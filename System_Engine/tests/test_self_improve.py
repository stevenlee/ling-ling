"""Metacognition M3: proposal generator (diagnosis → revised prompt proposal)."""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.absolute()))
os.environ.setdefault("LLM_PROVIDER", "vllm")

from maintenance.self_assessment import Axis, SelfAssessmentResult, RED, YELLOW
from maintenance.self_diagnosis import Diagnosis, DiagnosisResult
from maintenance.self_improve import run_self_improve
from services.improvement_store import list_proposals


class FakeLLM:
    """Returns canned find/replace edits via _complete_json (structured edit)."""
    def __init__(self, edits):
        self._edits = edits
        self.calls = 0

    def _complete_json(self, *, kind, system_prompt, user_msg, **kw):
        self.calls += 1
        return {"edits": self._edits}


def _vault(tmp_path):
    vault = tmp_path / "vault"
    f = vault / "Templates" / "Prompts" / "agent_counter.md"
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text("You are the lens.\nCheck the claims.\n" * 3, encoding="utf-8")
    return vault, f


def _assessment():
    # report-quality axis red, worst type = lens_report (1/1 bad)
    return SelfAssessmentResult(axes=[
        Axis("報告品質", RED, "bad", detail={"by_type": {"lens_report": {"bad": 2, "total": 2}}}),
        Axis("檢索品質", RED, "low", detail={"pass_rate": 0.5}),
    ])


def _diagnosis(axes=("報告品質", "檢索品質")):
    return DiagnosisResult(diagnoses=[
        Diagnosis(axis=a, lamp=RED, root_cause=f"rc {a}", candidate_fixes=[f"fix {a}"])
        for a in axes
    ])


def test_generates_proposal_from_structured_edits(tmp_path):
    vault, f = _vault(tmp_path)
    pending = tmp_path / "_pending"
    llm = FakeLLM(edits=[{"find": "Check the claims.",
                          "replace": "Check the claims rigorously with explicit pass/fail criteria.",
                          "why": "add criteria"}])
    res = run_self_improve(llm, _assessment(), _diagnosis(), vault_dir=vault, pending_dir=pending)
    assert res.status == "succeeded"
    assert len(res.proposals) == 1
    p = res.proposals[0]
    assert p["target_path"] == "Templates/Prompts/agent_counter.md"
    assert "rigorously" in p["revised_content"]
    # everything else preserved verbatim (deterministic apply)
    assert "You are the lens." in p["revised_content"]
    assert p["edits"][0]["why"] == "add criteria"
    # retrieval axis is recorded as skipped (not a single-prompt lever)
    assert any(ax == "檢索品質" for ax, _ in res.skipped_axes)
    assert list_proposals(pending)[0]["id"] == p["id"]


def test_skips_when_no_edits(tmp_path):
    vault, f = _vault(tmp_path)
    pending = tmp_path / "_pending"
    res = run_self_improve(FakeLLM(edits=[]), _assessment(), _diagnosis(("報告品質",)),
                           vault_dir=vault, pending_dir=pending)
    assert res.proposals == [] and res.status == "skipped"


def test_skips_when_find_not_verbatim(tmp_path):
    # Hallucinated find (not a verbatim substring) → edit dropped → no proposal.
    vault, f = _vault(tmp_path)
    pending = tmp_path / "_pending"
    llm = FakeLLM(edits=[{"find": "THIS TEXT IS NOT IN THE FILE", "replace": "whatever"}])
    res = run_self_improve(llm, _assessment(), _diagnosis(("報告品質",)),
                           vault_dir=vault, pending_dir=pending)
    assert res.proposals == []


def test_backstop_rejects_ballooning_replace(tmp_path):
    # A valid find but a giant replace → reconstructed file balloons >2.5x →
    # the structural backstop rejects it.
    vault, f = _vault(tmp_path)
    pending = tmp_path / "_pending"
    llm = FakeLLM(edits=[{"find": "Check the claims.", "replace": "x" * 5000}])
    res = run_self_improve(llm, _assessment(), _diagnosis(("報告品質",)),
                           vault_dir=vault, pending_dir=pending)
    assert res.proposals == []
    assert any("暴增" in reason for _, reason in res.skipped_axes)


def test_partial_match_applies_only_valid_edits(tmp_path):
    # One good edit + one hallucinated edit → the good one applies, garbage dropped.
    vault, f = _vault(tmp_path)
    pending = tmp_path / "_pending"
    llm = FakeLLM(edits=[
        {"find": "You are the lens.", "replace": "You are the lens (be exhaustive)."},
        {"find": "NONEXISTENT", "replace": "junk"},
    ])
    res = run_self_improve(llm, _assessment(), _diagnosis(("報告品質",)),
                           vault_dir=vault, pending_dir=pending)
    assert len(res.proposals) == 1
    assert len(res.proposals[0]["edits"]) == 1            # only the valid edit kept
    assert "be exhaustive" in res.proposals[0]["revised_content"]


def test_non_report_axis_skipped_with_reason(tmp_path):
    vault, _ = _vault(tmp_path)
    pending = tmp_path / "_pending"
    dx = DiagnosisResult(diagnoses=[
        Diagnosis(axis="Cortex 信念", lamp=YELLOW, root_cause="rc", candidate_fixes=["fix"]),
    ])
    res = run_self_improve(FakeLLM(edits=[{"find": "x", "replace": "y"}]), _assessment(), dx,
                           vault_dir=vault, pending_dir=pending)
    assert res.proposals == []
    assert res.skipped_axes and res.skipped_axes[0][0] == "Cortex 信念"
