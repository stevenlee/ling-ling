"""DigAgent — `@ling-dig <url>`: deep-dive one URL from a Scout report.

Companion to `@ling-scout`: the digest stays shallow (one page per item);
when something in it looks interesting, drop `@ling-dig <url>` and Scout
goes back, follows the few links actually worth following, and writes a
proper deep-dive note. Human picks the target — no crawl budget is spent
guessing what matters.
"""

from __future__ import annotations

import logging

from agents.base_agent import BaseAgent
from core.ui import ui


class DigAgent(BaseAgent):
    ERROR_LABEL = "Scout 深掘失敗"

    def execute(self, context: dict) -> str:
        from core.config import SCOUT_MIRROR_DIR, settings
        from services.scout.dig import first_url, run_dig

        url = first_url(context.get("user_directive", ""))
        if not url:
            return self._error_report(
                "指令裡找不到網址。用法：`@ling-dig https://…`（取第一個出現的 URL）。"
            )

        ui.set_status(f"🔭 Scout 深掘中：{url}")
        language = getattr(settings, "SCOUT_LANGUAGE", "") or settings.OUTPUT_LANGUAGE
        result = run_dig(self.llm, url, language=language)
        if result.status != "succeeded":
            return self._error_report(f"{result.summary}（目標：{url}）")

        body = self._compose_body(url, result)
        _, full_markdown = self._write_report(result.title, body, "Dig", {"source_url": url})
        if getattr(settings, "SCOUT_MIRROR", True):
            self._mirror(SCOUT_MIRROR_DIR, result.title, full_markdown)
        ui.success(f"🔭 深掘完成：{result.title} → fromLingLing/")
        return full_markdown

    def _compose_body(self, url: str, result) -> str:
        parts = [f"# 🔍 Scout 深掘：{result.title}", "", f"> 目標：{url}"]
        if result.via == "wayback":
            parts.append("> 💧 原站拒絕抓取，內文取自 Wayback Machine 快照（未跟進站內連結）。")
        parts += ["", result.body, ""]

        followed = [s for s in result.followed]
        if followed:
            parts += ["## 🧹 跟進的連結", ""]
            for source in followed:
                if source.content:
                    parts.append(f"- [{source.label}]({source.url})")
                else:
                    parts.append(f"- [{source.label}]({source.url})：未取得（{source.error}）")
            parts.append("")

        related = self._related_notes(result)
        if related:
            parts += ["## 🌱 相關筆記", ""]
            parts += [f"- [[{title}]]" for title in related]
            parts.append("")
        return "\n".join(parts)

    def _related_notes(self, result) -> list[str]:
        # Same conservative gate as the digest's bridging (P2.3).
        from services.scout.digest import BRIDGE_MAX_DISTANCE, is_own_report

        if self.rag is None:
            return []
        try:
            hits = self.rag.query_notes(f"{result.title}\n{result.body[:400]}", top_k=3)
        except Exception as e:
            logging.warning(f"Dig: bridging query failed: {e}")
            return []
        titles: list[str] = []
        for hit in hits or []:
            title = str((hit.get("metadata") or {}).get("title") or "").strip()
            distance = hit.get("distance")
            if not title or title in titles or is_own_report(title):
                continue
            if isinstance(distance, (int, float)) and distance > BRIDGE_MAX_DISTANCE:
                continue
            titles.append(title)
        return titles

    def _mirror(self, mirror_dir, title: str, full_markdown: str) -> None:
        from core.vault_utils import sanitize_filename

        try:
            mirror_dir.mkdir(parents=True, exist_ok=True)
            (mirror_dir / f"✅Dig-{sanitize_filename(title)}.md").write_text(
                full_markdown, encoding="utf-8"
            )
        except Exception as e:
            logging.warning(f"Dig: mirror failed: {e}")
