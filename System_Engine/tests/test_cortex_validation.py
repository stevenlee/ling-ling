"""Validation harness tools: signals backfill + the three-tier report."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.absolute()))

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
            f"---\ntitle: old insight\n---\n\n{body}", encoding="utf-8",
        )
        # Doc-anchored insight: source content exists → refute runs.
        (insights / "[20260601-020202][Doc A][insight-recency].md").write_text(
            f"---\ntitle: doc insight\n---\n\n{body}", encoding="utf-8",
        )
        (insights / "[20260602-010101][Vault][full-insight].md").write_text(
            "---\nsignals:\n  groundedness: 1.0\n---\n\nalready signed\n", encoding="utf-8",
        )

        result = backfill_signals(FakeRAG(), FakeLLM(), insights_dir=insights)

        assert result.backfilled == 2 and result.skipped_signed == 1
        vault_meta = parse_markdown_metadata(
            (insights / "[20260601-010101][Vault][full-insight].md").read_text(encoding="utf-8"))
        assert vault_meta["signals"]["refute_verdict"] is None     # no sources → skip
        assert vault_meta["signals_backfilled"] is True

        doc_text = (insights / "[20260601-020202][Doc A][insight-recency].md").read_text(encoding="utf-8")
        doc_meta = parse_markdown_metadata(doc_text)
        assert doc_meta["signals"]["refute_verdict"] == "survived"  # sources loaded from filename
        assert body.strip() in doc_text                             # body preserved


class TestValidationReport:
    def _page(self, cortex_dir, claim):
        claim_id = make_claim_id(claim)
        page = CortexPage(
            claim_id=claim_id, path=cortex_dir / f"{claim[:20]}.md", claim=claim,
            last_reinforced_at="2026-06-11T03:00:00",
            created="2026-06-11T03:00:00", updated="2026-06-11T03:00:00",
        )
        save_cortex_page(page)
        return page

    def test_green_when_consistent(self, tmp_path):
        cortex = tmp_path / "Cortex"
        p1 = self._page(cortex, "Claim one stands alone fine.")
        p2 = self._page(cortex, "Claim two stands alone fine.")
        rag = FakeRAG(facet_titles=[p1.claim_id, p2.claim_id])

        report = run_validation(
            rag, cortex_dir=cortex, insights_dir=tmp_path / "Insights",
            state_file=tmp_path / "state.json", bench_history=tmp_path / "bench.json",
            report_dir=tmp_path / "out",
        )

        assert report.verdict == "GREEN"
        assert report.stats["pages_total"] == 2
        text = report.report_path.read_text(encoding="utf-8")
        assert "Claim one stands alone fine." in text     # human review list

    def test_red_on_missing_facet_and_unparseable(self, tmp_path):
        cortex = tmp_path / "Cortex"
        self._page(cortex, "A claim without its facet.")
        (cortex / "broken.md").write_text("no frontmatter", encoding="utf-8")

        report = run_validation(
            FakeRAG(facet_titles=[]), cortex_dir=cortex,
            insights_dir=tmp_path / "Insights", state_file=tmp_path / "state.json",
            bench_history=tmp_path / "bench.json", report_dir=tmp_path / "out",
        )

        assert report.verdict == "RED"
        assert any("缺 facet" in f for f in report.red_flags)
        assert any("無法解析" in f for f in report.red_flags)

    def test_yellow_on_negative_facet_lift(self, tmp_path):
        import json
        cortex = tmp_path / "Cortex"
        p = self._page(cortex, "Healthy page with facet present.")
        bench = tmp_path / "bench.json"
        bench.write_text(json.dumps([
            {"pass_rate": 0.8, "facet_lift": -1},
        ]), encoding="utf-8")

        report = run_validation(
            FakeRAG(facet_titles=[p.claim_id]), cortex_dir=cortex,
            insights_dir=tmp_path / "Insights", state_file=tmp_path / "state.json",
            bench_history=bench, report_dir=tmp_path / "out",
        )

        assert report.verdict == "YELLOW"
        assert any("Facet lift" in f for f in report.yellow_flags)
