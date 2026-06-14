"""VisualizeAgent — `@ling-visualize`: render a note as the best learning aid.

Phase 6 (learning-artifacts axis), on-demand entry. Resolves a [[note]],
classifies its cognitive structure, and emits the matching artifact
(comparison table / Mermaid diagram of the right kind / …) — or says plainly
that the content has no strong visual structure. `as <type>` forces a type.
"""

from __future__ import annotations

import re
from pathlib import Path

from agents.base_agent import BaseAgent
from core.config import PAGES_DIR, WIKI_VAULT_DIR
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
        if m and m.group(1).lower() in ARTIFACT_TYPES:
            forced = m.group(1).lower()

        if not titles:
            ui.error("🖼️ @ling-visualize：請用 [[筆記名]] 指定要視覺化的對象")
            return self._write_report(
                "Visualize",
                "（未指定對象。範例：`@ling-visualize [[某篇筆記]]` 或 `@ling-visualize [[X]] as timeline`）",
                "visualize",
            )[1]

        title = titles[0]
        text, source = self._load_note(title)
        if not text:
            ui.error(f"🖼️ 找不到筆記：{title}")
            return self._write_report(
                f"Visualize: {title}",
                f"# 🖼️ {title}\n\n找不到這篇筆記的內容（pages/ 或 Notes/）。",
                "visualize",
            )[1]

        ui.set_status(f"🖼️ 視覺化：{title[:40]}")
        result = build_artifact(self.llm, text, forced_type=forced)
        body = self._render(title, source, result)
        _, full_markdown = self._write_report(
            f"Visualize: {title}", body, "visualize",
            {"target": title, "artifact_type": result["type"]},
        )
        ui.success(f"🖼️ 完成：{title} → {result['type']} → fromLingLing/")
        return full_markdown

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
            L.append("這篇內容沒有明顯的視覺結構（流程／比較／時序／階層／論證）,"
                     "硬畫成圖反而會誤導,因此不產生圖表。可改用 `@ling-visualize [[X]] as <type>` 指定類型。")
        elif not result.get("artifact"):
            L.append("（產生圖表時驗證失敗,已略過以免輸出壞掉的圖。可重試或指定其他類型。）")
        else:
            L.append(result["artifact"])
        return "\n".join(L)

    # ── Note resolution (canonical readable text for a title) ───────────
    def _load_note(self, title: str) -> tuple[str, str]:
        title = title.strip()
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
