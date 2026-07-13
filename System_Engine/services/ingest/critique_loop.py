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
_VERDICT_KEYWORD = r"(keep|revise|reject|保留|修訂|修正|修改|重做|拒絕)"
_VERDICT_RE = re.compile(
    rf"(?im)^\**\s*{_VERDICT_HEADER}[\s*]*[:：][^\n]{{0,40}}?{_VERDICT_KEYWORD}",
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
    rf"(?im)^(?:#{{1,6}}\s*|\**)\s*{_VERDICT_HEADER}[\s:：*]*$"
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
    "passed". A None verdict with NO section means critique is off/failed —
    nothing to gate against, so don't retry."""
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
        unparseable); an unparseable first verdict never triggers a retry.
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
            section, verdict = self.critique_once(base_title, text, part_digests, enabled=enabled)
            return {"text": text, "fixes": fixes, "section": section, "verdict": verdict}

        current = attempt(None)
        attempts = 1
        history = [current["verdict"]]

        retries_left = max_retries
        while _needs_retry(current) and retries_left > 0:
            retries_left -= 1
            feedback = current["section"].removeprefix(CRITIQUE_HEADER).strip()
            retry = attempt(feedback)
            attempts += 1
            history.append(retry["verdict"])
            if _VERDICT_RANK[retry["verdict"]] > _VERDICT_RANK[current["verdict"]]:
                current = retry
            else:
                logging.info(
                    f"Critique retry for {base_title} did not improve "
                    f"({history[-2]} → {history[-1]}); keeping the original synthesis."
                )

        current["attempts"] = attempts
        current["verdict_history"] = history
        return current

    def critique_once(
        self,
        base_title: str,
        synthesis_text: str,
        part_digests: list,
        *,
        enabled: bool,
    ) -> tuple[str, str | None]:
        """Critique the synthesis against its part digests. Fail-soft.

        Returns (body_section, verdict). `body_section` is the empty string
        when critique is disabled or fails, so the caller can splice it in
        unconditionally. `verdict` is one of "keep" / "revise" / "reject"
        if parseable, else None — None means "the critique text carried no
        parseable verdict", which is treated as no-retry (distinct from a
        genuine "revise").
        """
        if not enabled:
            return "", None
        if not part_digests or not synthesis_text.strip():
            return "", None

        sources = "\n\n".join(self.llm.format_digest_for_prompt(d) for d in part_digests)
        try:
            critique = self.llm.critique_text(
                candidate=synthesis_text,
                sources=sources,
                focus="Source-grounding, specificity preservation, and contradiction surfacing.",
            )
        except Exception as e:
            logging.warning(f"Critique failed for {base_title}: {e}")
            return "", None

        if not critique or not critique.strip() or critique.startswith("Critique failed"):
            return "", None

        verdict = parse_verdict(critique)
        section = f"{CRITIQUE_HEADER}\n\n{critique.strip()}\n\n"
        return section, verdict
