"""DocQuality P5 — template/reading-efficiency fixes.

- 5.1 demote_body_h1: synthesis body H1 -> H2 (shell owns the page title)
- 5.2 _nest_artifact_headings: artifact's own H2 -> H3 under the wrapper
- 5.3 _frontmatter_meta: trace_ids dropped from serialized frontmatter
- 5.4 glossary merge: per-part 詞彙 tables -> one deduped table
"""

from core.parser import demote_body_h1
from services.ingestion_pipeline import IngestionPipeline
from services.learning_artifacts import _nest_artifact_headings


class TestDemoteBodyH1:
    def test_model_authored_h1_demoted(self):
        # Observed live: `# CLOUD 法案…綜合報告` inside the Executive Summary.
        body = "# CLOUD 法案綜合報告\n\n## 執行摘要\n內容"
        out, fixes = demote_body_h1(body)
        assert out.startswith("## CLOUD 法案綜合報告")
        assert "## 執行摘要" in out  # existing H2 untouched
        assert fixes[0]["type"] == "demoted_body_h1"

    def test_multiple_h1_all_demoted(self):
        body = "# 一\n\n段落\n\n# 二\n\n段落"
        out, fixes = demote_body_h1(body)
        assert out.count("\n## ") + out.startswith("## ") == 2
        assert "1 heading" not in fixes[0]["before"]  # reports 2

    def test_hash_inside_fence_untouched(self):
        body = "## 真標題\n\n```python\n# 這是註解\nx = 1\n```"
        out, fixes = demote_body_h1(body)
        assert "# 這是註解" in out  # comment preserved
        assert fixes == []

    def test_no_h1_noop(self):
        body = "## 摘要\n\n內容\n\n### 細節"
        out, fixes = demote_body_h1(body)
        assert out == body
        assert fixes == []


class TestNestArtifactHeadings:
    def test_leading_h2_demoted_to_h3(self):
        # argument_map ships its own `## 🧩 論證結構（Toulmin）` — nest it.
        art = "## 🧩 論證結構（Toulmin）\n\n**主張**：X"
        assert _nest_artifact_headings(art).startswith("### 🧩 論證結構（Toulmin）")

    def test_mermaid_artifact_untouched(self):
        art = "```mermaid\ngraph TD\n  A --> B\n```"
        assert _nest_artifact_headings(art) == art

    def test_hash_inside_fence_not_demoted(self):
        art = "## 標題\n\n```\n## not a heading\n```"
        out = _nest_artifact_headings(art)
        assert out.startswith("### 標題")
        assert "\n## not a heading" in out  # fenced line preserved


class TestFrontmatterMetaSlimming:
    def test_trace_ids_dropped_run_id_kept(self):
        meta = {
            "title": "X (Synthesis)",
            "run_id": "run_abc",
            "trace_ids": ["llm_1", "llm_2", "llm_3"],
            "tags": ["a"],
        }
        slim = IngestionPipeline._frontmatter_meta(meta)
        assert "trace_ids" not in slim
        assert slim["run_id"] == "run_abc"
        assert slim["title"] == "X (Synthesis)"
        # original untouched — _record_artifact still needs trace_ids
        assert meta["trace_ids"] == ["llm_1", "llm_2", "llm_3"]

    def test_no_trace_ids_is_noop(self):
        meta = {"title": "X", "run_id": "r"}
        assert IngestionPipeline._frontmatter_meta(meta) == meta


class TestGlossaryMerge:
    _PART_BODY = """#### 摘要

段落內容。

#### 詞彙與關鍵術語

| 英文術語 | 繁體中文翻譯 | 說明 |
| :--- | :--- | :--- |
| **CLOUD Act** | CLOUD 法案 | 全稱。 |
| Comity | 國際禮讓 | 法律術語。 |

#### 下一節

更多內容。"""

    def test_glossary_stripped_from_body(self):
        body, rows = IngestionPipeline._split_glossary_section(self._PART_BODY)
        assert "詞彙與關鍵術語" not in body
        assert "CLOUD 法案" not in body  # table gone
        assert "#### 摘要" in body and "#### 下一節" in body  # surrounding kept

    def test_rows_parsed_without_header_or_separator(self):
        _, rows = IngestionPipeline._split_glossary_section(self._PART_BODY)
        assert rows == [
            ("**CLOUD Act**", "CLOUD 法案", "全稱。"),
            ("Comity", "國際禮讓", "法律術語。"),
        ]

    def test_body_without_glossary_unchanged(self):
        body = "#### 摘要\n\n沒有詞彙表的內容。"
        out, rows = IngestionPipeline._split_glossary_section(body)
        assert out == body
        assert rows == []

    def test_merge_dedupes_by_normalized_term(self):
        rows = [
            ("**CLOUD Act**", "CLOUD 法案", "短。"),
            ("CLOUD Act", "CLOUD 法案", "更長更完整的說明文字。"),
            ("Comity", "國際禮讓", "法律術語。"),
        ]
        merged = IngestionPipeline._merge_glossary(rows)
        # One row per term (2 unique), richest note kept for the duplicate.
        assert merged.count("| **CLOUD Act** |") == 1
        assert "更長更完整的說明文字。" in merged
        assert merged.count("| Comity |") == 1
        assert "## 📖 詞彙與關鍵術語（全篇合併）" in merged

    def test_merge_empty_is_blank(self):
        assert IngestionPipeline._merge_glossary([]) == ""

    def test_english_key_case_insensitive_dedup(self):
        rows = [("Executive Agreement", "行政協議", "a"), ("executive agreement", "行政協議", "b")]
        merged = IngestionPipeline._merge_glossary(rows)
        assert merged.count("行政協議") == 1
