"""CortexAgent — on-demand Cortex validation via `@ling-cortex`.

Runs the three-tier validation harness (pipeline red lines,
consolidation quality, retrieval effect) and drops the full report in
fromLingLing/. The report itself is the artifact — this agent only
triggers it and surfaces the verdict.
"""

from __future__ import annotations

from agents.base_agent import BaseAgent
from core.ui import ui
from maintenance.cortex_validation import run_validation


class CortexAgent(BaseAgent):
    def execute(self, context: dict) -> str:
        report = run_validation(self.rag)
        icon = {"GREEN": "🌸", "YELLOW": "🌼", "RED": "🥀"}.get(report.verdict, "")
        flags = len(report.red_flags) + len(report.yellow_flags)
        report_name = report.report_path.name if report.report_path else "(報告寫入失敗)"
        message = f"{icon} Cortex 驗證：{report.verdict}（{flags} 個警示）→ {report_name}"
        if report.verdict == "RED":
            ui.error(message)
        else:
            ui.success(message)
        return message
