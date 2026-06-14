"""Numeric auto-tuner — Metacognition M4.

The only phase of the self-improvement arc that changes behavior WITHOUT a
human gate — so it is deliberately the most constrained:

  - only registered NUMERIC knobs (never prompts, never code),
  - each bound to a concrete outcome metric,
  - a minimum-sample gate (no tuning on noise),
  - DAMPED steps (±20%, like the decay calibration it models on),
  - hard bounds per knob,
  - and AUTO-ROLLBACK: if the previous change was followed by the metric
    regressing past the danger threshold, revert it and freeze the knob.

The tuner writes only to autotune_state.json (its own live-param store);
consumers read via `services.autotune_store.get_tuned`. Default OFF.

v1 registers ONE knob: CORTEX_GROUND_FRACTION, bound to the echo-chamber
canary's novelty gap (grounded vs cold). This closes the F1 loop — the canary
that only *alarmed* now *acts*: too much echo-chamber signature → dial grounding
down; consistently healthy → edge it back up (bounded). Until F1 accumulates
≥5 grounded + ≥5 cold insights, the metric is "insufficient" and the tuner
correctly does nothing.

See DesignDoc/SelfImprovement_metacognition_plan.md.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from services.autotune_store import load_state, save_state

_STEP = 0.20                 # damped: ±20% per adjustment (matches decay calibration)
_INTERVAL_DAYS = 7           # don't re-tune the same knob more often than this


@dataclass
class Tunable:
    """One auto-tunable knob bound to an outcome metric.

    metric_fn() -> (value, n_samples). `worse_when_high` says which direction of
    the metric means "the knob is too aggressive": if True, a metric above
    `target_hi` dials the knob DOWN and below `target_lo` dials it UP (and vice
    versa). `danger` is the rollback trigger: if an UP step is followed by the
    metric crossing into danger, revert.
    """
    name: str
    default: float
    lo: float
    hi: float
    metric_fn: object                 # callable -> (float|None, int)
    target_lo: float
    target_hi: float
    danger: float
    worse_when_high: bool = True
    is_int: bool = False
    min_samples: int = 5
    step: float = _STEP
    interval_days: int = _INTERVAL_DAYS


@dataclass
class KnobOutcome:
    name: str
    action: str = "hold"              # hold | up | down | rollback | insufficient | cooldown
    old: float | None = None
    new: float | None = None
    metric: float | None = None
    samples: int = 0
    reason: str = ""


@dataclass
class AutotuneResult:
    status: str = "succeeded"
    message: str = ""
    outcomes: list = field(default_factory=list)   # list[KnobOutcome]


def _clamp(t: Tunable, v: float) -> float:
    v = max(t.lo, min(t.hi, v))
    return int(round(v)) if t.is_int else round(v, 4)


def _last_change(state: dict, name: str) -> dict | None:
    changes = state.get("changes", {}).get(name) or []
    return changes[-1] if changes else None


def _record(state: dict, name: str, change: dict) -> None:
    state.setdefault("changes", {}).setdefault(name, []).append(change)
    # keep the trail bounded
    state["changes"][name] = state["changes"][name][-50:]


def _due(state: dict, name: str, now: datetime, interval_days: int) -> bool:
    last = (state.get("last_tune") or {}).get(name) or ""
    if not last:
        return True
    try:
        return (now - datetime.fromisoformat(last)).days >= interval_days
    except ValueError:
        return True


def _tune_one(t: Tunable, state: dict, now: datetime) -> KnobOutcome:
    cur = state.get("params", {}).get(t.name, t.default)
    metric, n = t.metric_fn()
    out = KnobOutcome(name=t.name, old=cur, new=cur, metric=metric, samples=n)

    if metric is None or n < t.min_samples:
        out.action = "insufficient"
        out.reason = f"樣本不足（{n}/{t.min_samples}）,不調整。"
        return out

    # ── auto-rollback: a prior UP step that pushed us into danger gets reverted.
    last = _last_change(state, t.name)
    if last and last.get("dir") == "up":
        in_danger = metric >= t.danger if t.worse_when_high else metric <= t.danger
        if in_danger:
            reverted = _clamp(t, last.get("from", t.default))
            state.setdefault("params", {})[t.name] = reverted
            state.setdefault("last_tune", {})[t.name] = now.isoformat(timespec="seconds")
            _record(state, t.name, {"ts": now.isoformat(timespec="seconds"),
                                    "from": cur, "to": reverted, "dir": "rollback",
                                    "metric_before": metric, "reason": "上一次調升後指標進入危險區,回退"})
            out.action, out.new = "rollback", reverted
            out.reason = f"指標 {metric:.3f} 進危險區（≥{t.danger}）,回退上次調升 {cur}→{reverted}。"
            return out

    if not _due(state, t.name, now, t.interval_days):
        out.action = "cooldown"
        out.reason = "距上次調整未滿間隔,跳過。"
        return out

    # ── damped step toward the band.
    too_aggressive = metric > t.target_hi if t.worse_when_high else metric < t.target_lo
    too_timid = metric < t.target_lo if t.worse_when_high else metric > t.target_hi
    if too_aggressive:
        new = _clamp(t, cur * (1 - t.step))
        direction = "down"
    elif too_timid:
        new = _clamp(t, cur * (1 + t.step))
        direction = "up"
    else:
        out.action = "hold"
        out.reason = f"指標 {metric:.3f} 在目標帶內,維持 {cur}。"
        return out

    if new == cur:
        out.action = "hold"
        out.reason = f"已達邊界（{cur}）,無法再{('降' if direction == 'down' else '升')}。"
        return out

    state.setdefault("params", {})[t.name] = new
    state.setdefault("last_tune", {})[t.name] = now.isoformat(timespec="seconds")
    _record(state, t.name, {"ts": now.isoformat(timespec="seconds"), "from": cur, "to": new,
                            "dir": direction, "metric_before": metric, "reason": out.reason})
    out.action, out.new = direction, new
    out.reason = (f"指標 {metric:.3f} {'過高' if too_aggressive else '偏低'},"
                  f"{'調降' if direction == 'down' else '調升'} {cur}→{new}。")
    return out


# ── the registered knobs ──────────────────────────────────────────────────

def _ground_fraction_metric():
    """Echo-chamber canary novelty gap: cold_novelty - grounded_novelty.
    Higher = grounded insights losing novelty = echo-chamber risk = knob too
    aggressive. Samples = min(grounded_n, cold_n)."""
    try:
        from maintenance.echo_canary import run_echo_canary
        r = run_echo_canary()
        s = r.stats or {}
        gn, cn = s.get("grounded_novelty"), s.get("cold_novelty")
        n = min(r.grounded_n, r.cold_n)
        if gn is None or cn is None:
            return None, n
        return (cn - gn), n
    except Exception as e:
        logging.debug(f"autotune: ground_fraction metric failed: {e}")
        return None, 0


def _default_tunables() -> list:
    from core.config import (
        CORTEX_GROUND_FRACTION, CORTEX_GROUND_MIN_FRACTION, CORTEX_GROUND_MAX_FRACTION,
    )
    return [
        Tunable(
            name="CORTEX_GROUND_FRACTION",
            default=CORTEX_GROUND_FRACTION,
            lo=CORTEX_GROUND_MIN_FRACTION, hi=CORTEX_GROUND_MAX_FRACTION,
            metric_fn=_ground_fraction_metric,
            # novelty gap: <=0 means grounded is at least as novel (very safe →
            # edge up); >0.05 means a real drop (dial down); >=0.10 is danger.
            target_lo=0.0, target_hi=0.05, danger=0.10,
            worse_when_high=True, is_int=False, min_samples=5,
        ),
    ]


def run_autotune(
    *, state_file: Path | None = None, now: datetime | None = None, tunables: list | None = None,
) -> AutotuneResult:
    now = now or datetime.now()
    tunables = tunables if tunables is not None else _default_tunables()
    state = load_state(state_file)

    outcomes = [_tune_one(t, state, now) for t in tunables]
    # Persist only if something actually changed a param.
    if any(o.action in ("up", "down", "rollback") for o in outcomes):
        save_state(state, state_file)

    changed = [o for o in outcomes if o.action in ("up", "down", "rollback")]
    result = AutotuneResult(outcomes=outcomes)
    if changed:
        result.message = "；".join(f"{o.name} {o.old}→{o.new}（{o.action}）" for o in changed)
    else:
        result.message = "本次無調整：" + "／".join(f"{o.name}:{o.action}" for o in outcomes)
    return result
