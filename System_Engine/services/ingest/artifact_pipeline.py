"""Concurrent learning-aid jobs and conflict-safe inline page patches."""

from __future__ import annotations

import contextvars
import hashlib
import re
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor, TimeoutError as FutureTimeoutError
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

from core.parser import strip_body_frontmatter
from services.ingest.atomic_io import atomic_write_text

ARTIFACT_START_PREFIX = "<!-- lingling:learning-aids:start"
ARTIFACT_END = "<!-- lingling:learning-aids:end -->"
_ARTIFACT_HEADER = "## 🖼️ 學習輔助"
_DIGEST_HEADER = "## 🧩 Part Digest Appendix"
_NAV_HEADER_RE = re.compile(r"(?m)^## (?:🔗 知識導航|📂 Navigation)\s*$")
_SLOT_RE = re.compile(
    r"<!-- lingling:learning-aids:start(?P<attrs>[^>]*)-->\s*\n?"
    r"(?P<body>.*?)\n?<!-- lingling:learning-aids:end -->",
    re.DOTALL,
)


def content_hash(text: str) -> str:
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()


def _start_marker(
    *,
    basis_hash: str,
    ownership: str,
    section_hash: str = "",
    attempts: int = 0,
    quarantined_until: str = "",
    failure_hash: str = "",
) -> str:
    suffix = f' section_sha256="{section_hash}"' if section_hash else ""
    suffix += f' attempts="{attempts}"' if attempts else ""
    suffix += f' quarantined_until="{quarantined_until}"' if quarantined_until else ""
    suffix += f' failure_sha256="{failure_hash}"' if failure_hash else ""
    return (
        f'{ARTIFACT_START_PREFIX} basis_sha256="{basis_hash}" ownership="{ownership}"{suffix} -->'
    )


def _attr(attrs: str, name: str) -> str:
    match = re.search(rf'\b{re.escape(name)}="([^"]*)"', attrs)
    return match.group(1) if match else ""


def _slot_block(
    body: str,
    *,
    basis_hash: str,
    ownership: str,
    section_hash: str = "",
    attempts: int = 0,
    quarantined_until: str = "",
    failure_hash: str = "",
) -> str:
    body = body.strip()
    middle = f"\n{body}" if body else ""
    return (
        f"{_start_marker(basis_hash=basis_hash, ownership=ownership, section_hash=section_hash, attempts=attempts, quarantined_until=quarantined_until, failure_hash=failure_hash)}"
        f"{middle}\n{ARTIFACT_END}"
    )


def _legacy_artifact_block(text: str) -> str:
    start = text.find(_ARTIFACT_HEADER)
    if start == -1:
        return ""
    candidates = []
    for marker in (
        _DIGEST_HEADER,
        "## 📂 Navigation",
        "## 🔗 知識導航",
        ARTIFACT_START_PREFIX,
    ):
        end = text.find(marker, start + 1)
        if end != -1:
            candidates.append(end)
    return text[start : min(candidates) if candidates else None].strip()


def _remove_existing_artifacts(text: str) -> str:
    text = _SLOT_RE.sub("", text)
    legacy = _legacy_artifact_block(text)
    if legacy:
        text = text.replace(legacy, "", 1)
    return text


def _insert_before_navigation(text: str, block: str) -> str:
    nav = _NAV_HEADER_RE.search(text)
    if not nav:
        return f"{text.rstrip()}\n\n{block}\n"
    insert_at = nav.start()
    separator = text.rfind("\n---\n", 0, insert_at)
    if separator != -1 and not text[separator + 5 : insert_at].strip():
        insert_at = separator + 1
    return f"{text[:insert_at].rstrip()}\n\n{block}\n\n{text[insert_at:].lstrip()}"


@dataclass(frozen=True)
class PreparedArtifactSlot:
    text: str
    should_generate: bool
    status: str


@dataclass(frozen=True)
class ArtifactSlotSnapshot:
    basis_hash: str
    ownership: str
    body: str
    section_hash: str
    intact: bool


def artifact_slot_snapshot(text: str) -> ArtifactSlotSnapshot | None:
    match = _SLOT_RE.search(text or "")
    if not match:
        return None
    attrs = match.group("attrs")
    body = match.group("body").strip()
    section_hash = _attr(attrs, "section_sha256")
    return ArtifactSlotSnapshot(
        basis_hash=_attr(attrs, "basis_sha256"),
        ownership=_attr(attrs, "ownership"),
        body=body,
        section_hash=section_hash,
        intact=bool(section_hash) and content_hash(body) == section_hash,
    )


def prepare_artifact_slot(
    rendered_text: str,
    existing_text: str,
    *,
    basis_hash: str,
    enabled: bool,
) -> PreparedArtifactSlot:
    """Put one owned slot before navigation without overwriting old aids."""
    rendered_text = _remove_existing_artifacts(rendered_text)
    existing_slot = _SLOT_RE.search(existing_text or "")
    if existing_slot:
        attrs = existing_slot.group("attrs")
        body = existing_slot.group("body").strip()
        ownership = _attr(attrs, "ownership")
        old_basis = _attr(attrs, "basis_sha256")
        expected_hash = _attr(attrs, "section_sha256")
        if ownership == "pending" and not body and enabled:
            same_basis = old_basis == basis_hash
            block = _slot_block(
                "",
                basis_hash=basis_hash,
                ownership="pending",
                attempts=int(_attr(attrs, "attempts") or 0) if same_basis else 0,
                quarantined_until=(_attr(attrs, "quarantined_until") if same_basis else ""),
                failure_hash=_attr(attrs, "failure_sha256") if same_basis else "",
            )
            return PreparedArtifactSlot(
                _insert_before_navigation(rendered_text, block), True, "pending"
            )
        if ownership == "generated" and expected_hash and content_hash(body) == expected_hash:
            if old_basis == basis_hash:
                block = _slot_block(
                    body,
                    basis_hash=basis_hash,
                    ownership="generated",
                    section_hash=expected_hash,
                )
                return PreparedArtifactSlot(
                    _insert_before_navigation(rendered_text, block), False, "complete"
                )
            if enabled:
                block = _slot_block("", basis_hash=basis_hash, ownership="pending")
                return PreparedArtifactSlot(
                    _insert_before_navigation(rendered_text, block), True, "pending"
                )
        # Unknown ownership or a changed generated region belongs to the user.
        block = _slot_block(body, basis_hash=old_basis or basis_hash, ownership="preserved")
        return PreparedArtifactSlot(
            _insert_before_navigation(rendered_text, block), False, "preserved"
        )

    legacy = _legacy_artifact_block(existing_text or "")
    if legacy:
        block = _slot_block(legacy, basis_hash=basis_hash, ownership="preserved")
        return PreparedArtifactSlot(
            _insert_before_navigation(rendered_text, block), False, "preserved"
        )
    if not enabled:
        return PreparedArtifactSlot(rendered_text, False, "disabled")
    block = _slot_block("", basis_hash=basis_hash, ownership="pending")
    return PreparedArtifactSlot(_insert_before_navigation(rendered_text, block), True, "pending")


def artifact_slot_status(text: str, basis_hash: str) -> str:
    match = _SLOT_RE.search(text or "")
    if not match or _attr(match.group("attrs"), "basis_sha256") != basis_hash:
        return "missing"
    ownership = _attr(match.group("attrs"), "ownership")
    return ownership or "unknown"


def artifact_section_from_page(text: str) -> str:
    slot = _SLOT_RE.search(text or "")
    if slot:
        return slot.group("body").strip()
    return _legacy_artifact_block(text or "")


def core_content_for_artifact(text: str) -> str:
    body, _ = strip_body_frontmatter(text)
    body = _SLOT_RE.sub("", body)
    legacy = _legacy_artifact_block(body)
    if legacy:
        body = body.replace(legacy, "", 1)
    nav = _NAV_HEADER_RE.search(body)
    if nav:
        body = body[: nav.start()]
    digest = body.find(_DIGEST_HEADER)
    if digest != -1:
        body = body[:digest]
    return body.rstrip("\n- ")


@dataclass(frozen=True)
class ArtifactPatchResult:
    status: str  # applied | unchanged | conflict
    backup_path: Path | None = None
    detail: str = ""


@dataclass(frozen=True)
class ArtifactAttemptAdmission:
    allowed: bool
    status: str
    attempts: int


def _parse_utc(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def begin_artifact_attempt(
    path: Path,
    *,
    basis_hash: str,
    max_attempts: int,
    quarantine_hours: int = 24,
) -> ArtifactAttemptAdmission:
    """Persist an attempt before generation; pending slot is the ledger."""
    current = path.read_text(encoding="utf-8")
    match = _SLOT_RE.search(current)
    if not match:
        return ArtifactAttemptAdmission(False, "missing", 0)
    attrs = match.group("attrs")
    attempts = int(_attr(attrs, "attempts") or 0)
    if (
        _attr(attrs, "basis_sha256") != basis_hash
        or _attr(attrs, "ownership") != "pending"
        or match.group("body").strip()
    ):
        return ArtifactAttemptAdmission(False, "conflict", attempts)
    now = datetime.now(timezone.utc)
    until = _parse_utc(_attr(attrs, "quarantined_until"))
    if until and until > now:
        return ArtifactAttemptAdmission(False, "quarantined", attempts)
    if until:
        # One half-open probe after TTL; a crash cannot create an unbounded
        # fresh budget because the attempt is charged before generation.
        attempts = max(0, max_attempts - 1)
    elif attempts >= max_attempts:
        # A crash after charging the final attempt has no failure callback to
        # stamp quarantine. Recover by creating the TTL here instead of leaving
        # the slot permanently exhausted.
        quarantined_until = (now + timedelta(hours=quarantine_hours)).isoformat(timespec="seconds")
        block = _slot_block(
            "",
            basis_hash=basis_hash,
            ownership="pending",
            attempts=attempts,
            quarantined_until=quarantined_until,
            failure_hash=_attr(attrs, "failure_sha256"),
        )
        patched = f"{current[: match.start()]}{block}{current[match.end() :]}"
        atomic_write_text(path, patched)
        return ArtifactAttemptAdmission(False, "quarantined", attempts)
    attempts += 1
    block = _slot_block(
        "",
        basis_hash=basis_hash,
        ownership="pending",
        attempts=attempts,
    )
    patched = f"{current[: match.start()]}{block}{current[match.end() :]}"
    atomic_write_text(path, patched)
    return ArtifactAttemptAdmission(True, "admitted", attempts)


def defer_artifact_attempt(
    path: Path,
    *,
    basis_hash: str,
    detail: str,
    max_attempts: int,
    quarantine_hours: int,
    transient: bool = False,
) -> ArtifactPatchResult:
    """Persist failure/quarantine, or roll back an outage attempt."""
    current = path.read_text(encoding="utf-8")
    match = _SLOT_RE.search(current)
    if not match:
        return ArtifactPatchResult("conflict", detail="artifact slot missing")
    attrs = match.group("attrs")
    attempts = int(_attr(attrs, "attempts") or 0)
    if (
        _attr(attrs, "basis_sha256") != basis_hash
        or _attr(attrs, "ownership") != "pending"
        or match.group("body").strip()
    ):
        return ArtifactPatchResult("conflict", detail="artifact slot changed")
    if transient:
        attempts = max(0, attempts - 1)
    quarantined_until = ""
    if not transient and attempts >= max_attempts:
        quarantined_until = (
            datetime.now(timezone.utc) + timedelta(hours=quarantine_hours)
        ).isoformat(timespec="seconds")
    block = _slot_block(
        "",
        basis_hash=basis_hash,
        ownership="pending",
        attempts=attempts,
        quarantined_until=quarantined_until,
        failure_hash=content_hash(detail),
    )
    patched = f"{current[: match.start()]}{block}{current[match.end() :]}"
    atomic_write_text(path, patched)
    return ArtifactPatchResult("applied")


def reset_generated_artifact_slot(
    path: Path,
    *,
    basis_hash: str,
    backup_dir: Path,
) -> ArtifactPatchResult:
    """Reset only an untouched generated slot; preserve all page edits."""
    current = path.read_text(encoding="utf-8")
    match = _SLOT_RE.search(current)
    if not match:
        return ArtifactPatchResult("conflict", detail="artifact slot missing")
    attrs = match.group("attrs")
    body = match.group("body").strip()
    if _attr(attrs, "basis_sha256") != basis_hash:
        return ArtifactPatchResult("conflict", detail="artifact basis changed")
    if _attr(attrs, "ownership") != "generated":
        return ArtifactPatchResult("conflict", detail="artifact slot is not generated")
    expected_hash = _attr(attrs, "section_sha256")
    if not expected_hash or content_hash(body) != expected_hash:
        return ArtifactPatchResult("conflict", detail="generated slot was edited")
    block = _slot_block("", basis_hash=basis_hash, ownership="pending")
    patched = f"{current[: match.start()]}{block}{current[match.end() :]}"
    backup = backup_dir / content_hash(str(path))[:16] / f"{time.time_ns()}.md"
    atomic_write_text(backup, current)
    atomic_write_text(path, patched)
    return ArtifactPatchResult("applied", backup_path=backup)


def apply_artifact_section(
    path: Path,
    section: str,
    *,
    basis_hash: str,
    backup_dir: Path,
    retries: int = 3,
) -> ArtifactPatchResult:
    """Patch only an untouched pending slot in the latest on-disk page."""
    section = (section or "").strip()
    for _attempt in range(retries):
        current = path.read_text(encoding="utf-8")
        match = _SLOT_RE.search(current)
        if not match:
            return ArtifactPatchResult("conflict", detail="artifact slot missing")
        attrs = match.group("attrs")
        if _attr(attrs, "basis_sha256") != basis_hash:
            return ArtifactPatchResult("conflict", detail="artifact basis changed")
        if _attr(attrs, "ownership") != "pending" or match.group("body").strip():
            return ArtifactPatchResult("conflict", detail="artifact slot was edited")
        section_sha = content_hash(section)
        ownership = "generated" if section else "skipped"
        block = _slot_block(
            section,
            basis_hash=basis_hash,
            ownership=ownership,
            section_hash=section_sha,
        )
        patched = f"{current[: match.start()]}{block}{current[match.end() :]}"
        if path.read_text(encoding="utf-8") != current:
            continue
        backup = backup_dir / content_hash(str(path))[:16] / f"{time.time_ns()}.md"
        atomic_write_text(backup, current)
        atomic_write_text(path, patched)
        return ArtifactPatchResult("applied", backup_path=backup)
    return ArtifactPatchResult("conflict", detail="page changed during artifact patch")


class ArtifactJobDispatcher:
    """Local dispatch seam; a server-aware dispatcher can replace this API."""

    def __init__(self, workers: int = 1):
        self._executor = ThreadPoolExecutor(
            max_workers=max(1, workers), thread_name_prefix="IngestArtifact"
        )
        self._futures: list[Future] = []
        self._results: list = []
        self._job_info: dict[Future, dict] = {}
        self._lock = threading.Lock()

    def submit(
        self,
        fn: Callable,
        /,
        *args,
        wait_until_running: bool = False,
        job_label: str = "learning aids",
        **kwargs,
    ) -> None:
        context = contextvars.copy_context()
        admitted = threading.Event()
        info = {
            "label": job_label,
            "queued_at": time.monotonic(),
            "started_at": None,
        }

        def run():
            info["started_at"] = time.monotonic()
            admitted.set()
            return context.run(fn, *args, **kwargs)

        future = self._executor.submit(run)
        with self._lock:
            self._futures.append(future)
            self._job_info[future] = info
        if wait_until_running:
            admitted.wait()

    def enforce_max_inflight(
        self,
        max_inflight: int,
        on_wait: Callable[[dict], None] | None = None,
        heartbeat_seconds: float = 15.0,
    ) -> None:
        """Backpressure core work so enrichment cannot starve indefinitely."""
        while True:
            with self._lock:
                done = [future for future in self._futures if future.done()]
                self._futures = [future for future in self._futures if not future.done()]
                oldest = self._futures[0] if len(self._futures) >= max_inflight else None
            for future in done:
                self._collect(future)
            if oldest is None:
                return
            self._collect(
                oldest,
                on_wait=on_wait,
                heartbeat_seconds=max(0.01, heartbeat_seconds),
            )
            with self._lock:
                if oldest in self._futures:
                    self._futures.remove(oldest)

    def _collect(
        self,
        future: Future,
        on_wait: Callable[[dict], None] | None = None,
        heartbeat_seconds: float = 15.0,
    ) -> None:
        try:
            if on_wait is None:
                result = future.result()
            else:
                while True:
                    try:
                        result = future.result(timeout=heartbeat_seconds)
                        break
                    except FutureTimeoutError:
                        with self._lock:
                            raw_info = dict(self._job_info.get(future) or {})
                        started = raw_info.get("started_at") or raw_info.get("queued_at")
                        raw_info["elapsed_seconds"] = max(
                            0.0, time.monotonic() - float(started or time.monotonic())
                        )
                        on_wait(raw_info)
        except Exception as exc:
            result = {"status": "failed", "detail": str(exc)}
        with self._lock:
            self._results.append(result)
            self._job_info.pop(future, None)

    def wait(self) -> list:
        with self._lock:
            futures = list(self._futures)
            self._futures.clear()
        for future in futures:
            self._collect(future)
        with self._lock:
            results = list(self._results)
            self._results.clear()
        return results

    def shutdown(self) -> None:
        self._executor.shutdown(wait=True)
