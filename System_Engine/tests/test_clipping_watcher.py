"""R7-G-2: ClippingWatcher processes off the watchdog dispatch thread."""
import os
import queue
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).parent.parent.absolute()))
os.environ.setdefault("LLM_PROVIDER", "vllm")

from watchers.clipping_watcher import ClippingWatcher


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
    assert drained == []                  # not processed on the dispatch thread


def test_dotfile_and_unsupported_ignored(tmp_path):
    w = _watcher()
    for name in (".hidden.md", "@tmp.md", "note.txt"):
        f = tmp_path / name
        f.write_text("x", encoding="utf-8")
        w._handle_event(SimpleNamespace(is_directory=False, src_path=str(f)))
    assert w._queued_paths == set()       # .md dotfiles/@ and unsupported .txt skipped
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
