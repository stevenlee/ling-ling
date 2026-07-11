"""Phase 3 — @ling-scout command wiring + web.scout_digest adapter."""

from pathlib import Path

import services.scout.digest as digest_mod
from agents.scout_agent import ScoutAgent
from services.builtin_adapters import builtin_adapter_names, register_builtin_adapters
from services.command_dispatcher import detect_intent
from services.pipeline_runner import AdapterRegistry
from services.scout.digest import ScoutDigestResult


def test_intent_route_and_registry():
    assert detect_intent("note @ling-scout.md", "") == "scout"
    assert detect_intent("whatever.md", "please /scout now") == "scout"
    from agents.registry import AgentRegistry

    agent = AgentRegistry(llm=object(), rag=object()).get_agent("scout")
    assert isinstance(agent, ScoutAgent)


def test_scout_agent_runs_digest_with_rag(monkeypatch):
    calls = {}

    def fake_run(llm, rag=None, **kwargs):
        calls["llm"], calls["rag"] = llm, rag
        return ScoutDigestResult("succeeded", "9 new item(s).", Path("/tmp/✅Scout-x.md"))

    monkeypatch.setattr(digest_mod, "run_scout_digest", fake_run)
    llm, rag = object(), object()
    summary = ScoutAgent(llm, rag).execute({})
    assert summary == "9 new item(s)."
    assert calls["llm"] is llm and calls["rag"] is rag  # bridging path wired


def test_adapter_registered_and_contract(monkeypatch):
    assert "web.scout_digest" in builtin_adapter_names()

    def fake_run(llm, rag=None, **kwargs):
        assert rag is None  # adapter factories are llm-only → bridging skipped
        return ScoutDigestResult("succeeded", "done", None)

    monkeypatch.setattr(digest_mod, "run_scout_digest", fake_run)
    registry = AdapterRegistry()
    register_builtin_adapters(registry, llm=object())
    output = registry.get("web.scout_digest")({})
    assert output == {"status": "succeeded", "summary": "done", "report_path": None}
