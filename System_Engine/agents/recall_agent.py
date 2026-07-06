"""RecallAgent — `@ling-recall`: the system's distilled worldview on a topic.

The READ side of Cortex long-term memory (Phase 5, F2). Unlike `@ling` Q&A
(which answers from raw notes), recall answers from *consolidated beliefs*.

Design (after live feedback): at Cortex's current scale the whole corpus fits
in one prompt, so recall feeds ALL claims to the LLM and lets it select +
answer — that handles typos ("Hibert"→Hilbert), conceptual matches, and
conversational framing that embedding/BM25 retrieval over a tiny corpus could
not. Retrieval (hybrid `recall_claims`) is only a pre-filter once the corpus
outgrows the context budget. The answer deliberately surfaces falsifiers and
contradictions — a self-critical mirror, not a confidence amplifier.
"""

from __future__ import annotations

import re

from agents.base_agent import BaseAgent
from core.config import (
    CORTEX_DIR,
    CORTEX_RECALL_LLM_MAX,
    CORTEX_RECALL_PREFILTER,
)
from core.ui import ui
from services.cortex_recall import recall_claims
from services.cortex_store import load_all_pages

_CMD_TOKEN_RE = re.compile(r"(?:@ling-recall|/recall)\b", re.IGNORECASE)
_STATUS_BADGE = {"active": "🌸", "dormant": "💤", "falsified": "🍂"}
_CITE_RE = re.compile(r"#(\d+)")

# Fallback only — the canonical prompt lives in Templates/Prompts/agent_recall.md
# (editable in the vault, hot-reloaded via the prompt mtime cache). Keep it in
# sync with that file, including the de-hardcoded language rule (#3): output
# language comes from the pinned OUTPUT-LANGUAGE banner (complete(pin_language=
# True) at the call site), not a literal 繁體中文.
_FALLBACK_SYSTEM_PROMPT = (
    "你是 Ling-Ling 的長期記憶回想介面。使用者給一個主題，user message 裡附上系統蒸餾過的"
    "所有信念（每條編號 [#N]，含信心/可反駁性/反例）。\n\n"
    "規則：\n"
    "1. 挑出**真正相關**的主張，用 [#N] 引用。寧缺勿濫——若沒有相關的，只回一句"
    "「Cortex 中沒有與此主題相關的信念。」\n"
    "2. 容錯：使用者可能拼錯字或用不同說法，依語意對應（例如把 Hibert 對應到 Hilbert）。\n"
    "3. 用你自己的話綜述「關於這個主題，系統相信什麼」，主動點出不確定性與矛盾"
    "（自我批判的鏡子，不是附和）。每個論點標 [#N] 溯源。\n\n"
    "嚴格輸出要求：**只輸出最終綜述本身**。不要輸出你的推理過程、自我檢查、草稿、"
    "逐條 relevant/irrelevant 清單，也不要任何圖表（Mermaid）。直接從綜述第一句開始。"
)


class RecallAgent(BaseAgent):
    def execute(self, context: dict) -> str:
        directive = context.get("user_directive", "") or ""
        query = _CMD_TOKEN_RE.sub("", directive).strip()
        if not query:
            ui.error("📓 @ling-recall：請在指令後寫下要回想的主題或問題")
            return self._write_report(
                "Error",
                "（未指定回想主題。範例：`@ling-recall [[某主題]]`）",
                "ctx-recall",
            )[1]

        ui.set_status(f"📓 回想：{query[:40]}")
        pages = [
            p
            for p in load_all_pages(CORTEX_DIR)
            if p.claim.strip() and p.status in ("active", "dormant")
        ]
        if not pages:
            return self._write_report(
                query,
                f"# 📓 Cortex Recall\n\n對「{query}」沒有足夠的記憶。",
                "ctx-recall",
                {"query": query, "claims_returned": 0},
            )[1]

        # Whole corpus when it fits; hybrid pre-filter only when it doesn't.
        if len(pages) <= CORTEX_RECALL_LLM_MAX:
            candidates = pages
        else:
            candidates = [
                p
                for _, p in recall_claims(
                    self.rag, query, cortex_dir=CORTEX_DIR, top_k=CORTEX_RECALL_PREFILTER
                )
            ] or pages[:CORTEX_RECALL_PREFILTER]

        numbered = list(enumerate(candidates, 1))
        source_block = self._claims_block(numbered)
        # Lean completion (NOT answer_query): a caller-supplied system prompt
        # with no template/persona/visualization scaffolding, so the model
        # selects + summarizes instead of chasing a Mermaid diagram.
        user_msg = f"主題：{query}\n\n系統的所有信念：\n{source_block}"
        loaded = self._load_prompt("agent_recall", required=True)
        if not loaded:
            self.stats["used_fallback_prompt"] = True
        system_prompt = loaded or _FALLBACK_SYSTEM_PROMPT
        answer = (
            self.llm.complete(
                system_prompt,
                user_msg,
                temperature=0.2,
                stage="cortex_recall",
                pin_language=True,
            )
            or "（回想時 LLM 呼叫失敗。）"
        )

        body = self._render(query, answer, numbered)
        _, full_markdown = self._write_report(
            query,
            body,
            "ctx-recall",
            {"query": query, "candidates": len(candidates)},
        )
        ui.success(f"📓 回想完成：掃過 {len(candidates)} 條信念 → fromLingLing/")
        return full_markdown

    @staticmethod
    def _claims_block(numbered: list) -> str:
        lines = []
        for n, p in numbered:
            fz = "—" if p.falsifiability is None else f"{p.falsifiability:.2f}"
            parts = [
                f"[#{n}] {p.claim.strip()}",
                f"（信心 {p.confidence:.2f}；可反駁性 {fz}；狀態 {p.status}）",
            ]
            if p.falsifier:
                parts.append(f"反例：{p.falsifier}")
            lines.append(" ".join(parts))
        return "\n".join(lines)

    def _render(self, query: str, answer: str, numbered: list) -> str:
        lines = [
            f"# 📓 Cortex 回想：{query}",
            "",
            "> 這是 LLM 讀過系統**蒸餾信念**後的綜述（不是原始筆記檢索）。每個論點標 [#編號] 溯源。",
            "",
            answer.strip(),
        ]
        cited = sorted({int(m) for m in _CITE_RE.findall(answer) if 1 <= int(m) <= len(numbered)})
        if cited:
            by_num = dict(numbered)
            lines += ["", "---", "## 📎 引用的主張（含知識論）", ""]
            for n in cited:
                p = by_num[n]
                badge = _STATUS_BADGE.get(p.status, "")
                fz = "—" if p.falsifiability is None else f"{p.falsifiability:.2f}"
                lines.append(f"- **[#{n}]** {badge} {p.claim.strip()}")
                lines.append(
                    f"  - 信心 {p.confidence:.2f} · 可反駁性 {fz} · S {p.S:.2f} · `{p.status}`"
                )
                if p.falsifier:
                    lines.append(f"  - 反例：{p.falsifier}")
                ev = [
                    f"[[{(e.get('insight') or '')[:-3] if (e.get('insight') or '').endswith('.md') else e.get('insight')}]]"
                    for e in (p.evidence or [])[:3]
                    if isinstance(e, dict) and e.get("insight")
                ]
                if ev:
                    lines.append(f"  - 證據：{' · '.join(ev)}")
        return "\n".join(lines)
