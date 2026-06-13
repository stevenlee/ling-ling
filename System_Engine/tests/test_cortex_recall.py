"""Cortex Phase 5 F2: recall_claims primitive + RecallAgent rendering."""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.absolute()))
os.environ.setdefault("LLM_PROVIDER", "vllm")

from services.cortex_recall import recall_claims
from services.cortex_store import CortexPage, make_claim_id, save_cortex_page


class FakeRAG:
    """ef maps text → a deterministic one-hot vector by keyword substring."""
    def __init__(self, vec_map, default=(0.0, 0.0, 0.0)):
        self.vec_map = vec_map
        self.default = list(default)

    def ef(self, texts):
        out = []
        for t in texts:
            v = None
            for key, vec in self.vec_map.items():
                if key in t:
                    v = list(vec)
                    break
            out.append(v if v is not None else list(self.default))
        return out


def _page(cortex_dir, claim, *, status="active", confidence=0.5, falsifiability=0.5,
          falsifier="", contradictions=None):
    cid = make_claim_id(claim)
    p = CortexPage(
        claim_id=cid, path=cortex_dir / f"{cid}.md", claim=claim, status=status,
        confidence=confidence, falsifiability=falsifiability, falsifier=falsifier,
        contradictions=contradictions or [],
    )
    save_cortex_page(p)
    return p


def test_ranks_by_relevance(tmp_path):
    _page(tmp_path, "cats are liquid")
    _page(tmp_path, "dogs bark loudly")
    rag = FakeRAG({
        "cats": (1.0, 0.0, 0.0),
        "dogs": (0.0, 1.0, 0.0),
    })
    hits = recall_claims(rag, "cats", cortex_dir=tmp_path, top_k=8)
    assert len(hits) == 2
    assert "cats" in hits[0][1].claim          # most relevant first
    assert hits[0][0] > hits[1][0]             # strictly higher score


def test_hybrid_surfaces_literal_match_over_higher_cosine(tmp_path):
    # The decisive case: a claim with strong lexical overlap but the embedder
    # ranks it LOWER must still win under hybrid. Pure vector buries the literal
    # match (the live "知識圖譜" rank-9 bug); magnitude-aware fusion surfaces it.
    _page(tmp_path, "構建知識圖譜方法")         # lexical overlap, but lower cosine
    _page(tmp_path, "天氣晴朗")                # no overlap, but higher cosine
    # Filler pages so BM25 IDF isn't degenerate (a term in 1 of N=2 docs has
    # IDF=0; with N~6 the discriminating chars get positive IDF).
    for filler in ("貓咪睡覺", "汽車引擎", "海洋潮汐", "鋼琴演奏"):
        _page(tmp_path, filler)
    rag = FakeRAG({
        "查詢": (1.0, 0.0, 0.0),               # query
        "方法": (0.6, 0.8, 0.0),               # overlap claim — cosine 0.6 (buried)
        "天氣": (0.99, 0.14, 0.0),             # unrelated claim — cosine ~0.99
    })  # fillers fall to the default (0,0,0) → cosine 0, no interference
    hits = recall_claims(rag, "構建知識圖譜查詢", cortex_dir=tmp_path, hybrid=True)
    assert "知識圖譜" in hits[0][1].claim          # literal match wins under hybrid

    vec_only = recall_claims(rag, "構建知識圖譜查詢", cortex_dir=tmp_path, hybrid=False)
    assert "天氣" in vec_only[0][1].claim          # ...but loses on cosine alone


def test_top_k_caps(tmp_path):
    for i in range(5):
        _page(tmp_path, f"claim about topic {i}")
    rag = FakeRAG({"topic": (1.0, 0.0, 0.0)})
    hits = recall_claims(rag, "topic", cortex_dir=tmp_path, top_k=3)
    assert len(hits) == 3


def test_falsified_excluded_by_default_but_available(tmp_path):
    _page(tmp_path, "active belief about X")
    _page(tmp_path, "disproven belief about X", status="falsified")
    rag = FakeRAG({"belief": (1.0, 0.0, 0.0)})
    default = recall_claims(rag, "belief", cortex_dir=tmp_path)
    assert all(p.status != "falsified" for _, p in default)
    everything = recall_claims(rag, "belief", cortex_dir=tmp_path, statuses=None)
    assert any(p.status == "falsified" for _, p in everything)


def test_empty_query_and_no_pages_are_fail_open(tmp_path):
    rag = FakeRAG({})
    assert recall_claims(rag, "", cortex_dir=tmp_path) == []
    assert recall_claims(rag, "anything", cortex_dir=tmp_path) == []   # empty dir


def test_embedding_failure_is_fail_open(tmp_path):
    _page(tmp_path, "some claim")

    class Boom:
        def ef(self, texts):
            raise RuntimeError("embedder down")

    assert recall_claims(Boom(), "claim", cortex_dir=tmp_path) == []


# ── RecallAgent rendering ──────────────────────────────────────────────

def test_agent_render_surfaces_epistemics(tmp_path):
    from agents.recall_agent import RecallAgent
    agent = RecallAgent.__new__(RecallAgent)
    page = CortexPage(
        claim_id="cortex-aaa", path=tmp_path / "a.md", claim="X causes Y",
        status="active", confidence=0.7, falsifiability=0.8,
        falsifier="An experiment where X occurs but Y does not.",
        contradictions=["cortex-bbb"],
        evidence=[{"insight": "20260613-insight.md", "summary": "s"}],
    )
    body = agent._render("why X", [(0.91, page)], {"cortex-bbb": "Y happens without X"})
    assert "X causes Y" in body
    assert "0.91" in body                                  # relevance score
    assert "反例" in body and "Y does not" in body          # falsifier surfaced
    assert "⚔️" in body and "Y happens without X" in body   # contradiction resolved
    assert "[[20260613-insight]]" in body                  # evidence wikilink


def test_agent_render_empty_hits(tmp_path):
    from agents.recall_agent import RecallAgent
    agent = RecallAgent.__new__(RecallAgent)
    body = agent._render("obscure topic", [], {})
    assert "沒有與此主題相關的主張" in body
