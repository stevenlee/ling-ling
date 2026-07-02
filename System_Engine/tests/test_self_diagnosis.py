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
    assert all(d.candidate_fixes for d in r.diagnoses)
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
