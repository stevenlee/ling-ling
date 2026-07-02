"""services/command_dispatcher.py — intent routing table + dispatch seams (P2c).

The routing table previously had 25+ branches with only a handful covered
(via integration tests); this parametrizes the WHOLE table for both trigger
kinds, and pins the ordering rules that comments rely on.
"""

import pytest

from core.config import COMMAND_PREFIX
from services.command_dispatcher import (
    _BRAIN_OPS,
    INTENT_ROUTES,
    detect_intent,
    detect_planner_flags,
)

_ALL_CASES = [
    pytest.param(trigger, kind, intent, id=f"{kind}:{trigger}->{intent}")
    for triggers, slash_triggers, intent in INTENT_ROUTES
    for kind, ts in (("filename", triggers), ("slash", slash_triggers))
    for trigger in ts
]


@pytest.mark.parametrize("trigger,kind,intent", _ALL_CASES)
def test_every_route_trigger_resolves_to_its_intent(trigger, kind, intent):
    if kind == "filename":
        got = detect_intent(f"{COMMAND_PREFIX}{trigger} something.md".lower(), "")
    else:
        got = detect_intent("whatever.md", f"please /{trigger} this".lower())
    assert got == intent


def test_no_intent_for_plain_prompt():
    assert detect_intent("note.md", "what is entropy?") is None


def test_ordering_longer_triggers_win():
    # The table's ordering contract: these pairs false-match if reordered.
    assert detect_intent(f"{COMMAND_PREFIX}patrol-tags x.md", "") == "patrol_tags"
    assert detect_intent(f"{COMMAND_PREFIX}patrol x.md", "") == "patrol"
    assert detect_intent("x.md", "/recalled 熵總是增加") == "recalled"
    assert detect_intent("x.md", "/recall hilbert") == "recall"


def test_brain_ops_are_all_routable():
    routable = {intent for _, _, intent in INTENT_ROUTES}
    assert _BRAIN_OPS <= routable


def test_planner_flags():
    assert detect_planner_flags("@ling-insight planner-mode x") == {
        "planner_mode": True,
        "execute_plan": False,
    }
    assert detect_planner_flags("@ling-insight /execute x")["execute_plan"] is True


def test_agent_intents_resolve_in_registry():
    # Every non-brain-op, non-special intent must have a registered agent —
    # this is exactly the drift that made count/counter/tag_patrol dead aliases.
    from agents.registry import AgentRegistry

    special = _BRAIN_OPS | {"kb_zip", "kb_unzip", "kb_reset", "repair_tags", "research"}
    registry = AgentRegistry(None, None)  # class map only; llm/rag unused here
    for _, _, intent in INTENT_ROUTES:
        if intent in special:
            continue
        assert registry._registry.get(intent) is not None, (
            f"intent {intent!r} routes to no registered agent"
        )
