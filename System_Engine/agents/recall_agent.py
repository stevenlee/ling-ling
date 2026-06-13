"""RecallAgent — `@ling-recall`: the system's distilled worldview on a topic.

The READ side of Cortex long-term memory (Phase 5, F2). Unlike `@ling` Q&A
(which answers from raw notes), recall answers from *consolidated beliefs* —
the Cortex claims most relevant to the query, shown WITH their epistemics:
confidence, falsifiability, the concrete falsifier, evidence chain, and any
contradictions. Surfacing the falsifier and contradictions is deliberate: it
keeps recall a self-critical mirror, not a confidence amplifier.
"""

from __future__ import annotations

import re

from agents.base_agent import BaseAgent
from core.config import CORTEX_DIR, CORTEX_RECALL_TOP_K
from core.ui import ui
from services.cortex_recall import recall_claims
from services.cortex_store import load_all_pages

_CMD_TOKEN_RE = re.compile(r'(?:@ling-recall|/recall)\b', re.IGNORECASE)
_STATUS_BADGE = {"active": "🟢", "dormant": "💤", "falsified": "🪦"}


class RecallAgent(BaseAgent):
    def execute(self, context: dict) -> str:
        directive = context.get("user_directive", "") or ""
        query = _CMD_TOKEN_RE.sub("", directive).strip()
        if not query:
            ui.error("🧠 @ling-recall：請在指令後寫下要回想的主題或問題")
            return self._write_report(
                "Cortex Recall",
                "（未提供查詢主題。範例：`@ling-recall 我對 AI agent 協作相信什麼？`）",
                "cortex_recall",
            )[1]

        ui.set_status(f"🧠 回想：{query[:40]}")
        hits = recall_claims(self.rag, query, cortex_dir=CORTEX_DIR, top_k=CORTEX_RECALL_TOP_K)

        # Resolve contradiction claim_ids → claim text (cheap; few dozen pages).
        id_to_claim = {p.claim_id: p.claim for p in load_all_pages(CORTEX_DIR)}

        body = self._render(query, hits, id_to_claim)
        meta = {"query": query, "claims_returned": len(hits)}
        _, full_markdown = self._write_report(
            f"Cortex Recall: {query[:50]}", body, "cortex_recall", meta
        )
        ui.success(f"🧠 回想完成：{len(hits)} 條相關主張 → fromLingLing/")
        return full_markdown

    def _render(self, query: str, hits: list, id_to_claim: dict) -> str:
        lines = [
            f"# 🧠 Cortex 回想：{query}",
            "",
            "> 這是系統**蒸餾過的信念**（Cortex 長期記憶），不是原始筆記檢索。"
            "每條主張附上信心、可反駁性與反例——刻意把不確定性一起攤開。",
            "",
        ]
        if not hits:
            lines.append("（Cortex 中沒有與此主題相關的主張。可能是這個領域還沒累積足夠的洞察。）")
            return "\n".join(lines)

        for rank, (score, page) in enumerate(hits, 1):
            badge = _STATUS_BADGE.get(page.status, "")
            claim = page.claim.strip().replace("\n", " ")
            lines.append(f"## {rank}. {badge} {claim}")
            if page.applies_when:
                lines.append(f"> 適用情境：{page.applies_when}")
            lines.append("")

            fz = "—" if page.falsifiability is None else f"{page.falsifiability:.2f}"
            lines.append(
                f"- **相關度** {score:.2f} · **信心** {page.confidence:.2f} · "
                f"**可反駁性** {fz} · **強度 S** {page.S:.2f} · 狀態 `{page.status}`"
            )
            if page.falsifier:
                lines.append(f"- **反例（能推翻它的觀察）**：{page.falsifier}")

            # Evidence chain — wikilinks back to the source insights.
            ev_links = []
            for ev in (page.evidence or [])[:3]:
                src = ev.get("insight") if isinstance(ev, dict) else None
                if src:
                    stem = src[:-3] if src.endswith(".md") else src
                    ev_links.append(f"[[{stem}]]")
            if ev_links:
                lines.append(f"- **證據**：{' · '.join(ev_links)}")

            # Contradictions — the anti-echo-chamber surface.
            if page.contradictions:
                conflict = []
                for cid in page.contradictions[:3]:
                    txt = id_to_claim.get(cid, cid)
                    conflict.append(txt.strip().replace("\n", " ")[:80])
                lines.append("- ⚔️ **與此衝突**：" + "；".join(conflict))
            lines.append("")

        return "\n".join(lines)
