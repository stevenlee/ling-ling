"""VisualizeAgent — `@ling-visualize`: render a note as the best learning aid.

Phase 6 (learning-artifacts axis), on-demand entry. Resolves a [[note]],
classifies its cognitive structure, and emits the matching artifact
(comparison table / Mermaid diagram of the right kind / …) — or says plainly
that the content has no strong visual structure. `as <type>` forces a type.
"""

from __future__ import annotations

import re

from agents.base_agent import BaseAgent
from core.config import PAGES_DIR, WIKI_VAULT_DIR
from core.vault_utils import sanitize_filename
from core.ui import ui
from services.learning_artifacts import ARTIFACT_TYPES, build_artifact

_CMD_TOKEN_RE = re.compile(r"(?:@ling-visualize|/visualize)\b", re.IGNORECASE)
_WIKILINK_RE = re.compile(r"\[\[(.*?)\]\]")
_AS_TYPE_RE = re.compile(r"\bas\s+([a-z_]+)", re.IGNORECASE)


class VisualizeAgent(BaseAgent):
    def execute(self, context: dict) -> str:
        directive = context.get("user_directive", "") or ""
        titles = [t.split("|")[0].strip() for t in _WIKILINK_RE.findall(directive)]
        forced = None
        m = _AS_TYPE_RE.search(_CMD_TOKEN_RE.sub("", directive))
        if m:
            raw_forced = m.group(1).lower()
            if raw_forced in ARTIFACT_TYPES or raw_forced == "all":
                forced = raw_forced

        if not titles:
            ui.error("🖼️ @ling-visualize：請用 [[筆記名]] 指定要視覺化的對象")
            return self._write_report(
                "Error",
                "（未指定對象。範例：`@ling-visualize [[某篇筆記]]` 或 `@ling-visualize [[X]] as timeline`）",
                "vis",
            )[1]

        title = titles[0]
        text, source = self._load_note(title)
        if not text:
            ui.error(f"🖼️ 找不到筆記：{title}")
            return self._write_report(
                title,
                f"# 🖼️ {title}\n\n找不到這篇筆記的內容（pages/ 或 Notes/）。",
                "vis",
            )[1]

        ui.set_status(f"🖼️ 視覺化：{title[:40]}")

        if forced == "all":
            body = self._render_all(title, source, text)
            artifact_type_meta = "all"
        else:
            result = build_artifact(self.llm, text, forced_type=forced)
            body = self._render(title, source, result)
            artifact_type_meta = result["type"]

        _, full_markdown = self._write_report(
            title,
            body,
            "vis",
            {"target": title, "artifact_type": artifact_type_meta},
        )
        ui.success(f"🖼️ 完成：{title} → {artifact_type_meta} → fromLingLing/")
        return full_markdown

    def _render_all(self, title: str, source: str, text: str) -> str:
        L = [
            f"# 🖼️ 學習輔助（全部測試）：{title}",
            "",
            "此報告嘗試套用系統支援的所有圖表類型。若結構不合適導致無法產生，則會標示為「不適用」。",
            "",
        ]
        for t, desc in ARTIFACT_TYPES.items():
            if t == "none":
                continue

            result = build_artifact(self.llm, text, forced_type=t)

            L.append(f"## {t}")
            L.append(f"> 類型說明：{desc}")
            L.append("")

            if not result.get("artifact"):
                L.append("**Not Applicable (不適用)**")
                if t == "argument_map":
                    L.append("（這篇內容沒有可辨識的論證結構）")
                else:
                    L.append("（產生時驗證失敗，或缺乏此圖表所需的資料結構）")
            else:
                L.append(result["artifact"])
            L.append("")
            L.append("---")
            L.append("")

        return "\n".join(L)

    def _render(self, title: str, source: str, result: dict) -> str:
        t = result["type"]
        L = [
            f"# 🖼️ 學習輔助：{title}",
            "",
            f"> 產物類型：**{t}**（{ARTIFACT_TYPES.get(t, '')}）"
            + (f"　理由：{result['reason']}" if result.get("reason") else ""),
            "",
        ]
        if t == "none":
            L.append(
                "這篇內容沒有明顯的視覺結構（流程／比較／時序／階層／論證）,"
                "硬畫成圖反而會誤導,因此不產生圖表。可改用 `@ling-visualize [[X]] as <type>` 指定類型。"
            )
        elif not result.get("artifact"):
            if t == "argument_map":
                L.append(
                    "這篇內容沒有可辨識的論證結構（不是在主張／論證什麼）,因此無法產生論證圖。"
                    "論證圖適合評論、立場、申論型內容；說明或敘述型內容請改用其他類型。"
                )
            else:
                L.append("（產生圖表時驗證失敗,已略過以免輸出壞掉的圖。可重試或指定其他類型。）")
        else:
            L.append(result["artifact"])
        return "\n".join(L)

    # ── Note resolution (canonical readable text for a title) ───────────
    def _load_note(self, title: str) -> tuple[str, str]:
        title = sanitize_filename(title.strip())
        if not title:
            return "", ""
        folder = PAGES_DIR / title
        if folder.is_dir():
            for pattern in (f"{title} (Stitched).md", f"{title} (Synthesis).md", f"{title}.md"):
                p = folder / pattern
                if p.exists():
                    return p.read_text(encoding="utf-8"), str(p)
            parts = sorted(folder.glob("*.md"))
            if parts:
                return "\n\n---\n\n".join(p.read_text(encoding="utf-8") for p in parts), str(folder)
        direct = PAGES_DIR / f"{title}.md"
        if direct.exists():
            return direct.read_text(encoding="utf-8"), str(direct)
        notes = WIKI_VAULT_DIR / "Notes" / f"{title}.md"
        if notes.exists():
            return notes.read_text(encoding="utf-8"), str(notes)
        for md in PAGES_DIR.rglob(f"*{title}*.md"):
            return md.read_text(encoding="utf-8"), str(md)
        return "", ""
