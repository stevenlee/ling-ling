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
