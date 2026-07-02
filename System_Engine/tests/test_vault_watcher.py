from pathlib import Path
from unittest.mock import MagicMock


import core.vault_utils as vault_utils
from watchers.vault_watcher import VaultWatcher


class FakeEvent:
    def __init__(self, path: Path):
        self.is_directory = False
        self.src_path = str(path)


class TestVaultWatcherReadingIndex:
    def test_reading_index_is_watched_but_not_indexed(self, monkeypatch, tmp_path):
        reading_index = tmp_path / "ReadingIndex.md"
        reading_index.write_text("# ReadingIndex\n", encoding="utf-8")

        rag = MagicMock()
        watcher = VaultWatcher(rag)
        mock_update = MagicMock()

        monkeypatch.setattr(vault_utils, "READING_INDEX_FILE", reading_index)
        monkeypatch.setattr("watchers.vault_watcher.global_busy_state.is_busy", lambda: False)
        monkeypatch.setattr("core.vault_utils.update_wiki_index", mock_update)

        assert watcher._should_watch(reading_index) is True
        assert watcher._should_index(reading_index) is False
        assert watcher._should_refresh_index_only(reading_index) is True

        watcher._process_modification(reading_index, "ReadingIndex")

        mock_update.assert_called_once_with(reading_index, "ReadingIndex")
        rag.add_document.assert_not_called()

    def test_reading_index_modified_uses_fast_refresh_delay(self, monkeypatch, tmp_path):
        reading_index = tmp_path / "ReadingIndex.md"
        reading_index.write_text("# ReadingIndex\n", encoding="utf-8")

        watcher = VaultWatcher(MagicMock())
        mock_schedule = MagicMock()

        monkeypatch.setattr(vault_utils, "READING_INDEX_FILE", reading_index)
        monkeypatch.setattr(watcher, "_schedule_process", mock_schedule)

        watcher.on_modified(FakeEvent(reading_index))

        mock_schedule.assert_called_once_with(reading_index, "ReadingIndex", delay=2.0)

    def test_reading_index_deleted_refreshes_without_rag_delete(self, monkeypatch, tmp_path):
        reading_index = tmp_path / "ReadingIndex.md"
        rag = MagicMock()
        watcher = VaultWatcher(rag)
        mock_update = MagicMock()

        monkeypatch.setattr(vault_utils, "READING_INDEX_FILE", reading_index)
        monkeypatch.setattr("core.vault_utils.update_wiki_index", mock_update)

        watcher.on_deleted(FakeEvent(reading_index))

        mock_update.assert_called_once_with(reading_index, "ReadingIndex", sync_reading_index=True)
        rag.delete_document.assert_not_called()

    def test_deletion_retries_instead_of_running_without_lock(self, monkeypatch, tmp_path):
        rag = MagicMock()
        watcher = VaultWatcher(rag)

        monkeypatch.setattr("watchers.vault_watcher.global_busy_state.try_set_busy", lambda: False)

        try:
            watcher._process_deletion("Article L")

            # Lock unavailable: RAG must NOT be touched; a retry must be scheduled.
            rag.delete_document.assert_not_called()
            assert "del::Article L" in watcher._timers
        finally:
            for timer in watcher._timers.values():
                timer.cancel()

    def test_deletion_gives_up_after_max_retries(self, monkeypatch, tmp_path):
        rag = MagicMock()
        watcher = VaultWatcher(rag)

        monkeypatch.setattr("watchers.vault_watcher.global_busy_state.try_set_busy", lambda: False)

        watcher._process_deletion("Article L", attempt=10)

        rag.delete_document.assert_not_called()
        assert "del::Article L" not in watcher._timers

    def test_regular_file_deleted_syncs_reading_index(self, monkeypatch, tmp_path):
        regular_file = tmp_path / "Article L.md"
        rag = MagicMock()
        watcher = VaultWatcher(rag)
        mock_update = MagicMock()

        monkeypatch.setattr(vault_utils, "PAGES_DIR", tmp_path)
        monkeypatch.setattr(vault_utils, "NOTES_DIR", tmp_path)
        monkeypatch.setattr(watcher, "_should_watch", lambda x: True)
        monkeypatch.setattr("core.vault_utils.update_wiki_index", mock_update)

        watcher.on_deleted(FakeEvent(regular_file))

        rag.delete_document.assert_called_once_with("Article L")
        mock_update.assert_called_once_with(sync_reading_index=True)
