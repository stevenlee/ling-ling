"""ReviewAgent — `@ling-review [[note]]`: turn a note's Synthesis into a
learning-first blog review/report in the 報導者／書評人 voice.

Pairs the shared `reviewer` persona with the `review` operation and a per-genre
review template (book / explainer / paper / patent), then snaps any mangled
identifiers back to the canonical via identifier_guard. Output lands in
fromLingLing/ — it is NOT written into pages/, so it never re-enters ingestion.

Genre: `as <genre>` forces it; a patent number in the title auto-selects
`patent`; otherwise it defaults to `book`.
"""

from __future__ import annotations

import re

from agents.base_agent import BaseAgent
from core.config import PAGES_DIR
from core.vault_utils import sanitize_filename
from core.ui import ui
from services.identifier_guard import correct_identifiers, extract_identifiers

_WIKILINK_RE = re.compile(r"\[\[(.*?)\]\]")
_AS_GENRE_RE = re.compile(r"\bas\s+([a-z-]+)", re.IGNORECASE)
_FENCED_YAML_RE = re.compile(r"^```ya?ml\s*\n.*?\n```\s*", re.DOTALL)
_RAW_FRONTMATTER_RE = re.compile(r"^---\s*\n.*?\n---\s*\n?", re.DOTALL)

# Canonical genre → review template name.
_GENRE_TEMPLATES = {
    "book": "book-review",
    "explainer": "explainer-report",
    "paper": "paper-review",
    "patent": "patent-review",
}
# Accepted `as <alias>` keywords → canonical genre.
_GENRE_ALIASES = {
    "book": "book",
    "explainer": "explainer",
    "report": "explainer",
    "topic": "explainer",
    "paper": "paper",
    "research": "paper",
    "patent": "patent",
}
# Synthesis sections that are scaffolding, not understanding — trimmed from input.
_NAV_APPENDIX_MARKERS = (
    "## 🔗 原始溯源",
    "## 🧩 Part Digest Appendix",
    "原始溯源",
    "Part Digest Appendix",
    "## 📂 Navigation",
    "## 🔗 Navigation",
)


class ReviewAgent(BaseAgent):
    def execute(self, context: dict) -> str:
        directive = context.get("user_directive", "") or ""
        titles = context.get("target_titles") or [
            t.split("|")[0].strip() for t in _WIKILINK_RE.findall(directive)
        ]
        if not titles:
            ui.error("📝 @ling-review：請用 [[筆記名]] 指定要評論的對象")
            return self._write_report(
                "Review",
                "（未指定對象。範例：`@ling-review [[某本書]]` 或 `@ling-review [[某論文]] as paper`）",
                "rev",
            )[1]

        title = titles[0]
        syn_text, syn_path = self._load_synthesis(title)
        if not syn_text:
            ui.error(f"📝 找不到 Synthesis：{title}")
            return self._write_report(
                title,
                (
                    f"# 📝 {title}\n\n找不到這篇的 Synthesis"
                    f"（pages/{title}/{title} (Synthesis).md）。\n"
                    "請先讓它走完 ingestion 產生 Synthesis，再來 review。"
                ),
                "rev",
            )[1]

        genre = self._pick_genre(directive, title)
        template = _GENRE_TEMPLATES[genre]

        ui.set_status(f"📝 書評／報導：{title[:40]} ({genre})")
        review = (
            self.llm.answer_query(
                query_content=(
                    f"為《{title}》寫一篇『幫助學習』的部落格評論/報導。"
                    "評論並教讀者『關於』這個來源——絕不重製或取代它。"
                ),
                wiki_context=self._synthesis_body(syn_text),
                forced_template=template,
                persona="reviewer",
                operation="rev",
                temperature=0.5,
            )
            or ""
        )

        # The template makes the model emit its own YAML header; strip it so
        # _write_report owns the single canonical frontmatter (no duplication).
        body = self._strip_leading_frontmatter(review)

        # Deterministic identifier correction (gemma4:26b mangles them even when
        # the value is present verbatim in its input — see identifier_guard).
        canon = extract_identifiers(title, syn_path)
        body, fixes = correct_identifiers(body, canon)
        if fixes:
            ui.info(f"📝 識別碼校正 {len(fixes)} 處 → {canon}")

        _, full_markdown = self._write_report(
            title,
            body,
            "rev",
            {"target": title, "genre": genre, "template": template, "identifier_fixes": len(fixes)},
        )
        ui.success(f"📝 完成：{title} → {genre} → fromLingLing/")
        return full_markdown

    # ── genre selection ────────────────────────────────────────────────
    def _pick_genre(self, directive: str, title: str) -> str:
        m = _AS_GENRE_RE.search(_WIKILINK_RE.sub("", directive))
        if m and m.group(1).lower() in _GENRE_ALIASES:
            return _GENRE_ALIASES[m.group(1).lower()]
        # Reliable auto-detect: a patent number in the title ⇒ patent.
        if extract_identifiers(title):
            return "patent"
        return "book"

    # ── synthesis loading ──────────────────────────────────────────────
    @staticmethod
    def _load_synthesis(title: str) -> tuple[str, str]:
        title = sanitize_filename(title.strip())
        if not title:
            return "", ""
        p = PAGES_DIR / title / f"{title} (Synthesis).md"
        if p.exists():
            return p.read_text(encoding="utf-8"), str(p)
        for cand in PAGES_DIR.glob(f"*{title}*/*(Synthesis).md"):
            return cand.read_text(encoding="utf-8"), str(cand)
        return "", ""

    @classmethod
    def _synthesis_body(cls, text: str) -> str:
        body = cls._strip_leading_frontmatter(text)
        for marker in _NAV_APPENDIX_MARKERS:
            i = body.find(marker)
            if i != -1:
                body = body[:i]
        return body.strip()

    @staticmethod
    def _strip_leading_frontmatter(text: str) -> str:
        t = (text or "").strip()
        t = _FENCED_YAML_RE.sub("", t, count=1)
        t = _RAW_FRONTMATTER_RE.sub("", t, count=1)
        return t.strip()
