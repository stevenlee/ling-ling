"""Metacognition M1: unified self-assessment evaluator."""
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.absolute()))
os.environ.setdefault("LLM_PROVIDER", "vllm")

from maintenance.self_assessment import (
    GREEN, RED, YELLOW, UNKNOWN, run_self_assessment,
)


class FakeTrace:
    """Stand-in for TraceStore exposing only the two read helpers M1 uses."""
    def __init__(self, artifacts=None, health=None):
        self._artifacts = artifacts or []
        self._health = health or {"total": 0, "failed": 0, "error_rate": 0.0,
                                   "total_tokens": 0, "by_stage": {}}

    def query_all_artifacts(self, since_days=7):
        return list(self._artifacts)

    def llm_call_health(self, since_days=7):
        return dict(self._health)


def _paths(tmp_path):
    """Return kwargs pointing every file source at empty/tmp locations."""
    return dict(
        cortex_dir=tmp_path / "cortex",
        insights_dir=tmp_path / "insights",
        bench_history_file=tmp_path / "bench.json",
        decay_file=tmp_path / "decay.json",
        ledger_file=tmp_path / "ledger.json",
        report_dir=tmp_path / "out",
        log_path=tmp_path / "maint.log.md",
    )


def _axis(result, name):
    return next(a for a in result.axes if a.name == name)


# ── overall plumbing ──────────────────────────────────────────────────────

def test_empty_everything_is_green_and_quiet(tmp_path):
    # No data anywhere → axes evaluate to green/unknown, no observations,
    # so no full report is written (quiet week stays quiet).
    r = run_self_assessment(FakeTrace(), **_paths(tmp_path))
    assert r.status == "succeeded"
    assert r.overall in (GREEN, UNKNOWN)
    assert r.observations == []
    assert r.report_path is None
    # one-line summary still lands in the maintenance log
    assert (tmp_path / "maint.log.md").exists()


def test_six_axes_always_present(tmp_path):
    r = run_self_assessment(FakeTrace(), **_paths(tmp_path))
    names = {a.name for a in r.axes}
    assert names == {"報告品質", "LLM 健康", "檢索品質", "Cortex 信念", "記憶衰減", "洞察品質"}


# ── report-quality axis ───────────────────────────────────────────────────

def test_report_quality_red_on_high_revise(tmp_path):
    arts = [{"artifact_type": "report_synthesis", "quality_verdict": v}
            for v in ("revise", "reject", "revise", "keep")]
    r = run_self_assessment(FakeTrace(artifacts=arts), **_paths(tmp_path))
    ax = _axis(r, "報告品質")
    assert ax.lamp == RED                       # 3/4 = 75% bad
    assert any("report_synthesis" in o for o in r.observations)
    assert r.report_path is not None            # red → full report written


def test_report_quality_green_when_no_verdicts(tmp_path):
    arts = [{"artifact_type": "stitched_article", "quality_verdict": None}]
    ax = _axis(run_self_assessment(FakeTrace(artifacts=arts), **_paths(tmp_path)), "報告品質")
    assert ax.lamp == GREEN


# ── llm-health axis ───────────────────────────────────────────────────────

def test_llm_health_red_on_errors(tmp_path):
    health = {"total": 20, "failed": 5, "error_rate": 0.25, "total_tokens": 9999,
              "by_stage": {"synthesis": {"total": 20, "failed": 5, "tokens": 9999}}}
    r = run_self_assessment(FakeTrace(health=health), **_paths(tmp_path))
    ax = _axis(r, "LLM 健康")
    assert ax.lamp == RED
    assert any("synthesis" in o for o in r.observations)


# ── retrieval axis ────────────────────────────────────────────────────────

def test_retrieval_red_below_floor(tmp_path):
    p = _paths(tmp_path)
    p["bench_history_file"].write_text(json.dumps([
        {"ts": "t1", "pass_rate": 0.95}, {"ts": "t2", "pass_rate": 0.50},
    ]), encoding="utf-8")
    ax = _axis(run_self_assessment(FakeTrace(), **p), "檢索品質")
    assert ax.lamp == RED                        # 0.50 < default floor 0.8


def test_retrieval_yellow_on_regression_above_floor(tmp_path):
    p = _paths(tmp_path)
    p["bench_history_file"].write_text(json.dumps([
        {"ts": "t1", "pass_rate": 0.95}, {"ts": "t2", "pass_rate": 0.88},
    ]), encoding="utf-8")
    ax = _axis(run_self_assessment(FakeTrace(), **p), "檢索品質")
    assert ax.lamp == YELLOW                     # dropped but still >= 0.8


# ── cortex axis ───────────────────────────────────────────────────────────

def test_cortex_red_on_dogmatic(tmp_path, monkeypatch):
    p = _paths(tmp_path)

    class FakeReport:
        contradictions = []
        dogmatic = ["p1"]
        thin_evidence = []
        falsified = []
        total_pages = 5

    monkeypatch.setattr("services.cortex_tensions.scan_tensions", lambda d: FakeReport())
    r = run_self_assessment(FakeTrace(), **p)
    ax = _axis(r, "Cortex 信念")
    assert ax.lamp == RED
    assert any("教條" in o for o in r.observations)


# ── insight-quality axis ──────────────────────────────────────────────────

def test_insight_axis_reads_frontmatter(tmp_path):
    p = _paths(tmp_path)
    idir = p["insights_dir"]; idir.mkdir(parents=True)
    (idir / "a.md").write_text(
        "---\nsignals:\n  novelty: 0.7\n  groundedness: 0.9\n  refute_verdict: survived\n"
        "grounded_on:\n  - c1\n---\nbody", encoding="utf-8")
    (idir / "b.md").write_text(
        "---\nsignals:\n  novelty: 0.6\n  refute_verdict: refuted\n---\nbody", encoding="utf-8")
    ax = _axis(run_self_assessment(FakeTrace(), **p), "洞察品質")
    assert ax.detail["n"] == 2
    assert ax.detail["grounded_n"] == 1 and ax.detail["cold_n"] == 1
    assert ax.detail["refuted"] == 1


# ── fail-open ─────────────────────────────────────────────────────────────

def test_axis_failopen_does_not_crash_report(tmp_path):
    class Boom:
        def query_all_artifacts(self, since_days=7):
            raise RuntimeError("db gone")
        def llm_call_health(self, since_days=7):
            raise RuntimeError("db gone")

    r = run_self_assessment(Boom(), **_paths(tmp_path))
    assert r.status == "succeeded"               # other axes still ran
    # the two trace-backed axes degrade to unknown, not crash
    assert _axis(r, "報告品質").lamp == UNKNOWN
    assert _axis(r, "LLM 健康").lamp == UNKNOWN


# ── trend persistence ─────────────────────────────────────────────────────

def test_history_persisted_and_capped(tmp_path, monkeypatch):
    import maintenance.self_assessment as sa
    monkeypatch.setattr(sa, "SELF_ASSESSMENT_HISTORY_MAX", 3)
    p = _paths(tmp_path)
    hist_file = tmp_path / "hist.json"
    for _ in range(5):
        run_self_assessment(FakeTrace(), history_file=hist_file, **p)
    snaps = json.loads(hist_file.read_text())
    assert len(snaps) == 3                        # capped
    assert all("overall" in s and "axes" in s for s in snaps)


def test_trend_arrows_and_streak(tmp_path):
    p = _paths(tmp_path)
    hist_file = tmp_path / "hist.json"
    # Run 1: retrieval red.
    p["bench_history_file"].write_text(json.dumps([{"ts": "t", "pass_rate": 0.5}]), encoding="utf-8")
    r1 = run_self_assessment(FakeTrace(), history_file=hist_file, **p)
    assert r1.trend["檢索品質"]["arrow"] == "•"   # no prior → new
    assert r1.trend["檢索品質"]["streak"] == 1
    # Run 2: retrieval still red → streak grows, stable arrow.
    r2 = run_self_assessment(FakeTrace(), history_file=hist_file, **p)
    assert r2.trend["檢索品質"]["arrow"] == "→"
    assert r2.trend["檢索品質"]["streak"] == 2
    # Run 3: retrieval recovers to green → improving arrow.
    p["bench_history_file"].write_text(json.dumps([{"ts": "t", "pass_rate": 0.95}]), encoding="utf-8")
    r3 = run_self_assessment(FakeTrace(), history_file=hist_file, **p)
    assert r3.trend["檢索品質"]["arrow"] == "↑"


def test_chronic_axis_adds_observation(tmp_path):
    p = _paths(tmp_path)
    hist_file = tmp_path / "hist.json"
    p["bench_history_file"].write_text(json.dumps([{"ts": "t", "pass_rate": 0.5}]), encoding="utf-8")
    r = None
    for _ in range(3):
        r = run_self_assessment(FakeTrace(), history_file=hist_file, **p)
    # 3 consecutive reds on retrieval → a "慢性問題" observation appears.
    assert any("慢性" in o and "檢索品質" in o for o in r.observations)
