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


def _hit(title, text="passage text", distance=0.3, source_path=None):
    source_path = source_path or f"/vault/pages/{title}.md"
    return {
        "text": text,
        "distance": distance,
        "metadata": {"title": title, "source_path": source_path},
    }


class _StubRAG:
    def __init__(self, hits):
        self.hits = hits
        self.queries = []
        self.indexed = []

    def query_notes(self, query, top_k=3, **kwargs):
        self.queries.append(query)
        return list(self.hits)

    # Apply mode re-indexes the mutated page (consolidation-style). No-op record.
    def add_document(self, filepath, title, text, **kwargs):
        self.indexed.append(title)

    def add_facets(self, filepath, title, facets, **kwargs):
        return True


class _PerQueryRAG:
    def __init__(self, hits_by_query):
        self.hits_by_query = hits_by_query

    def query_notes(self, query, top_k=3, **kwargs):
        return list(self.hits_by_query[query])


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
    # Default OFF so a dev Scripture with apply:true can't leak into dry-run tests.
    monkeypatch.setattr("core.config.settings.EVIDENCE_TRACEBACK_APPLY", False, raising=False)
    return {"cortex": cortex, "out": out, "state": state, "monkeypatch": monkeypatch}


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


def test_relative_cortex_path_and_claim_id_title_are_excluded(env):
    _claim_page(env["cortex"], "cortex-thin1", "主張 A")
    rag = _StubRAG(
        [
            _hit(
                "主張文字作為 title",
                source_path="Cortex/claim-page.md",
            ),
            _hit(
                "cortex-thin1",
                source_path="another-claim-page.md",
            ),
            _hit("Independent", source_path="pages/Independent.md"),
        ]
    )

    result = _run(env, _StubLLM(), rag)

    assert [j.title for j in result.scans[0].judgments] == ["Independent"]
    assert result.scans[0].excluded_self == 2


def test_distance_gate_excludes_far_hits(env):
    _claim_page(env["cortex"], "cortex-thin1", "主張 A")
    rag = _StubRAG([_hit("Far Doc", distance=0.9), _hit("Near Doc", distance=0.2)])
    result = _run(env, _StubLLM(), rag)
    scan = result.scans[0]
    assert [j.title for j in scan.judgments] == ["Near Doc"]
    assert scan.excluded_far >= 1


def test_same_source_keeps_nearest_hit_across_queries(env):
    _claim_page(env["cortex"], "cortex-thin1", "主張 A", falsifier="反例情境 X")
    rag = _PerQueryRAG(
        {
            "反例情境 X": [_hit("Same Doc", distance=0.9)],
            "主張 A": [_hit("Same Doc", distance=0.2)],
        }
    )

    result = _run(env, _StubLLM(), rag)

    assert [j.title for j in result.scans[0].judgments] == ["Same Doc"]
    assert result.scans[0].judgments[0].distance == 0.2


def test_same_title_different_sources_are_distinct_candidates(env):
    _claim_page(env["cortex"], "cortex-thin1", "主張 A", falsifier="")
    rag = _StubRAG(
        [
            _hit("Shared Title", source_path="/vault/pages/a.md"),
            _hit("Shared Title", source_path="/vault/pages/b.md"),
        ]
    )

    result = _run(env, _StubLLM(), rag)

    assert [j.title for j in result.scans[0].judgments] == ["Shared Title", "Shared Title"]


def test_part_stitched_and_synthesis_share_one_source_family(env):
    _claim_page(env["cortex"], "cortex-thin1", "主張 A", falsifier="")
    rag = _StubRAG(
        [
            _hit(
                "Doc (Synthesis)",
                distance=0.3,
                source_path="pages/Doc/Doc (Synthesis).md",
            ),
            _hit(
                "Doc (Stitched)",
                distance=0.2,
                source_path="pages/Doc/Doc (Stitched).md",
            ),
            _hit(
                "Doc (Part 3)",
                distance=0.1,
                source_path="pages/Doc/Doc (Part 3).md",
            ),
        ]
    )

    result = _run(env, _StubLLM(), rag)

    assert [j.title for j in result.scans[0].judgments] == ["Doc (Part 3)"]
    assert result.scans[0].judgments[0].distance == 0.1


def test_same_filename_in_different_directories_remains_distinct(env):
    _claim_page(env["cortex"], "cortex-thin1", "主張 A", falsifier="")
    rag = _StubRAG(
        [
            _hit("Doc A", source_path="pages/A/Doc.md"),
            _hit("Doc B", source_path="pages/B/Doc.md"),
        ]
    )

    result = _run(env, _StubLLM(), rag)

    assert [j.title for j in result.scans[0].judgments] == ["Doc A", "Doc B"]


def test_non_finite_distance_is_excluded(env):
    _claim_page(env["cortex"], "cortex-thin1", "主張 A")
    result = _run(env, _StubLLM(), _StubRAG([_hit("NaN Doc", distance=float("nan"))]))

    assert result.scans[0].judgments == []
    assert result.scans[0].excluded_far == 1


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


# ── apply mode (EVIDENCE_TRACEBACK_APPLY=true) ───────────────────────────

from services.cortex_store import parse_cortex_page  # noqa: E402


def _apply_on(env):
    env["monkeypatch"].setattr("core.config.settings.EVIDENCE_TRACEBACK_APPLY", True, raising=False)


def _thin_claim_reinforceable(env, claim_id="cortex-thin1", claim="主張 A"):
    # last_reinforced far in the past so R<1 and a reinforce actually moves S.
    page = _claim_page(env["cortex"], claim_id, claim)
    page.S = 1.0
    page.confidence = 0.5
    page.last_reinforced_at = "2026-05-01T00:00:00"
    save_cortex_page(page)
    return page


def test_apply_supports_appends_evidence_and_reinforces(env):
    _apply_on(env)
    page = _thin_claim_reinforceable(env)
    rag = _StubRAG([_hit("Independent Doc")])

    result = _run(env, _StubLLM(relation="supports"), rag)

    reloaded = parse_cortex_page(page.path)
    # evidence thickened 1 → 2, no longer thin; new entry is EvidenceTrace-marked.
    assert len(reloaded.evidence) == 2
    markers = [e["insight"] for e in reloaded.evidence]
    assert "[EvidenceTrace] Independent Doc" in markers
    # gentle reinforce moved S up and nudged confidence.
    assert reloaded.S > 1.0
    assert reloaded.confidence == 0.55
    # re-indexed (the CLAIM page, keyed by claim_id) + report reflects apply.
    assert "cortex-thin1" in rag.indexed
    report = result.report_path.read_text(encoding="utf-8")
    assert "（apply）" in report
    assert "已套用：+1 evidence＋強化" in report
    assert result.scans[0].applied["added_evidence"] == 1


def test_apply_contradicts_records_counterpoint_no_reinforce(env):
    _apply_on(env)
    page = _thin_claim_reinforceable(env)
    before_S, before_conf = page.S, page.confidence

    result = _run(env, _StubLLM(relation="contradicts"), _StubRAG([_hit("Independent Doc")]))

    reloaded = parse_cortex_page(page.path)
    assert len(reloaded.evidence) == 1  # NOT thickened
    assert any("Independent Doc" in c for c in reloaded.counterpoints)  # tension visible
    assert reloaded.S == before_S  # no reinforce
    assert reloaded.confidence == before_conf  # no auto-flip / dent
    assert result.scans[0].applied["tensions"] == 1
    assert "記 1 筆 tension" in result.report_path.read_text(encoding="utf-8")


def test_apply_thickened_claim_leaves_thin_pool(env):
    # Structural idempotency: once a support apply lifts a claim to 2 evidence,
    # it drops out of the thin pool and is never re-scanned — so it cannot be
    # double-counted on a later night.
    _apply_on(env)
    page = _thin_claim_reinforceable(env)

    first = _run(env, _StubLLM(relation="supports"), _StubRAG([_hit("Independent Doc")]))
    assert first.scans[0].applied["added_evidence"] == 1
    assert len(parse_cortex_page(page.path).evidence) == 2

    second = _run(
        env,
        _StubLLM(relation="supports"),
        _StubRAG([_hit("Independent Doc")]),
        now=datetime(2026, 7, 26, 3, 0),
    )
    # No longer thin → not re-scanned; still 2 evidence, not 3.
    assert all(s.claim_id != "cortex-thin1" for s in second.scans)
    assert len(parse_cortex_page(page.path).evidence) == 2


def test_apply_scan_refreshes_existing_trace_marker_in_place(env):
    # Direct unit test of the refresh branch: a claim that STAYS thin (its one
    # real evidence plus a prior [EvidenceTrace] entry that was later trimmed
    # elsewhere) — re-finding the same source refreshes, never double-appends
    # or re-reinforces.
    from maintenance.evidence_traceback import ClaimScan, PassageJudgment, _apply_scan

    page = _thin_claim_reinforceable(env)
    page.evidence.append(
        {
            "insight": "[EvidenceTrace] Independent Doc",
            "sources": ["Independent Doc"],
            "date": "2026-07-01",
            "summary": "舊摘要",
        }
    )
    save_cortex_page(page)
    s_before = page.S

    scan = ClaimScan(claim_id=page.claim_id, claim=page.claim, falsifier=page.falsifier)
    scan.judgments = [
        PassageJudgment(title="Independent Doc", relation="supports", reason="新摘要")
    ]
    applied = _apply_scan(page, scan, _StubRAG([]), _NOW)

    assert applied["added_evidence"] == 0
    assert applied["refreshed"] == 1
    assert applied["reinforced"] is False
    reloaded = parse_cortex_page(page.path)
    markers = [e["insight"] for e in reloaded.evidence]
    assert markers.count("[EvidenceTrace] Independent Doc") == 1  # not doubled
    assert reloaded.S == s_before  # no second reinforce


def test_apply_neutral_leaves_page_untouched(env):
    _apply_on(env)
    page = _thin_claim_reinforceable(env)
    before = page.path.read_bytes()

    result = _run(env, _StubLLM(relation="neutral"), _StubRAG([_hit("Independent Doc")]))

    assert page.path.read_bytes() == before
    assert result.scans[0].applied == {
        "added_evidence": 0,
        "refreshed": 0,
        "tensions": 0,
        "reinforced": False,
    }


def test_apply_write_failure_recorded_not_silent(env, monkeypatch):
    _apply_on(env)
    _thin_claim_reinforceable(env, "cortex-thin1", "主張 A")
    _thin_claim_reinforceable(env, "cortex-thin2", "主張 B")
    monkeypatch.setattr("core.config.settings.EVIDENCE_TRACEBACK_BATCH", 2, raising=False)

    def boom(page):
        raise OSError("disk full")

    monkeypatch.setattr("maintenance.evidence_traceback.save_cortex_page", boom, raising=False)
    result = _run(env, _StubLLM(relation="supports"), _StubRAG([_hit("Independent Doc")]))

    # Both claims recorded a write error; run still completed (never aborted).
    assert result.status == "ok"
    assert all(s.applied.get("error") for s in result.scans)
    assert "寫入失敗" in result.report_path.read_text(encoding="utf-8")


def test_dry_run_default_leaves_applied_none(env):
    # env pins APPLY False → applied stays None, page untouched.
    page = _thin_claim_reinforceable(env)
    before = page.path.read_bytes()
    result = _run(env, _StubLLM(relation="supports"), _StubRAG([_hit("Independent Doc")]))
    assert result.scans[0].applied is None
    assert page.path.read_bytes() == before


def test_apply_contradicts_rescan_does_not_stack_counterpoints(env):
    # A contradicts leaves the claim thin (evidence unchanged), so it WILL be
    # re-scanned. Re-finding the same contradicting source must not stack
    # duplicate counterpoints.
    _apply_on(env)
    page = _thin_claim_reinforceable(env)

    _run(env, _StubLLM(relation="contradicts"), _StubRAG([_hit("Independent Doc")]))
    after_first = parse_cortex_page(page.path).counterpoints
    assert len(after_first) == 1

    _run(
        env,
        _StubLLM(relation="contradicts"),
        _StubRAG([_hit("Independent Doc")]),
        now=datetime(2026, 7, 26, 3, 0),
    )
    after_second = parse_cortex_page(page.path).counterpoints
    assert len(after_second) == 1  # same source → not stacked
