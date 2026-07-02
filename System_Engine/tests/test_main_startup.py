"""main.py startup wiring — the composition root, under heavy mocks (P4).

Pins the invariants the daemon boot depends on: construction order, translator
injection, watchdog scheduling, worker/scheduler startup, idle-callback
registration, pump kicks, and clean shutdown on KeyboardInterrupt. No real
LLM/DB/filesystem watching is involved.
"""

from unittest.mock import MagicMock

import pytest

import main as main_mod
from core.state import BusyState


@pytest.fixture
def wired(monkeypatch, tmp_path):
    """Run main.main() fully mocked; return the mocks for assertions."""
    m = {
        "llm_cls": MagicMock(name="LLMClient"),
        "rag_cls": MagicMock(name="RAGManager"),
        "clip_cls": MagicMock(name="ClippingWatcher"),
        "prompt_cls": MagicMock(name="PromptWatcher"),
        "vault_cls": MagicMock(name="VaultWatcher"),
        "sched_cls": MagicMock(name="MaintenanceScheduler"),
        "observer": MagicMock(name="Observer"),
    }
    monkeypatch.setattr(main_mod, "LLMClient", m["llm_cls"])
    monkeypatch.setattr(main_mod, "RAGManager", m["rag_cls"])
    monkeypatch.setattr(main_mod, "ClippingWatcher", m["clip_cls"])
    monkeypatch.setattr(main_mod, "PromptWatcher", m["prompt_cls"])
    monkeypatch.setattr(main_mod, "VaultWatcher", m["vault_cls"])
    monkeypatch.setattr(main_mod, "MaintenanceScheduler", m["sched_cls"])
    monkeypatch.setattr(
        main_mod.watchdog.observers, "Observer", MagicMock(return_value=m["observer"])
    )

    # Environment side effects → no-ops
    monkeypatch.setattr(main_mod, "acquire_pid_lock", MagicMock())
    monkeypatch.setattr(main_mod, "ensure_directories", MagicMock())
    monkeypatch.setattr(main_mod, "ensure_wiki_indexes", MagicMock())
    monkeypatch.setattr(main_mod.settings, "reload", MagicMock())
    monkeypatch.setattr("maintenance.migrate.apply_pending", lambda rag: [])
    monkeypatch.setattr("maintenance.facet_backfill.FacetBackfillPump", MagicMock())
    monkeypatch.setattr("maintenance.daydream.DaydreamPump", MagicMock())

    # Isolated busy state (don't pollute the real singleton's callback list)
    state = BusyState()
    state.lock_file = tmp_path / ".kb_lock"
    monkeypatch.setattr(main_mod, "global_busy_state", state)

    # Break the run loop immediately → exercises the shutdown path
    monkeypatch.setattr(main_mod.time, "sleep", MagicMock(side_effect=KeyboardInterrupt))

    main_mod.main()
    return m, state


def test_translator_is_injected_at_construction(wired):
    m, _ = wired
    llm = m["llm_cls"].return_value
    m["rag_cls"].assert_called_once_with(translator=llm.translate_query)


def test_watchers_receive_shared_services(wired):
    m, _ = wired
    llm = m["llm_cls"].return_value
    rag = m["rag_cls"].return_value
    m["clip_cls"].assert_called_once_with(llm, rag)
    m["prompt_cls"].assert_called_once_with(llm, rag)
    m["vault_cls"].assert_called_once_with(rag, llm)


def test_observer_schedules_and_starts(wired):
    m, _ = wired
    assert m["observer"].schedule.call_count >= 5  # consolidate/toLing/pages/notes/cortex/...
    m["observer"].start.assert_called_once()


def test_workers_and_scheduler_started(wired):
    m, _ = wired
    m["prompt_cls"].return_value.start.assert_called_once()
    m["clip_cls"].return_value.start.assert_called_once()
    m["sched_cls"].return_value.start.assert_called_once()


def test_startup_scan_runs_both_inboxes(wired):
    m, _ = wired
    m["clip_cls"].return_value.scan_existing.assert_called()
    m["prompt_cls"].return_value.scan_existing.assert_called()


def test_idle_callbacks_registered_in_priority_order(wired):
    m, state = wired
    # Order encodes priority: user inbox scans first, pumps last.
    cbs = state._idle_callbacks
    assert cbs[0] == m["clip_cls"].return_value.scan_existing
    assert cbs[1] == m["prompt_cls"].return_value.scan_existing
    assert len(cbs) == 4  # + facet pump + daydream pump


def test_shutdown_stops_observer_and_workers(wired):
    m, _ = wired
    m["observer"].stop.assert_called_once()
    m["prompt_cls"].return_value.stop.assert_called_once()
    m["clip_cls"].return_value.stop.assert_called_once()
