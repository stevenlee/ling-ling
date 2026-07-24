"""Persistent, content-addressed retry budget for deterministic bad Parts."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from core.config import (
    INGEST_ENTITY_CONTRACT_VERSION,
    INGEST_ENTITY_MAX_ATTEMPTS,
    INGEST_ENTITY_QUARANTINE_HOURS,
    INGEST_FAILURE_STATE_FILE,
)
from services.ingest.atomic_io import atomic_write_text


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _stamp(value: datetime) -> str:
    return value.isoformat(timespec="seconds")


class IngestFailureLedger:
    """Attempts are charged before an LLM call and rolled back for outages."""

    def __init__(
        self,
        path: Path = INGEST_FAILURE_STATE_FILE,
        *,
        max_attempts: int = INGEST_ENTITY_MAX_ATTEMPTS,
        quarantine_hours: int = INGEST_ENTITY_QUARANTINE_HOURS,
    ):
        self.path = path
        self.max_attempts = max_attempts
        self.quarantine_hours = quarantine_hours
        self.state = self._load()

    def _load(self) -> dict:
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            return {"version": 1, "failures": {}}
        if not isinstance(value, dict) or not isinstance(value.get("failures"), dict):
            return {"version": 1, "failures": {}}
        return value

    def _save(self) -> None:
        atomic_write_text(
            self.path,
            json.dumps(self.state, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        )

    @staticmethod
    def key(source: Path, part: int, chunk: str, model: str) -> tuple[str, str]:
        content_hash = hashlib.sha256((chunk or "").encode("utf-8")).hexdigest()
        identity = (
            f"{source.resolve()}\0{part}\0{content_hash}\0{model}\0{INGEST_ENTITY_CONTRACT_VERSION}"
        )
        return hashlib.sha256(identity.encode("utf-8")).hexdigest(), content_hash

    def begin(self, source: Path, part: int, chunk: str, model: str) -> tuple[bool, str, str]:
        key, content_hash = self.key(source, part, chunk, model)
        entry = self.state["failures"].get(key)
        now = _now()
        if isinstance(entry, dict) and entry.get("quarantined_until"):
            try:
                until = datetime.fromisoformat(str(entry["quarantined_until"]))
            except ValueError:
                until = now
            if until > now:
                return False, key, content_hash
            # TTL expiry is a single half-open probe with a fresh one-attempt
            # budget. Charging it before work prevents crash loops.
            entry["attempts"] = self.max_attempts - 1
            entry.pop("quarantined_until", None)
        if not isinstance(entry, dict):
            entry = {
                "source": str(source),
                "part": part,
                "content_hash": content_hash,
                "model": model,
                "contract_version": INGEST_ENTITY_CONTRACT_VERSION,
                "attempts": 0,
            }
            self.state["failures"][key] = entry
        entry["attempts"] = int(entry.get("attempts", 0)) + 1
        entry["last_attempt_at"] = _stamp(now)
        self._save()
        return True, key, content_hash

    def fail(self, key: str, *, stage: str, detail: str) -> None:
        entry = self.state["failures"].get(key)
        if not isinstance(entry, dict):
            return
        entry["stage"] = stage
        entry["detail"] = detail[:1000]
        if int(entry.get("attempts", 0)) >= self.max_attempts:
            entry["quarantined_until"] = _stamp(_now() + timedelta(hours=self.quarantine_hours))
        self._save()

    def outage(self, key: str) -> None:
        """Provider outage is not evidence that the content is poisonous."""
        entry = self.state["failures"].get(key)
        if not isinstance(entry, dict):
            return
        entry["attempts"] = max(0, int(entry.get("attempts", 1)) - 1)
        entry["last_outage_at"] = _stamp(_now())
        entry.pop("quarantined_until", None)
        if not entry["attempts"]:
            self.state["failures"].pop(key, None)
        self._save()

    def succeed(self, key: str) -> None:
        if self.state["failures"].pop(key, None) is not None:
            self._save()
