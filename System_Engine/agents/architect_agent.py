"""ArchitectAgent — `@ling-architect [[packed-note]]`: map a system's architecture.

Reads a `packed-code` note (from `tools/pack_code.py`; vault-only, like
CodeReviewAgent). A deterministic `ast` pre-scan extracts structural facts
(modules, top-level classes/functions, internal/external imports) and feeds them
to the LLM so a weak model transcribes the structure instead of guessing it. The
report (overview / module map / flows / state / deps / risks, with Mermaid
diagrams) goes through path A: persona=coder × operation=map_architecture ×
template=architecture-rpt. Output lands in fromLingLing/, never in pages/.
"""

from __future__ import annotations

import re

import yaml

from agents.base_agent import BaseAgent
from core.config import CODE_REVIEW_DIR, PAGES_DIR, WIKI_VAULT_DIR
from core.ui import ui
from core.vault_utils import sanitize_filename
from services.architecture_scan import format_facts, scan_architecture
from services.code_identifier_guard import correct_code_identifiers

_WIKILINK_RE = re.compile(r"\[\[(.*?)\]\]")
_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)
_LEADING_FM_RE = re.compile(r"^\s*```ya?ml\s*\n.*?\n```\s*|^\s*---\s*\n.*?\n---\s*\n?", re.DOTALL)

_MAX_CODE_CHARS = 8000  # bounded code appended after the facts table


class ArchitectAgent(BaseAgent):
    def execute(self, context: dict) -> str:
        directive = context.get("user_directive", "") or ""
        titles = [t.split("|")[0].strip() for t in _WIKILINK_RE.findall(directive)]
        if not titles:
            ui.error("🔔 @ling-architect：請用 [[打包筆記名]] 指定要測繪的對象")
            return self._write_report(
                "Error",
                "（未指定對象。先跑 `make pack-code SRC=...` 打包，再 `@ling-architect [[名稱]]`）",
                "architecture",
            )[1]

        title = sanitize_filename(titles[0].strip())
        text, identifiers, is_packed = self._load(title)
        if not text:
            ui.error(f"🔔 找不到可測繪的內容：{title}")
            return self._write_report(
                title,
                (
                    f"# 🔔 {title}\n\n找不到 `CodeReview/{title}.md`（打包筆記），"
                    "也找不到同名的 vault 筆記。\n請先 `make pack-code SRC=<路徑>` 再來測繪。"
                ),
                "architecture",
            )[1]

        if is_packed:
            facts = format_facts(scan_architecture(text))
            body_in = f"{facts}\n\n# 原始碼(節錄)\n\n{text[:_MAX_CODE_CHARS]}"
            ui.set_status(f"🔔 架構測繪：{title[:40]}")
        else:
            facts = ""
            body_in = text[: _MAX_CODE_CHARS * 2]
            ui.set_status(f"🔔 架構測繪(一般筆記)：{title[:40]}")

        report = (
            self.llm.answer_query(
                query_content=(
                    f"為《{title}》繪製系統架構報告。先依『結構事實』列出元件,再畫模組地圖與"
                    "關鍵流程(必要時狀態機),圖與文字必須一致,看不到的部分明說,不要臆測。"
                ),
                wiki_context=body_in,
                forced_template="architecture-rpt",
                persona="coder",
                operation="map_architecture",
                temperature=0.3,
            )
            or ""
        )
        body_out = self._strip_leading_frontmatter(report) or "（產生報告時 LLM 呼叫失敗。）"
        body_out, fixes = correct_code_identifiers(body_out, identifiers)
        if fixes:
            ui.info(f"🔔 識別符校正 {len(fixes)} 處")

        _, full = self._write_report(
            title,
            body_out,
            "architecture",
            {"target": title, "packed": is_packed, "identifier_fixes": len(fixes)},
        )
        ui.success(f"🔔 完成：{title} → 架構報告 → fromLingLing/")
        return full

    # ── loading ────────────────────────────────────────────────────────
    def _load(self, title: str) -> tuple[str, list[str], bool]:
        """Return (text, identifiers, is_packed). Prefer a packed note; fall
        back to a same-named vault note (no ast scan)."""
        packed = CODE_REVIEW_DIR / f"{title}.md"
        if packed.exists():
            raw = packed.read_text(encoding="utf-8")
            return raw, self._packed_identifiers(raw), True
        for cand in (
            PAGES_DIR / title / f"{title} (Synthesis).md",
            PAGES_DIR / f"{title}.md",
            WIKI_VAULT_DIR / "Notes" / f"{title}.md",
        ):
            if cand.exists():
                return cand.read_text(encoding="utf-8"), [], False
        return "", [], False

    @staticmethod
    def _packed_identifiers(raw: str) -> list[str]:
        m = _FRONTMATTER_RE.match(raw)
        if not m:
            return []
        try:
            fm = yaml.safe_load(m.group(1)) or {}
        except yaml.YAMLError:
            return []
        ids = fm.get("identifiers")
        return [str(x) for x in ids] if isinstance(ids, list) else []

    @staticmethod
    def _strip_leading_frontmatter(text: str) -> str:
        return _LEADING_FM_RE.sub("", text, count=1).strip()
