"""Spaced-review — turn the Cortex decay model into a study aid (Phase 2).

The decay layer already computes R (retrievability) for memory *hygiene*
(which beliefs to let fade). Read the other way, the same R is a spaced-
repetition schedule: a claim whose R has slipped toward the fading edge is
one you are about to forget but can still recall with effort — Bjork's
"desirable difficulty", the highest-value moment to review.

This module does three things, all through `cortex_store` (no ChromaDB, no
single-writer risk):

1. `select_due_claims`  — pick the non-falsified claims whose R has dropped
   below the "still fresh" line, most-urgent (lowest R) first.
2. `run_spaced_review`  — render an active-recall card (cue shown, claim
   hidden) in Ling-Ling's mentor voice and drop it in fromLingLing/.
3. `run_recalled_report`— on an explicit "I recalled it" report, fire the
   exact reinforcement the decay engine already defines for a retrieval
   event (`GAIN_RETRIEVAL`) and persist it. This closes the loop: a
   reviewed claim's S rises, its half-life lengthens, and it resurfaces
   later rather than tomorrow.

Card rendering is deterministic (assembled from claim fields) — no LLM call,
so the daily task is cheap and needs no dream-window/idle gating.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from core.config import CORTEX_DIR, FROM_LLM_DIR
from services import cortex_decay
from services.cortex_store import CortexPage, load_all_pages, save_cortex_page

# Claims with R at or above this are still fresh — reviewing them is wasted
# effort (the spacing effect makes a high-R reinforcement worth ~nothing).
# Matches cortex_decay.PROMOTE_ACTIVE so "due" == "no longer comfortably active".
FRESH_CUTOFF = cortex_decay.PROMOTE_ACTIVE  # 0.6

# How many claims one card holds by default.
DEFAULT_CARD_SIZE = 3


@dataclass
class SpacedReviewResult:
    status: str
    summary: str


def _page_r(page: CortexPage, params: dict, now: datetime) -> float:
    return cortex_decay.retrievability(
        page.S, page.last_reinforced_at,
        base_days=params["base_days"], growth=params["growth"], now=now,
    )


def select_due_claims(
    cortex_dir: Path = None,
    *,
    limit: int = DEFAULT_CARD_SIZE,
    now: datetime = None,
    params: dict = None,
    fallback: bool = True,
) -> list[tuple[CortexPage, float]]:
    """Non-falsified claims to review, most-urgent (lowest R) first.

    Primary selection is R < FRESH_CUTOFF — genuinely "about to forget".
    But a young or frequently-reinforced vault can have *nothing* below the
    cutoff (every claim still fresh), which would make the card perpetually
    empty. With `fallback=True` (default), when nothing is strictly due we
    still surface the least-fresh claims as a gentle refresher — the card is
    never empty while claims exist. The caller/renderer distinguishes the two
    by comparing each item's R to FRESH_CUTOFF. Falsified claims are graveyard
    records, never review targets.
    """
    cortex_dir = cortex_dir or CORTEX_DIR
    now = now or datetime.now()
    params = params or cortex_decay.load_params()
    limit = max(0, int(limit))

    ranked = [
        (page, _page_r(page, params, now))
        for page in load_all_pages(cortex_dir)
        if page.status != "falsified"
    ]
    ranked.sort(key=lambda pr: pr[1])  # ascending R — most forgotten first

    due = [pr for pr in ranked if pr[1] < FRESH_CUTOFF]
    if due:
        return due[:limit]
    if fallback:
        return ranked[:limit]   # nothing strictly due → least-fresh refresher
    return []


_CJK_PARENS_RE = re.compile(r'（([^（）]*[一-鿿][^（）]*)）')


def _zh_gloss(text: str) -> str:
    """Pull the Chinese gloss out of a bilingual 'English（中文）' string;
    return the whole thing if there is no such parenthetical."""
    m = _CJK_PARENS_RE.search(text or "")
    return (m.group(1) if m else (text or "")).strip()


def _clip(text: str, n: int = 60) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= n else text[:n].rstrip() + "…"


def _cue_for(page: CortexPage) -> str:
    """A hint that points at the claim without giving it away.

    Prefer `applies_when` (the scope — a genuine cue that doesn't restate
    the claim). Fall back to the falsifier, cleaned to its Chinese gloss and
    clipped. NOT the evidence summary — that paraphrases the claim and would
    give the answer away.
    """
    if page.applies_when.strip():
        return _clip(page.applies_when)
    if page.falsifier.strip():
        return f"一個在「{_clip(_zh_gloss(page.falsifier), 45)}」時會被推翻的主張"
    return "你筆記裡的一條核心主張"


def render_review_card(
    due: list[tuple[CortexPage, float]],
    *,
    now: datetime = None,
) -> str:
    """Mentor-voice active-recall card. Cue first, claim folded behind a
    <details> so the reader recalls before checking."""
    now = now or datetime.now()
    stamp = now.strftime("%Y-%m-%d")
    # "Refresher" batch = nothing crossed the forget-line; we surfaced the
    # least-fresh claims anyway. The intro softens accordingly.
    all_refresher = all(r >= FRESH_CUTOFF for _, r in due)
    if all_refresher:
        intro = ("> 今天沒有快忘記的東西 (ゝ∀･)b 挑幾條**還記得、但久沒碰**的溫習一下，"
                 "先自己回想再翻牌～")
    else:
        intro = "> 這些是你**快要忘記、但現在還救得回**的想法。先自己回想，再翻牌對答案～"
    lines = [
        f"# 🎴 玲玲的複習卡 · {stamp}",
        "",
        intro,
        "",
    ]
    for i, (page, r) in enumerate(due, 1):
        tag = "" if r < FRESH_CUTOFF else "（還不急，溫習用）"
        lines += [
            f"## {i}. 提示：{_cue_for(page)} {tag}".rstrip(),
            "",
            "你還記得核心主張是什麼嗎？（先想好再展開）",
            "",
            "<details>",
            "<summary>🔽 翻牌看答案</summary>",
            "",
            f"**主張**：{page.claim.strip()}",
            "",
        ]
        if page.evidence:
            src = page.evidence[0].get("source") if isinstance(page.evidence[0], dict) else None
            if src:
                lines += [f"**證據**：{str(src).strip()}", ""]
        if page.counterpoints:
            lines += [f"**別忘了反面**：{page.counterpoints[0].strip()}", ""]
        lines += [
            f"<sub>記得的話，投一張 `@ling-recalled [[{page.claim.strip()}]]` "
            f"我就幫你把它記得更牢。（目前熟悉度 R≈{r:.2f}）</sub>",
            "",
            "</details>",
            "",
        ]
    lines += [
        "---",
        "",
        f"<sub>玲玲小老師 · 間隔重複複習 · claim ids: "
        f"{', '.join(p.claim_id for p, _ in due)}</sub>",
        "",
    ]
    return "\n".join(lines)


def run_spaced_review(
    llm=None,
    rag=None,
    *,
    occasion: str = "Scheduled",
    limit: int = DEFAULT_CARD_SIZE,
    cortex_dir: Path = None,
    report_dir: Path = None,
    now: datetime = None,
) -> SpacedReviewResult:
    """Select due claims, render a card, write it to fromLingLing/.

    `llm`/`rag` are accepted (and ignored) so this matches the maintenance
    task / brain-op calling convention; card rendering is deterministic.
    """
    now = now or datetime.now()
    cortex_dir = cortex_dir or CORTEX_DIR
    report_dir = report_dir or FROM_LLM_DIR

    due = select_due_claims(cortex_dir, limit=limit, now=now)
    if not due:
        # With fallback on, this only happens when Cortex has no reviewable
        # (non-falsified) claims at all.
        return SpacedReviewResult("succeeded", "Cortex 還沒有可複習的主張。")

    card = render_review_card(due, now=now)
    report_dir.mkdir(parents=True, exist_ok=True)
    fname = f"🎴複習卡-{now.strftime('%Y%m%d-%H%M')}.md"
    frontmatter = (
        "---\n"
        "type: spaced_review\n"
        f"date_created: {now.isoformat(timespec='seconds')}\n"
        f"occasion: {occasion}\n"
        f"claim_count: {len(due)}\n"
        "---\n\n"
    )
    (report_dir / fname).write_text(frontmatter + card, encoding="utf-8")

    titles = "、".join(p.claim.strip()[:20] for p, _ in due)
    return SpacedReviewResult("succeeded", f"複習卡已出（{len(due)} 題）：{titles}")


def find_claim(target: str, cortex_dir: Path = None) -> CortexPage | None:
    """Resolve a `@ling-recalled` target to a CortexPage.

    Match order: exact claim_id → filename stem (case-insensitive) →
    claim-text substring. Returns None if nothing matches.
    """
    cortex_dir = cortex_dir or CORTEX_DIR
    target = (target or "").strip()
    if not target:
        return None
    pages = load_all_pages(cortex_dir)
    for page in pages:
        if page.claim_id == target:
            return page
    low = target.lower()
    for page in pages:
        if page.path and page.path.stem.lower() == low:
            return page
    for page in pages:
        if low in page.claim.lower() or page.claim.lower() in low:
            return page
    return None


def run_recalled_report(
    target_entities: list[str],
    *,
    cortex_dir: Path = None,
    now: datetime = None,
) -> SpacedReviewResult:
    """Reinforce each recalled claim (GAIN_RETRIEVAL) and persist it.

    This is the loop closer: a successful active recall lengthens the
    claim's half-life, so it resurfaces later instead of tomorrow.
    """
    cortex_dir = cortex_dir or CORTEX_DIR
    now = now or datetime.now()
    titles = [t.split("|")[0].strip() for t in (target_entities or []) if t.strip()]
    if not titles:
        return SpacedReviewResult("skipped", "請以 [[主張]] 指定你記得的是哪一條。")

    params = cortex_decay.load_params()
    done, missing = [], []
    for title in titles:
        page = find_claim(title, cortex_dir)
        if page is None:
            missing.append(title)
            continue
        delta = cortex_decay.reinforce(
            page, cortex_decay.GAIN_RETRIEVAL, params=params, now=now
        )
        save_cortex_page(page)
        done.append(f"{page.claim.strip()[:20]}（S+{delta:.2f}）")

    parts = []
    if done:
        parts.append(f"記牢了 {len(done)} 條：{'、'.join(done)}")
    if missing:
        parts.append(f"找不到：{'、'.join(missing)}")
    status = "succeeded" if done else "skipped"
    return SpacedReviewResult(status, "；".join(parts) or "沒有可強化的主張。")
