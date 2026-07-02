"""FPO patent search: honest failure vs empty (throttling: test_http_client)."""

import types

import pytest

from services.research_pipeline import ResearchPipeline, PatentFetchError


def _rp():
    return ResearchPipeline(llm_client=None)


def test_fetch_failure_raises_not_empty(monkeypatch):
    rp = _rp()

    def boom(url, **kw):
        raise ConnectionError("429 Too Many Requests")

    monkeypatch.setattr(rp.http, "get", boom)

    with pytest.raises(PatentFetchError):
        rp.search_patents("Large Language Models")


def test_genuine_empty_returns_empty_list(monkeypatch):
    rp = _rp()
    # Valid page, but no listing_table → genuinely no results.
    monkeypatch.setattr(
        rp.http,
        "get",
        lambda url, **kw: types.SimpleNamespace(text="<html><body>no hits</body></html>"),
    )
    assert rp.search_patents("asdfqwerzxcv-nonsense") == []


def test_parse_success_returns_rows(monkeypatch):
    rp = _rp()
    html_doc = """
    <table class="listing_table">
      <tr><th>#</th><th>ID</th><th>Title</th><th>Score</th></tr>
      <tr><td>1</td><td>US20250298995</td><td><a href="/US20250298995.html">LANGUAGE CAPABILITY EVALUATION</a><br/>An abstract here.</td><td>1000</td></tr>
    </table>
    """
    monkeypatch.setattr(rp.http, "get", lambda url, **kw: types.SimpleNamespace(text=html_doc))
    res = rp.search_patents("Large Language Models")
    assert len(res) == 1
    assert res[0]["id"] == "US20250298995"
    assert "LANGUAGE CAPABILITY" in res[0]["title"]
    assert res[0]["url"].endswith("/US20250298995.html")


def test_fpo_keeps_browser_user_agent(monkeypatch):
    # FPO is scraped with a browser UA; the client's research-bot default must
    # not leak into that request.
    rp = _rp()
    captured = {}

    def fake_get(url, *, source, headers=None, **kw):
        captured["source"] = source
        captured["headers"] = headers or {}
        return types.SimpleNamespace(text="<html></html>")

    monkeypatch.setattr(rp.http, "get", fake_get)
    rp.search_patents("anything")
    assert captured["source"] == "fpo"
    assert "Mozilla/5.0" in captured["headers"].get("User-Agent", "")


# ── patent table: language + three-tier robustness (moved from
#    test_llm_client in P2b, with the rendering methods) ──────────────


class TestPatentTable:
    _PATENTS = [
        {"id": "US111", "title": "Alpha", "summary": "a summary", "url": "http://x/1"},
        {"id": "US222", "title": "Beta", "summary": "b summary", "url": "http://x/2"},
    ]

    # -- pure row renderer --
    def test_rows_to_lines_maps_and_skips_bad_idx(self):
        rp = _rp()
        rows = [
            {"idx": 1, "relevance": "高", "subject": "主旨", "summary": "摘要"},
            {"idx": 9, "relevance": "低", "subject": "x", "summary": "y"},  # out of range
            {"idx": "nope"},  # bad idx
        ]
        lines = rp._patent_rows_to_lines(rows, self._PATENTS)
        assert len(lines) == 1
        assert "US222" in lines[0] and "主旨" in lines[0]

    # -- generate_patent_table tiers (mock the LLM JSON step) --
    def test_tier1_ranked_translated_table(self, monkeypatch):
        import services.research_pipeline as rpmod

        rp = _rp()
        monkeypatch.setattr(rpmod, "lang_hint", lambda: "Japanese (日本語)")
        monkeypatch.setattr(
            rp,
            "_safe_json_rows",
            lambda prompt: [
                {"idx": 0, "relevance": "高", "subject": "アルファ", "summary": "要約"}
            ],
        )
        out = rp.generate_patent_table(self._PATENTS, topic="t")
        assert "US111" in out and "アルファ" in out
        assert "未排序" not in out  # ranked, not a fallback

    def test_tier2_translate_only_when_ranking_fails(self, monkeypatch):
        import services.research_pipeline as rpmod

        rp = _rp()
        monkeypatch.setattr(rpmod, "lang_hint", lambda: "Japanese")
        calls = {"n": 0}

        def rows(prompt):
            calls["n"] += 1
            # first call (rank) fails, second call (translate-only) succeeds
            return (
                []
                if calls["n"] == 1
                else [
                    {"idx": 0, "subject": "アルファ", "summary": "要約A"},
                    {"idx": 1, "subject": "ベータ", "summary": "要約B"},
                ]
            )

        monkeypatch.setattr(rp, "_safe_json_rows", rows)
        out = rp.generate_patent_table(self._PATENTS, topic="t")
        assert "已翻譯但未排序" in out  # tier-2 note
        assert "アルファ" in out and "ベータ" in out
        assert "找不到相關的專利資料" not in out

    def test_tier3_raw_source_when_all_llm_fails(self, monkeypatch):
        import services.research_pipeline as rpmod

        rp = _rp()
        monkeypatch.setattr(rpmod, "lang_hint", lambda: "Japanese")
        monkeypatch.setattr(rp, "_safe_json_rows", lambda prompt: [])  # both tiers fail
        out = rp.generate_patent_table(self._PATENTS, topic="t")
        assert "US111" in out and "US222" in out  # patents preserved
        assert "原文" in out  # labelled raw/source
        assert "找不到相關的專利資料" not in out

    def test_genuine_empty_is_honest(self):
        rp = _rp()
        out = rp.generate_patent_table([], topic="t")
        assert "找不到相關的專利資料" not in out
        assert "查無符合的專利" in out
