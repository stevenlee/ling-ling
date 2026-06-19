"""The TUI's composed command files must route back to the intended intent
through the REAL PromptWatcher parser — this guards against the spec drifting
from INTENT_ROUTES."""
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).parent.parent.absolute()))
os.environ.setdefault("LLM_PROVIDER", "vllm")

from watchers.prompt_watcher import PromptWatcher
from tui.command_specs import COMMANDS, build_command_file


def _filled(spec):
    vals = {}
    for f in spec.fields:
        if f.kind == "links":
            vals[f.key] = ["DocA", "DocB"]
        elif f.kind == "text":
            vals[f.key] = "some directive"
        elif f.kind == "choice" and f.choices:
            vals[f.key] = f.choices[0]
    return vals


class TestCommandSpecsRoundTrip:
    def test_every_command_routes_back_to_its_intent(self):
        w = PromptWatcher(MagicMock(), MagicMock())
        for spec in COMMANDS:
            fn, content = build_command_file(spec, _filled(spec), stamp="20260619-000000")
            intent = w._detect_intent(fn.lower(), content.lower())
            assert intent == spec.intent, f"{spec.trigger!r} routed to {intent!r}, expected {spec.intent!r}"

    def test_filenames_are_unique(self):
        triggers = [s.trigger for s in COMMANDS]
        assert len(triggers) == len(set(triggers))

    def test_lens_renders_count_and_confidence(self):
        spec = next(s for s in COMMANDS if s.intent == "lens")
        _, content = build_command_file(
            spec, {"targets": ["X"], "body": "people who helped", "confidence": "high"}, stamp="t"
        )
        assert "[[X]]" in content
        assert "Count: people who helped" in content
        assert "Confidence: high" in content

    def test_insight_strategy_and_planner_flag(self):
        spec = next(s for s in COMMANDS if s.intent == "insight")
        _, content = build_command_file(
            spec, {"strategy": "montecarlo", "planner": True}, stamp="t"
        )
        assert "/montecarlo" in content
        assert "/planner" in content

    def test_visualize_as_type(self):
        spec = next(s for s in COMMANDS if s.intent == "visualize")
        _, content = build_command_file(
            spec, {"targets": ["N"], "as_type": "timeline"}, stamp="t"
        )
        assert "[[N]]" in content
        assert "as timeline" in content

    def test_fieldless_brain_op_is_filename_only(self):
        spec = next(s for s in COMMANDS if s.intent == "consolidate")
        fn, content = build_command_file(spec, {}, stamp="t")
        assert fn == "@ling-consolidate-t.md"
        assert content.strip() == ""  # nothing but the filename triggers it
