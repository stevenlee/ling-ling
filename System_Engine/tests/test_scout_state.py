"""services/scout/state.py — seen-window dedupe + per-target crawl clock."""

from datetime import datetime, timedelta

from services.scout.state import SEEN_WINDOW_DAYS, ScoutState


def test_seen_roundtrip_and_persistence(tmp_path):
    path = tmp_path / "scout_state.json"
    state = ScoutState(path)
    assert not state.is_seen("https://github.com/octo/rocket")
    state.mark_seen("https://github.com/octo/rocket")
    state.save()

    reloaded = ScoutState(path)
    assert reloaded.is_seen("https://github.com/octo/rocket")
    assert not reloaded.is_seen("https://github.com/other/repo")


def test_prune_drops_entries_older_than_window(tmp_path):
    state = ScoutState(tmp_path / "s.json")
    now = datetime(2026, 7, 11, 8, 0, 0)
    state.mark_seen("old", now=now - timedelta(days=SEEN_WINDOW_DAYS + 1))
    state.mark_seen("fresh", now=now - timedelta(days=1))
    assert state.prune_seen(now=now) == 1
    assert not state.is_seen("old")
    assert state.is_seen("fresh")


def test_mark_seen_keeps_first_seen_date(tmp_path):
    # Re-seeing an item must NOT refresh its window, or a repo that trends
    # every week would never expire from the dedupe set.
    state = ScoutState(tmp_path / "s.json")
    now = datetime(2026, 7, 11, 8, 0, 0)
    state.mark_seen("repo", now=now - timedelta(days=SEEN_WINDOW_DAYS + 2))
    state.mark_seen("repo", now=now)  # seen again today
    assert state.prune_seen(now=now) == 1


def test_corrupt_state_file_starts_fresh(tmp_path):
    path = tmp_path / "s.json"
    path.write_text("{not json", encoding="utf-8")
    state = ScoutState(path)
    assert not state.is_seen("anything")
    state.mark_seen("anything")
    state.save()  # and can save over the corrupt file
    assert ScoutState(path).is_seen("anything")


def test_crawl_clock(tmp_path):
    state = ScoutState(tmp_path / "s.json")
    url = "https://github.com/trending"
    assert state.last_crawled_at(url) is None
    now = datetime(2026, 7, 11, 8, 0, 0)
    state.mark_crawled(url, now=now)
    state.save()
    assert ScoutState(state.path).last_crawled_at(url) == now
