"""Packaging configuration must include runtime subpackages in built wheels."""

import tomllib
from pathlib import Path

from setuptools import find_packages


PROJECT_ROOT = Path(__file__).parents[2]


def test_setuptools_discovery_includes_runtime_subpackages():
    config = tomllib.loads((PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    find_config = config["tool"]["setuptools"]["packages"]["find"]
    discovered = set(find_packages(where=PROJECT_ROOT / find_config["where"][0]))

    assert {
        "agents.insight",
        "core.parsing",
        "maintenance.migrations",
        "services.ingest",
        "services.llm",
        "services.rag",
        "services.scout",
        "services.scout.parsers",
    } <= discovered
