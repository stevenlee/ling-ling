"""services/scout/state.py — dedupe window, streak counting, v1 migration."""

import json
from datetime import datetime, timedelta

from services.scout.state import SEEN_WINDOW_DAYS, ScoutState

NOW = datetime(2026, 7, 11, 8, 0, 0)


def test_sighting_roundtrip_and_persistence(tmp_path):
    path = tmp_path / "scout_state.json"
    state = ScoutState(path)
    assert not state.is_seen("https://github.com/octo/rocket")
    assert state.record_sighting("https://github.com/octo/rocket", title="octo/rocket") == 1
    state.save()

    reloaded = ScoutState(path)
    assert reloaded.is_seen("https://github.com/octo/rocket")
    assert not reloaded.is_seen("https://github.com/other/repo")


def test_streak_counts_consecutive_days_and_resets_on_gap(tmp_path):
    state = ScoutState(tmp_path / "s.json")
    assert state.record_sighting("repo", now=NOW - timedelta(days=2)) == 1
    assert state.record_sighting("repo", now=NOW - timedelta(days=1)) == 2
    assert state.record_sighting("repo", now=NOW - timedelta(days=1)) == 2  # same-day no-op
    assert state.record_sighting("repo", now=NOW) == 3
    # A 3-day gap breaks the run.
    assert state.record_sighting("repo", now=NOW + timedelta(days=3)) == 1


def test_prune_keys_on_last_seen_so_recurring_items_never_reenter(tmp_path):
    state = ScoutState(tmp_path / "s.json")
    # First seen far outside the window, but still on the list yesterday:
    state.record_sighting("evergreen", now=NOW - timedelta(days=SEEN_WINDOW_DAYS + 10))
    state.record_sighting("evergreen", now=NOW - timedelta(days=1))
    state.record_sighting("stale", now=NOW - timedelta(days=SEEN_WINDOW_DAYS + 1))
    assert state.prune_seen(now=NOW) == 1
    assert state.is_seen("evergreen")  # would otherwise re-appear as "new"
    assert not state.is_seen("stale")


def test_v1_string_entries_migrate(tmp_path):
    path = tmp_path / "s.json"
    first_seen = (NOW - timedelta(days=1)).isoformat(timespec="seconds")
    # v1 schema: seen value is a bare iso string, keyed by sha1.
    import hashlib

    key = hashlib.sha1(b"legacy-item").hexdigest()
    path.write_text(json.dumps({"targets": {}, "seen": {key: first_seen}}), encoding="utf-8")
    state = ScoutState(path)
    assert state.is_seen("legacy-item")
    # Migrated entry participates in streaks (yesterday → today = day 2).
    assert state.record_sighting("legacy-item", now=NOW) == 2
    state.save()
    assert ScoutState(path).is_seen("legacy-item")


def test_corrupt_state_file_starts_fresh(tmp_path):
    path = tmp_path / "s.json"
    path.write_text("{not json", encoding="utf-8")
    state = ScoutState(path)
    assert not state.is_seen("anything")
    state.record_sighting("anything")
    state.save()  # and can save over the corrupt file
    assert ScoutState(path).is_seen("anything")


def test_domain_skiplist_three_strikes_then_retry_window(tmp_path):
    state = ScoutState(tmp_path / "s.json")
    domain = "paywall.example.com"
    for _ in range(2):
        state.record_content_fetch(domain, ok=False, now=NOW)
    assert not state.domain_blocked(domain, now=NOW)  # 2 strikes → still trying
    state.record_content_fetch(domain, ok=False, now=NOW)
    assert state.domain_blocked(domain, now=NOW)  # 3rd strike → skipped
    # …until the retry window elapses, then it gets another chance.
    assert not state.domain_blocked(domain, now=NOW + timedelta(days=8))
    # A success wipes the record entirely.
    state.record_content_fetch(domain, ok=True, now=NOW)
    assert not state.domain_blocked(domain, now=NOW)
    state.save()
    assert not ScoutState(state.path).domain_blocked(domain, now=NOW)


def test_crawl_clock(tmp_path):
    state = ScoutState(tmp_path / "s.json")
    url = "https://github.com/trending"
    assert state.last_crawled_at(url) is None
    state.mark_crawled(url, now=NOW)
    state.save()
    assert ScoutState(state.path).last_crawled_at(url) == NOW
