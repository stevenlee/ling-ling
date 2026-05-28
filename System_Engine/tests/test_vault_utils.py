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

        vault_utils.update_wiki_index()

        output = index.read_text(encoding="utf-8")
        assert "> [!abstract]- 📅 2026-05-28 | Reading | I3 R5<br>**📂 Article A (2 items)**" in output
        assert "Details" not in output
        assert "> - 📍 Part 2" in output
        assert "> - 💬 Start with the synthesis." in output
        assert "[[Article A (Synthesis)]]" in output

        table = reading_index.read_text(encoding="utf-8")
        assert "Status: unread, reading, read, parked, skip" in table
        assert "Priority: 1-5, how soon you want to read it" in table
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

        vault_utils.update_wiki_index()

        table = reading_index.read_text(encoding="utf-8")
        assert "| [[Article A]] | reading | 4 |  |  | Part 2 | Keep this note. |  |" in table
        assert "| [[Article B]] |  |  |  |  |  |  |  |" in table
        assert "Removed Article" not in table

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

        vault_utils.update_wiki_index()

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

        vault_utils.update_wiki_index()

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
