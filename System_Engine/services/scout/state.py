"""Scout persistent state — seen-item dedupe window + per-target last-crawl.

Same atomic-write pattern as maintenance_state.json (temp file + rename).
Schema:
    {"targets": {"<url>": {"last_crawled_at": iso}},
     "seen":    {"<sha1(dedupe_key)>": "<first-seen iso>"}}
"""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime, timedelta
from pathlib import Path

SEEN_WINDOW_DAYS = 30


def _hash_key(dedupe_key: str) -> str:
    return hashlib.sha1(dedupe_key.encode("utf-8")).hexdigest()


class ScoutState:
    def __init__(self, path: Path):
        self.path = path
        self._data = self._load()

    def _load(self) -> dict:
        if not self.path.exists():
            return {"targets": {}, "seen": {}}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception as e:
            logging.warning(f"Scout: failed to load state ({e}); starting fresh.")
            return {"targets": {}, "seen": {}}
        if not isinstance(data, dict):
            return {"targets": {}, "seen": {}}
        data.setdefault("targets", {})
        data.setdefault("seen", {})
        return data

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(self._data, ensure_ascii=False, indent=2, sort_keys=True)
        tmp = self.path.with_name(self.path.name + ".tmp")
        tmp.write_text(payload, encoding="utf-8")
        tmp.replace(self.path)

    # ── seen window ────────────────────────────────────────────────────

    def is_seen(self, dedupe_key: str) -> bool:
        return _hash_key(dedupe_key) in self._data["seen"]

    def mark_seen(self, dedupe_key: str, *, now: datetime | None = None) -> None:
        now = now or datetime.now()
        self._data["seen"].setdefault(_hash_key(dedupe_key), now.isoformat(timespec="seconds"))

    def prune_seen(self, *, now: datetime | None = None) -> int:
        """Drop seen entries older than the rolling window; returns count."""
        now = now or datetime.now()
        cutoff = now - timedelta(days=SEEN_WINDOW_DAYS)
        stale = []
        for key, first_seen in self._data["seen"].items():
            try:
                if datetime.fromisoformat(first_seen) < cutoff:
                    stale.append(key)
            except (TypeError, ValueError):
                stale.append(key)  # unparseable timestamp → treat as stale
        for key in stale:
            del self._data["seen"][key]
        return len(stale)

    # ── per-target crawl clock ─────────────────────────────────────────

    def last_crawled_at(self, url: str) -> datetime | None:
        raw = self._data["targets"].get(url, {}).get("last_crawled_at")
        if not raw:
            return None
        try:
            return datetime.fromisoformat(raw)
        except (TypeError, ValueError):
            return None

    def mark_crawled(self, url: str, *, now: datetime | None = None) -> None:
        now = now or datetime.now()
        self._data["targets"].setdefault(url, {})["last_crawled_at"] = now.isoformat(
            timespec="seconds"
        )
