"""Nightly Cortex decay pass — reinforcement, transitions, calibration.

Order matters: behavioral reinforcements first (tonight's signals can
revive a page before it gets demoted), then hysteresis transitions
(facets enter/leave the index), then the damped revival-rate
calibration. The pass only writes pages that actually changed.

Behavioral signals (plan §3 weights):
- retrieval hit  (gain 0.5): the page surfaced in Q&A within the day.
- user edit      (gain 1.0): file mtime changed while frontmatter
  `updated` didn't — only a human (Obsidian) writes that way; machine
  writes always bump `updated`.
- revalidation   (gain 0.25, quota/night): fading high-S pages get
  re-verified against their evidence sources (sleep replays the
  important memories first); a failed re-verification dents confidence.

Ledger (CORTEX_DECAY_STATE_FILE): live params, per-page observation
baselines (mtime/updated/last dates), transition log (drives the
revival-rate calibration), last_calibration.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

from core.config import (
    CORTEX_DECAY_ENABLED,
    CORTEX_DECAY_STATE_FILE,
    CORTEX_DIR,
    CORTEX_REVALIDATIONS_PER_NIGHT,
    CORTEX_REVIVAL_TARGET_HIGH,
    CORTEX_REVIVAL_TARGET_LOW,
    MAINTENANCE_LOG_FILE,
    NOTES_DIR,
    PAGES_DIR,
)
from core.parser import strip_body_frontmatter
from services.cortex_decay import (
    GAIN_RETRIEVAL,
    GAIN_REVALIDATION,
    GAIN_USER_EDIT,
    derive_status,
    load_params,
    reinforce,
    retrievability,
)
from services.cortex_store import active_evidence, load_all_pages, save_cortex_page

_REVALIDATION_COOLDOWN_DAYS = 7
_CALIBRATION_INTERVAL_DAYS = 30
_CALIBRATION_MIN_SAMPLES = 20
_CALIBRATION_STEP = 0.2  # damped: ±20% per adjustment
_BASE_DAYS_MIN, _BASE_DAYS_MAX = 7.0, 90.0
_TRANSITIONS_CAP = 500
_CONFIDENCE_FLOOR = 0.1


@dataclass
class DecayPassResult:
    status: str  # "succeeded" | "skipped"
    message: str
    reinforced: int = 0
    transitions: list = field(default_factory=list)
    revalidated: int = 0
    revalidation_failures: int = 0
    calibrated: bool = False


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _load_state(path: Path) -> dict:
    default = {
        "params": {},
        "observed": {},  # claim_id -> {mtime, updated, retrieval_date, revalidated}
        "transitions": [],
        "last_calibration": "",
    }
    try:
        if path.exists():
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                for key, value in default.items():
                    data.setdefault(key, value)
                return data
    except Exception as e:
        logging.warning(f"DecayPass: state unreadable, starting fresh: {e}")
    return default


def _save_state(path: Path, state: dict) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(path)
    except Exception as e:
        logging.warning(f"DecayPass: state write failed: {e}")


def _load_source_contents(titles: list[str], pages_dir: Path, notes_dir: Path) -> list[str]:
    contents = []
    for title in titles:
        for root in (pages_dir, notes_dir):
            target = root / f"{title}.md"
            if target.exists():
                try:
                    body, _ = strip_body_frontmatter(target.read_text(encoding="utf-8"))
                    contents.append(body[:2000])
                except Exception:
                    pass
                break
    return contents


def run_decay_pass(
    llm,
    rag,
    *,
    cortex_dir: Path = None,
    state_file: Path = None,
    log_path: Path = None,
    pages_dir: Path = None,
    notes_dir: Path = None,
    revalidations: int = None,
    enabled: bool = None,
    now: datetime = None,
) -> DecayPassResult:
    cortex_dir = cortex_dir or CORTEX_DIR
    state_file = state_file or CORTEX_DECAY_STATE_FILE
    log_path = log_path or MAINTENANCE_LOG_FILE
    pages_dir = pages_dir or PAGES_DIR
    notes_dir = notes_dir or NOTES_DIR
    revalidations = revalidations if revalidations is not None else CORTEX_REVALIDATIONS_PER_NIGHT
    enabled = enabled if enabled is not None else CORTEX_DECAY_ENABLED
    now = now or datetime.now()

    if not enabled:
        return DecayPassResult(status="skipped", message="Cortex decay disabled.")

    pages = load_all_pages(cortex_dir)
    if not pages:
        return DecayPassResult(status="skipped", message="No cortex pages.")

    state = _load_state(state_file)
    params = load_params(state_file)
    observed = state["observed"]
    dirty: set[str] = set()
    result = DecayPassResult(status="succeeded", message="")

    # ── 1. Behavioral reinforcements ─────────────────────────────────
    today = now.strftime("%Y-%m-%d")
    retrieval_hits = set()
    trace_store = getattr(llm, "trace_store", None)
    if trace_store is not None and hasattr(trace_store, "recently_retrieved_titles"):
        try:
            retrieval_hits = set(trace_store.recently_retrieved_titles(1))
        except Exception:
            pass

    for page in pages:
        if page.status == "falsified":
            continue
        record = observed.setdefault(page.claim_id, {})

        # User edit: mtime moved while machine timestamp `updated` didn't.
        try:
            mtime = page.path.stat().st_mtime
        except OSError:
            mtime = None
        baseline_mtime = record.get("mtime")
        baseline_updated = record.get("updated")
        if (
            mtime is not None
            and baseline_mtime is not None
            and mtime != baseline_mtime
            and page.updated == baseline_updated
        ):
            reinforce(page, GAIN_USER_EDIT, params=params, now=now)
            dirty.add(page.claim_id)
            result.reinforced += 1

        # Retrieval hit (once per day per page).
        if page.claim_id in retrieval_hits and record.get("retrieval_date") != today:
            reinforce(page, GAIN_RETRIEVAL, params=params, now=now)
            record["retrieval_date"] = today
            dirty.add(page.claim_id)
            result.reinforced += 1

    # ── 2. Revalidation (sleep replays the important fading memories) ─
    by_id = {p.claim_id: p for p in pages}
    fading = sorted(
        (
            p
            for p in pages
            if p.status == "fading"
            and (observed.get(p.claim_id, {}).get("revalidated", "") or "0000")[:10]
            < (now - timedelta(days=_REVALIDATION_COOLDOWN_DAYS)).strftime("%Y-%m-%d")
        ),
        key=lambda p: -p.S,
    )
    for page in fading[: max(0, revalidations)]:
        if not hasattr(llm, "refute_insight"):
            break
        sources = []
        for evidence in active_evidence(page):
            sources.extend(evidence.get("sources") or [])
        contents = _load_source_contents(sources, pages_dir, notes_dir)
        if not contents:
            continue
        try:
            verdict = llm.refute_insight(page.claim, contents).get("verdict")
        except Exception as e:
            logging.warning(f"DecayPass: revalidation failed for {page.claim_id}: {e}")
            continue
        observed.setdefault(page.claim_id, {})["revalidated"] = _now_iso()
        result.revalidated += 1
        if verdict == "survived":
            reinforce(page, GAIN_REVALIDATION, params=params, now=now)
        elif verdict == "refuted":
            page.confidence = round(max(_CONFIDENCE_FLOOR, page.confidence - 0.1), 4)
            page.updated = _now_iso()
            result.revalidation_failures += 1
        dirty.add(page.claim_id)

    # ── 3. Hysteresis transitions (facets enter/leave the index) ─────
    for page in pages:
        if page.status == "falsified":
            continue
        r = retrievability(
            page.S,
            page.last_reinforced_at,
            base_days=params["base_days"],
            growth=params["growth"],
            now=now,
        )
        new_status = derive_status(page.status, r)
        if new_status == page.status:
            continue
        old_status = page.status
        page.status = new_status
        page.updated = _now_iso()
        dirty.add(page.claim_id)
        state["transitions"].append(
            {"claim_id": page.claim_id, "from": old_status, "to": new_status, "ts": _now_iso()}
        )
        result.transitions.append((page.claim_id, old_status, new_status))
        try:
            if new_status == "dormant":
                rag.remove_facets(page.path)
            elif old_status == "dormant":
                rag.add_facets(page.path, page.claim_id, [page.claim])
        except Exception as e:
            logging.warning(f"DecayPass: facet membership update failed: {e}")
    state["transitions"] = state["transitions"][-_TRANSITIONS_CAP:]

    # ── 4. Persist changed pages + observation baselines ─────────────
    for claim_id in dirty:
        save_cortex_page(by_id[claim_id])
    for page in pages:
        record = observed.setdefault(page.claim_id, {})
        try:
            record["mtime"] = page.path.stat().st_mtime
        except OSError:
            pass
        record["updated"] = page.updated
    # Drop observations for deleted pages.
    for claim_id in list(observed):
        if claim_id not in by_id:
            del observed[claim_id]

    # ── 5. Damped revival-rate calibration ───────────────────────────
    result.calibrated = _maybe_calibrate(state, params, now)

    _save_state(state_file, state)

    result.message = (
        f"Cortex decay: {result.reinforced} reinforcement(s), "
        f"{len(result.transitions)} transition(s), "
        f"{result.revalidated} revalidated ({result.revalidation_failures} failed)"
        + (", params recalibrated" if result.calibrated else "")
        + f". base={params['base_days']:.1f}d"
    )
    _append_log(log_path, result)
    return result


def _maybe_calibrate(state: dict, params: dict, now: datetime) -> bool:
    last = state.get("last_calibration") or ""
    if last:
        try:
            if (now - datetime.fromisoformat(last)).days < _CALIBRATION_INTERVAL_DAYS:
                return False
        except ValueError:
            pass

    demotions = [t for t in state["transitions"] if t.get("to") == "dormant"]
    if len(demotions) < _CALIBRATION_MIN_SAMPLES:
        return False
    demoted_ids = {t["claim_id"] for t in demotions}
    revivals = {
        t["claim_id"]
        for t in state["transitions"]
        if t.get("from") == "dormant" and t["claim_id"] in demoted_ids
    }
    revival_rate = len(revivals) / len(demoted_ids)

    base = params["base_days"]
    if revival_rate > CORTEX_REVIVAL_TARGET_HIGH:
        base *= 1 + _CALIBRATION_STEP  # 太多人被提早埋葬 → 衰減放慢
    elif revival_rate < CORTEX_REVIVAL_TARGET_LOW:
        base *= 1 - _CALIBRATION_STEP  # 沒人懷念 dormant 區 → 可以更果斷
    else:
        return False
    params["base_days"] = max(_BASE_DAYS_MIN, min(_BASE_DAYS_MAX, round(base, 2)))
    state["params"] = dict(params)
    state["last_calibration"] = _now_iso()
    logging.info(f"DecayPass: revival rate {revival_rate:.0%} → base_days = {params['base_days']}")
    return True


def _append_log(log_path: Path, result: DecayPassResult) -> None:
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
        with log_path.open("a", encoding="utf-8") as f:
            f.write(f"\n## [{stamp}] Cortex Decay | {result.message}\n")
    except Exception as e:
        logging.warning(f"DecayPass: log append failed: {e}")
