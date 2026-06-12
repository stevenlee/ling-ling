"""Cortex decay — the dual-strength (S/R) long-term memory model.

Bjork's New Theory of Disuse, operationalized (CortexMemory plan §3):

    S  storage strength      — only grows; how deeply consolidated.
    R  retrievability        — exp(−Δt / t½(S)); computed at read time,
                               never stored (no write storms, no drift).
    t½(S) = base × growth^S  — half-life grows exponentially with S.

Spacing effect (the anti-inflation rule): a reinforcement at high R is
worth almost nothing; one that arrives when the memory is nearly
forgotten consolidates deeply:

    ΔS = gain × (1 − R_at_event);  R resets to 1 (last_reinforced_at=now)

States derive from R with HYSTERESIS — promote thresholds sit above
demote thresholds so pages can't flap across a boundary and churn the
facet index:

    demote:  active→fading at R<0.5;  fading→dormant at R<0.2
    promote: dormant→fading at R>0.3; fading→active at R>0.6

`base`/`growth` start from config but live in CORTEX_DECAY_STATE_FILE —
the nightly pass damped-calibrates base against the revival rate
(dormant pages later revived; too many = decaying too fast).
"""

from __future__ import annotations

import json
import logging
import math
from datetime import datetime
from pathlib import Path

from core.config import (
    CORTEX_DECAY_BASE_DAYS,
    CORTEX_DECAY_GROWTH,
    CORTEX_DECAY_STATE_FILE,
)

DEMOTE_FADING = 0.5      # active → fading below this
DEMOTE_DORMANT = 0.2     # fading → dormant below this
PROMOTE_FADING = 0.3     # dormant → fading above this
PROMOTE_ACTIVE = 0.6     # fading → active above this

# Reinforcement gains by event kind (plan §3 weights).
GAIN_REDISCOVERY = 1.0   # independent re-discovery (merge path)
GAIN_USER_EDIT = 1.0     # the user touched the page
GAIN_RETRIEVAL = 0.5     # surfaced in Q&A retrieval
GAIN_REVALIDATION = 0.25 # nightly re-verification passed

_SECONDS_PER_DAY = 86400.0


def load_params(state_file: Path = None) -> dict:
    """Live decay params: state-file override, else config initials."""
    state_file = state_file or CORTEX_DECAY_STATE_FILE
    params = {"base_days": CORTEX_DECAY_BASE_DAYS, "growth": CORTEX_DECAY_GROWTH}
    try:
        if state_file.exists():
            data = json.loads(state_file.read_text(encoding="utf-8"))
            stored = data.get("params") if isinstance(data, dict) else None
            if isinstance(stored, dict):
                base = stored.get("base_days")
                growth = stored.get("growth")
                if isinstance(base, (int, float)) and base > 0:
                    params["base_days"] = float(base)
                if isinstance(growth, (int, float)) and growth > 1.0:
                    params["growth"] = float(growth)
    except Exception as e:
        logging.warning(f"CortexDecay: params unreadable, using config: {e}")
    return params


def half_life_days(S: float, *, base_days: float, growth: float) -> float:
    return base_days * (growth ** max(0.0, float(S)))


def retrievability(
    S: float,
    last_reinforced_at: str,
    *,
    base_days: float,
    growth: float,
    now: datetime = None,
) -> float:
    """R = exp(−Δt·ln2 / t½). Unparseable timestamps → 1.0 (fail-open:
    never bury a page because its clock is broken)."""
    try:
        reinforced = datetime.fromisoformat(str(last_reinforced_at))
    except (TypeError, ValueError):
        return 1.0
    now = now or datetime.now()
    delta_days = max(0.0, (now - reinforced).total_seconds() / _SECONDS_PER_DAY)
    t_half = half_life_days(S, base_days=base_days, growth=growth)
    if t_half <= 0:
        return 0.0
    return math.exp(-delta_days * math.log(2) / t_half)


def derive_status(current: str, r: float) -> str:
    """Hysteresis state machine. `falsified` is terminal — decay never
    touches it (that's Phase 4's verdict, not an attention question)."""
    if current == "falsified":
        return "falsified"
    if current == "dormant":
        if r > PROMOTE_ACTIVE:
            return "active"
        if r > PROMOTE_FADING:
            return "fading"
        return "dormant"
    if current == "fading":
        if r > PROMOTE_ACTIVE:
            return "active"
        if r < DEMOTE_DORMANT:
            return "dormant"
        return "fading"
    # default: active
    if r < DEMOTE_DORMANT:
        return "dormant"
    if r < DEMOTE_FADING:
        return "fading"
    return "active"


def reinforce(page, gain: float, *, params: dict = None, now: datetime = None) -> float:
    """Apply a spacing-effect reinforcement to a CortexPage in place.

    Returns the ΔS applied. R resets to 1 via last_reinforced_at=now;
    S grows by gain × (1−R_at_event) — same-night duplicates are nearly
    free of effect, near-forgotten rediscoveries consolidate deeply.
    Status is NOT recomputed here; the nightly pass owns transitions.
    """
    params = params or load_params()
    now = now or datetime.now()
    r_now = retrievability(
        page.S, page.last_reinforced_at,
        base_days=params["base_days"], growth=params["growth"], now=now,
    )
    delta = max(0.0, float(gain)) * (1.0 - r_now)
    page.S = round(float(page.S) + delta, 4)
    page.last_reinforced_at = now.isoformat(timespec="seconds")
    page.updated = now.isoformat(timespec="seconds")
    return delta
