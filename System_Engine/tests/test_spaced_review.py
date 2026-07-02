"""Phase 2 spaced-review: selection sweet-spot, card rendering, reinforce loop."""

from datetime import datetime, timedelta
from pathlib import Path


from services.cortex_store import CortexPage, save_cortex_page, load_all_pages
from services import cortex_decay
from maintenance import spaced_review


NOW = datetime(2026, 7, 1, 12, 0, 0)
PARAMS = {"base_days": 21.0, "growth": 1.8}  # half-life at S=1 ≈ 37.8 days


def _make_page(cortex_dir: Path, claim: str, *, days_ago: float, status="active", S=1.0):
    reinforced = (NOW - timedelta(days=days_ago)).isoformat(timespec="seconds")
    page = CortexPage(
        claim_id="cortex-" + claim.encode("utf-8").hex()[:16],
        path=cortex_dir / f"{claim}.md",
        claim=claim,
        status=status,
        S=S,
        last_reinforced_at=reinforced,
        created=reinforced,
        updated=reinforced,
    )
    save_cortex_page(page)
    return page


def test_select_skips_fresh_and_falsified_and_sorts_by_urgency(tmp_path):
    _make_page(tmp_path, "fresh-claim", days_ago=0)  # R≈1.0 → excluded
    _make_page(tmp_path, "mild-claim", days_ago=40)  # R≈0.48 → due
    _make_page(tmp_path, "urgent-claim", days_ago=120)  # R≈0.11 → due (first)
    _make_page(tmp_path, "dead-claim", days_ago=200, status="falsified")  # excluded

    due = spaced_review.select_due_claims(tmp_path, limit=5, now=NOW, params=PARAMS)
    claims = [p.claim for p, _ in due]

    assert "fresh-claim" not in claims  # still fresh, not worth reviewing
    assert "dead-claim" not in claims  # falsified graveyard, never a target
    assert claims == ["urgent-claim", "mild-claim"]  # lowest R first


def test_select_respects_limit(tmp_path):
    for i in range(5):
        _make_page(tmp_path, f"claim-{i}", days_ago=60 + i * 10)
    due = spaced_review.select_due_claims(tmp_path, limit=2, now=NOW, params=PARAMS)
    assert len(due) == 2


def test_fallback_surfaces_least_fresh_when_nothing_strictly_due(tmp_path):
    # All fresh (R >= 0.6): strict selection is empty, fallback kicks in.
    _make_page(tmp_path, "freshest", days_ago=1)
    _make_page(tmp_path, "less-fresh", days_ago=20)  # still R>0.6, but lowest
    due = spaced_review.select_due_claims(tmp_path, limit=1, now=NOW, params=PARAMS)
    assert len(due) == 1
    assert due[0][0].claim == "less-fresh"  # least-fresh first
    assert due[0][1] >= spaced_review.FRESH_CUTOFF  # confirmed a refresher

    # fallback=False keeps the strict (empty) behavior
    strict = spaced_review.select_due_claims(
        tmp_path, limit=5, now=NOW, params=PARAMS, fallback=False
    )
    assert strict == []


def test_fallback_card_uses_softer_framing(tmp_path):
    _make_page(tmp_path, "still-fresh", days_ago=10)
    due = spaced_review.select_due_claims(tmp_path, limit=5, now=NOW, params=PARAMS)
    card = spaced_review.render_review_card(due, now=NOW)
    assert "今天沒有快忘記的東西" in card  # refresher intro, not the urgent one
    assert "還不急" in card  # per-item non-urgent tag


def test_card_hides_claim_behind_fold_and_lists_ids(tmp_path):
    p = _make_page(tmp_path, "重力會使時間變慢", days_ago=90)
    due = spaced_review.select_due_claims(tmp_path, limit=5, now=NOW, params=PARAMS)
    card = spaced_review.render_review_card(due, now=NOW)

    assert "<details>" in card and "翻牌" in card  # active recall, not re-read
    assert "重力會使時間變慢" in card  # answer present (folded)
    assert p.claim_id in card  # id for @ling-recalled


def test_recalled_reinforces_and_persists(tmp_path):
    _make_page(tmp_path, "熵總是增加", days_ago=90)
    before = load_all_pages(tmp_path)[0]
    r_before = cortex_decay.retrievability(
        before.S,
        before.last_reinforced_at,
        base_days=PARAMS["base_days"],
        growth=PARAMS["growth"],
        now=NOW,
    )
    assert r_before < cortex_decay.PROMOTE_ACTIVE  # it was genuinely due

    res = spaced_review.run_recalled_report(["熵總是增加"], cortex_dir=tmp_path, now=NOW)
    assert res.status == "succeeded"

    after = load_all_pages(tmp_path)[0]
    assert after.S > before.S  # storage strength grew
    assert after.last_reinforced_at == NOW.isoformat(timespec="seconds")  # R reset


def test_recalled_missing_claim_is_skipped_not_crash(tmp_path):
    _make_page(tmp_path, "real-claim", days_ago=90)
    res = spaced_review.run_recalled_report(["does-not-exist"], cortex_dir=tmp_path, now=NOW)
    assert res.status == "skipped"
    assert "找不到" in res.summary


def test_run_spaced_review_writes_card_file(tmp_path):
    cortex = tmp_path / "Cortex"
    cortex.mkdir()
    out = tmp_path / "from"
    out.mkdir()
    _make_page(cortex, "claim-due", days_ago=90)

    res = spaced_review.run_spaced_review(
        occasion="Manual",
        cortex_dir=cortex,
        report_dir=out,
        now=NOW,
    )
    assert res.status == "succeeded"
    cards = list(out.glob("*.md"))
    assert len(cards) == 1
    assert "type: spaced_review" in cards[0].read_text(encoding="utf-8")


def test_run_spaced_review_writes_refresher_card_when_all_fresh(tmp_path):
    # All fresh → fallback still produces a (softly-framed) card, not empty.
    cortex = tmp_path / "Cortex"
    cortex.mkdir()
    out = tmp_path / "from"
    out.mkdir()
    _make_page(cortex, "brand-new", days_ago=0)  # R≈1.0
    res = spaced_review.run_spaced_review(cortex_dir=cortex, report_dir=out, now=NOW)
    assert res.status == "succeeded"
    cards = list(out.glob("*.md"))
    assert len(cards) == 1
    assert "今天沒有快忘記的東西" in cards[0].read_text(encoding="utf-8")


def test_run_spaced_review_truly_empty_when_no_claims(tmp_path):
    cortex = tmp_path / "Cortex"
    cortex.mkdir()
    out = tmp_path / "from"
    out.mkdir()
    res = spaced_review.run_spaced_review(cortex_dir=cortex, report_dir=out, now=NOW)
    assert res.status == "succeeded"
    assert list(out.glob("*.md")) == []  # no claims at all → no card
