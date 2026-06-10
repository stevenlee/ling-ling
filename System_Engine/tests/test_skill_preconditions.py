"""Skills declare `applicable_when` preconditions (database_populated,
min_documents, has_tag_graph); InsightAgent must refuse to run a skill
whose conditions the live vault doesn't meet, with a clear message."""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.absolute()))
os.environ.setdefault("LLM_PROVIDER", "vllm")

from agents.insight_agent import InsightAgent


class FakeRag:
    def __init__(self, chunks=0, tagged=False):
        self.chunks = chunks
        self.tagged = tagged

    def get_total_chunks_count(self):
        return self.chunks

    def has_tagged_documents(self, sample_limit=200):
        return self.tagged


def _agent(rag) -> InsightAgent:
    agent = InsightAgent.__new__(InsightAgent)
    agent.llm = None
    agent.rag = rag
    agent.stats = {"input_chars": 0, "output_chars": 0}
    agent.strategies = {}
    return agent


class TestSkillPreconditions:
    def test_no_conditions_always_runnable(self):
        assert _agent(FakeRag()). _check_skill_preconditions({}) == []
        assert _agent(None)._check_skill_preconditions({"min_documents": 5}) == []

    def test_database_populated_blocks_empty_vault(self):
        blockers = _agent(FakeRag(chunks=0))._check_skill_preconditions(
            {"database_populated": True}
        )
        assert len(blockers) == 1

    def test_min_documents_threshold(self):
        cond = {"database_populated": True, "min_documents": 20}
        assert _agent(FakeRag(chunks=5))._check_skill_preconditions(cond)
        assert _agent(FakeRag(chunks=25))._check_skill_preconditions(cond) == []

    def test_has_tag_graph(self):
        cond = {"has_tag_graph": True}
        assert _agent(FakeRag(chunks=10, tagged=False))._check_skill_preconditions(cond)
        assert _agent(FakeRag(chunks=10, tagged=True))._check_skill_preconditions(cond) == []

    def test_fail_open_on_rag_error(self):
        class BrokenRag:
            def get_total_chunks_count(self):
                raise RuntimeError("chroma down")

        blockers = _agent(BrokenRag())._check_skill_preconditions(
            {"database_populated": True}
        )
        assert blockers == []

    def test_generate_insight_skips_blocked_skill(self, monkeypatch):
        agent = _agent(FakeRag(chunks=3))
        agent.strategies = {
            "montecarlo": {
                "name": "Monte Carlo",
                "description": "x",
                "pipeline": "montecarlo",
                "applicable_when": {"database_populated": True, "min_documents": 20},
            }
        }
        ran = []
        monkeypatch.setattr(agent, "_run_montecarlo", lambda *a, **k: ran.append(1))
        result = agent.generate_insight("montecarlo")
        assert "前置條件未滿足" in result
        assert ran == []
