"""Cortex Phase 5 F2: recall_claims primitive + RecallAgent rendering."""

import os

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


def _page(
    cortex_dir,
    claim,
    *,
    status="active",
    confidence=0.5,
    falsifiability=0.5,
    falsifier="",
    contradictions=None,
):
    cid = make_claim_id(claim)
    p = CortexPage(
        claim_id=cid,
        path=cortex_dir / f"{cid}.md",
        claim=claim,
        status=status,
        confidence=confidence,
        falsifiability=falsifiability,
        falsifier=falsifier,
        contradictions=contradictions or [],
    )
    save_cortex_page(p)
    return p


def test_ranks_by_relevance(tmp_path):
    _page(tmp_path, "cats are liquid")
    _page(tmp_path, "dogs bark loudly")
    rag = FakeRAG(
        {
            "cats": (1.0, 0.0, 0.0),
            "dogs": (0.0, 1.0, 0.0),
        }
    )
    hits = recall_claims(rag, "cats", cortex_dir=tmp_path, top_k=8)
    assert len(hits) == 2
    assert "cats" in hits[0][1].claim  # most relevant first
    assert hits[0][0] > hits[1][0]  # strictly higher score


def test_hybrid_surfaces_literal_match_over_higher_cosine(tmp_path):
    # The decisive case: a claim with strong lexical overlap but the embedder
    # ranks it LOWER must still win under hybrid. Pure vector buries the literal
    # match (the live "知識圖譜" rank-9 bug); magnitude-aware fusion surfaces it.
    _page(tmp_path, "構建知識圖譜方法")  # lexical overlap, but lower cosine
    _page(tmp_path, "天氣晴朗")  # no overlap, but higher cosine
    # Filler pages so BM25 IDF isn't degenerate (a term in 1 of N=2 docs has
    # IDF=0; with N~6 the discriminating chars get positive IDF).
    for filler in ("貓咪睡覺", "汽車引擎", "海洋潮汐", "鋼琴演奏"):
        _page(tmp_path, filler)
    rag = FakeRAG(
        {
            "查詢": (1.0, 0.0, 0.0),  # query
            "方法": (0.6, 0.8, 0.0),  # overlap claim — cosine 0.6 (buried)
            "天氣": (0.99, 0.14, 0.0),  # unrelated claim — cosine ~0.99
        }
    )  # fillers fall to the default (0,0,0) → cosine 0, no interference
    hits = recall_claims(rag, "構建知識圖譜查詢", cortex_dir=tmp_path, hybrid=True)
    assert "知識圖譜" in hits[0][1].claim  # literal match wins under hybrid

    vec_only = recall_claims(rag, "構建知識圖譜查詢", cortex_dir=tmp_path, hybrid=False)
    assert "天氣" in vec_only[0][1].claim  # ...but loses on cosine alone


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
    assert recall_claims(rag, "anything", cortex_dir=tmp_path) == []  # empty dir


def test_embedding_failure_is_fail_open(tmp_path):
    _page(tmp_path, "some claim")

    class Boom:
        def ef(self, texts):
            raise RuntimeError("embedder down")

    assert recall_claims(Boom(), "claim", cortex_dir=tmp_path) == []


# ── RecallAgent: LLM-over-corpus flow + rendering ──────────────────────


def _agent_page(tmp_path, claim, **kw):
    return CortexPage(claim_id=make_claim_id(claim), path=tmp_path / "x.md", claim=claim, **kw)


def test_claims_block_numbers_with_epistemics(tmp_path):
    from agents.recall_agent import RecallAgent

    numbered = [
        (
            1,
            _agent_page(
                tmp_path, "X causes Y", confidence=0.7, falsifiability=0.8, falsifier="X without Y"
            ),
        ),
        (2, _agent_page(tmp_path, "A relates to B", confidence=0.5, falsifiability=0.5)),
    ]
    block = RecallAgent._claims_block(numbered)
    assert "[#1] X causes Y" in block
    assert "信心 0.70" in block and "可反駁性 0.80" in block
    assert "反例：X without Y" in block
    assert "[#2] A relates to B" in block


def test_render_appends_only_cited_claims(tmp_path):
    from agents.recall_agent import RecallAgent

    agent = RecallAgent.__new__(RecallAgent)
    numbered = [
        (1, _agent_page(tmp_path, "irrelevant claim")),
        (
            2,
            _agent_page(
                tmp_path,
                "X causes Y",
                confidence=0.7,
                falsifiability=0.8,
                falsifier="X without Y",
                evidence=[{"insight": "20260613-insight.md"}],
            ),
        ),
    ]
    answer = "系統相信 X 會導致 Y [#2]，但這是有條件的。"
    body = agent._render("why X", answer, numbered)
    assert answer.strip() in body  # LLM answer is the body
    assert "[#2]" in body and "X causes Y" in body  # cited claim in appendix
    assert "反例：X without Y" in body  # epistemics surfaced
    assert "[[20260613-insight]]" in body  # evidence wikilink
    assert "irrelevant claim" not in body  # uncited claim NOT appended


def test_render_no_citations_no_appendix(tmp_path):
    from agents.recall_agent import RecallAgent

    agent = RecallAgent.__new__(RecallAgent)
    numbered = [(1, _agent_page(tmp_path, "some claim"))]
    body = agent._render("obscure", "Cortex 中沒有與此主題相關的信念。", numbered)
    assert "沒有與此主題相關" in body
    assert "引用的主張" not in body  # no appendix when nothing cited


def test_execute_small_corpus_feeds_all_claims(tmp_path, monkeypatch):
    """At small scale, the LLM sees every claim (no retrieval pre-filter) — so a
    typo'd or paraphrased query still finds the right claim via the LLM."""
    import agents.recall_agent as ra

    pages = [_agent_page(tmp_path, f"claim number {i}") for i in range(5)]
    monkeypatch.setattr(ra, "load_all_pages", lambda d: pages)

    captured = {}

    class FakeLLM:
        def complete(self, system_prompt, user_msg, **kw):
            captured["system_prompt"] = system_prompt
            captured["user_msg"] = user_msg
            captured["kw"] = kw
            return "synthesis [#1]"

    agent = ra.RecallAgent.__new__(ra.RecallAgent)
    agent.llm = FakeLLM()
    agent.rag = None
    agent.stats = {"input_chars": 0, "output_chars": 0}
    # Stay hermetic: skip the vault prompt file so the fallback prompt is used.
    agent._load_prompt = lambda name, **kw: ""
    agent._write_report = lambda title, body, rtype, meta=None: (None, body)

    out = agent.execute({"user_directive": "@ling-recall claim number 3"})
    # All 5 claims handed to the LLM (small corpus → no retrieval pre-filter).
    for i in range(5):
        assert f"claim number {i}" in captured["user_msg"]
    # Uses the lean recall system prompt, not the Q&A document scaffolding.
    assert "只輸出最終綜述" in captured["system_prompt"]
    assert "synthesis" in out
    # P3: dropping to the hardcoded fallback is observable in stats.
    assert agent.stats.get("used_fallback_prompt") is True
    # P4: recall pins to OUTPUT_LANGUAGE (banner), not a hardcoded 繁體中文.
    assert captured["kw"].get("pin_language") is True
    assert "繁體中文" not in captured["system_prompt"]
