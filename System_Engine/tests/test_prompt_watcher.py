import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.absolute()))
os.environ.setdefault("LLM_PROVIDER", "vllm")

from watchers.prompt_watcher import PromptWatcher


class TestPromptWatcherPlannerFlags:
    def test_planner_mode_keyword_sets_preview_flag(self):
        flags = PromptWatcher._detect_planner_flags(
            "@ling-insight planner-mode compare notes"
        )
        assert flags["planner_mode"] is True
        assert flags["execute_plan"] is False

    def test_planner_slash_alias_sets_preview_flag(self):
        flags = PromptWatcher._detect_planner_flags(
            "@ling-insight /planner compare notes"
        )
        assert flags["planner_mode"] is True
        assert flags["execute_plan"] is False

    def test_execute_flag_is_detected_but_does_not_imply_planner(self):
        flags = PromptWatcher._detect_planner_flags(
            "@ling-insight /execute compare notes"
        )
        assert flags["planner_mode"] is False
        assert flags["execute_plan"] is True

    def test_execution_alias_sets_execute_flag(self):
        flags = PromptWatcher._detect_planner_flags(
            "@ling-insight planner-mode /execution compare notes"
        )
        assert flags["planner_mode"] is True
        assert flags["execute_plan"] is True
