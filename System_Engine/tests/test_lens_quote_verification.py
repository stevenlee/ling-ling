"""Batch-3 D2: LingLens quote verification — pure-helper tests, no LLM."""

import os

os.environ.setdefault("LLM_PROVIDER", "vllm")


from agents.counter_agent import CounterAgent


def _matrix(instances_by_cell):
    """{(article, concept): [instances]} → results_matrix shape."""
    matrix = {}
    for (article, concept), instances in instances_by_cell.items():
        matrix.setdefault(article, {})[concept] = {"instances": instances}
    return matrix


def _inst(id_, quote, grounded):
    inst = {"id": id_, "quote": quote}
    if grounded:
        inst["source_offset"] = 42
    return inst


class TestVerifyQuoteGrounding:
    def test_all_grounded_is_keep(self, monkeypatch):
        monkeypatch.setattr("core.config.LENS_QUOTE_MIN_GROUNDED_RATIO", 0.8)
        matrix = _matrix({("A", "c"): [_inst(1, "q1", True), _inst(2, "q2", True)]})
        v = CounterAgent._verify_quote_grounding(matrix)
        assert (v["total"], v["grounded"]) == (2, 2)
        assert v["ratio"] == 1.0
        assert v["verdict"] == "keep"
        assert v["ungrounded"] == []

    def test_below_ratio_is_revise(self, monkeypatch):
        monkeypatch.setattr("core.config.LENS_QUOTE_MIN_GROUNDED_RATIO", 0.8)
        matrix = _matrix(
            {
                ("A", "c"): [_inst(1, "found", True), _inst(2, "missing", False)],
            }
        )
        v = CounterAgent._verify_quote_grounding(matrix)
        assert (v["total"], v["grounded"]) == (2, 1)
        assert v["verdict"] == "revise"
        assert v["ungrounded"][0]["quote"] == "missing"
        assert v["ungrounded"][0]["article"] == "A"
        assert v["ungrounded"][0]["concept"] == "c"

    def test_at_threshold_is_keep(self, monkeypatch):
        monkeypatch.setattr("core.config.LENS_QUOTE_MIN_GROUNDED_RATIO", 0.8)
        instances = [_inst(i, f"q{i}", i != 0) for i in range(5)]  # 4/5 = 0.8
        v = CounterAgent._verify_quote_grounding(_matrix({("A", "c"): instances}))
        assert v["verdict"] == "keep"

    def test_empty_matrix_has_no_verdict(self):
        v = CounterAgent._verify_quote_grounding({})
        assert v["total"] == 0
        assert v["ratio"] is None
        assert v["verdict"] is None

    def test_counts_span_articles_and_concepts(self, monkeypatch):
        monkeypatch.setattr("core.config.LENS_QUOTE_MIN_GROUNDED_RATIO", 0.5)
        matrix = _matrix(
            {
                ("A", "c1"): [_inst(1, "x", True)],
                ("A", "c2"): [_inst(1, "y", False)],
                ("B", "c1"): [_inst(1, "z", True)],
            }
        )
        v = CounterAgent._verify_quote_grounding(matrix)
        assert (v["total"], v["grounded"]) == (3, 2)
        assert v["verdict"] == "keep"


class TestJsonCallsOptOutOfTemplateAxis:
    """JSON-expecting answer_query callers must pass forced_template/persona
    "none" — otherwise the default wiki-note template (STRICT ADHERENCE)
    overrides the JSON instruction and the model writes a note instead.
    Observed live: LingLens extraction returned a full wiki note, 0 instances.
    """

    class _CapturingLLM:
        def __init__(self, reply="[]", replies=None):
            self.reply = reply
            self.replies = list(replies) if replies is not None else None
            self.calls = []

        def answer_query(self, *args, **kwargs):
            self.calls.append(kwargs)
            if self.replies is not None:
                return self.replies.pop(0) if self.replies else ""
            return self.reply

    def _agent(self, llm):
        agent = CounterAgent.__new__(CounterAgent)
        agent.llm = llm
        agent.stats = {"input_chars": 0, "output_chars": 0}
        return agent

    def test_extract_from_chunk_opts_out(self, monkeypatch):
        monkeypatch.setattr(CounterAgent, "_load_prompt", lambda self, name, **kw: "")
        llm = self._CapturingLLM()
        self._agent(llm)._extract_from_chunk("c", "chunk text", 1, 1, "medium")
        assert llm.calls[0]["forced_template"] == "none"
        assert llm.calls[0]["persona"] == "none"

    def test_tally_instances_opts_out(self):
        llm = self._CapturingLLM(reply='{"total_count": 4, "instances": []}')
        instances = [{"quote": f"q{i}"} for i in range(4)]  # >3 → LLM tally path
        self._agent(llm)._tally_instances("c", instances, 1)
        assert llm.calls[0]["forced_template"] == "none"
        assert llm.calls[0]["persona"] == "none"

    def test_extract_from_chunk_retries_once_on_no_array(self, monkeypatch):
        # Reasoning-channel miss (no JSON) on attempt 1, recovers on attempt 2.
        monkeypatch.setattr(CounterAgent, "_load_prompt", lambda self, name, **kw: "")
        llm = self._CapturingLLM(
            replies=[
                "thinking out loud, no array here",
                '[{"quote": "found", "confidence": "high"}]',
            ]
        )
        out = self._agent(llm)._extract_from_chunk("c", "chunk", 1, 1, "medium")
        assert len(llm.calls) == 2
        assert len(out) == 1 and out[0]["quote"] == "found"

    def test_extract_from_chunk_no_retry_on_genuine_empty(self, monkeypatch):
        # A literal [] is a real zero, not a parse miss — must not retry.
        monkeypatch.setattr(CounterAgent, "_load_prompt", lambda self, name, **kw: "")
        llm = self._CapturingLLM(reply="[]")
        out = self._agent(llm)._extract_from_chunk("c", "chunk", 1, 1, "medium")
        assert len(llm.calls) == 1
        assert out == []

    def test_tally_retries_once_then_recovers(self):
        # >3 instances → LLM tally path; first reply has no object, second works.
        llm = self._CapturingLLM(
            replies=[
                "no json object here",
                '{"total_count": 2, "instances": [{"id": 1, "quote": "a"}, {"id": 2, "quote": "b"}]}',
            ]
        )
        instances = [{"quote": f"q{i}"} for i in range(4)]
        tally = self._agent(llm)._tally_instances("c", instances, 1)
        assert len(llm.calls) == 2
        assert tally["total_count"] == 2

    def test_tally_falls_back_to_local_after_two_misses(self):
        # Both attempts miss → cruder local dedup (4 distinct quotes survive).
        llm = self._CapturingLLM(replies=["nope", "still nope"])
        instances = [{"quote": f"q{i}", "confidence": "low"} for i in range(4)]
        tally = self._agent(llm)._tally_instances("c", instances, 1)
        assert len(llm.calls) == 2
        assert tally["total_count"] == 4  # local fallback kept all distinct quotes
        assert "deduplicated by exact quote" in tally["methodology_note"]


class TestFormatQuoteVerification:
    def test_empty_section(self):
        section = CounterAgent._format_quote_verification(
            {"total": 0, "grounded": 0, "ratio": None, "ungrounded": [], "verdict": None}
        )
        assert section.startswith("## 🔍 Quote Verification")
        assert "沒有實例可驗證" in section

    def test_lists_ungrounded_quotes(self):
        v = {
            "total": 3,
            "grounded": 1,
            "ratio": 1 / 3,
            "verdict": "revise",
            "ungrounded": [
                {"article": "A", "concept": "appeal", "id": 2, "quote": "ghost quote"},
                {"article": "A", "concept": "appeal", "id": 3, "quote": "another"},
            ],
        }
        section = CounterAgent._format_quote_verification(v)
        assert "**1/3**" in section
        assert "revise" in section
        assert "ghost quote" in section
        assert "#2" in section and "#3" in section

    def test_caps_listing_at_ten(self):
        ungrounded = [
            {"article": "A", "concept": "c", "id": i, "quote": f"q{i}"} for i in range(15)
        ]
        v = {
            "total": 15,
            "grounded": 0,
            "ratio": 0.0,
            "verdict": "revise",
            "ungrounded": ungrounded,
        }
        section = CounterAgent._format_quote_verification(v)
        assert "q9" in section
        assert "q10" not in section
        assert "及其他 5 條" in section
