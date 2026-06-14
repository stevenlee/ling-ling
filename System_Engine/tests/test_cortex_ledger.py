"""Phase 4: conservative falsification, un-merge feedback, strict mode."""
import json
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.absolute()))

from maintenance.cortex_ledger import is_adjudication_strict, run_ledger_pass
from services.cortex_store import CortexPage, load_all_pages, make_claim_id, save_cortex_page

NOW = datetime(2026, 6, 12, 4, 0, 0)


def _page(cortex_dir, claim, *, contradictions=(), evidence_insights=("i1.md",),
          status="active", **kw):
    page = CortexPage(
        claim_id=make_claim_id(claim), path=cortex_dir / f"{claim[:24]}.md", claim=claim,
        status=status, contradictions=list(contradictions),
        evidence=[{"insight": n, "sources": [], "date": "2026-06-01", "summary": "s"}
                  for n in evidence_insights],
        last_reinforced_at="2026-06-11T03:00:00",
        created="2026-06-11T03:00:00", updated="2026-06-11T03:00:00", **kw,
    )
    save_cortex_page(page)
    return page


class FakeRAG:
    def __init__(self):
        self.removed = []

    def remove_facets(self, path):
        self.removed.append(path.name)


class FakeLLM:
    def __init__(self, verdict="refuted"):
        self.verdict = verdict
        self.calls = []

    def refute_insight(self, claim, sources):
        self.calls.append(claim)
        return {"verdict": self.verdict, "notes": ""}


def _env(tmp_path):
    return dict(
        cortex_dir=tmp_path / "Cortex",
        state_file=tmp_path / "ledger_state.json",
        log_path=tmp_path / "maintenance.log.md",
        enabled=True,
        now=NOW,
    )


def _contradiction_triangle(cortex_dir, *, independent=True):
    """Target claim contradicted by two others. independent=False makes
    both contradictors trace to the SAME insight."""
    a = "Target claim that may be falsified soon."
    b = "First contradicting claim from one study."
    c = "Second contradicting claim from another study."
    ida, idb, idc = (make_claim_id(x) for x in (a, b, c))
    target = _page(cortex_dir, a, contradictions=[idb, idc])
    _page(cortex_dir, b, contradictions=[ida],
          evidence_insights=("insight-one.md",))
    _page(cortex_dir, c, contradictions=[ida],
          evidence_insights=("insight-one.md",) if not independent else ("insight-two.md",))
    return target


class TestFalsified:
    def test_two_independent_contradictions_plus_confirm_falsifies(self, tmp_path):
        env = _env(tmp_path)
        target = _contradiction_triangle(env["cortex_dir"], independent=True)
        rag = FakeRAG()

        result = run_ledger_pass(FakeLLM(verdict="refuted"), rag, **env)

        assert result.falsified == [target.claim_id]
        assert rag.removed == [target.path.name]
        reloaded = {p.claim_id: p for p in load_all_pages(env["cortex_dir"])}
        page = reloaded[target.claim_id]
        assert page.status == "falsified"
        assert page.confidence == 0.1
        assert any("Falsified" in c for c in page.counterpoints)

    def test_single_source_pileon_not_independent(self, tmp_path):
        env = _env(tmp_path)
        _contradiction_triangle(env["cortex_dir"], independent=False)
        result = run_ledger_pass(FakeLLM(verdict="refuted"), FakeRAG(), **env)
        assert result.falsified == []
        assert result.candidates_checked == 0

    def test_one_contradiction_never_kills(self, tmp_path):
        env = _env(tmp_path)
        a = "Claim with a single objection."
        b = "The lone contradicting claim."
        _page(env["cortex_dir"], a, contradictions=[make_claim_id(b)])
        _page(env["cortex_dir"], b, contradictions=[make_claim_id(a)],
              evidence_insights=("other.md",))
        result = run_ledger_pass(FakeLLM(verdict="refuted"), FakeRAG(), **env)
        assert result.falsified == [] and result.candidates_checked == 0

    def test_refute_survival_blocks_kill_with_cooldown(self, tmp_path):
        env = _env(tmp_path)
        target = _contradiction_triangle(env["cortex_dir"], independent=True)
        llm = FakeLLM(verdict="survived")

        first = run_ledger_pass(llm, FakeRAG(), **env)
        assert first.falsified == [] and first.candidates_checked == 1

        # Cooldown: next pass doesn't re-check the survivor.
        second = run_ledger_pass(llm, FakeRAG(), **env)
        assert second.candidates_checked == 0
        assert len(llm.calls) == 1

    def test_quota_respected(self, tmp_path):
        env = _env(tmp_path)
        # Three independent triangles → 3 candidates, quota 1.
        for i in range(3):
            sub = env["cortex_dir"]
            a = f"Triangle {i} target claim here."
            b = f"Triangle {i} first contradictor."
            c = f"Triangle {i} second contradictor."
            _page(sub, a, contradictions=[make_claim_id(b), make_claim_id(c)])
            _page(sub, b, contradictions=[make_claim_id(a)], evidence_insights=(f"x{i}.md",))
            _page(sub, c, contradictions=[make_claim_id(a)], evidence_insights=(f"y{i}.md",))

        result = run_ledger_pass(FakeLLM(verdict="refuted"), FakeRAG(), **env, falsify_quota=1)
        assert len(result.falsified) == 1


class TestUnmergeFeedback:
    def test_shrinkage_detected_and_strict_mode_engages(self, tmp_path):
        env = _env(tmp_path)
        page = _page(env["cortex_dir"], "Merged page the user will split.",
                     evidence_insights=("a.md", "b.md", "c.md"))
        run_ledger_pass(FakeLLM(), FakeRAG(), **env)       # snapshot baseline

        # User splits: remove two evidence entries.
        page.evidence = page.evidence[:1]
        save_cortex_page(page)

        # Seed enough merge events that min-samples is met and rate is high.
        state = json.loads(env["state_file"].read_text())
        state["events"] = [
            {"kind": "merge", "claim_id": "x", "ts": "2026-06-10T00:00:00"}
        ] * 4
        env["state_file"].write_text(json.dumps(state), encoding="utf-8")

        result = run_ledger_pass(FakeLLM(), FakeRAG(), **env)

        assert result.unmerge_events == 1
        assert result.strict_mode is True                  # 1/5 = 20% ≥ 10%
        assert is_adjudication_strict(env["state_file"])

    def test_growth_counts_as_merge_event(self, tmp_path):
        env = _env(tmp_path)
        page = _page(env["cortex_dir"], "Page that will gain evidence.")
        run_ledger_pass(FakeLLM(), FakeRAG(), **env)

        page.evidence.append({"insight": "new.md", "sources": [], "date": "d", "summary": "s"})
        save_cortex_page(page)

        result = run_ledger_pass(FakeLLM(), FakeRAG(), **env)
        assert result.merge_events == 1
        assert result.strict_mode is False

    def test_flag_off_skips(self, tmp_path):
        env = _env(tmp_path)
        env["enabled"] = False
        _page(env["cortex_dir"], "Any claim.")
        assert run_ledger_pass(FakeLLM(), FakeRAG(), **env).status == "skipped"


class TestStrictModeInConsolidation:
    def test_equivalent_demotes_to_link_under_strict(self, tmp_path, monkeypatch):
        from maintenance.cortex_consolidation import run_consolidation
        from tests.test_cortex_consolidation import FakeLLM as ConsFakeLLM, FakeRAG as ConsFakeRAG, _write_insight

        # Engage strict mode via the ledger state the consolidation reads.
        ledger_state = tmp_path / "ledger_state.json"
        ledger_state.write_text(json.dumps({"adjudication_strict": True}), encoding="utf-8")
        import maintenance.cortex_ledger as ledger_mod
        monkeypatch.setattr(ledger_mod, "CORTEX_LEDGER_STATE_FILE", ledger_state)

        env = dict(
            insights_dir=tmp_path / "Insights", cortex_dir=tmp_path / "Cortex",
            state_file=tmp_path / "Database" / "state.json",
            cache_file=tmp_path / "Database" / "cache.json",
            report_dir=tmp_path / "out", log_path=tmp_path / "log.md", enabled=True,
        )
        _write_insight(env["insights_dir"], "n1.md", body="MARKER-A")
        llm1 = ConsFakeLLM(claims_map={"MARKER-A": [{"claim": "ALPHA base claim.", "summary": "s"}]})
        run_consolidation(llm1, ConsFakeRAG(), **env)

        _write_insight(env["insights_dir"], "n2.md", body="MARKER-B")
        llm2 = ConsFakeLLM(
            claims_map={"MARKER-B": [{"claim": "NEARALPHA same thing reworded.", "summary": "s"}]},
            verdicts={("NEARALPHA", "ALPHA"): "equivalent"},
        )
        result = run_consolidation(llm2, ConsFakeRAG(), **env)

        # Strict: no merge — a new page with a typed link instead.
        assert result.merged == 0 and result.created == 1
        pages = load_all_pages(env["cortex_dir"])
        assert len(pages) == 2
        assert any(p.related for p in pages)


def test_independent_insights_excludes_grounded_self_dissent():
    """F1 defense 1 (falsification side): an insight grounded ON the claim being
    judged is prompted dissent, not independent evidence — excluded from the count."""
    from maintenance.cortex_ledger import _independent_insights
    target = "cortex-P"
    c1 = CortexPage(claim_id="c1", path=Path("c1.md"), claim="not P",
                    evidence=[{"insight": "g1.md", "grounded_on": [target]}])
    c2 = CortexPage(claim_id="c2", path=Path("c2.md"), claim="also not P",
                    evidence=[{"insight": "g2.md", "grounded_on": [target]}])
    # Both contradictors come from insights grounded ON P → not independent of P.
    assert _independent_insights([c1, c2], exclude_grounded_on=target) == set()
    # External evidence (or judging a different claim) → both count.
    assert len(_independent_insights([c1, c2])) == 2
    assert len(_independent_insights([c1, c2], exclude_grounded_on="cortex-OTHER")) == 2
