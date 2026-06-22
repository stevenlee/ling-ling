"""BlogAgent — `@ling-blog`: deliver curated reviews into the kafu blog repo.

The ling-ling "push" half of the publish flow (mirrors `make blog`): runs the
blog_transform pass over lings-desktop/Blog/ and writes web-ready Quartz
markdown into the kafu repo's content/. Touches only local files — no LLM, no
network, and deliberately NO build and NO push (those are kafu's side, run
explicitly via `make publish`).

Target kafu repo: $KAFU env var if set, else ~/projects/kafu (matches the
Makefile's `KAFU ?= $(HOME)/projects/kafu`).
"""

from __future__ import annotations

import os
from datetime import date
from pathlib import Path

from agents.base_agent import BaseAgent
from core.config import WIKI_VAULT_DIR
from core.ui import ui
from services.blog_transform import publish_blog


def _kafu_content_dir() -> Path:
    return Path(os.environ.get("KAFU") or (Path.home() / "projects" / "kafu")) / "content"


class BlogAgent(BaseAgent):
    def execute(self, context: dict) -> str:
        blog_dir = WIKI_VAULT_DIR / "Blog"
        content_dir = _kafu_content_dir()

        if not content_dir.parent.parent.exists():
            ui.error(f"🪷 找不到 kafu repo：{content_dir.parent}")
            return self._write_report(
                "Blog",
                f"# 🪷 發布到 kafu\n\n找不到 kafu repo（`{content_dir.parent}`）。"
                "請確認它存在，或設定 `KAFU` 環境變數指向正確路徑。",
                "blog",
            )[1]

        sources = [f for f in blog_dir.glob("*.md") if not f.name.startswith("_")] if blog_dir.exists() else []
        if not sources:
            ui.error("🪷 Blog/ 沒有可發布的 review")
            return self._write_report(
                "Blog",
                "# 🪷 發布到 kafu\n\n`lings-desktop/Blog/` 裡沒有 review。"
                "把核可要發布的 `@ling-review` 產出複製進 Blog/，再跑一次。",
                "blog",
            )[1]

        ui.set_status(f"🪷 轉換 {len(sources)} 篇 → kafu/content/")
        try:
            written = publish_blog(blog_dir, content_dir, date.today().isoformat())
        except Exception as e:  # pragma: no cover - surfaced to the user as a report
            ui.error(f"🪷 轉換失敗：{e}")
            return self._write_report("Blog", f"# 🪷 發布到 kafu\n\n轉換失敗：`{e}`", "blog")[1]

        listing = "\n".join(f"- `{p.name}`" for p in written)
        body = (
            f"# 🪷 發布到 kafu\n\n"
            f"已把 **{len(written)}** 篇轉成 Quartz 內容，送進 `{content_dir}`：\n\n"
            f"{listing}\n\n"
            f"## 下一步\n"
            f"到 kafu 上線（build + push）：\n\n"
            f"```bash\ncd {content_dir.parent} && make publish\n```\n\n"
            f"或先 `make preview` 在本機看一眼。"
        )
        _, full_markdown = self._write_report(
            "Blog", body, "blog", {"published": len(written), "target": str(content_dir)},
        )
        ui.success(f"🪷 完成：{len(written)} 篇 → kafu/content/（下一步在 kafu 跑 make publish）")
        return full_markdown
