import tempfile
from pathlib import Path

from agents.counter_agent import CounterAgent
from core import config
from core.ui import ui
from watchers.prompt_watcher import PromptWatcher


def test_ground_tally_locations_uses_quote_to_find_heading():
    agent = CounterAgent(llm=None)
    article = """# Opening

No match here.

## Evidence Section

This paragraph makes a distinctive claim about memory and attention.
"""
    tally = {
        "instances": [
            {
                "id": 1,
                "quote": "distinctive claim about memory and attention",
                "closest_heading": "Wrong Heading",
            }
        ]
    }

    agent._ground_tally_locations(tally, article)

    inst = tally["instances"][0]
    assert inst["closest_heading"] == "Evidence Section"
    assert inst["source_offset"] > 0


def test_report_links_use_obsidian_wikilink_pipe_without_escape():
    agent = CounterAgent(llm=None)
    report = agent._format_matrix_report(
        ["appeals"],
        [("Article A", "", "")],
        {
            "Article A": {
                "appeals": {
                    "total_count": 1,
                    "instances": [
                        {
                            "id": 1,
                            "quote": "quoted source text",
                            "reasoning": "qualifies",
                            "confidence": "high",
                            "closest_heading": "Section One",
                        }
                    ],
                }
            }
        },
    )

    assert "[[Article A#Section One|🔗原文]]" in report
    assert "[[Article A#Section One\\|🔗原文]]" not in report


def test_matrix_report_keeps_multiline_quotes_inside_one_table_row():
    agent = CounterAgent(llm=None)
    report = agent._format_matrix_report(
        ["security"],
        [("Article A", "", "")],
        {
            "Article A": {
                "security": {
                    "total_count": 1,
                    "instances": [
                        {
                            "id": 1,
                            "quote": "優勢：\n\n* 隱私與資安：\n    數據在本地處理",
                            "reasoning": "此段落明確提到了「資安」\n直接討論資訊安全。",
                            "confidence": "high",
                            "closest_heading": "Edge",
                        }
                    ],
                }
            }
        },
    )

    evidence_row = next(line for line in report.splitlines() if line.startswith("| 1 |"))
    assert "隱私與資安" in evidence_row
    assert "數據在本地處理" in evidence_row
    assert "* 隱私與資安" in evidence_row
    assert not any(line.startswith("* 隱私與資安") for line in report.splitlines())


def test_scan_existing_resets_ui_status_after_processing():
    with tempfile.TemporaryDirectory() as tmpdir:
        prompt_dir = Path(tmpdir)
        prompt = prompt_dir / "@ling-count-test.md"
        prompt.write_text("@ling-count [[Article]]\nCount: claims", encoding="utf-8")

        old_to_llm_dir = config.TO_LLM_DIR
        config.TO_LLM_DIR = prompt_dir
        watcher = PromptWatcher(llm_client=None, rag_manager=None)

        def fake_process(filepath):
            ui.set_status("🔢 正在生成報告...")
            filepath.unlink()

        try:
            watcher.process_prompt = fake_process
            processed = watcher.scan_existing()
        finally:
            config.TO_LLM_DIR = old_to_llm_dir

    assert processed == 1
    assert ui._is_busy is False
    assert "Ling Ling is waiting" in ui._status_text


if __name__ == "__main__":
    test_ground_tally_locations_uses_quote_to_find_heading()
    test_report_links_use_obsidian_wikilink_pipe_without_escape()
    test_matrix_report_keeps_multiline_quotes_inside_one_table_row()
    test_scan_existing_resets_ui_status_after_processing()
    print("counter_agent tests passed")
