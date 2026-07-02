"""Pytest configuration for System_Engine tests.

Adds a --run-live-llm flag that controls whether tests marked
`@pytest.mark.live_llm` actually run. Default behaviour: skip them, so the
normal test suite never makes real LLM calls.
"""

import pytest


def pytest_addoption(parser):
    parser.addoption(
        "--run-live-llm",
        action="store_true",
        default=False,
        help="Run tests that make real LLM API calls.",
    )
    parser.addoption(
        "--update-snapshots",
        action="store_true",
        default=False,
        help="Overwrite saved chunker snapshots instead of comparing.",
    )


@pytest.fixture
def update_snapshots(request):
    """Fixture: True when pytest was invoked with --update-snapshots."""
    return bool(request.config.getoption("--update-snapshots"))


@pytest.fixture(autouse=True)
def _no_leak_daemon_status(monkeypatch):
    """Stop tests from leaking busy state into the live daemon_status.json.

    Tests that exercise an agent's .execute() hit the real `ui` singleton,
    whose set_status() mirrors {busy, message} to disk for the TUI. Without
    this, e.g. the visualize test persists 'busy: true · 視覺化：Some Doc' and
    the TUI shows that stale message indefinitely (the daemon only overwrites
    it when it does its own work)."""
    try:
        import core.ui as cui

        monkeypatch.setattr(cui.ui, "_persist_status", lambda *a, **k: None, raising=False)
    except Exception:
        pass


def pytest_collection_modifyitems(config, items):
    if config.getoption("--run-live-llm"):
        return
    skip = pytest.mark.skip(reason="needs --run-live-llm to run real LLM scoring")
    for item in items:
        if "live_llm" in item.keywords:
            item.add_marker(skip)
