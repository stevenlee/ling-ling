"""CodeReviewAgent — `@ling-code-review [[packed-note]]`: review packed source.

Reads a `packed-code` note produced by `tools/pack_code.py` (vault-only: the
agent never touches files outside lings-desktop/). Map → reduce → report:

  1. map    — one lean `_complete_json` pass per file/chunk extracts findings
              (identifier-anchored, never line-numbered — gemma4:26b invents
              line numbers; a function/class name can be snapped back).
  2. reduce — deterministic dedup by (file, anchor, category), capped by severity.
  3. report — path A: answer_query(persona=coder × operation=review_code ×
              template=code-review-rpt), then correct_code_identifiers snaps any
              mangled identifier back to the packed note's ast-harvested manifest.

Output lands in fromLingLing/ — never in pages/, so it never re-enters ingestion.
"""

from __future__ import annotations

import re

import yaml

from agents.base_agent import BaseAgent
from core.config import CODE_REVIEW_DIR
from core.ui import ui
from core.vault_utils import sanitize_filename
from services.code_identifier_guard import correct_code_identifiers

_WIKILINK_RE = re.compile(r"\[\[(.*?)\]\]")
_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)
_LEADING_FM_RE = re.compile(r"^\s*```ya?ml\s*\n.*?\n```\s*|^\s*---\s*\n.*?\n---\s*\n?", re.DOTALL)

# Control-prompt for the extraction pass (stays in code per the prompt-system
# boundary: JSON extraction is control, not content). The report voice comes
# from the coder persona + review_code operation via answer_query.
_EXTRACT_SYSTEM = (
    "You are a code-review extractor. Read the source and return ONLY a JSON array "
    "of findings — no prose, no markdown. Each element:\n"
    '{"anchor": "<exact function/class/module name copied from the code>", '
    '"severity": "high"|"med"|"low", '
    '"category": "correctness"|"error-handling"|"edge-case"|"resource"|"readability"|"testing"|"security", '
    '"claim": "<one sentence: what is wrong and why>", '
    '"excerpt": "<short snippet copied character-for-character>", '
    '"suggestion": "<how to fix it>"}\n'
    "Rules: the anchor MUST be an identifier that appears verbatim in the code — "
    "never invent one, and NEVER use a line number. The excerpt MUST be verbatim. "
    "Report only real issues; an empty array [] is a perfectly good answer. "
    "high = it will misbehave, crash, leak, or mislead a maintainer; "
    "med = a clearly better/safer way; low = style/taste."
)

_MAX_CHUNK_CHARS = 12000
_MAX_FINDINGS = 20
_SEV_ORDER = {"high": 0, "med": 1, "low": 2}
_VALID_SEV = set(_SEV_ORDER)


class CodeReviewAgent(BaseAgent):
    def execute(self, context: dict) -> str:
        directive = context.get("user_directive", "") or ""
        titles = [t.split("|")[0].strip() for t in _WIKILINK_RE.findall(directive)]
        if not titles:
            ui.error("🔔 @ling-code-review：請用 [[打包筆記名]] 指定要 review 的對象")
            return self._write_report(
                "Error",
                "（未指定對象。先跑 `make pack-code SRC=...` 打包，再 `@ling-code-review [[名稱]]`）",
                "code-review",
            )[1]

        title = sanitize_filename(titles[0].strip())
        note = CODE_REVIEW_DIR / f"{title}.md"
        if not note.exists():
            ui.error(f"🔔 找不到打包筆記：{title}")
            return self._write_report(
                title,
                (
                    f"# 🔔 {title}\n\n找不到打包筆記 `CodeReview/{title}.md`。\n"
                    "請先執行 `make pack-code SRC=<路徑>` 把原始碼打包進 vault，再來 review。"
                ),
                "code-review",
            )[1]

        raw = note.read_text(encoding="utf-8")
        identifiers, body = self._parse_packed(raw)
        chunks = self._chunk_by_file(body)
        if not chunks:
            ui.error(f"🔔 打包筆記沒有可 review 的程式碼：{title}")
            return self._write_report(
                title, f"# 🔔 {title}\n\n這個打包筆記裡沒有找到程式碼區塊。", "code-review"
            )[1]

        ui.set_status(f"🔔 Code review：{title[:40]}（{len(chunks)} 段）")
        findings = self._extract_all(chunks)
        findings = self._reduce(findings)

        if not findings:
            body_out = (
                f"# 🔔 Code Review：{title}\n\n"
                "## 總評\n"
                f"掃過 {len(chunks)} 段程式碼，沒有發現值得提的問題 🌸 "
                "（這是掃描器的判讀，仍建議搭配測試與人工複核）。\n"
            )
            _, full = self._write_report(
                title, body_out, "code-review", {"target": title, "findings": 0}
            )
            ui.success(f"🔔 完成：{title} → 0 findings → fromLingLing/")
            return full

        review = (
            self.llm.answer_query(
                query_content=(
                    f"為打包的程式碼《{title}》寫一篇 code review 報告。"
                    "根據下方已萃取的 findings（每條含識別符錨點、嚴重度、逐字摘錄與建議），"
                    "以工程師的判斷力組織成報告；只依據 findings，不要臆測看不到的行為。"
                ),
                wiki_context=self._findings_context(findings),
                forced_template="code-review-rpt",
                persona="coder",
                operation="review_code",
                temperature=0.3,
            )
            or ""
        )
        body_out = self._strip_leading_frontmatter(review) or "（產生報告時 LLM 呼叫失敗。）"

        body_out, fixes = correct_code_identifiers(body_out, identifiers)
        if fixes:
            ui.info(f"🔔 識別符校正 {len(fixes)} 處")

        _, full = self._write_report(
            title,
            body_out,
            "code-review",
            {"target": title, "findings": len(findings), "identifier_fixes": len(fixes)},
        )
        ui.success(f"🔔 完成：{title} → {len(findings)} findings → fromLingLing/")
        return full

    # ── packed-note parsing ────────────────────────────────────────────
    @staticmethod
    def _parse_packed(raw: str) -> tuple[list[str], str]:
        """Return (identifiers, body-without-frontmatter)."""
        m = _FRONTMATTER_RE.match(raw)
        identifiers: list[str] = []
        body = raw
        if m:
            try:
                fm = yaml.safe_load(m.group(1)) or {}
                ids = fm.get("identifiers")
                if isinstance(ids, list):
                    identifiers = [str(x) for x in ids]
            except yaml.YAMLError:
                pass
            body = raw[m.end() :]
        return identifiers, body

    @staticmethod
    def _chunk_by_file(body: str) -> list[tuple[str, str]]:
        """Split into (file_label, chunk_text) — one per `## file` section, with
        over-long sections windowed while keeping the file header on each."""
        sections = re.split(r"(?m)^## (?=\S)", body)
        chunks: list[tuple[str, str]] = []
        for sec in sections:
            sec = sec.strip()
            if not sec or "```" not in sec:
                continue
            label = sec.splitlines()[0].strip()
            full = "## " + sec
            if len(full) <= _MAX_CHUNK_CHARS:
                chunks.append((label, full))
                continue
            header = f"## {label}\n\n"
            payload = full[len(header) :]
            for i in range(0, len(payload), _MAX_CHUNK_CHARS):
                chunks.append((label, header + payload[i : i + _MAX_CHUNK_CHARS]))
        return chunks

    # ── map ────────────────────────────────────────────────────────────
    def _extract_all(self, chunks: list[tuple[str, str]]) -> list[dict]:
        findings: list[dict] = []
        for label, chunk in chunks:
            arr = self.llm._complete_json(
                kind="array",
                system_prompt=_EXTRACT_SYSTEM,
                user_msg=chunk,
                temperature=0.2,
                trace_context={"stage": "code_review_extract", "metadata": {"file": label}},
            )
            for item in arr or []:
                if not isinstance(item, dict) or not item.get("anchor"):
                    continue
                sev = str(item.get("severity", "low")).lower()
                findings.append(
                    {
                        "file": label,
                        "anchor": str(item.get("anchor", "")).strip(),
                        "severity": sev if sev in _VALID_SEV else "low",
                        "category": str(item.get("category", "")).strip(),
                        "claim": str(item.get("claim", "")).strip(),
                        "excerpt": str(item.get("excerpt", "")).strip(),
                        "suggestion": str(item.get("suggestion", "")).strip(),
                    }
                )
        return findings

    # ── reduce ─────────────────────────────────────────────────────────
    @staticmethod
    def _reduce(findings: list[dict]) -> list[dict]:
        seen: set[tuple[str, str, str]] = set()
        deduped: list[dict] = []
        for f in findings:
            key = (f["file"], f["anchor"], f["category"])
            if key in seen:
                continue
            seen.add(key)
            deduped.append(f)
        deduped.sort(key=lambda f: _SEV_ORDER.get(f["severity"], 3))
        return deduped[:_MAX_FINDINGS]

    @staticmethod
    def _findings_context(findings: list[dict]) -> str:
        lines = ["# 已萃取的 findings（依嚴重度排序）\n"]
        for i, f in enumerate(findings, 1):
            lines.append(
                f"## [{i}] {f['severity'].upper()} · {f['file']} → {f['anchor']}"
                f"（{f['category']}）\n"
                f"- 問題：{f['claim']}\n"
                f"- 摘錄：\n```\n{f['excerpt']}\n```\n"
                f"- 建議：{f['suggestion']}\n"
            )
        return "\n".join(lines)

    @staticmethod
    def _strip_leading_frontmatter(text: str) -> str:
        return _LEADING_FM_RE.sub("", text, count=1).strip()
