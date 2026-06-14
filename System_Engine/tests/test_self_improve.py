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
    def __init__(self, revised):
        self._revised = revised
        self.calls = 0

    def complete(self, system, user, **kw):
        self.calls += 1
        return self._revised


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


def test_generates_proposal_for_report_quality(tmp_path):
    vault, f = _vault(tmp_path)
    pending = tmp_path / "_pending"
    llm = FakeLLM(revised="You are the lens.\nCheck the claims rigorously with explicit criteria.\n" * 3)
    res = run_self_improve(llm, _assessment(), _diagnosis(), vault_dir=vault, pending_dir=pending)
    assert res.status == "succeeded"
    assert len(res.proposals) == 1
    p = res.proposals[0]
    assert p["target_path"] == "Templates/Prompts/agent_counter.md"
    assert "rigorously" in p["revised_content"]
    # retrieval axis is recorded as skipped (not a single-prompt lever)
    assert any(ax == "檢索品質" for ax, _ in res.skipped_axes)
    assert list_proposals(pending)[0]["id"] == p["id"]


def test_skips_when_revision_identical(tmp_path):
    vault, f = _vault(tmp_path)
    pending = tmp_path / "_pending"
    original = f.read_text(encoding="utf-8")
    res = run_self_improve(FakeLLM(revised=original), _assessment(), _diagnosis(("報告品質",)),
                           vault_dir=vault, pending_dir=pending)
    assert res.proposals == []
    assert res.status == "skipped"


def test_skips_truncated_revision(tmp_path):
    vault, f = _vault(tmp_path)
    pending = tmp_path / "_pending"
    res = run_self_improve(FakeLLM(revised="too short"), _assessment(), _diagnosis(("報告品質",)),
                           vault_dir=vault, pending_dir=pending)
    assert res.proposals == []   # <50% of original → rejected as truncation


def test_skips_derail_that_balloons_and_loses_structure(tmp_path):
    # The observed live failure: model echoes the meta-instruction → different
    # content, much longer, ~0% of original lines retained. Must be rejected.
    vault, f = _vault(tmp_path)
    pending = tmp_path / "_pending"
    derail = "Role: Prompt/Template reviser.\n" + ("totally different content line\n" * 200)
    res = run_self_improve(FakeLLM(revised=derail), _assessment(), _diagnosis(("報告品質",)),
                           vault_dir=vault, pending_dir=pending)
    assert res.proposals == []
    assert any("離題" in reason or "重寫" in reason for _, reason in res.skipped_axes)


def test_targeted_edit_passes_structure_check(tmp_path):
    # A real targeted edit: keep all original lines, append a couple → accepted.
    vault, f = _vault(tmp_path)
    pending = tmp_path / "_pending"
    original = f.read_text(encoding="utf-8")
    revised = original + "\n6. **Be explicit**: state the pass/fail criteria you applied.\n"
    res = run_self_improve(FakeLLM(revised=revised), _assessment(), _diagnosis(("報告品質",)),
                           vault_dir=vault, pending_dir=pending)
    assert len(res.proposals) == 1


def test_non_report_axis_skipped_with_reason(tmp_path):
    vault, _ = _vault(tmp_path)
    pending = tmp_path / "_pending"
    dx = DiagnosisResult(diagnoses=[
        Diagnosis(axis="Cortex 信念", lamp=YELLOW, root_cause="rc", candidate_fixes=["fix"]),
    ])
    res = run_self_improve(FakeLLM(revised="x" * 999), _assessment(), dx,
                           vault_dir=vault, pending_dir=pending)
    assert res.proposals == []
    assert res.skipped_axes and res.skipped_axes[0][0] == "Cortex 信念"
