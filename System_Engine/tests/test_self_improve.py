"""Metacognition M3: proposal generator (diagnosis → revised prompt proposal)."""

import os

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
    prompts = vault / "Templates" / "Prompts"
    prompts.mkdir(parents=True, exist_ok=True)
    f = prompts / "agent_counter.md"
    f.write_text("You are the lens.\nCheck the claims.\n" * 3, encoding="utf-8")
    # the insight-generation prompt — 洞察品質 axis's lever
    (prompts / "agent_insight.md").write_text(
        "You generate insights.\nSpark novel connections.\n" * 3, encoding="utf-8"
    )
    # the falsifiability prompt — Cortex 信念 axis's lever (A1 externalized)
    (prompts / "cortex_falsifiability.md").write_text(
        "You assess falsifiability.\nScore the claim.\n" * 3, encoding="utf-8"
    )
    return vault, f


def _assessment():
    # report-quality axis red, worst type = lens_report (1/1 bad)
    return SelfAssessmentResult(
        axes=[
            Axis(
                "報告品質", RED, "bad", detail={"by_type": {"lens_report": {"bad": 2, "total": 2}}}
            ),
            Axis("檢索品質", RED, "low", detail={"pass_rate": 0.5}),
        ]
    )


def _diagnosis(axes=("報告品質", "檢索品質")):
    return DiagnosisResult(
        diagnoses=[
            Diagnosis(axis=a, lamp=RED, root_cause=f"rc {a}", candidate_fixes=[f"fix {a}"])
            for a in axes
        ]
    )


def test_generates_proposal_from_structured_edits(tmp_path):
    vault, f = _vault(tmp_path)
    pending = tmp_path / "_pending"
    llm = FakeLLM(
        edits=[
            {
                "find": "Check the claims.",
                "replace": "Check the claims rigorously with explicit pass/fail criteria.",
                "why": "add criteria",
            }
        ]
    )
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
    res = run_self_improve(
        FakeLLM(edits=[]),
        _assessment(),
        _diagnosis(("報告品質",)),
        vault_dir=vault,
        pending_dir=pending,
    )
    assert res.proposals == [] and res.status == "skipped"


def test_skips_when_find_not_verbatim(tmp_path):
    # Hallucinated find (not a verbatim substring) → edit dropped → no proposal.
    vault, f = _vault(tmp_path)
    pending = tmp_path / "_pending"
    llm = FakeLLM(edits=[{"find": "THIS TEXT IS NOT IN THE FILE", "replace": "whatever"}])
    res = run_self_improve(
        llm, _assessment(), _diagnosis(("報告品質",)), vault_dir=vault, pending_dir=pending
    )
    assert res.proposals == []


def test_backstop_rejects_ballooning_replace(tmp_path):
    # A valid find but a giant replace → reconstructed file balloons >2.5x →
    # the structural backstop rejects it.
    vault, f = _vault(tmp_path)
    pending = tmp_path / "_pending"
    llm = FakeLLM(edits=[{"find": "Check the claims.", "replace": "x" * 5000}])
    res = run_self_improve(
        llm, _assessment(), _diagnosis(("報告品質",)), vault_dir=vault, pending_dir=pending
    )
    assert res.proposals == []
    assert any("暴增" in reason for _, reason in res.skipped_axes)


def test_partial_match_applies_only_valid_edits(tmp_path):
    # One good edit + one hallucinated edit → the good one applies, garbage dropped.
    vault, f = _vault(tmp_path)
    pending = tmp_path / "_pending"
    llm = FakeLLM(
        edits=[
            {"find": "You are the lens.", "replace": "You are the lens (be exhaustive)."},
            {"find": "NONEXISTENT", "replace": "junk"},
        ]
    )
    res = run_self_improve(
        llm, _assessment(), _diagnosis(("報告品質",)), vault_dir=vault, pending_dir=pending
    )
    assert len(res.proposals) == 1
    assert len(res.proposals[0]["edits"]) == 1  # only the valid edit kept
    assert "be exhaustive" in res.proposals[0]["revised_content"]


def test_cortex_axis_generates_proposal(tmp_path):
    # A1 (2026-07-13): the Cortex prompts are externalized to vault, so M3 now
    # reaches the Cortex axis via cortex_falsifiability.md (was a code-lever skip).
    vault, _ = _vault(tmp_path)
    pending = tmp_path / "_pending"
    dx = DiagnosisResult(
        diagnoses=[
            Diagnosis(
                axis="Cortex 信念",
                lamp=YELLOW,
                root_cause="教條化",
                candidate_fixes=["加入反向壓力測試步驟以降低教條化"],
            ),
        ]
    )
    llm = FakeLLM(
        edits=[
            {
                "find": "Score the claim.",
                "replace": "Score the claim; require a concrete falsifier before high confidence.",
                "why": "reverse pressure test",
            }
        ]
    )
    res = run_self_improve(llm, _assessment(), dx, vault_dir=vault, pending_dir=pending)
    assert res.status == "succeeded"
    assert len(res.proposals) == 1
    assert res.proposals[0]["target_path"] == "Templates/Prompts/cortex_falsifiability.md"


def test_code_lever_axis_skipped_with_specific_reason(tmp_path):
    # 檢索品質's lever is index/reranker config — M3 must skip it with a SPECIFIC
    # reason, not a generic "needs a human" (the audit's break: recurred invisibly).
    vault, _ = _vault(tmp_path)
    pending = tmp_path / "_pending"
    dx = DiagnosisResult(
        diagnoses=[
            Diagnosis(axis="檢索品質", lamp=YELLOW, root_cause="rc", candidate_fixes=["fix"]),
        ]
    )
    res = run_self_improve(
        FakeLLM(edits=[{"find": "x", "replace": "y"}]),
        _assessment(),
        dx,
        vault_dir=vault,
        pending_dir=pending,
    )
    assert res.proposals == []
    assert res.skipped_axes and res.skipped_axes[0][0] == "檢索品質"
    assert "reranker" in res.skipped_axes[0][1] or "config" in res.skipped_axes[0][1]


def test_insight_axis_generates_proposal(tmp_path):
    # 洞察品質 → agent_insight.md: the audit's clean win. Before the M-arc fix
    # this axis was blanket-skipped; now its prompt-editable fixes become proposals.
    vault, _ = _vault(tmp_path)
    pending = tmp_path / "_pending"
    dx = DiagnosisResult(
        diagnoses=[
            Diagnosis(
                axis="洞察品質",
                lamp=YELLOW,
                root_cause="novelty 低",
                candidate_fixes=["加入與既有知識的差異化描述欄位"],
            ),
        ]
    )
    llm = FakeLLM(
        edits=[
            {
                "find": "Spark novel connections.",
                "replace": "Spark novel connections that differ from prior insights.",
                "why": "novelty diff",
            }
        ]
    )
    res = run_self_improve(llm, _assessment(), dx, vault_dir=vault, pending_dir=pending)
    assert res.status == "succeeded"
    assert len(res.proposals) == 1
    assert res.proposals[0]["target_path"] == "Templates/Prompts/agent_insight.md"
    assert "differ from prior insights" in res.proposals[0]["revised_content"]


def test_stale_pending_surfaced(tmp_path, monkeypatch):
    import maintenance.self_improve as si
    from services.improvement_store import make_proposal, save_proposal

    monkeypatch.setattr(si, "SELF_IMPROVE_STALE_DAYS", 14)
    vault, _ = _vault(tmp_path)
    pending = tmp_path / "_pending"
    # a proposal created 30 days ago (stamp the created field in the past)
    old = make_proposal(
        axis="報告品質",
        target_path="Templates/Prompts/agent_counter.md",
        rationale="r",
        addressed_fixes=["f"],
        original_content="a",
        revised_content="b",
        edits=[],
    )
    old["created"] = "2026-06-12T00:00:00"  # >14d before the 2026-07-12 run
    save_proposal(old, pending)
    # run with no new diagnoses → still surfaces the stale one
    res = run_self_improve(
        FakeLLM(edits=[]),
        _assessment(),
        DiagnosisResult(diagnoses=[]),
        vault_dir=vault,
        pending_dir=pending,
    )
    assert res.stale_pending and res.stale_pending[0][0] == old["id"]
    assert res.stale_pending[0][1] >= 14
    assert "待審逾" in res.message
