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


def _prior_proposal(
    vault, *, target="agent_counter", rationale="old problem", fixes=None, edits=None
):
    from services.improvement_store import make_proposal

    rel = f"Templates/Prompts/{target}.md"
    original = (vault / rel).read_text(encoding="utf-8")
    edits = edits or [{"find": "Check the claims.", "replace": "Check the claims carefully."}]
    revised = original
    for edit in edits:
        revised = revised.replace(edit["find"], edit["replace"], 1)
    return make_proposal(
        axis="報告品質",
        target_path=rel,
        rationale=rationale,
        addressed_fixes=fixes or ["old fix"],
        original_content=original,
        revised_content=revised,
        edits=edits,
        stamp="20260101000000",
    )


def test_existing_pending_basis_skips_before_llm_without_writing(tmp_path):
    from services.improvement_store import save_proposal

    vault, target = _vault(tmp_path)
    pending = tmp_path / "_pending"
    proposal = _prior_proposal(vault)
    proposal["target_path"] = "Templates/Prompts/../Prompts/agent_counter.md"
    path = save_proposal(proposal, pending)
    before = path.read_bytes(), target.read_bytes()
    llm = FakeLLM([])
    res = run_self_improve(llm, _assessment(), _diagnosis(), vault_dir=vault, pending_dir=pending)
    assert llm.calls == 0 and res.proposals == []
    assert proposal["id"] in str(res.skipped_axes)
    assert (path.read_bytes(), target.read_bytes()) == before


def test_pending_old_basis_does_not_block_new_version(tmp_path):
    from services.improvement_store import save_proposal

    vault, target = _vault(tmp_path)
    pending = tmp_path / "_pending"
    save_proposal(_prior_proposal(vault), pending)
    target.write_text(target.read_text(encoding="utf-8") + "New boundary.\n", encoding="utf-8")
    llm = FakeLLM([{"find": "Check the claims.", "replace": "Check the claims carefully."}])
    res = run_self_improve(llm, _assessment(), _diagnosis(), vault_dir=vault, pending_dir=pending)
    assert llm.calls == 1 and len(res.proposals) == 1
    assert len(list_proposals(pending)) == 2


def test_same_run_axes_share_one_pending_slot(tmp_path):
    vault, _ = _vault(tmp_path)
    assessment = SelfAssessmentResult(
        axes=[
            Axis(
                "報告品質",
                RED,
                "bad",
                detail={"by_type": {"report_insight": {"bad": 1, "total": 1}}},
            ),
        ]
    )
    llm = FakeLLM(
        [{"find": "Spark novel connections.", "replace": "Spark novel and grounded connections."}]
    )
    res = run_self_improve(
        llm,
        assessment,
        _diagnosis(("報告品質", "洞察品質")),
        vault_dir=vault,
        pending_dir=tmp_path / "_pending",
    )
    assert llm.calls == 1 and len(res.proposals) == 1
    assert "已有待審" in str(res.skipped_axes)


def test_exact_rejected_diagnosis_skips_before_llm(tmp_path):
    from services.improvement_store import save_proposal

    vault, _ = _vault(tmp_path)
    save_proposal(
        _prior_proposal(vault, rationale="rc 報告品質", fixes=["fix 報告品質"]),
        tmp_path / "_rejected",
    )
    llm = FakeLLM([])
    res = run_self_improve(
        llm, _assessment(), _diagnosis(), vault_dir=vault, pending_dir=tmp_path / "_pending"
    )
    assert llm.calls == 0 and res.proposals == []
    assert "相同版本與診斷已退回" in str(res.skipped_axes)


def test_rejected_edit_is_blocked_even_with_different_why(tmp_path):
    from services.improvement_store import save_proposal

    vault, _ = _vault(tmp_path)
    proposal = _prior_proposal(vault)
    proposal["rejected_reason"] = "不能靠額外欄位修正程式。"
    save_proposal(proposal, tmp_path / "_rejected")

    class CapturingLLM(FakeLLM):
        def _complete_json(self, **kwargs):
            self.message = kwargs["user_msg"]
            return super()._complete_json(**kwargs)

    llm = CapturingLLM([{**proposal["edits"][0], "why": "new wording"}])
    res = run_self_improve(
        llm, _assessment(), _diagnosis(), vault_dir=vault, pending_dir=tmp_path / "_pending"
    )
    assert llm.calls == 1 and res.proposals == []
    assert "不能靠額外欄位" in llm.message
    assert "相同編輯已退回" in str(res.skipped_axes)


def test_rejected_old_basis_allows_same_edit_on_new_version(tmp_path):
    from services.improvement_store import save_proposal

    vault, target = _vault(tmp_path)
    proposal = _prior_proposal(vault)
    save_proposal(proposal, tmp_path / "_rejected")
    target.write_text(target.read_text(encoding="utf-8") + "New version.\n", encoding="utf-8")
    res = run_self_improve(
        FakeLLM(proposal["edits"]),
        _assessment(),
        _diagnosis(),
        vault_dir=vault,
        pending_dir=tmp_path / "_pending",
    )
    assert len(res.proposals) == 1


def test_new_edit_after_rejection_is_allowed(tmp_path):
    from services.improvement_store import save_proposal

    vault, _ = _vault(tmp_path)
    save_proposal(_prior_proposal(vault), tmp_path / "_rejected")
    llm = FakeLLM([{"find": "You are the lens.", "replace": "You are an evidence auditor."}])
    res = run_self_improve(
        llm, _assessment(), _diagnosis(), vault_dir=vault, pending_dir=tmp_path / "_pending"
    )
    assert len(res.proposals) == 1


def test_corrupt_history_cannot_permanently_block_target(tmp_path, caplog):
    import json

    vault, _ = _vault(tmp_path)
    pending = tmp_path / "_pending"
    pending.mkdir()
    (pending / "broken.json").write_text("{", encoding="utf-8")
    (pending / "array.json").write_text("[]", encoding="utf-8")
    record = _prior_proposal(vault)
    record["original_sha"] = "bad hash"
    (pending / "invalid.json").write_text(json.dumps(record), encoding="utf-8")
    llm = FakeLLM([{"find": "Check the claims.", "replace": "Check the claims carefully."}])
    res = run_self_improve(llm, _assessment(), _diagnosis(), vault_dir=vault, pending_dir=pending)
    assert len(res.proposals) == 1 and llm.calls == 1
    assert "broken.json" in caplog.text and "invalid.json" in caplog.text
    assert (pending / "broken.json").read_text(encoding="utf-8") == "{"


def test_proposal_arriving_during_generation_is_not_duplicated(tmp_path):
    from services.improvement_store import save_proposal

    vault, _ = _vault(tmp_path)
    pending = tmp_path / "_pending"

    class RacingLLM(FakeLLM):
        def _complete_json(self, **kwargs):
            save_proposal(_prior_proposal(vault), pending)
            return super()._complete_json(**kwargs)

    llm = RacingLLM([{"find": "Check the claims.", "replace": "Check the claims carefully."}])
    res = run_self_improve(llm, _assessment(), _diagnosis(), vault_dir=vault, pending_dir=pending)
    assert llm.calls == 1 and res.proposals == []
    assert len(list_proposals(pending)) == 1
    assert "生成期間已有" in str(res.skipped_axes)


def test_target_change_during_generation_discards_stale_revision(tmp_path):
    vault, target = _vault(tmp_path)

    class EditingLLM(FakeLLM):
        def _complete_json(self, **kwargs):
            target.write_text("Human edited this prompt.", encoding="utf-8")
            return super()._complete_json(**kwargs)

    res = run_self_improve(
        EditingLLM([{"find": "Check the claims.", "replace": "Check the claims carefully."}]),
        _assessment(),
        _diagnosis(),
        vault_dir=vault,
        pending_dir=tmp_path / "_pending",
    )
    assert res.proposals == []
    assert "生成期間已變更" in str(res.skipped_axes)
    assert target.read_text(encoding="utf-8") == "Human edited this prompt."


def test_atomic_pending_save_failure_is_visible_without_partial_file(tmp_path, monkeypatch):
    from pathlib import Path

    vault, target = _vault(tmp_path)
    original = target.read_bytes()
    pending = tmp_path / "_pending"
    replace = Path.replace

    def fail_queue_replace(self, destination):
        if Path(destination).parent == pending:
            raise OSError("disk full")
        return replace(self, destination)

    monkeypatch.setattr(Path, "replace", fail_queue_replace)
    res = run_self_improve(
        FakeLLM([{"find": "Check the claims.", "replace": "Check the claims carefully."}]),
        _assessment(),
        _diagnosis(),
        vault_dir=vault,
        pending_dir=pending,
    )
    assert res.proposals == [] and "儲存失敗" in str(res.skipped_axes)
    assert not list(pending.iterdir())
    assert target.read_bytes() == original


def test_cortex_new_json_field_is_not_queued(tmp_path):
    vault, _ = _vault(tmp_path)
    target = vault / "Templates/Prompts/cortex_falsifiability.md"
    original = target.read_bytes()
    llm = FakeLLM(
        [{"find": "Score the claim.", "replace": 'Score the claim.\n{"needs_enrichment": true}'}]
    )
    res = run_self_improve(
        llm,
        _assessment(),
        _diagnosis(("Cortex 信念",)),
        vault_dir=vault,
        pending_dir=tmp_path / "_pending",
    )
    assert llm.calls == 1 and res.proposals == []
    assert "JSON 契約" in str(res.skipped_axes)
    assert target.read_bytes() == original


def test_target_read_failure_is_reported_without_llm(tmp_path, monkeypatch):
    from pathlib import Path

    vault, target = _vault(tmp_path)
    read_text = Path.read_text

    def fail_target_read(self, *args, **kwargs):
        if self == target:
            raise OSError("permission denied")
        return read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", fail_target_read)
    llm = FakeLLM([])
    res = run_self_improve(
        llm, _assessment(), _diagnosis(), vault_dir=vault, pending_dir=tmp_path / "_pending"
    )
    assert llm.calls == 0 and res.proposals == []
    assert "無法讀取" in str(res.skipped_axes)


def test_rejection_record_arriving_during_generation_blocks_duplicate(tmp_path):
    from services.improvement_store import save_proposal

    vault, _ = _vault(tmp_path)
    proposal = _prior_proposal(vault)

    class RacingLLM(FakeLLM):
        def _complete_json(self, **kwargs):
            save_proposal(proposal, tmp_path / "_rejected")
            return super()._complete_json(**kwargs)

    res = run_self_improve(
        RacingLLM(proposal["edits"]),
        _assessment(),
        _diagnosis(),
        vault_dir=vault,
        pending_dir=tmp_path / "_pending",
    )
    assert res.proposals == [] and "相同編輯已退回" in str(res.skipped_axes)
