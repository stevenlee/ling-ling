import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.absolute()))

import core.vault_utils as vault_utils


class TestUpdateWikiIndex:
    def test_merges_human_reading_annotations(self, monkeypatch, tmp_path):
        pages = tmp_path / "pages"
        notes = tmp_path / "Notes"
        raw = tmp_path / "raw" / "consolidate"
        index = tmp_path / "index.md"
        reading_index = tmp_path / "ReadingIndex.md"

        article_dir = pages / "Article A"
        article_dir.mkdir(parents=True)
        notes.mkdir()
        raw.mkdir(parents=True)

        (article_dir / "Article A.md").write_text(
            "---\ntitle: Article A\ntags: [topic]\ndate: 2026-05-28\n---\n\nBody",
            encoding="utf-8",
        )
        (article_dir / "Article A (Synthesis).md").write_text(
            "---\ntitle: Article A (Synthesis)\ntags: [summary]\n---\n\nSummary",
            encoding="utf-8",
        )
        reading_index.write_text(
            "# ReadingIndex\n\n"
            "| Article | Status | Priority | Importance | Relevance | Progress | Comment | Updated |\n"
            "| --- | --- | --- | --- | --- | --- | --- | --- |\n"
            "| [[Article A]] | reading | 4 | 3 | 5 | Part 2 | Start with the synthesis. | 2026-05-28 |\n",
            encoding="utf-8",
        )

        monkeypatch.setattr(vault_utils, "PAGES_DIR", pages)
        monkeypatch.setattr(vault_utils, "NOTES_DIR", notes)
        monkeypatch.setattr(vault_utils, "RAW_CONSOLIDATE_DIR", raw)
        monkeypatch.setattr(vault_utils, "INDEX_FILE", index)
        monkeypatch.setattr(vault_utils, "READING_INDEX_FILE", reading_index)

        vault_utils.update_wiki_index(sync_reading_index=True)

        output = index.read_text(encoding="utf-8")
        assert "> [!abstract]- 📅 2026-05-28 | Reading | I3 R5<br>**📂 Article A (2 items)**" in output
        assert "Details" not in output
        assert "> - 📍 Part 2" in output
        assert "> - 💬 Start with the synthesis." in output
        assert "[[Article A (Synthesis)]]" in output

        table = reading_index.read_text(encoding="utf-8")
        assert "| [[Article A]] | reading | 4 | 3 | 5 | Part 2 | Start with the synthesis. | 2026-05-28 |" in table

    def test_syncs_reading_index_article_column(self, monkeypatch, tmp_path):
        pages = tmp_path / "pages"
        notes = tmp_path / "Notes"
        raw = tmp_path / "raw" / "consolidate"
        index = tmp_path / "index.md"
        reading_index = tmp_path / "ReadingIndex.md"

        for title in ("Article A", "Article B"):
            article_dir = pages / title
            article_dir.mkdir(parents=True)
            (article_dir / f"{title}.md").write_text("Body", encoding="utf-8")
        notes.mkdir()
        raw.mkdir(parents=True)
        reading_index.write_text(
            "# ReadingIndex\n\n"
            "| Article | Status | Priority | Importance | Relevance | Progress | Comment | Updated |\n"
            "| --- | --- | --- | --- | --- | --- | --- | --- |\n"
            "| [[Article A]] | reading | 4 |  |  | Part 2 | Keep this note. |  |\n"
            "| [[Removed Article]] | skip |  |  |  |  | Old row. |  |\n",
            encoding="utf-8",
        )

        monkeypatch.setattr(vault_utils, "PAGES_DIR", pages)
        monkeypatch.setattr(vault_utils, "NOTES_DIR", notes)
        monkeypatch.setattr(vault_utils, "RAW_CONSOLIDATE_DIR", raw)
        monkeypatch.setattr(vault_utils, "INDEX_FILE", index)
        monkeypatch.setattr(vault_utils, "READING_INDEX_FILE", reading_index)

        vault_utils.update_wiki_index(sync_reading_index=True)

        table = reading_index.read_text(encoding="utf-8")
        assert "| [[Article A]] | reading | 4 |  |  | Part 2 | Keep this note. |  |" in table
        assert "| [[Article B]] |  |  |  |  |  |  |  |" in table
        assert "| [[Removed Article]] | skip |  |  |  |  | Old row. |  |" in table

    def test_preserves_escaped_pipes_in_human_columns(self, monkeypatch, tmp_path):
        pages = tmp_path / "pages"
        notes = tmp_path / "Notes"
        raw = tmp_path / "raw" / "consolidate"
        index = tmp_path / "index.md"
        reading_index = tmp_path / "ReadingIndex.md"

        article_dir = pages / "Article C"
        article_dir.mkdir(parents=True)
        notes.mkdir()
        raw.mkdir(parents=True)
        (article_dir / "Article C.md").write_text("Body", encoding="utf-8")
        reading_index.write_text(
            "# ReadingIndex\n\n"
            "| Article | Status | Priority | Importance | Relevance | Progress | Comment | Updated |\n"
            "| --- | --- | --- | --- | --- | --- | --- | --- |\n"
            "| [[Article C]] | reading |  |  |  |  | compare A\\|B and C:\\\\tmp |  |\n",
            encoding="utf-8",
        )

        monkeypatch.setattr(vault_utils, "PAGES_DIR", pages)
        monkeypatch.setattr(vault_utils, "NOTES_DIR", notes)
        monkeypatch.setattr(vault_utils, "RAW_CONSOLIDATE_DIR", raw)
        monkeypatch.setattr(vault_utils, "INDEX_FILE", index)
        monkeypatch.setattr(vault_utils, "READING_INDEX_FILE", reading_index)

        vault_utils.update_wiki_index(sync_reading_index=True)

        table = reading_index.read_text(encoding="utf-8")
        assert "compare A\\|B and C:\\\\tmp" in table
        output = index.read_text(encoding="utf-8")
        assert "> - 💬 compare A|B and C:\\tmp" in output

    def test_missing_reading_index_is_optional(self, monkeypatch, tmp_path):
        pages = tmp_path / "pages"
        notes = tmp_path / "Notes"
        raw = tmp_path / "raw" / "consolidate"
        index = tmp_path / "index.md"

        article_dir = pages / "Article B"
        article_dir.mkdir(parents=True)
        notes.mkdir()
        raw.mkdir(parents=True)
        (article_dir / "Article B.md").write_text("Body", encoding="utf-8")

        monkeypatch.setattr(vault_utils, "PAGES_DIR", pages)
        monkeypatch.setattr(vault_utils, "NOTES_DIR", notes)
        monkeypatch.setattr(vault_utils, "RAW_CONSOLIDATE_DIR", raw)
        monkeypatch.setattr(vault_utils, "INDEX_FILE", index)
        monkeypatch.setattr(vault_utils, "READING_INDEX_FILE", tmp_path / "missing.yml")

        vault_utils.update_wiki_index(sync_reading_index=True)

        output = index.read_text(encoding="utf-8")
        assert "> [!abstract]- 📅 " in output
        assert "<br>**📂 Article B (1 items)**" in output
        assert "🔖" not in output

    def test_ensure_wiki_indexes_creates_dashboard_and_reading_index(self, monkeypatch, tmp_path):
        pages = tmp_path / "pages"
        notes = tmp_path / "Notes"
        raw = tmp_path / "raw" / "consolidate"
        index = tmp_path / "index.md"
        reading_index = tmp_path / "ReadingIndex.md"

        article_dir = pages / "Article D"
        article_dir.mkdir(parents=True)
        notes.mkdir()
        raw.mkdir(parents=True)
        (article_dir / "Article D.md").write_text("Body", encoding="utf-8")

        monkeypatch.setattr(vault_utils, "PAGES_DIR", pages)
        monkeypatch.setattr(vault_utils, "NOTES_DIR", notes)
        monkeypatch.setattr(vault_utils, "RAW_CONSOLIDATE_DIR", raw)
        monkeypatch.setattr(vault_utils, "INDEX_FILE", index)
        monkeypatch.setattr(vault_utils, "READING_INDEX_FILE", reading_index)

        vault_utils.ensure_wiki_indexes()

        assert index.exists()
        assert reading_index.exists()
        assert "- ✍️ [[ReadingIndex]]" in index.read_text(encoding="utf-8")
        assert "| [[Article D]] |  |  |  |  |  |  |  |" in reading_index.read_text(encoding="utf-8")

    def test_update_wiki_index_does_not_rewrite_reading_index_by_default(self, monkeypatch, tmp_path):
        pages = tmp_path / "pages"
        notes = tmp_path / "Notes"
        raw = tmp_path / "raw" / "consolidate"
        index = tmp_path / "index.md"
        reading_index = tmp_path / "ReadingIndex.md"

        article_dir = pages / "Article E"
        article_dir.mkdir(parents=True)
        notes.mkdir()
        raw.mkdir(parents=True)
        (article_dir / "Article E.md").write_text("Body", encoding="utf-8")
        reading_index.write_text("# My hand-edited reading list\n", encoding="utf-8")

        monkeypatch.setattr(vault_utils, "PAGES_DIR", pages)
        monkeypatch.setattr(vault_utils, "NOTES_DIR", notes)
        monkeypatch.setattr(vault_utils, "RAW_CONSOLIDATE_DIR", raw)
        monkeypatch.setattr(vault_utils, "INDEX_FILE", index)
        monkeypatch.setattr(vault_utils, "READING_INDEX_FILE", reading_index)

        vault_utils.update_wiki_index()

        assert reading_index.read_text(encoding="utf-8") == "# My hand-edited reading list\n"

    def test_sync_reading_index_ignores_raw_consolidate_files(self, monkeypatch, tmp_path):
        pages = tmp_path / "pages"
        notes = tmp_path / "Notes"
        raw = tmp_path / "raw" / "consolidate"
        index = tmp_path / "index.md"
        reading_index = tmp_path / "ReadingIndex.md"

        article_dir = pages / "Article F"
        article_dir.mkdir(parents=True)
        notes.mkdir()
        raw.mkdir(parents=True)
        (article_dir / "Article F.md").write_text("Body", encoding="utf-8")
        (raw / "input.md").write_text("Raw source", encoding="utf-8")

        monkeypatch.setattr(vault_utils, "PAGES_DIR", pages)
        monkeypatch.setattr(vault_utils, "NOTES_DIR", notes)
        monkeypatch.setattr(vault_utils, "RAW_CONSOLIDATE_DIR", raw)
        monkeypatch.setattr(vault_utils, "INDEX_FILE", index)
        monkeypatch.setattr(vault_utils, "READING_INDEX_FILE", reading_index)

        vault_utils.ensure_wiki_indexes()

        table = reading_index.read_text(encoding="utf-8")
        assert "| [[Article F]] |  |  |  |  |  |  |  |" in table
        assert "[[input]]" not in table

    def test_sync_reading_index_aborts_on_header_typo(self, monkeypatch, tmp_path):
        pages = tmp_path / "pages"
        notes = tmp_path / "Notes"
        raw = tmp_path / "raw" / "consolidate"
        index = tmp_path / "index.md"
        reading_index = tmp_path / "ReadingIndex.md"

        article_dir = pages / "Article G"
        article_dir.mkdir(parents=True)
        notes.mkdir()
        raw.mkdir(parents=True)
        (article_dir / "Article G.md").write_text("Body", encoding="utf-8")
        original = (
            "# ReadingIndex\n\n"
            "| Article | Status | Priority | Importance | Relevance | Progress | Notes | Updated |\n"
            "| --- | --- | --- | --- | --- | --- | --- | --- |\n"
            "| [[Article G]] | reading |  |  |  |  | Do not lose this. |  |\n"
        )
        reading_index.write_text(original, encoding="utf-8")

        monkeypatch.setattr(vault_utils, "PAGES_DIR", pages)
        monkeypatch.setattr(vault_utils, "NOTES_DIR", notes)
        monkeypatch.setattr(vault_utils, "RAW_CONSOLIDATE_DIR", raw)
        monkeypatch.setattr(vault_utils, "INDEX_FILE", index)
        monkeypatch.setattr(vault_utils, "READING_INDEX_FILE", reading_index)

        vault_utils.ensure_wiki_indexes()

        assert reading_index.read_text(encoding="utf-8") == original

    def test_sync_reading_index_removes_unannotated_orphan_rows(self, monkeypatch, tmp_path):
        pages = tmp_path / "pages"
        notes = tmp_path / "Notes"
        raw = tmp_path / "raw" / "consolidate"
        index = tmp_path / "index.md"
        reading_index = tmp_path / "ReadingIndex.md"

        article_dir = pages / "Article H"
        article_dir.mkdir(parents=True)
        notes.mkdir()
        raw.mkdir(parents=True)
        (article_dir / "Article H.md").write_text("Body", encoding="utf-8")
        reading_index.write_text(
            "# ReadingIndex\n\n"
            "| Article | Status | Priority | Importance | Relevance | Progress | Comment | Updated |\n"
            "| --- | --- | --- | --- | --- | --- | --- | --- |\n"
            "| [[Article H]] |  |  |  |  |  |  |  |\n"
            "| [[Old Empty Article]] |  |  |  |  |  |  |  |\n",
            encoding="utf-8",
        )

        monkeypatch.setattr(vault_utils, "PAGES_DIR", pages)
        monkeypatch.setattr(vault_utils, "NOTES_DIR", notes)
        monkeypatch.setattr(vault_utils, "RAW_CONSOLIDATE_DIR", raw)
        monkeypatch.setattr(vault_utils, "INDEX_FILE", index)
        monkeypatch.setattr(vault_utils, "READING_INDEX_FILE", reading_index)

        vault_utils.ensure_wiki_indexes()

        table = reading_index.read_text(encoding="utf-8")
        assert "[[Article H]]" in table
        assert "Old Empty Article" not in table

    def test_sync_reading_index_skips_write_when_article_order_is_current(self, monkeypatch, tmp_path):
        pages = tmp_path / "pages"
        notes = tmp_path / "Notes"
        raw = tmp_path / "raw" / "consolidate"
        index = tmp_path / "index.md"
        reading_index = tmp_path / "ReadingIndex.md"

        for title in ("Article I", "Article J"):
            article_dir = pages / title
            article_dir.mkdir(parents=True)
            (article_dir / f"{title}.md").write_text("Body", encoding="utf-8")
        notes.mkdir()
        raw.mkdir(parents=True)
        original = (
            "# Custom heading kept intact\n\n"
            "| Article | Status | Priority | Importance | Relevance | Progress | Comment | Updated |\n"
            "| --- | --- | --- | --- | --- | --- | --- | --- |\n"
            "| [[Article I]] | reading |  |  |  |  | User changed this directly. |  |\n"
            "| [[Article J]] |  |  |  |  |  |  |  |\n"
        )
        reading_index.write_text(original, encoding="utf-8")

        monkeypatch.setattr(vault_utils, "PAGES_DIR", pages)
        monkeypatch.setattr(vault_utils, "NOTES_DIR", notes)
        monkeypatch.setattr(vault_utils, "RAW_CONSOLIDATE_DIR", raw)
        monkeypatch.setattr(vault_utils, "INDEX_FILE", index)
        monkeypatch.setattr(vault_utils, "READING_INDEX_FILE", reading_index)

        vault_utils.ensure_wiki_indexes()

        assert reading_index.read_text(encoding="utf-8") == original

    def test_sync_reading_index_initializes_empty_file(self, monkeypatch, tmp_path):
        pages = tmp_path / "pages"
        notes = tmp_path / "Notes"
        raw = tmp_path / "raw" / "consolidate"
        index = tmp_path / "index.md"
        reading_index = tmp_path / "ReadingIndex.md"

        article_dir = pages / "Article K"
        article_dir.mkdir(parents=True)
        notes.mkdir()
        raw.mkdir(parents=True)
        (article_dir / "Article K.md").write_text("Body", encoding="utf-8")
        reading_index.write_text("   \n  \n", encoding="utf-8")

        monkeypatch.setattr(vault_utils, "PAGES_DIR", pages)
        monkeypatch.setattr(vault_utils, "NOTES_DIR", notes)
        monkeypatch.setattr(vault_utils, "RAW_CONSOLIDATE_DIR", raw)
        monkeypatch.setattr(vault_utils, "INDEX_FILE", index)
        monkeypatch.setattr(vault_utils, "READING_INDEX_FILE", reading_index)

        vault_utils.ensure_wiki_indexes()

        table = reading_index.read_text(encoding="utf-8")
        assert "| [[Article K]] |  |  |  |  |  |  |  |" in table

