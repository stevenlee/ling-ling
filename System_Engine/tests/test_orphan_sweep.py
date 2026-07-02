"""Orphan chunk cleanup: ChromaDB must not retain chunks for deleted,
renamed, or moved files. Covers RAGManager.prune_orphan_chunks and the
VaultWatcher events that trigger it (folder deletion, rename/move)."""

from unittest.mock import MagicMock


from services.rag_manager import RAGManager
from watchers.vault_watcher import VaultWatcher


class FakeCollection:
    """Minimal Chroma collection: id -> metadata, delete(ids=...)."""

    def __init__(self, chunks: dict[str, dict]):
        self.chunks = dict(chunks)

    def get(self, include=None, **kwargs):
        ids = list(self.chunks.keys())
        return {"ids": ids, "metadatas": [self.chunks[i] for i in ids]}

    def delete(self, ids=None, where=None):
        for chunk_id in ids or []:
            self.chunks.pop(chunk_id, None)


def _rag_with(chunks: dict[str, dict]) -> RAGManager:
    rag = RAGManager.__new__(RAGManager)
    rag.collection = FakeCollection(chunks)
    rag._bm25 = MagicMock()
    return rag


class TestPruneOrphanChunks:
    def test_removes_chunks_for_vanished_files(self, tmp_path):
        pages = tmp_path / "pages"
        alive = pages / "Alive" / "Alive (Synthesis).md"
        alive.parent.mkdir(parents=True)
        alive.write_text("x", encoding="utf-8")

        alive_id = RAGManager._get_doc_id(alive)
        rag = _rag_with(
            {
                "a_1": {"doc_id": alive_id, "title": "Alive (Synthesis)"},
                "d_1": {"doc_id": "deadbeef" * 8, "title": "Deleted Article"},
                "d_2": {"doc_id": "deadbeef" * 8, "title": "Deleted Article"},
            }
        )

        result = rag.prune_orphan_chunks(roots=[pages])

        assert result["scanned"] == 3
        assert result["deleted_chunks"] == 2
        assert result["orphan_docs"] == 1
        assert result["titles"] == ["Deleted Article"]
        assert set(rag.collection.chunks) == {"a_1"}
        rag._bm25.mark_dirty.assert_called_once()

    def test_sweeps_legacy_chunks_without_doc_id(self, tmp_path):
        rag = _rag_with({"legacy_1": {"title": "Old Format"}})
        result = rag.prune_orphan_chunks(roots=[tmp_path / "pages"])
        assert result["deleted_chunks"] == 1
        assert rag.collection.chunks == {}

    def test_noop_when_everything_alive(self, tmp_path):
        pages = tmp_path / "pages"
        f = pages / "A.md"
        pages.mkdir()
        f.write_text("x", encoding="utf-8")
        rag = _rag_with({"a_1": {"doc_id": RAGManager._get_doc_id(f), "title": "A"}})

        result = rag.prune_orphan_chunks(roots=[pages])

        assert result["deleted_chunks"] == 0
        assert set(rag.collection.chunks) == {"a_1"}
        rag._bm25.mark_dirty.assert_not_called()


class FakeDirEvent:
    def __init__(self, path, dest=None, is_directory=True):
        self.is_directory = is_directory
        self.src_path = str(path)
        if dest is not None:
            self.dest_path = str(dest)


class TestVaultWatcherOrphanTriggers:
    def _watcher(self, monkeypatch, tmp_path):
        pages = tmp_path / "pages"
        notes = tmp_path / "Notes"
        pages.mkdir()
        notes.mkdir()
        from core import config

        monkeypatch.setattr(config, "PAGES_DIR", pages)
        monkeypatch.setattr(config, "NOTES_DIR", notes)
        watcher = VaultWatcher(MagicMock())
        return watcher, pages

    def test_folder_deletion_schedules_sweep(self, monkeypatch, tmp_path):
        watcher, pages = self._watcher(monkeypatch, tmp_path)
        scheduled = []
        monkeypatch.setattr(watcher, "_schedule_orphan_sweep", lambda *a, **k: scheduled.append(1))

        watcher.on_deleted(FakeDirEvent(pages / "Some Article"))

        assert scheduled == [1]

    def test_unrelated_folder_deletion_ignored(self, monkeypatch, tmp_path):
        watcher, _ = self._watcher(monkeypatch, tmp_path)
        scheduled = []
        monkeypatch.setattr(watcher, "_schedule_orphan_sweep", lambda *a, **k: scheduled.append(1))

        watcher.on_deleted(FakeDirEvent(tmp_path / "raw" / "stuff"))

        assert scheduled == []

    def test_file_rename_deletes_old_and_indexes_new(self, monkeypatch, tmp_path):
        watcher, pages = self._watcher(monkeypatch, tmp_path)
        deletions, indexed = [], []
        monkeypatch.setattr(
            watcher, "_process_deletion", lambda title, **k: deletions.append(title)
        )
        monkeypatch.setattr(
            watcher,
            "_schedule_process",
            lambda fp, title, delay: indexed.append(title),
        )

        watcher.on_moved(
            FakeDirEvent(
                pages / "Old Name.md",
                dest=pages / "New Name.md",
                is_directory=False,
            )
        )

        assert deletions == ["Old Name"]
        assert indexed == ["New Name"]

    def test_sweep_runs_prune_under_busy_lock(self, monkeypatch, tmp_path):
        watcher, _ = self._watcher(monkeypatch, tmp_path)
        watcher.rag.prune_orphan_chunks.return_value = {
            "scanned": 5,
            "orphan_docs": 1,
            "deleted_chunks": 2,
            "titles": ["X"],
        }
        monkeypatch.setattr("core.vault_utils.update_wiki_index", MagicMock())

        watcher._process_orphan_sweep()

        watcher.rag.prune_orphan_chunks.assert_called_once()
        from core.state import global_busy_state

        assert not global_busy_state.is_busy()

    def test_sweep_retries_when_busy(self, monkeypatch, tmp_path):
        watcher, _ = self._watcher(monkeypatch, tmp_path)
        monkeypatch.setattr("watchers.vault_watcher.global_busy_state.try_set_busy", lambda: False)
        try:
            watcher._process_orphan_sweep()
            watcher.rag.prune_orphan_chunks.assert_not_called()
            assert "orphan::sweep" in watcher._timers
        finally:
            for timer in watcher._timers.values():
                timer.cancel()
