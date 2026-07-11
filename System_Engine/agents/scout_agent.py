"""ScoutAgent — `@ling-scout`: run the Scout crawl → digest on demand.

The scheduled `scout_daily` task runs the same `run_scout_digest` path
nightly; this is the manual "去偵查一趟" trigger (works regardless of the
`scout` Scripture switch, like other on-demand commands). The digest writes
its own report — this agent only relays status, it does not write a second
file. Dedupe state is shared with the nightly run, so a manual crawl at noon
simply means tonight's run finds fewer new items.
"""

from __future__ import annotations

from agents.base_agent import BaseAgent
from core.ui import ui


class ScoutAgent(BaseAgent):
    def execute(self, context: dict) -> str:
        from services.scout.digest import run_scout_digest

        ui.set_status("🔭 Scout 出動偵查中…")
        result = run_scout_digest(self.llm, rag=self.rag)
        if result.report_path is not None:
            ui.success(f"🔭 Scout 回報：{result.summary} → {result.report_path.name}")
        elif result.status == "succeeded":
            ui.success(f"🔭 Scout 回報：{result.summary}")
        else:
            ui.warning(f"🔭 Scout：{result.summary}")
        return result.summary
