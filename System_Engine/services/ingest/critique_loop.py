"""SynthesisCritiqueLoop — generate → critique → conditional retry (P2d).

Moved from IngestionPipeline (_synthesize_with_critique_retry /
_run_synthesis_critique / _parse_verdict). `enabled` / `max_retries` are
call parameters, not config reads: the caller owns configuration, which also
keeps the existing tests (which patch the pipeline module's flags) working.
"""

from __future__ import annotations

import logging
import re

from core.parsing.markdown_quality import run_markdown_quality_checks
from services.ingest.digest_format import format_digest_appendix  # noqa: F401  (re-export convenience)

CRITIQUE_HEADER = "## 🔍 Quality Critique"

# Verdicts come from Operations/critique.md ("keep, revise, or reject").
# The model is allowed to use either English or zh-translated equivalents, and
# often wraps the keyword in prose ("應修正 (revise)" — observed live on
# gemma), so allow a short gap after the colon and take the first keyword on
# the line. A negated revise ("不需修正") counts as keep.
# Header variants observed live: 總體判定 / 整體判定 / 總體評定 / 總體評價
# (traditional and simplified), plus the canonical English. gemma4:26b also
# reaches for 總體結論 / 總體裁定 (conclusion / ruling) — three of four
# "unparseable" syntheses in the 2026-07-12 audit window had a perfectly clear
# verdict under one of those headers, so 結/结/裁 (pos3) and 論/论 (pos4) are in
# the class. Still anchored to 總體/整體 + a verdict keyword nearby, so a plain
# 總結 (summary, no 體) section can't false-match as a verdict.
_VERDICT_HEADER = r"(?:Overall\s+Verdict|[總总整][體体][判評评結结裁][定价價論论])"
# critique.md (REQUIRED verdict section, bb811db) mandates the bilingual header
# `**總體判定 (Overall Verdict)**` — and gemma complies faithfully, writing the
# parenthesized English tail verbatim. The 2026-07-24 obs-window re-audit found
# 11/14 syntheses unparseable because neither regex accepted that tail: the
# format the prompt requires must always parse (see the contract test in
# test_prompt_assets.py, which reads the header straight out of critique.md).
_VERDICT_HEADER_TAIL = r"(?:\s*[（(][^()（）\n]{0,40}[)）])?"
_VERDICT_KEYWORD = r"(keep|revise|reject|保留|修訂|修正|修改|重做|拒絕)"
_VERDICT_RE = re.compile(
    rf"(?im)^\**\s*{_VERDICT_HEADER}{_VERDICT_HEADER_TAIL}[\s*]*[:：][^\n]{{0,40}}?{_VERDICT_KEYWORD}",
)
# Fully localized "verdict as its own section" shape (observed live on gemma
# for cloud_act): the header is a markdown heading (or bold line, with the
# colon sometimes INSIDE the bold — `**總體評定：**`) and the keyword on the
# first following non-empty line, usually bold-wrapped and doubled as
# "中文 (english)":
#   ### 總體判定
#
#   **拒絕 (Reject)**。該文件包含兩項關鍵的事實錯誤...
_VERDICT_SECTION_RE = re.compile(
    rf"(?im)^(?:#{{1,6}}\s*|\**)\s*{_VERDICT_HEADER}{_VERDICT_HEADER_TAIL}[\s:：*]*$"
    rf"\s*^\**\s*[^\n]{{0,20}}?{_VERDICT_KEYWORD}",
)
_VERDICT_NEGATION_RE = re.compile(r"(不需|不必|無需|无需|毋須|毋须)\s*$")
_VERDICT_NORMALISE = {
    "keep": "keep",
    "保留": "keep",
    "revise": "revise",
    "修訂": "revise",
    "修正": "revise",
    "修改": "revise",
    "reject": "reject",
    "重做": "reject",
    "拒絕": "reject",
}

_VERDICT_RANK = {"keep": 2, "revise": 1, "reject": 0, None: -1}


def _needs_retry(result: dict) -> bool:
    """Whether a synthesis attempt should be regenerated.

    Retry on revise/reject (the critic wants changes) OR when the critic RAN but
    its verdict was unparseable — verdict None WITH a non-empty critique section.
    That last case is the 2026-07-12 audit hole: the gate produced findings but
    no readable verdict (gemma sometimes writes 總結與建議 instead of a verdict
    line), so the synthesis would otherwise ship UNGATED. Retrying gives the gate
    another chance at a readable verdict; if it never parses, the synthesis still
    ships but is recorded/counted as unparseable (visible), never silently
    "passed". Operational errors never trigger whole-synthesis regeneration;
    they stay visible as incomplete reviews rather than verdicts."""
    if result.get("critique_status") in ("error", "disabled"):
        return False
    v = result.get("verdict")
    return v in ("revise", "reject") or (v is None and bool(result.get("section")))


def parse_verdict(critique: str) -> str | None:
    m = _VERDICT_RE.search(critique) or _VERDICT_SECTION_RE.search(critique)
    if not m:
        return None
    verdict = _VERDICT_NORMALISE.get(m.group(1).strip().lower())
    if verdict == "revise" and _VERDICT_NEGATION_RE.search(critique[: m.start(1)]):
        return "keep"
    return verdict


class SynthesisCritiqueLoop:
    def __init__(self, llm):
        self.llm = llm

    def run(
        self,
        base_title: str,
        *,
        part_digests: list,
        pending_concepts: str,
        template,
        persona,
        enabled: bool,
        max_retries: int,
    ) -> dict:
        """Generate the synthesis, then act on the critique verdict.

        An explicit revise/reject verdict triggers up to ``max_retries``
        regenerations with the critique findings fed back. A retry is adopted
        only when its verdict ranks strictly higher (keep > revise > reject >
        unparseable). Missing verdicts can retry; operational failures stop
        regeneration and preserve the best already-reviewed candidate.
        Worst case adds one synthesis + one critique call per retry (local
        model).

        Returns {"text", "fixes", "section", "verdict", "attempts",
        "verdict_history"}.
        """

        def attempt(feedback: str | None) -> dict:
            # Pass critique_feedback only when set, so doubles of the LLM
            # client that predate the kwarg keep working on the normal path.
            extra = {"critique_feedback": feedback} if feedback is not None else {}
            text = self.llm.generate_synthesis(
                base_title,
                part_digests,
                pending_concepts,
                template=template,
                persona=persona,
                **extra,
            )
            text, fixes = run_markdown_quality_checks(text, strip_frontmatter=True)
            # Critique runs against the same digests the synthesis was
            # generated from — so any drift away from the sources surfaces.
            critique = self.critique_result(base_title, text, part_digests, enabled=enabled)
            return {"text": text, "fixes": fixes, **critique}

        current = attempt(None)
        attempts = 1
        history = [current["verdict"]]
        status_history = [current["critique_status"]]
        error_history = [current["critique_error"]]

        retries_left = max_retries
        while _needs_retry(current) and retries_left > 0:
            retries_left -= 1
            feedback = current["section"].removeprefix(CRITIQUE_HEADER).strip()
            retry = attempt(feedback)
            attempts += 1
            history.append(retry["verdict"])
            status_history.append(retry["critique_status"])
            error_history.append(retry["critique_error"])
            if retry["critique_status"] == "error":
                logging.warning(
                    "Critique retry for %s failed; retaining the prior candidate.", base_title
                )
                break
            if _VERDICT_RANK[retry["verdict"]] > _VERDICT_RANK[current["verdict"]]:
                current = retry
            else:
                logging.info(
                    f"Critique retry for {base_title} did not improve "
                    f"({history[-2]} → {history[-1]}); keeping the original synthesis."
                )

        current["attempts"] = attempts
        current["verdict_history"] = history
        current["critique_status_history"] = status_history
        current["critique_error_history"] = error_history
        return current

    def critique_once(
        self,
        base_title: str,
        synthesis_text: str,
        part_digests: list,
        *,
        enabled: bool,
    ) -> tuple[str, str | None]:
        """Compatibility view; failed enabled reviews carry a visible section."""
        result = self.critique_result(base_title, synthesis_text, part_digests, enabled=enabled)
        return result["section"], result["verdict"]

    def critique_result(
        self,
        base_title: str,
        synthesis_text: str,
        part_digests: list,
        *,
        enabled: bool,
    ) -> dict:
        """Distinguish an intentional disabled gate from an incomplete review."""

        def outcome(status, section="", verdict=None, error=""):
            return {
                "critique_status": status,
                "section": section,
                "verdict": verdict,
                "critique_error": error,
            }

        def failed(reason):
            detail = " ".join(str(reason).split())[:300]
            section = f"{CRITIQUE_HEADER}\n\n> 審查未完成：{detail}\n\n"
            return outcome("error", section=section, error=detail)

        if not enabled:
            return outcome("disabled")
        if not part_digests or not isinstance(synthesis_text, str) or not synthesis_text.strip():
            return failed("缺少來源摘要或候選內容，無法執行審查。")
        try:
            sources = "\n\n".join(self.llm.format_digest_for_prompt(d) for d in part_digests)
            critique = self.llm.critique_text(
                candidate=synthesis_text,
                sources=sources,
                focus="Source-grounding, specificity preservation, and contradiction surfacing.",
            )
        except Exception as e:
            logging.warning(f"Critique failed for {base_title}: {e}")
            return failed(f"呼叫失敗：{e}")

        if not isinstance(critique, str) or not critique.strip():
            return failed("審查未回傳有效文字。")
        if critique.lstrip().startswith("Critique failed"):
            return failed(critique)
        verdict = parse_verdict(critique)
        section = f"{CRITIQUE_HEADER}\n\n{critique.strip()}\n\n"
        return outcome("succeeded" if verdict else "unparseable", section, verdict)
