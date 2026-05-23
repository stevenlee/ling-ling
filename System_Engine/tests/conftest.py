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


def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        "live_llm: marks tests that make real LLM API calls (skipped unless --run-live-llm)",
    )


@pytest.fixture
def update_snapshots(request):
    """Fixture: True when pytest was invoked with --update-snapshots."""
    return bool(request.config.getoption("--update-snapshots"))


def pytest_collection_modifyitems(config, items):
    if config.getoption("--run-live-llm"):
        return
    skip = pytest.mark.skip(reason="needs --run-live-llm to run real LLM scoring")
    for item in items:
        if "live_llm" in item.keywords:
            item.add_marker(skip)
