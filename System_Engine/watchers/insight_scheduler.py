"""Backward-compatible import for the old InsightScheduler name."""

from __future__ import annotations

from pathlib import Path

from watchers.maintenance_scheduler import MaintenanceScheduler


class InsightScheduler(MaintenanceScheduler):
    """Compatibility wrapper; insight is now a MaintenanceScheduler task."""

    def __init__(self, project_root: Path, llm, rag):
        super().__init__(project_root, llm, rag)
