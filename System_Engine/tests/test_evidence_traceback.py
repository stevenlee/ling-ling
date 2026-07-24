"""A2 evidence traceback — falsifier-first, dry-run-only corroboration scan."""

import json
from datetime import datetime

import pytest

from maintenance.evidence_traceback import (
    TracebackResult,
    run_evidence_traceback,
)
from services.cortex_store import CortexPage, save_cortex_page

_NOW = datetime(2026, 7, 25, 3, 0, 0)


def _claim_page(
    cortex_dir,
    claim_id,
    claim,
    *,
    falsifier="會推翻它的觀察",
    evidence=None,
    created="2026-06-01T00:00:00",
):
    page = CortexPage(
        claim_id=claim_id,
        path=cortex_dir / f"{claim_id}.md",
        claim=claim,
        falsifier=falsifier,
        created=created,
        updated=created,
        evidence=evidence
        if evidence is not None
        else [
            {
                "insight": f"[20260601-000000][OriginDoc (Synthesis)+Other][{claim_id}].md",
                "sources": [],
                "date": "2026-06-01",
                "summary": "origin",
            }
        ],
    )
    save_cortex_page(page)
    return page


def _hit(title, text="passage text", distance=0.3, source_path="/vault/pages/x/doc.md"):
    return {
        "text": text,
        "distance": distance,
        "metadata": {"title": title, "source_path": source_path},
    }


class _StubRAG:
    def __init__(self, hits):
        self.hits = hits
        self.queries = []

    def query_notes(self, query, top_k=3, **kwargs):
        self.queries.append(query)
        return list(self.hits)


class _StubLLM:
    def __init__(self, relation="supports", raise_exc=None, parsed=None):
        self.relation = relation
        self.raise_exc = raise_exc
        self.parsed = parsed
        self.calls = []

    def _complete_json(self, **kwargs):
        self.calls.append(kwargs)
        if self.raise_exc:
            raise self.raise_exc
        if self.parsed is not None:
            return self.parsed
        return {"relation": self.relation, "reason": "理由"}


@pytest.fixture
def env(tmp_path, monkeypatch):
    cortex = tmp_path / "Cortex"
    out = tmp_path / "fromLingLing"
    state = tmp_path / "Database" / "evidence_traceback_state.json"
    cortex.mkdir()
    monkeypatch.setattr("core.config.settings.EVIDENCE_TRACEBACK_BATCH", 5, raising=False)
    monkeypatch.setattr("core.config.settings.EVIDENCE_TRACEBACK_MAX_DISTANCE", 0.45, raising=False)
    return {"cortex": cortex, "out": out, "state": state}


def _run(env, llm, rag, now=_NOW) -> TracebackResult:
    return run_evidence_traceback(
        llm, rag, cortex_dir=env["cortex"], out_dir=env["out"], state_file=env["state"], now=now
    )


def test_no_rag_skips(env):
    assert _run(env, _StubLLM(), None).status == "skipped"


def test_no_thin_claims_is_ok_without_report(env):
    _claim_page(
        env["cortex"],
        "cortex-multi",
        "已有兩筆證據的主張",
        evidence=[
            {"insight": "a.md", "sources": [], "date": "2026-06-01", "summary": "s1"},
            {"insight": "b.md", "sources": [], "date": "2026-06-02", "summary": "s2"},
        ],
    )
    result = _run(env, _StubLLM(), _StubRAG([_hit("Some Doc")]))
    assert result.status == "ok"
    assert result.report_path is None
    assert not env["out"].exists()


def test_falsifier_query_runs_first(env):
    _claim_page(env["cortex"], "cortex-thin1", "主張 A", falsifier="反例情境 X")
    rag = _StubRAG([_hit("Independent Doc")])
    result = _run(env, _StubLLM(), rag)
    assert result.status == "ok"
    assert rag.queries[0] == "反例情境 X"  # falsifier-first
    assert rag.queries[1] == "主張 A"


def test_claim_only_query_when_falsifier_empty(env):
    _claim_page(env["cortex"], "cortex-thin1", "主張 A", falsifier="")
    rag = _StubRAG([_hit("Independent Doc")])
    _run(env, _StubLLM(), rag)
    assert rag.queries == ["主張 A"]


def test_dry_run_never_mutates_cortex(env):
    page = _claim_page(env["cortex"], "cortex-thin1", "主張 A")
    before = page.path.read_bytes()
    result = _run(env, _StubLLM(relation="supports"), _StubRAG([_hit("Independent Doc")]))
    assert result.status == "ok"
    assert page.path.read_bytes() == before  # claim file untouched
    report = result.report_path.read_text(encoding="utf-8")
    assert "dry-run" in report
    assert "[supports] Independent Doc" in report
    assert "+1 evidence" in report  # would-be action is REPORTED, not applied


def test_self_and_derivative_sources_excluded(env):
    _claim_page(env["cortex"], "cortex-thin1", "主張 A")
    rag = _StubRAG(
        [
            _hit("OriginDoc (Synthesis)"),  # claim's own origin (prefix match)
            _hit("Some belief page", source_path="/vault/Cortex/belief.md"),
            _hit("✅Scout-2026-07-11"),
            _hit("Truly Independent Doc"),
        ]
    )
    llm = _StubLLM()
    result = _run(env, llm, rag)
    scan = result.scans[0]
    titles = [j.title for j in scan.judgments]
    assert titles == ["Truly Independent Doc"]
    # 3 excluded per query pass, and each seen title is judged at most once.
    assert scan.excluded_self >= 3
    assert len(llm.calls) == 1


def test_distance_gate_excludes_far_hits(env):
    _claim_page(env["cortex"], "cortex-thin1", "主張 A")
    rag = _StubRAG([_hit("Far Doc", distance=0.9), _hit("Near Doc", distance=0.2)])
    result = _run(env, _StubLLM(), rag)
    scan = result.scans[0]
    assert [j.title for j in scan.judgments] == ["Near Doc"]
    assert scan.excluded_far >= 1


def test_contradicts_maps_to_tension_action(env):
    _claim_page(env["cortex"], "cortex-thin1", "主張 A")
    result = _run(env, _StubLLM(relation="contradicts"), _StubRAG([_hit("Independent Doc")]))
    assert "記 tension" in result.report_path.read_text(encoding="utf-8")


def test_llm_failure_is_error_not_verdict_and_run_continues(env):
    _claim_page(env["cortex"], "cortex-thin1", "主張 A", created="2026-06-01T00:00:00")
    _claim_page(env["cortex"], "cortex-thin2", "主張 B", created="2026-06-02T00:00:00")
    result = _run(env, _StubLLM(raise_exc=RuntimeError("boom")), _StubRAG([_hit("Fresh Doc")]))
    assert result.status == "ok"  # one bad judgment never aborts the batch
    assert len(result.scans) == 2
    relations = [j.relation for s in result.scans for j in s.judgments]
    assert relations == ["error", "error"]  # never coerced to neutral
    assert "錯誤 2" in result.report_path.read_text(encoding="utf-8")


def test_invalid_relation_counts_unparseable(env):
    _claim_page(env["cortex"], "cortex-thin1", "主張 A")
    result = _run(
        env, _StubLLM(parsed={"relation": "maybe", "reason": "?"}), _StubRAG([_hit("Fresh Doc")])
    )
    assert result.scans[0].judgments[0].relation == "unparseable"
    assert "無法解析 1" in result.report_path.read_text(encoding="utf-8")


def test_batch_bound_and_rotation_cursor(env, monkeypatch):
    monkeypatch.setattr("core.config.settings.EVIDENCE_TRACEBACK_BATCH", 1, raising=False)
    _claim_page(env["cortex"], "cortex-thin1", "主張 A", created="2026-06-01T00:00:00")
    _claim_page(env["cortex"], "cortex-thin2", "主張 B", created="2026-06-02T00:00:00")

    first = _run(env, _StubLLM(), _StubRAG([_hit("Fresh Doc")]))
    assert [s.claim_id for s in first.scans] == ["cortex-thin1"]  # oldest first

    second = _run(env, _StubLLM(), _StubRAG([_hit("Fresh Doc")]), now=datetime(2026, 7, 26, 3, 0))
    assert [s.claim_id for s in second.scans] == ["cortex-thin2"]  # cursor rotated

    state = json.loads(env["state"].read_text(encoding="utf-8"))
    assert set(state["checked"]) == {"cortex-thin1", "cortex-thin2"}


def test_scheduler_gate_skips_when_disabled(monkeypatch, tmp_path):
    from watchers.maintenance_scheduler import MaintenanceScheduler

    monkeypatch.setattr("core.config.settings.EVIDENCE_TRACEBACK_ENABLED", False, raising=False)
    scheduler = MaintenanceScheduler(tmp_path, llm=None, rag=None, state_file=tmp_path / "s.json")
    task = next(t for t in scheduler.tasks if t.name == "evidence_traceback_daily")
    result = task.action()
    assert result.status == "skipped"
    assert "disabled" in result.summary


def test_short_title_needs_exact_match_to_count_as_self_source(env):
    # "Doc" is a substring of the origin filename's "OriginDoc", but a 3-char
    # prefix is not distinctive — fuzzy containment would exclude half the
    # vault, so short titles are excluded only on exact match.
    _claim_page(env["cortex"], "cortex-thin1", "主張 A")
    result = _run(env, _StubLLM(), _StubRAG([_hit("Doc")]))
    assert [j.title for j in result.scans[0].judgments] == ["Doc"]
