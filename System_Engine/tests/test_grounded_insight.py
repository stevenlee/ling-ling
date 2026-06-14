"""Cortex Phase 5 F1 (stage 1): grounded-insight injection + defenses 2/3.

Defense 2 (dialectical framing) and 3 (falsifiability gate) live in the
injection side; this pins them. The provenance firewall (defense 1/4) and
canary (5) land in stage 2 — the flag stays OFF until then.
"""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.absolute()))
os.environ.setdefault("LLM_PROVIDER", "vllm")

from agents.insight_agent import InsightAgent
from services.cortex_store import CortexPage, make_claim_id, save_cortex_page


def _agent():
    a = InsightAgent.__new__(InsightAgent)
    a.rag = None
    return a


def _page(tmp_path, claim, falsifiability, **kw):
    cid = make_claim_id(claim)
    p = CortexPage(claim_id=cid, path=tmp_path / f"{cid}.md", claim=claim,
                   falsifiability=falsifiability, **kw)
    save_cortex_page(p)
    return p


def test_should_ground_flag_off(monkeypatch):
    monkeypatch.setattr("core.config.CORTEX_GROUNDED_INSIGHT_ENABLED", False)
    assert _agent()._should_ground("any idea here") is False


def test_should_ground_fraction_bounds(monkeypatch):
    monkeypatch.setattr("core.config.CORTEX_GROUNDED_INSIGHT_ENABLED", True)
    monkeypatch.setattr("core.config.CORTEX_GROUND_FRACTION", 1.0)
    assert _agent()._should_ground("idea") is True       # all grounded
    monkeypatch.setattr("core.config.CORTEX_GROUND_FRACTION", 0.0)
    assert _agent()._should_ground("idea") is False      # all cold (canary control)


def test_cortex_priors_falsifiability_gate(tmp_path, monkeypatch):
    _page(tmp_path, "falsifiable causal claim", 0.8, falsifier="X without Y")
    _page(tmp_path, "unfalsifiable value statement", 0.0)   # below gate → excluded
    _page(tmp_path, "unscored aphorism", None)              # None → excluded
    monkeypatch.setattr("core.config.CORTEX_DIR", tmp_path)
    monkeypatch.setattr("core.config.CORTEX_GROUND_MIN_FALSIFIABILITY", 0.5)
    monkeypatch.setattr("core.config.CORTEX_GROUND_TOP_K", 5)

    claims = {p.claim for p in _agent()._cortex_priors("any topic")}
    assert "falsifiable causal claim" in claims
    assert "unfalsifiable value statement" not in claims    # echo-chamber fuel kept out
    assert "unscored aphorism" not in claims


def test_grounding_block_is_dialectical(tmp_path):
    p = _page(tmp_path, "X causes Y", 0.8, falsifier="X without Y")
    block = _agent()._grounding_block([p])
    assert "X causes Y" in block and "X without Y" in block
    # Frames the priors as something to challenge, not a template to confirm.
    assert "挑戰" in block and "推翻" in block and "張力" in block


def test_expand_seed_flag_off_is_inert(tmp_path, monkeypatch):
    monkeypatch.setattr("core.config.CORTEX_GROUNDED_INSIGHT_ENABLED", False)
    captured = {}

    class Rag:
        def query_similar_notes(self, idea, top_k=5):
            return []

    agent = _agent()
    agent.rag = Rag()
    agent._load_prompt = lambda name: ""
    agent.llm = type("L", (), {"answer_query": lambda self, **kw: captured.setdefault("prompt", kw["custom_instruction"]) or "expansion"})()

    out = agent._expand_seed({"idea": "some idea", "source_a": "A", "source_b": "B"}, {})
    assert out["grounded_on"] == []                          # nothing grounded
    assert "你對相關主題已有的信念" not in captured["prompt"]   # no prior block injected


def test_expand_seed_grounded_injects_and_marks(tmp_path, monkeypatch):
    _page(tmp_path, "falsifiable prior claim", 0.8, falsifier="counterexample Z")
    monkeypatch.setattr("core.config.CORTEX_GROUNDED_INSIGHT_ENABLED", True)
    monkeypatch.setattr("core.config.CORTEX_GROUND_FRACTION", 1.0)
    monkeypatch.setattr("core.config.CORTEX_DIR", tmp_path)
    monkeypatch.setattr("core.config.CORTEX_GROUND_MIN_FALSIFIABILITY", 0.5)
    monkeypatch.setattr("core.config.CORTEX_GROUND_TOP_K", 3)
    captured = {}

    class Rag:
        def query_similar_notes(self, idea, top_k=5):
            return []

    agent = _agent()
    agent.rag = Rag()
    agent._load_prompt = lambda name: ""
    agent.llm = type("L", (), {"answer_query": lambda self, **kw: captured.setdefault("prompt", kw["custom_instruction"]) or "expansion"})()

    out = agent._expand_seed({"idea": "grounded idea", "source_a": "A", "source_b": "B"}, {})
    assert out["grounded_on"]                                  # provenance recorded
    assert "falsifiable prior claim" in captured["prompt"]     # prior injected
    assert "挑戰" in captured["prompt"]                         # dialectically
