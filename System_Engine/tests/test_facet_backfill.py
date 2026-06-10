"""Facet backfill pump: low-priority idle work that yields to user work,
derives its queue from the DB, and goes silent when everything is done."""
import sys
import time
from pathlib import Path
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).parent.parent.absolute()))

import pytest

import maintenance.facet_backfill as fb_mod
from maintenance.facet_backfill import FacetBackfillPump, parse_digest_appendix


PART_NOTE = """---
title: Book (Part 3)
---
# Body

content here — padded so the page clears the min-bytes filter.
Lorem ipsum dolor sit amet, consectetur adipiscing elit, sed do eiusmod
tempor incididunt ut labore et dolore magna aliqua.

## 🧩 Part Digest Appendix

> 每個 Part 的結構化摘要。

### Part 3: Chapter title

- **Thesis**: The chapter argues that memory is reconstructive.
- **Key Points**:
  - Recall rewrites the trace each time.
  - Confidence does not track accuracy.
- **Terms**:
  - reconsolidation
"""


class FakeLLM:
    def __init__(self):
        self.digest_calls = []
        self.fail = False

    def generate_part_digest(self, title, n, total, raw, note, pending=""):
        self.digest_calls.append(title)
        if self.fail:
            raise RuntimeError("provider down")
        return {"thesis": f"Thesis for {title}.", "key_points": ["A solid key point."]}


class FakeRAG:
    def __init__(self, facet_titles=None):
        self.facet_titles = set(facet_titles or [])
        self.added = []

    def get_facet_entries(self):
        return [{"title": t} for t in self.facet_titles]

    def add_facets(self, path, title, facets, tags=None):
        self.added.append((title, facets))


@pytest.fixture
def env(tmp_path, monkeypatch):
    pages = tmp_path / "pages"
    notes = tmp_path / "Notes"
    to_llm = tmp_path / "toLingLing"
    consolidate = tmp_path / "Consolidate"
    for d in (pages, notes, to_llm, consolidate):
        d.mkdir()
    monkeypatch.setattr(fb_mod, "PAGES_DIR", pages)
    monkeypatch.setattr(fb_mod, "NOTES_DIR", notes)
    monkeypatch.setattr(fb_mod, "TO_LLM_DIR", to_llm)
    monkeypatch.setattr(fb_mod, "CONSOLIDATE_DIR", consolidate)
    monkeypatch.setattr(fb_mod, "MAINTENANCE_LOG_FILE", tmp_path / "maintenance.log.md")
    # Quiet busy state: not busy, try_set_busy succeeds, release is a no-op.
    busy = MagicMock()
    busy.is_busy.return_value = False
    busy.try_set_busy.return_value = True
    monkeypatch.setattr(fb_mod, "global_busy_state", busy)
    return tmp_path, pages, notes, to_llm, consolidate, busy


def _pump(llm, rag, tmp_path, **kw):
    defaults = dict(
        state_file=tmp_path / "state.json", enabled=True,
        grace_seconds=180, step_gap_seconds=30, daily_budget=1000,
    )
    defaults.update(kw)
    pump = FacetBackfillPump(llm, rag, **defaults)
    pump._kicks = []
    pump.kick = lambda delay=None, replace=True: pump._kicks.append(delay)
    return pump


def _page(dir_path, name, content="x" * 600):
    f = dir_path / f"{name}.md"
    f.write_text(content, encoding="utf-8")
    return f


class TestAppendixParse:
    def test_extracts_thesis_and_key_points(self):
        digest = parse_digest_appendix(PART_NOTE)
        assert digest["thesis"] == "The chapter argues that memory is reconstructive."
        assert digest["key_points"] == [
            "Recall rewrites the trace each time.",
            "Confidence does not track accuracy.",
        ]

    def test_no_appendix_returns_none(self):
        assert parse_digest_appendix("# Just a page\n\nbody") is None


class TestQueue:
    def test_priority_and_exclusions(self, env):
        tmp_path, pages, notes, *_ = env
        _page(pages, "Book (Synthesis)")
        _page(pages, "Book (Part 1)")
        _page(pages, "Book (Stitched)")          # excluded: stitched
        _page(notes, "My Note")
        _page(pages, "Covered (Synthesis)")       # excluded: already has facets
        _page(pages, "_tagScrapbook")             # excluded: underscore
        _page(pages, "Stub", content="tiny")      # excluded: too small

        rag = FakeRAG(facet_titles=["Covered (Synthesis)"])
        pump = _pump(FakeLLM(), rag, tmp_path)
        queue = pump._build_queue()
        titles = [t for _, _, t in queue]
        assert titles == ["Book (Synthesis)", "My Note", "Book (Part 1)"]

    def test_recently_retrieved_prioritized(self, env):
        tmp_path, pages, notes, *_ = env
        _page(notes, "Hot Note")
        _page(notes, "Cold Note")
        llm = FakeLLM()
        llm.trace_store = MagicMock()
        llm.trace_store.recently_retrieved_titles.return_value = {"Hot Note"}
        pump = _pump(llm, FakeRAG(), tmp_path)
        titles = [t for _, _, t in pump._build_queue()]
        assert titles == ["Hot Note", "Cold Note"]


class TestYielding:
    def test_busy_means_no_work(self, env):
        tmp_path, pages, *_ , busy = env
        _page(pages, "A (Synthesis)")
        busy.is_busy.return_value = True
        pump = _pump(FakeLLM(), FakeRAG(), tmp_path)
        pump._run_step()
        busy.try_set_busy.assert_not_called()

    def test_fresh_inbox_file_yields(self, env):
        tmp_path, pages, _, to_llm, _, busy = env
        _page(pages, "A (Synthesis)")
        (to_llm / "@ling-insight do it.md").write_text("x", encoding="utf-8")
        pump = _pump(FakeLLM(), FakeRAG(), tmp_path)
        pump._run_step()
        busy.try_set_busy.assert_not_called()

    def test_stale_inbox_file_does_not_starve(self, env):
        tmp_path, pages, _, to_llm, _, busy = env
        _page(pages, "A (Synthesis)", content=PART_NOTE)
        stuck = to_llm / "broken.md"
        stuck.write_text("x", encoding="utf-8")
        import os
        old = time.time() - 3600
        os.utime(stuck, (old, old))

        rag = FakeRAG()
        pump = _pump(FakeLLM(), rag, tmp_path)
        pump._run_step()
        assert len(rag.added) == 1  # the stuck file didn't block us


class TestStep:
    def test_part_page_parsed_without_llm(self, env):
        tmp_path, pages, *_ = env
        _page(pages, "Book (Part 3)", content=PART_NOTE)
        llm, rag = FakeLLM(), FakeRAG()
        pump = _pump(llm, rag, tmp_path)
        pump._run_step()

        assert llm.digest_calls == []                 # zero LLM cost
        title, facets = rag.added[0]
        assert title == "Book (Part 3)"
        assert "The chapter argues that memory is reconstructive." in facets
        assert pump._ledger["budget"]["used"] == 0    # nothing charged

    def test_plain_page_uses_one_llm_call_and_charges_budget(self, env):
        tmp_path, _, notes, *_ = env
        _page(notes, "Plain Note")
        llm, rag = FakeLLM(), FakeRAG()
        pump = _pump(llm, rag, tmp_path)
        pump._run_step()

        assert llm.digest_calls == ["Plain Note"]
        assert rag.added[0][0] == "Plain Note"
        assert pump._ledger["budget"]["used"] == 1

    def test_next_step_scheduled_with_gap_while_work_remains(self, env):
        tmp_path, pages, *_ = env
        _page(pages, "A (Synthesis)", content=PART_NOTE)
        _page(pages, "B (Synthesis)", content=PART_NOTE)
        pump = _pump(FakeLLM(), FakeRAG(), tmp_path)
        pump._run_step()
        assert pump._kicks == [30]


class TestFailureHandling:
    def test_quarantine_after_max_attempts_and_mtime_requalify(self, env):
        tmp_path, _, notes, *_ = env
        page = _page(notes, "Cursed Note")
        llm, rag = FakeLLM(), FakeRAG()
        llm.fail = True
        pump = _pump(llm, rag, tmp_path, max_attempts=3)

        for _ in range(3):
            pump._queue = [(2, page, "Cursed Note")]
            pump._run_step()

        assert "Cursed Note" in pump._ledger["quarantine"]
        assert pump._build_queue() == []              # quarantined → excluded

        # Editing the file requalifies it.
        import os
        future = time.time() + 10
        os.utime(page, (future, future))
        assert [t for _, _, t in pump._build_queue()] == ["Cursed Note"]

    def test_global_backoff_after_distinct_failures(self, env):
        tmp_path, _, notes, *_ = env
        llm = FakeLLM()
        llm.fail = True
        pump = _pump(llm, FakeRAG(), tmp_path)
        for name in ("N1", "N2", "N3"):
            page = _page(notes, name)
            pump._queue = [(2, page, name)]
            pump._run_step()
        assert pump._backoff_until > time.time()

    def test_budget_exhausted_schedules_tomorrow(self, env):
        tmp_path, _, notes, *_ = env
        _page(notes, "Plain Note")
        pump = _pump(FakeLLM(), FakeRAG(), tmp_path, daily_budget=0)
        pump._run_step()
        assert pump._kicks and pump._kicks[0] > 0     # resume scheduled
        assert FakeRAG().added == []


class TestCompletion:
    def test_empty_queue_logs_once_then_silent(self, env):
        tmp_path, *_ , busy = env
        pump = _pump(FakeLLM(), FakeRAG(), tmp_path)
        pump._run_step()
        assert pump._ledger["completed_logged"] is True
        log = (tmp_path / "maintenance.log.md").read_text(encoding="utf-8")
        assert "Facet Backfill" in log

        pump._run_step()                              # second time: silent
        assert log == (tmp_path / "maintenance.log.md").read_text(encoding="utf-8")
        busy.try_set_busy.assert_not_called()         # never grabbed the lock
