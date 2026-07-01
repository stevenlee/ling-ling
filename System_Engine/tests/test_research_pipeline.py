"""FPO patent search: honest failure vs empty, and self-throttling."""

import sys
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.absolute()))

import pytest

from services.research_pipeline import ResearchPipeline, PatentFetchError


def _rp():
    return ResearchPipeline(llm_client=None)


def test_fetch_failure_raises_not_empty(monkeypatch):
    rp = _rp()
    monkeypatch.setattr(rp, "_throttle_fpo", lambda: None)

    def boom(url, headers):
        raise ConnectionError("429 Too Many Requests")
    monkeypatch.setattr(rp, "_get_with_retry", boom)

    with pytest.raises(PatentFetchError):
        rp.search_patents("Large Language Models")


def test_genuine_empty_returns_empty_list(monkeypatch):
    rp = _rp()
    monkeypatch.setattr(rp, "_throttle_fpo", lambda: None)
    # Valid page, but no listing_table → genuinely no results.
    monkeypatch.setattr(rp, "_get_with_retry",
                        lambda url, headers: types.SimpleNamespace(text="<html><body>no hits</body></html>"))
    assert rp.search_patents("asdfqwerzxcv-nonsense") == []


def test_parse_success_returns_rows(monkeypatch):
    rp = _rp()
    monkeypatch.setattr(rp, "_throttle_fpo", lambda: None)
    html_doc = """
    <table class="listing_table">
      <tr><th>#</th><th>ID</th><th>Title</th><th>Score</th></tr>
      <tr><td>1</td><td>US20250298995</td><td><a href="/US20250298995.html">LANGUAGE CAPABILITY EVALUATION</a><br/>An abstract here.</td><td>1000</td></tr>
    </table>
    """
    monkeypatch.setattr(rp, "_get_with_retry",
                        lambda url, headers: types.SimpleNamespace(text=html_doc))
    res = rp.search_patents("Large Language Models")
    assert len(res) == 1
    assert res[0]["id"] == "US20250298995"
    assert "LANGUAGE CAPABILITY" in res[0]["title"]
    assert res[0]["url"].endswith("/US20250298995.html")


def test_throttle_spaces_consecutive_requests(monkeypatch):
    from services import research_pipeline as rpmod
    slept = []
    monkeypatch.setattr(rpmod.time, "sleep", lambda s: slept.append(s))
    # deterministic clock
    clock = {"t": 100.0}
    monkeypatch.setattr(rpmod.time, "monotonic", lambda: clock["t"])

    rp = _rp()
    rp._throttle_fpo()                 # first call: last_fpo_at was 0 → no wait
    assert slept == []
    rp._throttle_fpo()                 # immediate second call → must wait ~FPO_MIN_INTERVAL
    assert len(slept) == 1
    assert abs(slept[0] - rpmod.FPO_MIN_INTERVAL) < 0.01
