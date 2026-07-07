"""Validation harness tools: signals backfill + the three-tier report."""

from core.parser import parse_markdown_metadata
from maintenance.cortex_validation import run_validation
from maintenance.insight_signals_backfill import backfill_signals
from services.cortex_store import CortexPage, make_claim_id, save_cortex_page


class FakeRAG:
    def __init__(self, facet_titles=None):
        self.facet_titles = list(facet_titles or [])

    def ef(self, texts):
        return [[1.0, 0.0] for _ in texts]

    def get_all_indexed_titles(self):
        return set()

    def get_facet_entries(self):
        return [{"title": t} for t in self.facet_titles]


class FakeLLM:
    def refute_insight(self, candidate, sources):
        return {"verdict": "survived", "notes": "n"}


class TestBackfill:
    def test_unsigned_insight_gains_signals_body_preserved(self, tmp_path, monkeypatch):
        import services.insight_signals as sig

        monkeypatch.setattr(sig, "INSIGHT_SIGNALS_ENABLED", True)
        monkeypatch.setattr(sig, "INSIGHT_REFUTE_ENABLED", True)
        monkeypatch.setattr(sig, "INSIGHT_SIGNALS_FILE", tmp_path / "db" / "sig.json")
        monkeypatch.setattr(sig, "PAGES_DIR", tmp_path / "pages")
        monkeypatch.setattr(sig, "NOTES_DIR", tmp_path / "Notes")

        pages = tmp_path / "pages"
        pages.mkdir()
        (pages / "Doc A.md").write_text("Source content about X and Y.\n" * 5, encoding="utf-8")

        insights = tmp_path / "Insights"
        insights.mkdir()
        body = "# 洞察\n\n本文主張 X 與 Y 相關。\n"
        # Vault-wide insight: no source pages → refute skips (M4 behavior).
        (insights / "[20260601-010101][Vault][full-insight].md").write_text(
            f"---\ntitle: old insight\n---\n\n{body}",
            encoding="utf-8",
        )
        # Doc-anchored insight: source content exists → refute runs.
        (insights / "[20260601-020202][Doc A][insight-recency].md").write_text(
            f"---\ntitle: doc insight\n---\n\n{body}",
            encoding="utf-8",
        )
        (insights / "[20260602-010101][Vault][full-insight].md").write_text(
            "---\nsignals:\n  groundedness: 1.0\n---\n\nalready signed\n",
            encoding="utf-8",
        )

        result = backfill_signals(FakeRAG(), FakeLLM(), insights_dir=insights)

        assert result.backfilled == 2 and result.skipped_signed == 1
        vault_meta = parse_markdown_metadata(
            (insights / "[20260601-010101][Vault][full-insight].md").read_text(encoding="utf-8")
        )
        assert vault_meta["signals"]["refute_verdict"] is None  # no sources → skip
        assert vault_meta["signals_backfilled"] is True

        doc_text = (insights / "[20260601-020202][Doc A][insight-recency].md").read_text(
            encoding="utf-8"
        )
        doc_meta = parse_markdown_metadata(doc_text)
        assert doc_meta["signals"]["refute_verdict"] == "survived"  # sources loaded from filename
        assert body.strip() in doc_text  # body preserved

    def _patch_dirs(self, tmp_path, monkeypatch):
        import services.insight_signals as sig

        monkeypatch.setattr(sig, "INSIGHT_SIGNALS_ENABLED", True)
        monkeypatch.setattr(sig, "INSIGHT_REFUTE_ENABLED", True)
        monkeypatch.setattr(sig, "INSIGHT_SIGNALS_FILE", tmp_path / "db" / "sig.json")
        monkeypatch.setattr(sig, "PAGES_DIR", tmp_path / "pages")
        monkeypatch.setattr(sig, "NOTES_DIR", tmp_path / "Notes")
        (tmp_path / "pages").mkdir()
        (tmp_path / "Notes").mkdir()
        insights = tmp_path / "Insights"
        insights.mkdir()
        return tmp_path / "pages", insights

    def test_force_resigns_and_preserves_verdict_without_refute(self, tmp_path, monkeypatch):
        pages, insights = self._patch_dirs(tmp_path, monkeypatch)
        (pages / "Doc A.md").write_text("Source content.", encoding="utf-8")

        signed = insights / "[20260601-020202][Doc A][insight-recency].md"
        signed.write_text(
            "---\nsignals:\n  groundedness: 0.0\n  novelty: null\n  bridging: 0.0\n"
            "  refute_verdict: survived\n---\n\nbody [[Doc A]]\n",
            encoding="utf-8",
        )

        # Without force: skipped.
        result = backfill_signals(FakeRAG(), None, insights_dir=insights, run_refute=False)
        assert result.skipped_signed == 1 and result.resigned == 0

        # With force: recomputed, but the earned verdict survives a no-refute run.
        result = backfill_signals(
            FakeRAG(), None, insights_dir=insights, run_refute=False, force=True
        )
        assert result.resigned == 1 and result.backfilled == 0
        meta = parse_markdown_metadata(signed.read_text(encoding="utf-8"))
        assert meta["signals"]["groundedness"] == 1.0  # [[Doc A]] resolves now
        assert meta["signals"]["refute_verdict"] == "survived"  # preserved

    def test_dry_run_writes_nothing(self, tmp_path, monkeypatch):
        pages, insights = self._patch_dirs(tmp_path, monkeypatch)
        f = insights / "[20260601-010101][Vault][full-insight].md"
        original = "---\ntitle: t\n---\n\nbody\n"
        f.write_text(original, encoding="utf-8")

        result = backfill_signals(
            FakeRAG(), None, insights_dir=insights, run_refute=False, dry_run=True
        )
        assert result.backfilled == 1
        assert f.read_text(encoding="utf-8") == original

    def test_mirror_segment_truncated_and_plus_joined(self, tmp_path, monkeypatch):
        """Filename related-segment: titles joined with '+', each truncated —
        must resolve back to real page stems by prefix."""
        pages, insights = self._patch_dirs(tmp_path, monkeypatch)
        nested = pages / "The Bitter Lesson"
        nested.mkdir()
        (nested / "The Bitter Lesson (Synthesis).md").write_text("bitter", encoding="utf-8")
        nested2 = (
            pages / "Identifying and remediating a persistent memory compromise in Claude Code"
        )
        nested2.mkdir()
        (
            nested2
            / "Identifying and remediating a persistent memory compromise in Claude Code (Synthesis).md"
        ).write_text("memory", encoding="utf-8")

        f = (
            insights
            / "[20260707-041607][Identifying and remediating a persistent memory compromise in Claude Code (Synth+The Bitter Lesson (Synthesis)][insight-montecarlo].md"
        )
        f.write_text("---\ntitle: mc\n---\n\nbody\n", encoding="utf-8")

        result = backfill_signals(FakeRAG(), None, insights_dir=insights, run_refute=False)
        assert result.backfilled == 1
        assert result.unresolved == []
        meta = parse_markdown_metadata(f.read_text(encoding="utf-8"))
        # Both sources resolved → bridging computed (FakeRAG embeds everything
        # identically → similarity 1.0 → bridging 0.0, but NOT via the
        # missing-sources fallback: unresolved is empty above).
        assert meta["signals"]["bridging"] is not None

    def test_bare_title_prefers_synthesis_among_siblings(self, tmp_path, monkeypatch):
        pages, insights = self._patch_dirs(tmp_path, monkeypatch)
        nested = pages / "Doc B"
        nested.mkdir()
        for suffix in ("(Part 1)", "(Part 2)", "(Synthesis)", "(Stitched)"):
            (nested / f"Doc B {suffix}.md").write_text("content", encoding="utf-8")
        (pages / "Doc C.md").write_text("content c", encoding="utf-8")

        f = insights / "[20260601-030303][Doc B+Doc C][insight-montecarlo].md"
        f.write_text("---\ntitle: t\n---\n\nbody\n", encoding="utf-8")

        result = backfill_signals(FakeRAG(), None, insights_dir=insights, run_refute=False)
        assert result.backfilled == 1
        assert result.unresolved == []  # "Doc B" → "Doc B (Synthesis)", "Doc C" exact

    def test_replay_order_follows_date_created(self, tmp_path, monkeypatch):
        """Novelty replay must run oldest-first by date_created, not filename
        order (legacy 🎐 files sort after mirror-named ones alphabetically)."""
        pages, insights = self._patch_dirs(tmp_path, monkeypatch)

        # Legacy file is OLDER but its name sorts after "[2026..." names.
        # Bodies differ (distinct content-hash ids) but FakeRAG embeds both
        # identically, so whichever runs second sees the first at sim 1.0.
        legacy = insights / "🎐insight-plan-20260526-171611.md"
        legacy.write_text(
            "---\ndate_created: '2026-05-26 17:16:11'\n---\n\nnovel stuff here\n",
            encoding="utf-8",
        )
        newer = insights / "[20260601-010101][Vault][full-insight].md"
        newer.write_text(
            "---\ndate_created: '2026-06-01 01:01:01'\n---\n\nnovel other things\n",
            encoding="utf-8",
        )

        backfill_signals(FakeRAG(), None, insights_dir=insights, run_refute=False)

        legacy_meta = parse_markdown_metadata(legacy.read_text(encoding="utf-8"))
        newer_meta = parse_markdown_metadata(newer.read_text(encoding="utf-8"))
        # Oldest goes first: nothing in history → novelty 1.0; the newer one
        # then sees it (identical body) → novelty ~0.
        assert legacy_meta["signals"]["novelty"] == 1.0
        assert newer_meta["signals"]["novelty"] < 0.01


class TestValidationReport:
    def _page(self, cortex_dir, claim):
        claim_id = make_claim_id(claim)
        page = CortexPage(
            claim_id=claim_id,
            path=cortex_dir / f"{claim[:20]}.md",
            claim=claim,
            last_reinforced_at="2026-06-11T03:00:00",
            created="2026-06-11T03:00:00",
            updated="2026-06-11T03:00:00",
        )
        save_cortex_page(page)
        return page

    def test_green_when_consistent(self, tmp_path):
        cortex = tmp_path / "Cortex"
        p1 = self._page(cortex, "Claim one stands alone fine.")
        p2 = self._page(cortex, "Claim two stands alone fine.")
        rag = FakeRAG(facet_titles=[p1.claim_id, p2.claim_id])

        report = run_validation(
            rag,
            cortex_dir=cortex,
            insights_dir=tmp_path / "Insights",
            state_file=tmp_path / "state.json",
            bench_history=tmp_path / "bench.json",
            report_dir=tmp_path / "out",
        )

        assert report.verdict == "GREEN"
        assert report.stats["pages_total"] == 2
        text = report.report_path.read_text(encoding="utf-8")
        assert "Claim one stands alone fine." in text  # human review list

    def test_red_on_missing_facet_and_unparseable(self, tmp_path):
        cortex = tmp_path / "Cortex"
        self._page(cortex, "A claim without its facet.")
        (cortex / "broken.md").write_text("no frontmatter", encoding="utf-8")

        report = run_validation(
            FakeRAG(facet_titles=[]),
            cortex_dir=cortex,
            insights_dir=tmp_path / "Insights",
            state_file=tmp_path / "state.json",
            bench_history=tmp_path / "bench.json",
            report_dir=tmp_path / "out",
        )

        assert report.verdict == "RED"
        assert any("缺 facet" in f for f in report.red_flags)
        assert any("無法解析" in f for f in report.red_flags)

    def test_yellow_on_negative_facet_lift(self, tmp_path):
        import json

        cortex = tmp_path / "Cortex"
        p = self._page(cortex, "Healthy page with facet present.")
        bench = tmp_path / "bench.json"
        bench.write_text(
            json.dumps(
                [
                    {"pass_rate": 0.8, "facet_lift": -1},
                ]
            ),
            encoding="utf-8",
        )

        report = run_validation(
            FakeRAG(facet_titles=[p.claim_id]),
            cortex_dir=cortex,
            insights_dir=tmp_path / "Insights",
            state_file=tmp_path / "state.json",
            bench_history=bench,
            report_dir=tmp_path / "out",
        )

        assert report.verdict == "YELLOW"
        assert any("Facet lift" in f for f in report.yellow_flags)


# ── Phase 2.5: gauge fixes + falsifiability backfill ──────────────────


def _signed_insight(insights_dir, name, *, groundedness, refute="survived"):
    insights_dir.mkdir(parents=True, exist_ok=True)
    g = "null" if groundedness is None else groundedness
    (insights_dir / name).write_text(
        f"---\nsignals:\n  groundedness: {g}\n  novelty: 0.5\n  bridging: 0.5\n"
        f"  refute_verdict: {refute}\nsignals_version: 1\n---\n\nbody\n",
        encoding="utf-8",
    )


class TestReportScores:
    def test_report_shows_scores_under_each_claim(self, tmp_path):
        cortex = tmp_path / "Cortex"
        claim = "Scored claim with falsifier."
        page = CortexPage(
            claim_id=make_claim_id(claim),
            path=cortex / "c.md",
            claim=claim,
            falsifiability=0.5,
            falsifier="EN text（中文輔助）",
            confidence=0.5,
            last_reinforced_at="2026-06-12T03:00:00",
            created="2026-06-12T03:00:00",
            updated="2026-06-12T03:00:00",
        )
        save_cortex_page(page)
        report = run_validation(
            FakeRAG(facet_titles=[page.claim_id]),
            cortex_dir=cortex,
            insights_dir=tmp_path / "Insights",
            state_file=tmp_path / "s.json",
            bench_history=tmp_path / "b.json",
            report_dir=tmp_path / "out",
        )
        text = report.report_path.read_text(encoding="utf-8")
        assert "證偽：EN text（中文輔助）" in text
        assert "falsifiability: 0.5 ｜ confidence: 0.5" in text


class TestGaugeFixes:
    def _run(self, tmp_path, rag=None):
        return run_validation(
            rag or FakeRAG(),
            cortex_dir=tmp_path / "Cortex",
            insights_dir=tmp_path / "Insights",
            state_file=tmp_path / "state.json",
            bench_history=tmp_path / "bench.json",
            report_dir=tmp_path / "out",
        )

    def test_broken_rate_scoped_to_gate_passers(self, tmp_path):
        insights = tmp_path / "Insights"
        # 過閘者：groundedness 0.9（健康）；被擋者：0.0 斷鏈與 refuted
        _signed_insight(insights, "good.md", groundedness=0.9)
        _signed_insight(insights, "planner.md", groundedness=0.0)
        _signed_insight(insights, "refuted.md", groundedness=0.9, refute="refuted")

        report = self._run(tmp_path)

        # 被閘門擋掉的 0.0 不得污染斷鏈率 → 無黃線、mean 取自過閘者
        assert report.stats["groundedness_mean"] == 0.9
        assert report.stats["broken_link_insight_rate"] == 0.0
        assert not any("斷鏈" in f for f in report.yellow_flags)

    def test_refute_coverage_exposed(self, tmp_path):
        insights = tmp_path / "Insights"
        _signed_insight(insights, "a.md", groundedness=0.9)
        _signed_insight(insights, "b.md", groundedness=0.9, refute="None")
        report = self._run(tmp_path)
        assert report.stats["refute_coverage"] == 0.5

    def test_falsifiability_distribution_and_yellow(self, tmp_path):
        cortex = tmp_path / "Cortex"
        cortex.mkdir()
        from services.cortex_store import CortexPage, make_claim_id, save_cortex_page

        for i, score in enumerate((0.0, 0.0, 1.0)):
            claim = f"Claim number {i} for the gauge."
            page = CortexPage(
                claim_id=make_claim_id(claim),
                path=cortex / f"c{i}.md",
                claim=claim,
                falsifiability=score,
                last_reinforced_at="2026-06-11T03:00:00",
                created="2026-06-11T03:00:00",
                updated="2026-06-11T03:00:00",
            )
            save_cortex_page(page)
        rag = FakeRAG(
            facet_titles=[make_claim_id(f"Claim number {i} for the gauge.") for i in range(3)]
        )

        report = self._run(tmp_path, rag=rag)

        assert report.stats["falsifiability_mean"] == 0.333
        assert report.stats["falsifiability_lt_0.3_rate"] == 0.667
        assert any("Falsifiability" in f for f in report.yellow_flags)


class TestFalsifiabilityBackfill:
    def test_backfills_without_touching_history(self, tmp_path):
        from maintenance.backfill_falsifiability import backfill_falsifiability
        from services.cortex_store import (
            CortexPage,
            load_all_pages,
            make_claim_id,
            save_cortex_page,
        )

        cortex = tmp_path / "Cortex"
        claim_old = "Old page lacking the fifth signal."
        old = CortexPage(
            claim_id=make_claim_id(claim_old),
            path=cortex / "old.md",
            claim=claim_old,
            confidence=0.7,
            S=3,
            last_reinforced_at="2026-06-10T03:00:00",
            created="2026-06-09T03:00:00",
            updated="2026-06-10T03:00:00",
        )
        save_cortex_page(old)
        claim_done = "Already measured page."
        done = CortexPage(
            claim_id=make_claim_id(claim_done),
            path=cortex / "done.md",
            claim=claim_done,
            falsifiability=0.9,
            falsifier="existing",
            last_reinforced_at="2026-06-10T03:00:00",
            created="2026-06-10T03:00:00",
            updated="2026-06-10T03:00:00",
        )
        save_cortex_page(done)

        class AssessLLM:
            def assess_falsifiability(self, claim):
                return {"score": 2.0, "falsifier": "  observable refutation  "}

        result = backfill_falsifiability(AssessLLM(), cortex_dir=cortex)

        assert result.backfilled == 1 and result.skipped == 1 and not result.failed
        pages = {p.claim_id: p for p in load_all_pages(cortex)}
        refreshed = pages[old.claim_id]
        assert refreshed.falsifiability == 1.0  # clamp 生效
        assert refreshed.falsifier == "observable refutation"
        # 測量不得改寫歷史
        assert refreshed.confidence == 0.7
        assert refreshed.S == 3
        assert refreshed.updated == "2026-06-10T03:00:00"
        assert pages[done.claim_id].falsifier == "existing"

    def test_failure_recorded_not_raised(self, tmp_path):
        from maintenance.backfill_falsifiability import backfill_falsifiability
        from services.cortex_store import CortexPage, make_claim_id, save_cortex_page

        cortex = tmp_path / "Cortex"
        claim = "Page whose assessment crashes."
        save_cortex_page(
            CortexPage(
                claim_id=make_claim_id(claim),
                path=cortex / "x.md",
                claim=claim,
                last_reinforced_at="t",
                created="t",
                updated="t",
            )
        )

        class CrashLLM:
            def assess_falsifiability(self, claim):
                raise RuntimeError("down")

        result = backfill_falsifiability(CrashLLM(), cortex_dir=cortex)
        assert result.backfilled == 0 and len(result.failed) == 1


class TestCortexAgent:
    def test_agent_triggers_validation_and_reports_verdict(self, tmp_path, monkeypatch):
        from agents.cortex_agent import CortexAgent
        import agents.cortex_agent as agent_mod

        class FakeReport:
            verdict = "GREEN"
            red_flags = []
            yellow_flags = []
            report_path = tmp_path / "[report] cortex validation x.md"

        calls = []
        monkeypatch.setattr(
            agent_mod, "run_validation", lambda rag: calls.append(rag) or FakeReport()
        )

        agent = CortexAgent.__new__(CortexAgent)
        agent.llm = None
        agent.rag = "the-rag"
        agent.stats = {"input_chars": 0, "output_chars": 0}

        message = agent.execute({})

        assert calls == ["the-rag"]
        assert "GREEN" in message and "cortex validation" in message

    def test_route_registered(self):
        from watchers.prompt_watcher import INTENT_ROUTES
        from agents.registry import AgentRegistry
        from unittest.mock import MagicMock

        assert any(key == "cortex" for _, _, key in INTENT_ROUTES)
        registry = AgentRegistry(MagicMock(), MagicMock())
        assert registry.get_agent("cortex") is not None
