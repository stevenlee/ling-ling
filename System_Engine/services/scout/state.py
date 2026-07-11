"""Scout persistent state — seen-item dedupe window, streaks, crawl clocks.

Same atomic-write pattern as maintenance_state.json (temp file + rename).
Schema (v2, P2.2):
    {"targets": {"<url>": {"last_crawled_at": iso}},
     "seen":    {"<sha1(dedupe_key)>": {"first_seen": iso, "last_seen": iso,
                                         "streak": N, "title": str}}}
v1 stored seen values as a bare first-seen iso string — migrated on load.
Streaks count CONSECUTIVE-day sightings (a listing item that stays on a
daily-crawled list); a gap resets to 1. Only meaningful for daily targets.
"""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime, timedelta
from pathlib import Path

SEEN_WINDOW_DAYS = 30
# Adaptive content-fetch skip: after this many CONSECUTIVE failed content
# fetches, a domain is skipped (snippet-only analysis) until the retry window
# elapses — paywalled/bot-hostile sites (sciencedirect …) otherwise burn
# 15-30s of timeout per item every single day. A success resets the counter.
DOMAIN_SKIP_AFTER_FAILS = 3
DOMAIN_RETRY_DAYS = 7


def _hash_key(dedupe_key: str) -> str:
    return hashlib.sha1(dedupe_key.encode("utf-8")).hexdigest()


def _parse_dt(value: object) -> datetime | None:
    try:
        return datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None


class ScoutState:
    def __init__(self, path: Path):
        self.path = path
        self._data = self._load()

    @staticmethod
    def _fresh() -> dict:
        return {"targets": {}, "seen": {}, "domains": {}}

    def _load(self) -> dict:
        if not self.path.exists():
            return self._fresh()
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception as e:
            logging.warning(f"Scout: failed to load state ({e}); starting fresh.")
            return self._fresh()
        if not isinstance(data, dict):
            return self._fresh()
        data.setdefault("targets", {})
        data.setdefault("seen", {})
        data.setdefault("domains", {})
        # v1 → v2: bare first-seen iso string becomes an entry dict.
        for key, value in list(data["seen"].items()):
            if isinstance(value, str):
                data["seen"][key] = {
                    "first_seen": value,
                    "last_seen": value,
                    "streak": 1,
                    "title": "",
                }
        return data

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(self._data, ensure_ascii=False, indent=2, sort_keys=True)
        tmp = self.path.with_name(self.path.name + ".tmp")
        tmp.write_text(payload, encoding="utf-8")
        tmp.replace(self.path)

    # ── seen window + streaks ──────────────────────────────────────────

    def is_seen(self, dedupe_key: str) -> bool:
        return _hash_key(dedupe_key) in self._data["seen"]

    def record_sighting(
        self, dedupe_key: str, *, title: str = "", now: datetime | None = None
    ) -> int:
        """Record that ``dedupe_key`` appeared in today's crawl; returns the
        current consecutive-day streak. Same-day repeats are no-ops."""
        now = now or datetime.now()
        entry = self._data["seen"].get(_hash_key(dedupe_key))
        if entry is None:
            self._data["seen"][_hash_key(dedupe_key)] = {
                "first_seen": now.isoformat(timespec="seconds"),
                "last_seen": now.isoformat(timespec="seconds"),
                "streak": 1,
                "title": title,
            }
            return 1

        last_seen = _parse_dt(entry.get("last_seen"))
        gap_days = (now.date() - last_seen.date()).days if last_seen else None
        if gap_days == 0:
            return int(entry.get("streak", 1))  # same-day repeat
        entry["streak"] = int(entry.get("streak", 1)) + 1 if gap_days == 1 else 1
        entry["last_seen"] = now.isoformat(timespec="seconds")
        if title:
            entry["title"] = title
        return int(entry["streak"])

    def prune_seen(self, *, now: datetime | None = None) -> int:
        """Drop entries not sighted within the rolling window; returns count.
        Keyed on last_seen — an item still appearing daily never re-enters the
        report as "new", no matter how long it stays on the list."""
        now = now or datetime.now()
        cutoff = now - timedelta(days=SEEN_WINDOW_DAYS)
        stale = []
        for key, entry in self._data["seen"].items():
            last_seen = _parse_dt(entry.get("last_seen")) if isinstance(entry, dict) else None
            if last_seen is None or last_seen < cutoff:
                stale.append(key)  # unparseable → treat as stale
        for key in stale:
            del self._data["seen"][key]
        return len(stale)

    # ── adaptive content-fetch domain skiplist ─────────────────────────

    def domain_blocked(self, domain: str, *, now: datetime | None = None) -> bool:
        entry = self._data["domains"].get(domain)
        if not entry or int(entry.get("fails", 0)) < DOMAIN_SKIP_AFTER_FAILS:
            return False
        last_fail = _parse_dt(entry.get("last_fail_at"))
        if last_fail is None:
            return False
        now = now or datetime.now()
        return (now - last_fail) < timedelta(days=DOMAIN_RETRY_DAYS)

    def record_content_fetch(self, domain: str, *, ok: bool, now: datetime | None = None) -> None:
        if ok:
            self._data["domains"].pop(domain, None)  # success resets the strike count
            return
        now = now or datetime.now()
        entry = self._data["domains"].setdefault(domain, {"fails": 0})
        entry["fails"] = int(entry.get("fails", 0)) + 1
        entry["last_fail_at"] = now.isoformat(timespec="seconds")

    # ── per-target crawl clock ─────────────────────────────────────────

    def last_crawled_at(self, url: str) -> datetime | None:
        return _parse_dt(self._data["targets"].get(url, {}).get("last_crawled_at"))

    def mark_crawled(self, url: str, *, now: datetime | None = None) -> None:
        now = now or datetime.now()
        self._data["targets"].setdefault(url, {})["last_crawled_at"] = now.isoformat(
            timespec="seconds"
        )
