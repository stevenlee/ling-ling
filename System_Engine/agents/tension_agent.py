"""TensionAgent — `@ling-tensions`: surface the tensions in long-term memory.

Cortex Phase 5, F3. The anti-echo-chamber counterpart to `@ling-recall`:
recall says "what do I believe"; tensions says "where is that belief in
conflict, unfalsifiable, thinly-evidenced, or already disproven". Pure scan —
no LLM, no embedding — so it works regardless of retrieval quality.
"""

from __future__ import annotations

from agents.base_agent import BaseAgent
from core.config import CORTEX_DIR
from core.ui import ui
from services.cortex_tensions import scan_tensions

_STATUS_BADGE = {"active": "🌸", "dormant": "💤", "falsified": "🍂"}


class TensionAgent(BaseAgent):
    def execute(self, context: dict) -> str:
        ui.set_status("💬 掃描知識張力…")
        report = scan_tensions(CORTEX_DIR)
        counts = {
            "contradictions": len(report.contradictions),
            "dogmatic": len(report.dogmatic),
            "thin_evidence": len(report.thin_evidence),
            "falsified": len(report.falsified),
        }
        # be58079 swapped _render(report) for report.format_markdown() here —
        # but TensionReport never had that method, so @ling-tensions crashed
        # with AttributeError from 2026-06-28 until this fix. _render is the
        # real renderer (and what test_cortex_tensions pins).
        _, full_markdown = self._write_report(
            "Tensions", self._render(report), "ctx-tension", counts
        )
        flagged = sum(counts.values())
        ui.success(f"💬 張力掃描完成：{flagged} 處（{report.total_pages} 頁）→ fromLingLing/")
        return full_markdown

    def _render(self, r) -> str:
        L = [
            "# 💬 Cortex 知識張力",
            "",
            "> 這不是「我相信什麼」，而是「我的知識在哪裡有張力」——矛盾、不可反駁、"
            "證據單薄、已被推翻。把異議攤開，是對抗自我印證的解藥。",
            "",
            f"掃描 {r.total_pages} 頁 Cortex。",
            "",
        ]
        if not r.any:
            L.append("目前沒有偵測到張力（語料還小，或知識尚未累積到產生衝突）。")
            return "\n".join(L)

        def claim_line(p):
            return p.claim.strip().replace("\n", " ")

        if r.contradictions:
            L += ["## 💬 矛盾對（活的異議）", ""]
            for p, others in r.contradictions:
                L.append(f"- {_STATUS_BADGE.get(p.status, '')} **{claim_line(p)}**")
                for o in others:
                    L.append(f"  - ↔ 與之衝突：{o}")
            L.append("")

        if r.dogmatic:
            L += [
                "## 🧱 教條風險（高信心、低可反駁性）",
                "",
                "> 這些主張**信得高、卻難以被推翻**——同溫層的結構性燃料。優先重新檢視或補上可檢驗的反例。",
                "",
            ]
            for p in r.dogmatic:
                fz = "—" if p.falsifiability is None else f"{p.falsifiability:.2f}"
                L.append(f"- {claim_line(p)}（信心 {p.confidence:.2f}；可反駁性 {fz}）")
            L.append("")

        if r.thin_evidence:
            L += ["## 🪶 證據單薄（≤1 來源）", ""]
            for p in r.thin_evidence:
                L.append(f"- {claim_line(p)}")
            L.append("")

        if r.falsified:
            L += ["## 🍂 已被推翻（透明保留）", ""]
            for p in r.falsified:
                line = f"- ~~{claim_line(p)}~~"
                if p.counterpoints:
                    line += f"　死因：{'; '.join(c[:80] for c in p.counterpoints[:2])}"
                L.append(line)
            L.append("")

        return "\n".join(L)
