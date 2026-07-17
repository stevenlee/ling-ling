"""Cortex Phase 5 F1 (stage 1): grounded-insight injection + defenses 2/3.

Defense 2 (dialectical framing) and 3 (falsifiability gate) live in the
injection side; this pins them. The provenance firewall (defense 1/4) and
canary (5) land in stage 2 — the flag stays OFF until then.
"""

import os

os.environ.setdefault("LLM_PROVIDER", "vllm")

import pytest
from unittest.mock import MagicMock

from agents.insight_agent import InsightAgent
from services.cortex_store import CortexPage, make_claim_id, save_cortex_page


@pytest.fixture(autouse=True)
def _isolate_live_autotune(monkeypatch):
    """Grounding tests control the configured fraction directly.

    Never let a developer's real autotune_state.json override those values and
    make the suite depend on local runtime history.
    """
    monkeypatch.setattr("core.config.AUTOTUNE_ENABLED", False)


def _agent():
    a = InsightAgent.__new__(InsightAgent)
    a.rag = None
    return a


def _page(tmp_path, claim, falsifiability, **kw):
    cid = make_claim_id(claim)
    p = CortexPage(
        claim_id=cid, path=tmp_path / f"{cid}.md", claim=claim, falsifiability=falsifiability, **kw
    )
    save_cortex_page(p)
    return p


def test_should_ground_flag_off(monkeypatch):
    monkeypatch.setattr("core.config.CORTEX_GROUNDED_INSIGHT_ENABLED", False)
    assert _agent()._should_ground("any idea here") is False


def test_should_ground_fraction_bounds(monkeypatch):
    monkeypatch.setattr("core.config.CORTEX_GROUNDED_INSIGHT_ENABLED", True)
    monkeypatch.setattr("core.config.CORTEX_GROUND_FRACTION", 1.0)
    assert _agent()._should_ground("idea") is True  # all grounded
    monkeypatch.setattr("core.config.CORTEX_GROUND_FRACTION", 0.0)
    assert _agent()._should_ground("idea") is False  # all cold (canary control)


def test_cortex_priors_falsifiability_gate(tmp_path, monkeypatch):
    _page(tmp_path, "falsifiable causal claim", 0.8, falsifier="X without Y")
    _page(tmp_path, "unfalsifiable value statement", 0.0)  # below gate → excluded
    _page(tmp_path, "unscored aphorism", None)  # None → excluded
    monkeypatch.setattr("core.config.CORTEX_DIR", tmp_path)
    monkeypatch.setattr("core.config.CORTEX_GROUND_MIN_FALSIFIABILITY", 0.5)
    monkeypatch.setattr("core.config.CORTEX_GROUND_TOP_K", 5)

    claims = {p.claim for p in _agent()._cortex_priors("any topic")}
    assert "falsifiable causal claim" in claims
    assert "unfalsifiable value statement" not in claims  # echo-chamber fuel kept out
    assert "unscored aphorism" not in claims


class _FakeRAG:
    """ef → one-hot by keyword substring (deterministic)."""

    def __init__(self, vec_map, default=(0.01, 0.01, 0.01)):
        self.vec_map = vec_map
        self.default = list(default)

    def ef(self, texts):
        out = []
        for t in texts:
            v = next((list(vec) for key, vec in self.vec_map.items() if key in t), None)
            out.append(v if v is not None else list(self.default))
        return out


def test_cortex_priors_falsifiability_gate_holds_through_ranked_path(tmp_path, monkeypatch):
    """When >TOP_K falsifiable claims exist, selection goes through recall_claims
    (which ranks ALL active/dormant claims, not just falsifiable ones). The gate
    must still exclude unfalsifiable claims — previously the ranked path leaked
    them (2026-07-12 audit fix)."""
    _page(tmp_path, "HUB falsifiable one", 0.8, falsifier="a")
    _page(tmp_path, "HUB falsifiable two", 0.8, falsifier="b")
    _page(tmp_path, "ALPHA falsifiable three", 0.8, falsifier="c")
    _page(tmp_path, "HUB unfalsifiable value", 0.0)  # high-relevance but must stay out
    monkeypatch.setattr("core.config.CORTEX_DIR", tmp_path)
    monkeypatch.setattr("core.config.CORTEX_GROUND_MIN_FALSIFIABILITY", 0.5)
    monkeypatch.setattr("core.config.CORTEX_GROUND_TOP_K", 2)

    a = InsightAgent.__new__(InsightAgent)
    a.rag = _FakeRAG({"HUB": (1.0, 0.0, 0.0), "ALPHA": (0.0, 1.0, 0.0)})
    claims = {p.claim for p in a._cortex_priors("HUB topic")}
    assert "HUB unfalsifiable value" not in claims  # gate holds through ranked path
    assert len(claims) == 2


def test_cortex_priors_mmr_diversifies_over_hub(tmp_path, monkeypatch):
    """Two near-duplicate hubs + one distinct claim, TOP_K=2. Pure relevance
    returns both hubs; MMR must swap the 2nd hub for the distinct claim."""
    _page(tmp_path, "HUB falsifiable one", 0.8, falsifier="a")
    _page(tmp_path, "HUB falsifiable two", 0.8, falsifier="b")
    _page(tmp_path, "ALPHA distinct claim", 0.8, falsifier="c")
    monkeypatch.setattr("core.config.CORTEX_DIR", tmp_path)
    monkeypatch.setattr("core.config.CORTEX_GROUND_MIN_FALSIFIABILITY", 0.5)
    monkeypatch.setattr("core.config.CORTEX_GROUND_TOP_K", 2)
    monkeypatch.setattr("core.config.CORTEX_GROUND_MMR_LAMBDA", 0.5)

    a = InsightAgent.__new__(InsightAgent)
    # Query is equally relevant to the hub axis and the alpha axis; the two hubs
    # are mutually identical, alpha is orthogonal to the hub. Pure top-k would
    # keep both hubs (equal relevance); MMR must swap the redundant 2nd hub for
    # alpha, whose similarity to the selected hub is 0.
    a.rag = _FakeRAG(
        {
            "QUERY": (0.7, 0.7, 0.0),
            "HUB": (1.0, 0.0, 0.0),
            "ALPHA": (0.0, 1.0, 0.0),
        }
    )
    claims = {p.claim for p in a._cortex_priors("QUERY topic")}
    assert "ALPHA distinct claim" in claims  # diversity broke the hub cluster
    assert not ({"HUB falsifiable one", "HUB falsifiable two"} <= claims)


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
    agent._load_prompt = lambda name, **kw: ""
    agent.llm = type(
        "L",
        (),
        {
            "answer_query": lambda self, **kw: (
                captured.setdefault("prompt", kw["custom_instruction"]) or "expansion"
            )
        },
    )()

    out = agent._expand_seed({"idea": "some idea", "source_a": "A", "source_b": "B"}, {})
    assert out["grounded_on"] == []  # nothing grounded
    assert "你對相關主題已有的信念" not in captured["prompt"]  # no prior block injected


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
    agent._load_prompt = lambda name, **kw: ""
    agent.llm = type(
        "L",
        (),
        {
            "answer_query": lambda self, **kw: (
                captured.setdefault("prompt", kw["custom_instruction"]) or "expansion"
            )
        },
    )()

    out = agent._expand_seed({"idea": "grounded idea", "source_a": "A", "source_b": "B"}, {})
    assert out["grounded_on"]  # provenance recorded
    assert "falsifiable prior claim" in captured["prompt"]  # prior injected
    assert "挑戰" in captured["prompt"]  # dialectically


# ── Stage 2: provenance firewall in consolidation (defenses 1+4) ────────


def _full_page(tmp_path, claim, **kw):
    cid = make_claim_id(claim)
    p = CortexPage(claim_id=cid, path=tmp_path / f"{cid}.md", claim=claim, **kw)
    save_cortex_page(p)
    return p


def _worker(tmp_path):
    from maintenance.cortex_consolidation import _Consolidator

    w = _Consolidator(
        MagicMock(),
        MagicMock(),
        cortex_dir=tmp_path,
        state={"claim_embeddings": {}},
        adjudication_cache={},
        max_adjudications=0,
        top_k=3,
        link_threshold=0.6,
        merge_threshold=0.8,
        max_variants=5,
    )
    w._index_page = lambda p: None  # isolate the firewall from RAG indexing
    return w


def test_firewall_skips_reinforcement_on_self_agreement(tmp_path):
    page = _full_page(tmp_path, "X causes Y", confidence=0.5, S=1.0, falsifiability=0.8)
    w = _worker(tmp_path)
    w.pages = [page]
    w.by_claim_id = {page.claim_id: page}
    before = page.confidence
    ev = {
        "insight": "grounded.md",
        "sources": [],
        "date": "2026-06-14",
        "summary": "s",
        "grounded_on": [page.claim_id],
    }

    # A grounded insight "agrees" with the very claim it was grounded on.
    w._merge_into(page, "X causes Y (restated)", ev, grounded_on=[page.claim_id])

    assert w.firewalled == 1
    assert page.confidence == before  # NO reinforcement (circular)
    assert any(e.get("insight") == "grounded.md" for e in page.evidence)  # link still recorded


def test_normal_merge_still_reinforces(tmp_path):
    page = _full_page(tmp_path, "X causes Y", confidence=0.5, S=1.0, falsifiability=0.8)
    w = _worker(tmp_path)
    w.pages = [page]
    w.by_claim_id = {page.claim_id: page}
    before = page.confidence
    ev = {"insight": "external.md", "sources": [], "date": "2026-06-14", "summary": "s"}

    # External (non-grounded) evidence: reinforce as usual.
    w._merge_into(page, "X causes Y (restated)", ev, grounded_on=[])

    assert w.firewalled == 0
    assert page.confidence > before  # external evidence DOES reinforce


def test_firewall_only_fires_for_the_grounded_claim(tmp_path):
    # Grounded on claim B, but merging into claim A → A is NOT self-agreement,
    # so A still reinforces (the firewall is per-claim, not blanket).
    page_a = _full_page(tmp_path, "claim A", confidence=0.5, falsifiability=0.8)
    w = _worker(tmp_path)
    w.pages = [page_a]
    w.by_claim_id = {page_a.claim_id: page_a}
    before = page_a.confidence
    ev = {
        "insight": "g.md",
        "sources": [],
        "date": "2026-06-14",
        "summary": "s",
        "grounded_on": ["cortex-someotherclaim"],
    }
    w._merge_into(page_a, "claim A restated", ev, grounded_on=["cortex-someotherclaim"])
    assert w.firewalled == 0
    assert page_a.confidence > before
