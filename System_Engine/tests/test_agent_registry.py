"""agents/registry.py — isolated lookup tests (P4 coverage gap)."""

from unittest.mock import MagicMock

from agents.registry import AgentRegistry


def _registry():
    llm = MagicMock()
    llm.capability_manager = MagicMock()
    return AgentRegistry(llm, MagicMock())


def test_known_intent_constructs_agent_with_injected_deps():
    reg = _registry()
    agent = reg.get_agent("recall")
    assert agent is not None
    assert agent.llm is reg.llm
    assert agent.rag is reg.rag


def test_unknown_intent_returns_none():
    assert _registry().get_agent("no-such-intent") is None


def test_list_commands_matches_registry_keys():
    reg = _registry()
    commands = reg.list_commands()
    assert "insight" in commands and "recall" in commands
    assert len(commands) == len(set(commands))


def test_every_registered_agent_is_constructible():
    reg = _registry()
    for key in reg.list_commands():
        assert reg.get_agent(key) is not None, f"agent for {key!r} failed to construct"
