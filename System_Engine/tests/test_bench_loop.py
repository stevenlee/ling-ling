"""Self-improving bench loop: auto-grown cases (quality-gated), facet A/B
lift measurement, history tracking, and regression alerts."""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.absolute()))

import yaml

from maintenance.bench_builder import build_bench_cases
from maintenance.retrieval_bench import load_bench_cases, run_retrieval_bench


def _hit(title):
    return {"id": f"{title}_1", "metadata": {"title": title, "source": f"{title}.md"}}


class FakeRag:
    """query_notes keyed by exact query string; use_facets=False can degrade."""

    def __init__(self, answers=None, facet_entries=None, off_answers=None):
        self.answers = answers or {}
        self.off_answers = off_answers
        self.facets = facet_entries or []

    def query_notes(self, query, **kwargs):
        if kwargs.get("use_facets") is False and self.off_answers is not None:
            return self.off_answers.get(query, [])
        return self.answers.get(query, [])

    def get_facet_entries(self):
        return self.facets


class FakeLLM:
    def __init__(self, question_map=None):
        self.question_map = question_map or {}

    def generate_bench_question(self, title, thesis):
        return self.question_map.get(title, f"What does {thesis} mean?")


class TestBenchBuilder:
    def test_gate_accepts_only_currently_answerable(self, tmp_path):
        bench = tmp_path / "bench.yml"
        auto = tmp_path / "auto.yml"
        rag = FakeRag(
            answers={"Q-good": [_hit("Good Doc")]},   # Q-bad returns nothing
            facet_entries=[
                {"title": "Good Doc", "text": "good thesis", "facet_index": 0, "timestamp": "2"},
                {"title": "Bad Doc", "text": "bad thesis", "facet_index": 0, "timestamp": "1"},
            ],
        )
        llm = FakeLLM({"Good Doc": "Q-good", "Bad Doc": "Q-bad"})

        result = build_bench_cases(rag, llm, bench_path=bench, auto_path=auto,
                                   max_total=30, per_run=5)

        assert result.status == "succeeded"
        assert len(result.added) == 1 and result.rejected == 1
        cases = load_bench_cases(auto)
        assert cases[0]["query"] == "Q-good"
        assert cases[0]["expected_top_k"] == ["Good Doc"]
        assert cases[0]["auto_generated"] is True

    def test_covered_titles_skipped(self, tmp_path):
        bench = tmp_path / "bench.yml"
        bench.write_text(
            yaml.safe_dump({"queries": [
                {"query": "manual", "expected_top_k": ["Good Doc"]},
            ]}), encoding="utf-8")
        auto = tmp_path / "auto.yml"
        rag = FakeRag(
            answers={"Q-good": [_hit("Good Doc")]},
            facet_entries=[{"title": "Good Doc", "text": "t", "facet_index": 0, "timestamp": "1"}],
        )
        result = build_bench_cases(rag, FakeLLM({"Good Doc": "Q-good"}),
                                   bench_path=bench, auto_path=auto, max_total=30, per_run=5)
        assert result.status == "skipped"
        assert not auto.exists()

    def test_cap_respected(self, tmp_path):
        auto = tmp_path / "auto.yml"
        existing = [{"query": f"q{i}", "expected_top_k": [f"D{i}"], "auto_generated": True}
                    for i in range(3)]
        auto.write_text(yaml.safe_dump({"queries": existing}), encoding="utf-8")
        rag = FakeRag(facet_entries=[{"title": "New", "text": "t", "facet_index": 0, "timestamp": "1"}])

        result = build_bench_cases(rag, FakeLLM(), bench_path=tmp_path / "b.yml",
                                   auto_path=auto, max_total=3, per_run=5)
        assert result.status == "skipped"
        assert "cap" in result.message


class TestBenchABAndHistory:
    def _bench_file(self, tmp_path, queries):
        bench = tmp_path / "bench.yml"
        bench.write_text(yaml.safe_dump({"queries": queries}), encoding="utf-8")
        return bench

    def test_facet_lift_measured(self, tmp_path):
        bench = self._bench_file(tmp_path, [
            {"query": "q1", "expected_top_k": ["Doc A"]},
            {"query": "q2", "expected_top_k": ["Doc B"]},
        ])
        # Facets on: both pass. Facets off: only q1 passes → lift +1.
        rag = FakeRag(
            answers={"q1": [_hit("Doc A")], "q2": [_hit("Doc B")]},
            off_answers={"q1": [_hit("Doc A")], "q2": []},
        )
        result = run_retrieval_bench(
            rag, bench_path=bench, auto_path=None,
            log_path=tmp_path / "log.md", min_pass_rate=0.5, ab_facets=True,
        )
        assert result.passed == 2
        assert result.facet_off_passed == 1
        assert result.facet_lift == 1
        assert "facet lift +1" in result.message

    def test_history_appended_and_regression_alerts(self, tmp_path):
        bench = self._bench_file(tmp_path, [
            {"query": "q1", "expected_top_k": ["Doc A"]},
            {"query": "q2", "expected_top_k": ["Doc B"]},
        ])
        history = tmp_path / "history.json"
        report_dir = tmp_path / "fromLingLing"

        # Run 1: all pass.
        rag = FakeRag(answers={"q1": [_hit("Doc A")], "q2": [_hit("Doc B")]})
        r1 = run_retrieval_bench(
            rag, bench_path=bench, auto_path=None, log_path=tmp_path / "log.md",
            min_pass_rate=0.5, history_path=history, report_dir=report_dir,
        )
        assert not r1.regression
        assert len(json.loads(history.read_text())) == 1

        # Run 2: q2 broke → regression alert.
        rag2 = FakeRag(answers={"q1": [_hit("Doc A")], "q2": []})
        r2 = run_retrieval_bench(
            rag2, bench_path=bench, auto_path=None, log_path=tmp_path / "log.md",
            min_pass_rate=0.5, history_path=history, report_dir=report_dir,
        )
        assert r2.regression
        assert r2.status in ("regressed", "failed")
        assert r2.alert_path is not None and r2.alert_path.exists()
        alert = r2.alert_path.read_text(encoding="utf-8")
        assert "q2" in alert
        assert len(json.loads(history.read_text())) == 2

    def test_auto_cases_merged_into_run(self, tmp_path):
        bench = self._bench_file(tmp_path, [{"query": "q1", "expected_top_k": ["Doc A"]}])
        auto = tmp_path / "auto.yml"
        auto.write_text(yaml.safe_dump({"queries": [
            {"query": "q-auto", "expected_top_k": ["Doc B"], "auto_generated": True},
        ]}), encoding="utf-8")
        rag = FakeRag(answers={"q1": [_hit("Doc A")], "q-auto": [_hit("Doc B")]})

        result = run_retrieval_bench(
            rag, bench_path=bench, auto_path=auto,
            log_path=tmp_path / "log.md", min_pass_rate=1.0,
        )
        assert result.total == 2 and result.passed == 2


class TestUseFacetsSwitch:
    def test_use_facets_false_drops_facets_instead_of_dereferencing(self, tmp_path):
        from unittest.mock import MagicMock
        from services.rag_manager import RAGManager

        rag = RAGManager.__new__(RAGManager)
        rag._bm25 = MagicMock()
        facet = {"id": "f1", "text": "thesis",
                 "metadata": {"role": "facet", "doc_id": "x"}, "distance": 0.1}
        chunk = {"id": "c1", "text": "body", "metadata": {}, "distance": 0.2}

        # The post-filter path (use_facets=False) must not need parent lookups.
        filtered = [c for c in [facet, chunk] if (c.get("metadata") or {}).get("role") != "facet"]
        assert [c["id"] for c in filtered] == ["c1"]
