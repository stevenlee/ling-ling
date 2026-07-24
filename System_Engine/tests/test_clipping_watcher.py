"""R7-G-2: ClippingWatcher processes off the watchdog dispatch thread."""

import os
import queue
import threading
from pathlib import Path
from types import SimpleNamespace

os.environ.setdefault("LLM_PROVIDER", "vllm")

import watchers.clipping_watcher as cw_mod
from watchers.clipping_watcher import ClippingWatcher
from services.ingest.result import DocumentIngestResult


def _archiver(tmp_path, monkeypatch):
    """Minimal watcher + tmp archive root + captured warnings for the
    sidecar-archival tests."""
    raw = tmp_path / "raw_consolidate"
    monkeypatch.setattr(cw_mod, "RAW_CONSOLIDATE_DIR", raw)
    warnings = []
    monkeypatch.setattr(cw_mod.ui, "warning", lambda m: warnings.append(m))
    cons = tmp_path / "Consolidate"
    cons.mkdir()
    return ClippingWatcher.__new__(ClippingWatcher), raw, cons, warnings


def _img(path: Path):
    path.write_bytes(b"\xff\xd8\xff")  # suffix is what matters; content irrelevant


def test_canonical_sidecar_layout_archived(tmp_path, monkeypatch):
    w, raw, cons, warnings = _archiver(tmp_path, monkeypatch)
    (cons / "images" / "Doc").mkdir(parents=True)
    md = cons / "Doc.md"
    md.write_text("# x", encoding="utf-8")
    _img(cons / "images" / "Doc" / "a.jpeg")

    w._archive_markdown_with_sidecar_images(md)

    assert (raw / "Doc.md").exists()
    assert (raw / "images" / "Doc" / "a.jpeg").exists()
    assert not (cons / "images" / "Doc").exists()
    assert warnings == []


def test_flat_imageonly_folder_fallback_archived(tmp_path, monkeypatch):
    """Upstream dropped the images/ parent → flat Consolidate/<stem>/ of
    images. It must still be archived (and silently)."""
    w, raw, cons, warnings = _archiver(tmp_path, monkeypatch)
    (cons / "Doc").mkdir()
    md = cons / "Doc.md"
    md.write_text("# x", encoding="utf-8")
    _img(cons / "Doc" / "_page_1_Picture_1.jpeg")

    w._archive_markdown_with_sidecar_images(md)

    assert (raw / "Doc.md").exists()
    assert (raw / "images" / "Doc" / "_page_1_Picture_1.jpeg").exists()
    assert not (cons / "Doc").exists()
    assert warnings == []


def test_flat_nonimage_folder_not_swept_but_warns(tmp_path, monkeypatch):
    """A same-named folder that isn't image-only must NOT be swept — but the
    leftover should be surfaced, not left silently."""
    w, raw, cons, warnings = _archiver(tmp_path, monkeypatch)
    (cons / "Doc").mkdir()
    md = cons / "Doc.md"
    md.write_text("# x", encoding="utf-8")
    (cons / "Doc" / "notes.txt").write_text("keep me", encoding="utf-8")

    w._archive_markdown_with_sidecar_images(md)

    assert (raw / "Doc.md").exists()
    assert not (raw / "images" / "Doc").exists()  # not swept
    assert (cons / "Doc").exists()  # left in place
    assert warnings and "Doc" in warnings[0]  # but surfaced


def _watcher():
    # Build without the real IngestionPipeline — the worker tests only exercise
    # the queue/worker plumbing.
    w = ClippingWatcher.__new__(ClippingWatcher)
    w._job_queue = queue.Queue()
    w._queued_paths = set()
    w._queue_lock = threading.Lock()
    w._wake = threading.Event()
    w._stop = threading.Event()
    w._worker = None
    w._stability_delay = 0
    return w


def test_handle_event_enqueues_without_processing(tmp_path):
    w = _watcher()
    drained = []
    w._drain_queue = lambda: drained.append(1)
    f = tmp_path / "clip.md"
    f.write_text("hi", encoding="utf-8")

    w._handle_event(SimpleNamespace(is_directory=False, src_path=str(f)))

    assert str(f) in w._queued_paths
    assert w._wake.is_set()
    assert drained == []  # not processed on the dispatch thread


def test_dotfile_and_unsupported_ignored(tmp_path):
    w = _watcher()
    for name in (".hidden.md", "@tmp.md", "note.txt"):
        f = tmp_path / name
        f.write_text("x", encoding="utf-8")
        w._handle_event(SimpleNamespace(is_directory=False, src_path=str(f)))
    assert w._queued_paths == set()  # .md dotfiles/@ and unsupported .txt skipped
    assert not w._wake.is_set()


def test_worker_drains_off_thread(tmp_path):
    w = _watcher()
    drained = threading.Event()
    w._drain_queue = lambda: drained.set()
    w.start()
    try:
        f = tmp_path / "clip.md"
        f.write_text("hi", encoding="utf-8")
        w._handle_event(SimpleNamespace(is_directory=False, src_path=str(f)))
        assert drained.wait(timeout=3)
    finally:
        w.stop()
    assert not w._worker.is_alive()


def test_partial_markdown_result_is_not_archived(tmp_path, monkeypatch):
    w, raw, cons, _ = _archiver(tmp_path, monkeypatch)
    source = cons / "partial.md"
    source.write_text("# source", encoding="utf-8")
    w.pipeline = SimpleNamespace(
        ingest_markdown=lambda *_: DocumentIngestResult(
            ok=False,
            status="partial",
            stage="distill_parts",
            expected_parts=2,
            completed_parts=[1],
            archivable=False,
            detail="part 2 failed",
        )
    )

    w._handle_markdown(source)

    assert source.exists()
    assert not (raw / source.name).exists()


def test_complete_markdown_result_is_archived(tmp_path, monkeypatch):
    w, raw, cons, _ = _archiver(tmp_path, monkeypatch)
    source = cons / "complete.md"
    source.write_text("# source", encoding="utf-8")
    w.pipeline = SimpleNamespace(
        ingest_markdown=lambda *_: DocumentIngestResult(
            ok=True,
            status="complete",
            stage="done",
            expected_parts=2,
            completed_parts=[1, 2],
            archivable=True,
        )
    )

    w._handle_markdown(source)

    assert not source.exists()
    assert (raw / source.name).exists()
