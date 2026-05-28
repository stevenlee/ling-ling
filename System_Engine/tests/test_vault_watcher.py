import sys
from pathlib import Path
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).parent.parent.absolute()))

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

        mock_update.assert_called_once_with(reading_index, "ReadingIndex")
        rag.delete_document.assert_not_called()
