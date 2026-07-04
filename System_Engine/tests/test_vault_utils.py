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
            "| Article | Stat | Re | Im | Comment |\n"
            "| --- | --- | --- | --- | --- |\n"
            "| [[Article A]] | reading | 5 | 3 | Start with the synthesis. |\n",
            encoding="utf-8",
        )

        monkeypatch.setattr(vault_utils, "PAGES_DIR", pages)
        monkeypatch.setattr(vault_utils, "NOTES_DIR", notes)
        monkeypatch.setattr(vault_utils, "RAW_CONSOLIDATE_DIR", raw)
        monkeypatch.setattr(vault_utils, "INDEX_FILE", index)
        monkeypatch.setattr(vault_utils, "READING_INDEX_FILE", reading_index)

        vault_utils.update_wiki_index(sync_reading_index=True)

        output = index.read_text(encoding="utf-8")
        assert (
            "> [!abstract]- 📅 2026-05-28 | Reading | I3 R5<br>**📂 Article A (2 items)**" in output
        )
        assert "Details" not in output
        assert "> - 💬 Start with the synthesis." in output
        assert "[[Article A (Synthesis)]]" in output

        table = reading_index.read_text(encoding="utf-8")
        assert (
            "| [[Article A (Synthesis)\\|Article A]] | reading | 5 | 3 | Start with the synthesis. |"
            in table
        )

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
            "| Article | Stat | Re | Im | Comment |\n"
            "| --- | --- | --- | --- | --- |\n"
            "| [[Article A]] | reading | 5 |  | Keep this note. |\n"
            "| [[Removed Article]] | skip |  |  | Old row. |\n",
            encoding="utf-8",
        )

        monkeypatch.setattr(vault_utils, "PAGES_DIR", pages)
        monkeypatch.setattr(vault_utils, "NOTES_DIR", notes)
        monkeypatch.setattr(vault_utils, "RAW_CONSOLIDATE_DIR", raw)
        monkeypatch.setattr(vault_utils, "INDEX_FILE", index)
        monkeypatch.setattr(vault_utils, "READING_INDEX_FILE", reading_index)

        vault_utils.update_wiki_index(sync_reading_index=True)

        table = reading_index.read_text(encoding="utf-8")
        assert (
            "| [[Article A (Synthesis)\\|Article A]] | reading | 5 |  | Keep this note. |" in table
        )
        assert "| [[Article B (Synthesis)\\|Article B]] |  |  |  |  |" in table
        assert (
            "| [[Removed Article (Synthesis)\\|Removed Article]] | skip |  |  | Old row. |" in table
        )

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
            "| Article | Stat | Re | Im | Comment |\n"
            "| --- | --- | --- | --- | --- |\n"
            "| [[Article C]] | reading |  |  | compare A\\|B and C:\\\\tmp |\n",
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
        assert "| [[Article D (Synthesis)\\|Article D]] |  |  |  |  |" in reading_index.read_text(
            encoding="utf-8"
        )

    def test_update_wiki_index_does_not_rewrite_reading_index_by_default(
        self, monkeypatch, tmp_path
    ):
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
        assert "| [[Article F (Synthesis)\\|Article F]] |  |  |  |  |" in table
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
            "| Article | Stat | Re | Im | Comment |\n"
            "| --- | --- | --- | --- | --- |\n"
            "| [[Article H]] |  |  |  |  |\n"
            "| [[Old Empty Article]] |  |  |  |  |\n",
            encoding="utf-8",
        )

        monkeypatch.setattr(vault_utils, "PAGES_DIR", pages)
        monkeypatch.setattr(vault_utils, "NOTES_DIR", notes)
        monkeypatch.setattr(vault_utils, "RAW_CONSOLIDATE_DIR", raw)
        monkeypatch.setattr(vault_utils, "INDEX_FILE", index)
        monkeypatch.setattr(vault_utils, "READING_INDEX_FILE", reading_index)

        vault_utils.ensure_wiki_indexes()

        table = reading_index.read_text(encoding="utf-8")
        assert "[[Article H (Synthesis)\\|Article H]]" in table
        assert "Old Empty Article" not in table

    def test_sync_reading_index_skips_write_when_article_order_is_current(
        self, monkeypatch, tmp_path
    ):
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
            "# ReadingIndex\n\nEdit the human-maintained columns. The Article column is regenerated from the vault.\n\n"
            "- Stat: unread, reading, read, parked, skip\n"
            "- Re (Relevance): 1-5, fit for your current question or project\n"
            "- Im (Importance): 1-5, long-term value or objective weight\n"
            "- Comment: short human note for deciding what to read next\n\n"
            "| Article | Stat | Re | Im | Comment |\n"
            "| --- | --- | --- | --- | --- |\n"
            "| [[Article I (Synthesis)\\|Article I]] | reading | 5 |  | User changed this directly. |\n"
            "| [[Article J (Synthesis)\\|Article J]] |  |  |  |  |\n"
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
        assert "| [[Article K (Synthesis)\\|Article K]] |  |  |  |  |" in table

    def test_sync_reading_index_migrates_old_schema(self, monkeypatch, tmp_path):
        pages = tmp_path / "pages"
        notes = tmp_path / "Notes"
        raw = tmp_path / "raw" / "consolidate"
        index = tmp_path / "index.md"
        reading_index = tmp_path / "ReadingIndex.md"

        article_dir = pages / "Article M"
        article_dir.mkdir(parents=True)
        notes.mkdir()
        raw.mkdir(parents=True)
        (article_dir / "Article M.md").write_text("Body", encoding="utf-8")

        # Write old 8-column layout
        reading_index.write_text(
            "# ReadingIndex\n\n"
            "| Article | Status | Priority | Importance | Relevance | Progress | Comment | Updated |\n"
            "| --- | --- | --- | --- | --- | --- | --- | --- |\n"
            "| [[Article M]] | reading | 4 | 3 | 5 | Part 2 | Migrate this note. | 2026-05-28 |\n",
            encoding="utf-8",
        )

        monkeypatch.setattr(vault_utils, "PAGES_DIR", pages)
        monkeypatch.setattr(vault_utils, "NOTES_DIR", notes)
        monkeypatch.setattr(vault_utils, "RAW_CONSOLIDATE_DIR", raw)
        monkeypatch.setattr(vault_utils, "INDEX_FILE", index)
        monkeypatch.setattr(vault_utils, "READING_INDEX_FILE", reading_index)

        vault_utils.ensure_wiki_indexes()

        table = reading_index.read_text(encoding="utf-8")
        # Assert that it was rewritten to the new 5-column layout
        assert "| Article | Stat | Re | Im | Comment |" in table
        # Assert that priority, progress, updated are discarded, and others are migrated
        assert (
            "| [[Article M (Synthesis)\\|Article M]] | reading | 5 | 3 | Migrate this note. |"
            in table
        )

    def test_sync_reading_index_migrates_custom_5_column_schema(self, monkeypatch, tmp_path):
        pages = tmp_path / "pages"
        notes = tmp_path / "Notes"
        raw = tmp_path / "raw" / "consolidate"
        index = tmp_path / "index.md"
        reading_index = tmp_path / "ReadingIndex.md"

        article_dir = pages / "Article N"
        article_dir.mkdir(parents=True)
        notes.mkdir()
        raw.mkdir(parents=True)
        (article_dir / "Article N.md").write_text("Body", encoding="utf-8")

        # Write custom 5-column layout: Status | Im | Re | Comment
        reading_index.write_text(
            "# ReadingIndex\n\n"
            "| Article | Status | Im | Re | Comment |\n"
            "| --- | --- | --- | --- | --- |\n"
            "| [[Article N]] | reading | 4 | 5 | Custom 5-column note. |\n",
            encoding="utf-8",
        )

        monkeypatch.setattr(vault_utils, "PAGES_DIR", pages)
        monkeypatch.setattr(vault_utils, "NOTES_DIR", notes)
        monkeypatch.setattr(vault_utils, "RAW_CONSOLIDATE_DIR", raw)
        monkeypatch.setattr(vault_utils, "INDEX_FILE", index)
        monkeypatch.setattr(vault_utils, "READING_INDEX_FILE", reading_index)

        vault_utils.ensure_wiki_indexes()

        table = reading_index.read_text(encoding="utf-8")
        # Assert it was rewritten to standard 5-column layout
        assert "| Article | Stat | Re | Im | Comment |" in table
        # Assert the Status, Im, Re columns were mapped and aligned correctly
        assert (
            "| [[Article N (Synthesis)\\|Article N]] | reading | 5 | 4 | Custom 5-column note. |"
            in table
        )


class TestRecentBlock:
    """🆕 最近新增 — newest-first block at the top of index.md."""

    def _vault(self, monkeypatch, tmp_path, docs: dict[str, str]):
        """docs maps title → date_created; each becomes a one-page entity."""
        pages = tmp_path / "pages"
        notes = tmp_path / "Notes"
        raw = tmp_path / "raw" / "consolidate"
        notes.mkdir(parents=True)
        raw.mkdir(parents=True)
        for title, date in docs.items():
            d = pages / title
            d.mkdir(parents=True)
            (d / f"{title}.md").write_text(
                f"---\ntitle: {title}\ntags: [t]\ndate_created: '{date}'\n---\n\nBody",
                encoding="utf-8",
            )
        monkeypatch.setattr(vault_utils, "PAGES_DIR", pages)
        monkeypatch.setattr(vault_utils, "NOTES_DIR", notes)
        monkeypatch.setattr(vault_utils, "RAW_CONSOLIDATE_DIR", raw)
        monkeypatch.setattr(vault_utils, "INDEX_FILE", tmp_path / "index.md")
        monkeypatch.setattr(vault_utils, "READING_INDEX_FILE", tmp_path / "ReadingIndex.md")
        return tmp_path / "index.md"

    def test_recent_block_orders_newest_first_and_respects_limit(self, monkeypatch, tmp_path):
        monkeypatch.setattr(vault_utils.settings, "RECENT_COUNT", 2)
        index = self._vault(
            monkeypatch,
            tmp_path,
            {
                "Old Doc": "2026-01-01",
                "Mid Doc": "2026-03-15",
                "New Doc": "2026-06-20",
            },
        )
        vault_utils.update_wiki_index()
        out = index.read_text(encoding="utf-8")

        head = out.split("## 🆕 最近新增", 1)[1].split("##", 1)[0]
        # Only the 2 newest appear, newest first; the oldest is excluded.
        assert head.index("[[New Doc]]") < head.index("[[Mid Doc]]")
        assert "[[Old Doc]]" not in head
        assert "`📅 2026-06-20`" in head
        # The alphabetical Entities list below still carries every doc.
        assert "[[Old Doc]]" in out

    def test_recent_count_zero_disables_block(self, monkeypatch, tmp_path):
        monkeypatch.setattr(vault_utils.settings, "RECENT_COUNT", 0)
        index = self._vault(monkeypatch, tmp_path, {"A Doc": "2026-06-20"})
        vault_utils.update_wiki_index()
        out = index.read_text(encoding="utf-8")
        assert "## 🆕 最近新增" not in out
        assert "[[A Doc]]" in out  # still listed alphabetically below


class TestSanitizeFilename:
    def test_reduces_latex_math_span(self):
        # The reported bug: a LaTeX title split the vault into a phantom dir.
        out = vault_utils.sanitize_filename(
            "數學分析原理：$\\mathcal{L}^2$ 函數空間與瑞斯-費希爾定理"
        )
        assert out == "數學分析原理：L2 函數空間與瑞斯-費希爾定理"
        assert "/" not in out and "\\" not in out and "$" not in out

    def test_strips_path_separators(self):
        assert vault_utils.sanitize_filename("A/B\\C Testing") == "ABC Testing"

    def test_reduces_display_and_paren_math(self):
        assert vault_utils.sanitize_filename("能量 $$E=mc^2$$ 公式") == "能量 E=mc2 公式"
        assert vault_utils.sanitize_filename("極限 \\(x_0\\) 定義") == "極限 x0 定義"

    def test_noop_on_clean_titles(self):
        for t in ("深度學習導論", "data_2024 (Part 3)", "f(x) and g(x)", "2^10 notes"):
            assert vault_utils.sanitize_filename(t) == t

    def test_idempotent(self):
        raw = "積分 $\\int_0^1 f(x)\\,dx$ / 章節"
        once = vault_utils.sanitize_filename(raw)
        assert vault_utils.sanitize_filename(once) == once

    def test_empty_and_length_cap(self):
        assert vault_utils.sanitize_filename("") == ""
        assert len(vault_utils.sanitize_filename("字" * 300)) == 120
