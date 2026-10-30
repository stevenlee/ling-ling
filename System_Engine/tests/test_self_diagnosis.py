"""Metacognition M2: self-diagnosis over the M1 scorecard."""

import os

os.environ.setdefault("LLM_PROVIDER", "vllm")

from maintenance.self_assessment import Axis, SelfAssessmentResult, GREEN, RED, YELLOW
from maintenance.self_diagnosis import run_self_diagnosis


class FakeLLM:
    """Returns a canned diagnosis; records how many axes it was asked about."""

    def __init__(self, ret=None, boom=False):
        self._ret = ret or {
            "root_cause": "rc",
            "candidate_fixes": ["fix A", "fix B"],
            "confidence": 0.7,
            "needs": "",
        }
        self._boom = boom
        self.calls = []

    def _complete_json(self, *, kind, system_prompt, user_msg, **kw):
        self.calls.append(kw.get("trace_context", {}).get("metadata", {}).get("axis"))
        if self._boom:
            raise RuntimeError("llm down")
        return self._ret


def _assessment(axes, trend=None):
    return SelfAssessmentResult(axes=axes, trend=trend or {})


def _paths(tmp_path):
    return dict(
        cortex_dir=tmp_path / "cortex",
        report_dir=tmp_path / "out",
        log_path=tmp_path / "maint.log.md",
    )


def test_skips_when_all_green(tmp_path):
    a = _assessment([Axis("檢索品質", GREEN, "fine"), Axis("LLM 健康", GREEN, "fine")])
    r = run_self_diagnosis(FakeLLM(), a, **_paths(tmp_path))
    assert r.status == "skipped"
    assert r.report_path is None


def test_diagnoses_only_flagged_axes(tmp_path):
    axes = [
        Axis("檢索品質", RED, "pass 50%", detail={"pass_rate": 0.5, "prev_pass_rate": 0.9}),
        Axis("LLM 健康", GREEN, "ok", detail={}),
        Axis("Cortex 信念", YELLOW, "thin", detail={"thin_evidence": 5, "total_pages": 10}),
    ]
    llm = FakeLLM()
    r = run_self_diagnosis(llm, _assessment(axes), **_paths(tmp_path))
    # only the 2 non-green axes get an LLM call
    assert set(filter(None, llm.calls)) == {"檢索品質", "Cortex 信念"}
    assert len(r.diagnoses) == 2
    assert r.diagnoses[0].candidate_fixes
    assert not r.diagnoses[1].proposal_eligible
    assert r.diagnoses[1].hypothesis == "rc"
    assert r.diagnoses[1].candidate_fixes == []
    assert r.report_path is not None


def test_report_includes_root_cause_and_fixes(tmp_path):
    axes = [Axis("檢索品質", RED, "pass 50%", detail={"pass_rate": 0.5})]
    r = run_self_diagnosis(FakeLLM(), _assessment(axes), **_paths(tmp_path))
    body = r.report_path.read_text(encoding="utf-8")
    assert "根因" in body and "rc" in body
    assert "fix A" in body and "fix B" in body
    # M2 must frame fixes as not-yet-applied
    assert "尚未套用" in body or "候選" in body


def test_chronic_streak_surfaced(tmp_path):
    axes = [Axis("檢索品質", RED, "pass 50%", detail={"pass_rate": 0.5})]
    trend = {"檢索品質": {"arrow": "→", "prev": RED, "streak": 4}}
    r = run_self_diagnosis(FakeLLM(), _assessment(axes, trend), **_paths(tmp_path))
    assert r.diagnoses[0].streak == 4
    assert "連續 4 次" in r.report_path.read_text(encoding="utf-8")


def test_per_axis_failopen(tmp_path):
    axes = [Axis("檢索品質", RED, "pass 50%", detail={"pass_rate": 0.5})]
    r = run_self_diagnosis(FakeLLM(boom=True), _assessment(axes), **_paths(tmp_path))
    # LLM raised → diagnosis lands empty, no crash, no report (nothing landed)
    assert r.status == "succeeded"
    assert r.diagnoses[0].root_cause == ""
    assert r.report_path is None


def test_cortex_context_uses_assessment_snapshot_without_rescan(tmp_path, monkeypatch):
    from maintenance.self_diagnosis import _ctx_cortex
    import services.cortex_tensions as ct

    def forbidden(*args, **kwargs):
        raise AssertionError("M2 must not rescan another snapshot")

    monkeypatch.setattr(ct, "scan_tensions", forbidden)
    ctx = _ctx_cortex(
        {
            "total_pages": 12,
            "thin_evidence": 10,
            "by_status": {
                "active": {"total": 3, "thin": 1, "a2_pages": 2, "a2_evidence": 4},
                "dormant": {"total": 9, "thin": 9, "a2_pages": 1, "a2_evidence": 1},
            },
            "a2_enabled": True,
            "a2_apply": False,
            "active_thin_examples": ["snapshot claim"],
            "dogmatic_examples": [{"claim": "dogma", "status": "dormant"}],
        },
        tmp_path,
    )
    assert "active 補證需求為 1/3" in ctx
    assert "2 條主張／4 筆證據" in ctx
    assert "A2 排程啟用：是" in ctx and "A2 寫回設定：否" in ctx
    assert "非近期增厚次數或因果成效" in ctx
    assert "snapshot claim" in ctx and "[dormant]：dogma" in ctx


def test_legacy_cortex_context_does_not_invent_missing_a2_metrics(tmp_path):
    from maintenance.self_diagnosis import _ctx_cortex

    ctx = _ctx_cortex({"total_pages": 10, "thin_evidence": 8}, tmp_path)
    assert "視為未知" in ctx
    assert "A2 排程啟用：未知" in ctx and "A2 寫回設定：未知" in ctx


def test_report_context_distinguishes_execution_failure_from_content():
    from maintenance.self_diagnosis import _ctx_report_quality

    ctx = _ctx_report_quality(
        {
            "total": 4,
            "content_bad": 1,
            "gate_error": 2,
            "unparseable": 1,
            "rate": 1,
            "by_type": {"Synthesis": {"total": 4, "bad": 1, "gate_error": 2, "unparseable": 1}},
        }
    )
    assert "1 份 revise/reject" in ctx
    assert "2 份審查未完成" in ctx
    assert "不能當成內容差" in ctx and "非內容 verdict" in ctx


def test_cortex_overconfident_causal_claim_is_held_as_unverified(tmp_path):
    # Regression for the real 2026-10-30 M2 output: global thin counts were
    # used to assert A2 failure despite the explicit active/inactive split.
    llm = FakeLLM(
        ret={
            "root_cause": "A2 驗證機制陷入低質量循環，97/122 薄證據證明無法增厚。",
            "candidate_fixes": ["修改 A2 Prompt 強制搜尋至少兩個獨立來源"],
            "confidence": 0.99,
            "needs": "",
            "proposal_eligible": True,
        }
    )
    assessment = _assessment(
        [
            Axis(
                "Cortex 信念",
                RED,
                "thin",
                detail={
                    "total_pages": 122,
                    "thin_evidence": 97,
                    "by_status": {
                        "active": {"total": 19, "thin": 12, "a2_pages": 7, "a2_evidence": 8}
                    },
                    "a2_enabled": True,
                    "a2_apply": True,
                },
            )
        ]
    )
    result = run_self_diagnosis(llm, assessment, **_paths(tmp_path))
    dx = result.diagnoses[0]
    assert dx.proposal_eligible is False
    assert dx.confidence == 0 and dx.candidate_fixes == []
    assert "低質量循環" in dx.hypothesis
    assert "低質量循環" not in dx.root_cause
    assert "尚未確認根因" in dx.root_cause
    assert "固定 cohort" in dx.needs
    assert len(dx.investigation_steps) == 3
    body = result.report_path.read_text(encoding="utf-8")
    assert "模型未驗證假設" in body and "未送入 M3" in body
    assert "信心 99%" not in body and "強制搜尋" not in body
    assert "查證項目（尚未執行）" in body
    assert "能力邊界" in dx.context and "沒有搜尋工具" in dx.context
    assert "A2 已只選 active" in dx.context


def test_legacy_cortex_metrics_also_require_causal_evidence(tmp_path):
    result = run_self_diagnosis(
        FakeLLM(),
        _assessment(
            [
                Axis("Cortex 信念", RED, "old snapshot", detail={"thin_evidence": 10}),
                Axis("檢索品質", RED, "low", detail={"pass_rate": 0.5}),
            ]
        ),
        **_paths(tmp_path),
    )
    cortex, retrieval = result.diagnoses
    assert not cortex.proposal_eligible and cortex.candidate_fixes == []
    assert retrieval.proposal_eligible and retrieval.candidate_fixes


def test_cortex_provider_failure_does_not_become_a_diagnosis(tmp_path):
    result = run_self_diagnosis(
        FakeLLM(boom=True),
        _assessment([Axis("Cortex 信念", RED, "thin", detail={})]),
        **_paths(tmp_path),
    )
    assert result.diagnoses[0].root_cause == ""
    assert result.diagnoses[0].hypothesis == ""
    assert result.report_path is None


def test_malformed_confidence_does_not_leak_partial_fixes(tmp_path):
    llm = FakeLLM(
        ret={
            "root_cause": "unverified",
            "candidate_fixes": ["change prompt"],
            "confidence": "not a number",
        }
    )
    result = run_self_diagnosis(
        llm,
        _assessment(
            [
                Axis("Cortex 信念", RED, "thin", detail={}),
                Axis("報告品質", RED, "bad", detail={}),
            ]
        ),
        **_paths(tmp_path),
    )
    assert all(not dx.candidate_fixes and not dx.proposal_eligible for dx in result.diagnoses)
    assert result.report_path is None
