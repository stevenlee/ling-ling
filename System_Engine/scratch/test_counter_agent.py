import tempfile
from pathlib import Path

from agents.counter_agent import CounterAgent
from core import config
from core.ui import ui
from services.ingestion_pipeline import IngestionPipeline
from services.text_splitter import TextSplitter
from watchers.clipping_watcher import ClippingWatcher
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
    assert inst["source_anchor"] == "Evidence Section"
    assert inst["source_offset"] > 0


def test_ground_tally_locations_prefers_stitched_part_anchor():
    agent = CounterAgent(llm=None)
    article = """# Article (Stitched)

## Part 1

Intro text.

## Part 2

Original range: lines 10-20
Original chars: 100-250

### Local Heading

This paragraph makes a distinctive claim about edge security.
"""
    tally = {
        "instances": [
            {
                "id": 1,
                "quote": "distinctive claim about edge security",
                "closest_heading": "Wrong Heading",
            }
        ]
    }

    agent._ground_tally_locations(tally, article)

    inst = tally["instances"][0]
    assert inst["closest_heading"] == "Local Heading"
    assert inst["source_part_anchor"] == "Part 2"
    assert inst["source_anchor"] == "Part 2"
    assert inst["original_source_range"] == {
        "start_line": 10,
        "end_line": 20,
        "start_char": 100,
        "end_char": 250,
    }


def test_report_links_use_obsidian_wikilink_pipe_without_escape():
    agent = CounterAgent(llm=None)
    report = agent._format_matrix_report(
        ["appeals"],
        [("Article A", "", "/tmp/Article A/Article A (Stitched).md")],
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
                            "original_source_range": {"start_line": 10, "end_line": 20},
                        }
                    ],
                }
            }
        },
    )

    assert "| # | Confidence | Quote | Reasoning | Reference |" in report
    assert "[[Article A (Stitched)#Section One|🔗分析錨點]]" in report
    assert "[[Article A|🔗原始檔]]" in report
    assert "原文 lines 10-20" in report
    assert "[[Article A (Stitched)#Section One\\|🔗分析錨點]]" not in report


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


def test_parse_concepts_accepts_ling_lens_command():
    agent = CounterAgent(llm=None)
    concepts = agent._parse_concepts("@ling-lens [[Article]]\nCount: privacy risk")

    assert concepts == ["privacy risk"]


def test_parse_concepts_keeps_ling_count_legacy_alias():
    agent = CounterAgent(llm=None)
    concepts = agent._parse_concepts("@ling-count [[Article]]\nCount: privacy risk")

    assert concepts == ["privacy risk"]


def test_find_in_pages_prefers_stitched_note_over_original(monkeypatch=None):
    agent = CounterAgent(llm=None)
    with tempfile.TemporaryDirectory() as tmpdir:
        pages_dir = Path(tmpdir)
        article_dir = pages_dir / "Article A"
        article_dir.mkdir()
        original = pages_dir / "Article A.md"
        stitched = article_dir / "Article A (Stitched).md"
        original.write_text("original text", encoding="utf-8")
        stitched.write_text("stitched text", encoding="utf-8")

        import agents.counter_agent as counter_module
        old_pages_dir = counter_module.PAGES_DIR
        counter_module.PAGES_DIR = pages_dir
        try:
            text, resolved_path = agent._find_in_pages("Article A")
        finally:
            counter_module.PAGES_DIR = old_pages_dir

    assert text == "stitched text"
    assert Path(resolved_path).name == "Article A (Stitched).md"


def test_original_source_title_prefers_raw_original_when_page_missing():
    agent = CounterAgent(llm=None)
    with tempfile.TemporaryDirectory() as tmpdir:
        raw_dir = Path(tmpdir)
        raw = raw_dir / "Article A.md"
        raw.write_text("raw original", encoding="utf-8")

        import agents.counter_agent as counter_module
        old_pages_dir = counter_module.PAGES_DIR
        old_raw_dir = counter_module.RAW_CONSOLIDATE_DIR
        counter_module.PAGES_DIR = raw_dir / "pages"
        counter_module.RAW_CONSOLIDATE_DIR = raw_dir
        try:
            title = agent._original_source_title("Article A")
        finally:
            counter_module.PAGES_DIR = old_pages_dir
            counter_module.RAW_CONSOLIDATE_DIR = old_raw_dir

    assert title == "Article A"


def test_splitter_spans_map_back_to_original_text():
    splitter = TextSplitter(chunk_size=20, overlap=5)
    source = "line one\n\nline two is longer\n\nline three ends here"
    spans = splitter.split_text_with_spans(source)

    assert len(spans) > 1
    for span in spans:
        assert source[span["start"]:span["end"]] == span["text"]


def test_clipping_watcher_formats_source_range():
    pipeline = IngestionPipeline(llm_client=None, rag_manager=None)
    source = "line 1\nline 2\nline 3\n"
    span = {"start": 7, "end": 13}
    source_span = pipeline._source_span_for_chunk(source, span, 2)
    formatted = pipeline._format_source_range(source_span)

    assert source_span["part"] == 2
    assert source_span["source_start_line"] == 2
    assert source_span["source_end_line"] == 2
    assert "Original range: lines 2-2" in formatted


def test_archive_markdown_moves_sidecar_images():
    watcher = ClippingWatcher(llm_client=None, rag_manager=None)
    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        consolidate = root / "Consolidate"
        raw_consolidate = root / "raw" / "consolidate"
        markdown = consolidate / "Article A.md"
        sidecar = consolidate / "images" / "Article A"
        sidecar.mkdir(parents=True)
        markdown.parent.mkdir(parents=True, exist_ok=True)
        markdown.write_text("source", encoding="utf-8")
        (sidecar / "page_1.jpeg").write_text("image", encoding="utf-8")

        import watchers.clipping_watcher as clipping_module
        old_raw_consolidate = clipping_module.RAW_CONSOLIDATE_DIR
        clipping_module.RAW_CONSOLIDATE_DIR = raw_consolidate
        try:
            watcher._archive_markdown_with_sidecar_images(markdown)
        finally:
            clipping_module.RAW_CONSOLIDATE_DIR = old_raw_consolidate

        assert not markdown.exists()
        assert not sidecar.exists()
        assert (raw_consolidate / "Article A.md").exists()
        assert (raw_consolidate / "images" / "Article A" / "page_1.jpeg").exists()


def test_scan_existing_resets_ui_status_after_processing():
    with tempfile.TemporaryDirectory() as tmpdir:
        prompt_dir = Path(tmpdir)
        prompt = prompt_dir / "@ling-lens-test.md"
        prompt.write_text("@ling-lens [[Article]]\nCount: claims", encoding="utf-8")

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
    test_ground_tally_locations_prefers_stitched_part_anchor()
    test_report_links_use_obsidian_wikilink_pipe_without_escape()
    test_matrix_report_keeps_multiline_quotes_inside_one_table_row()
    test_parse_concepts_accepts_ling_lens_command()
    test_parse_concepts_keeps_ling_count_legacy_alias()
    test_find_in_pages_prefers_stitched_note_over_original()
    test_original_source_title_prefers_raw_original_when_page_missing()
    test_splitter_spans_map_back_to_original_text()
    test_clipping_watcher_formats_source_range()
    test_archive_markdown_moves_sidecar_images()
    test_scan_existing_resets_ui_status_after_processing()
    print("counter_agent tests passed")
