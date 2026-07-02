"""services/http_client.py — PoliteHttpClient throttling and headers (P1).

Retry semantics themselves are covered by test_retrying.py; here we pin the
politeness intervals, per-source isolation, and User-Agent merging that
research_pipeline relies on.
"""

import types

from services import http_client as hcmod
from services.http_client import DEFAULT_MIN_INTERVALS, PoliteHttpClient, RESEARCH_USER_AGENT


def test_throttle_spaces_consecutive_requests(monkeypatch):
    slept = []
    monkeypatch.setattr(hcmod.time, "sleep", lambda s: slept.append(s))
    clock = {"t": 100.0}
    monkeypatch.setattr(hcmod.time, "monotonic", lambda: clock["t"])

    client = PoliteHttpClient()
    client.throttle("fpo")  # first call for this source → no wait
    assert slept == []
    client.throttle("fpo")  # immediate second → wait ~fpo interval
    assert len(slept) == 1
    assert abs(slept[0] - DEFAULT_MIN_INTERVALS["fpo"]) < 0.01


def test_throttle_is_per_source(monkeypatch):
    # Each source has an independent clock — throttling FPO must not make the
    # first Wikipedia call wait.
    slept = []
    monkeypatch.setattr(hcmod.time, "sleep", lambda s: slept.append(s))
    monkeypatch.setattr(hcmod.time, "monotonic", lambda: 100.0)

    client = PoliteHttpClient()
    client.throttle("fpo")
    client.throttle("wikipedia")  # different source, first hit → no wait
    client.throttle("arxiv")  # different source, first hit → no wait
    assert slept == []


def test_unknown_source_uses_default_interval(monkeypatch):
    slept = []
    monkeypatch.setattr(hcmod.time, "sleep", lambda s: slept.append(s))
    monkeypatch.setattr(hcmod.time, "monotonic", lambda: 100.0)

    client = PoliteHttpClient(default_interval=2.5)
    client.throttle("new-source")
    client.throttle("new-source")
    assert slept == [2.5]


def test_get_merges_default_ua_and_caller_headers_win(monkeypatch):
    captured = {}

    def fake_requests_get(url, headers=None, timeout=None):
        captured["headers"] = headers
        return types.SimpleNamespace(raise_for_status=lambda: None)

    monkeypatch.setattr(hcmod.requests, "get", fake_requests_get)
    monkeypatch.setattr(hcmod.time, "sleep", lambda s: None)

    client = PoliteHttpClient()
    client.get("http://x", source="arxiv")
    assert captured["headers"]["User-Agent"] == RESEARCH_USER_AGENT

    client.get("http://x", source="fpo", headers={"User-Agent": "Mozilla/5.0 fake"})
    assert captured["headers"]["User-Agent"] == "Mozilla/5.0 fake"
