"""Metacognition M4: numeric auto-tuner (damped, gated, bounded, rollback)."""
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.absolute()))
os.environ.setdefault("LLM_PROVIDER", "vllm")

from maintenance.autotune import Tunable, run_autotune
from services.autotune_store import load_state, save_state, get_tuned


def _knob(metric, n, *, default=0.7, lo=0.3, hi=0.85, worse_when_high=True):
    return Tunable(
        name="K", default=default, lo=lo, hi=hi,
        metric_fn=lambda: (metric, n),
        target_lo=0.0, target_hi=0.05, danger=0.10,
        worse_when_high=worse_when_high, is_int=False, min_samples=5,
    )


def _run(tmp_path, knob, now=None):
    sf = tmp_path / "autotune.json"
    return run_autotune(state_file=sf, now=now or datetime(2026, 6, 14), tunables=[knob]), sf


# ── gates ─────────────────────────────────────────────────────────────────

def test_insufficient_samples_no_change(tmp_path):
    res, sf = _run(tmp_path, _knob(metric=0.2, n=2))    # n<5
    o = res.outcomes[0]
    assert o.action == "insufficient" and o.new == 0.7
    assert not sf.exists()                               # no write when nothing changed


def test_hold_when_metric_in_band(tmp_path):
    res, _ = _run(tmp_path, _knob(metric=0.02, n=10))    # within [0, 0.05]
    assert res.outcomes[0].action == "hold"


# ── damped steps ──────────────────────────────────────────────────────────

def test_step_down_when_metric_too_high(tmp_path):
    res, sf = _run(tmp_path, _knob(metric=0.30, n=10))   # >target_hi → too aggressive
    o = res.outcomes[0]
    assert o.action == "down" and o.new == round(0.7 * 0.8, 4)   # 0.56
    assert load_state(sf)["params"]["K"] == o.new


def test_step_up_when_metric_too_low(tmp_path):
    # metric below target_lo (grounded MORE novel than cold) → safe → edge up
    res, _ = _run(tmp_path, _knob(metric=-0.05, n=10))
    o = res.outcomes[0]
    assert o.action == "up" and o.new == round(0.7 * 1.2, 4)     # 0.84


def test_bounds_clamp(tmp_path):
    # already near the ceiling → an up-step clamps to hi, reported as hold
    res, _ = _run(tmp_path, _knob(metric=-0.05, n=10, default=0.84, hi=0.85))
    o = res.outcomes[0]
    assert o.new <= 0.85


# ── interval gate ─────────────────────────────────────────────────────────

def test_cooldown_blocks_refrequent_tuning(tmp_path):
    sf = tmp_path / "autotune.json"
    save_state({"params": {"K": 0.7}, "changes": {},
                "last_tune": {"K": datetime(2026, 6, 13).isoformat()}}, sf)
    res = run_autotune(state_file=sf, now=datetime(2026, 6, 14),   # 1 day < 7
                       tunables=[_knob(metric=0.30, n=10)])
    assert res.outcomes[0].action == "cooldown"


# ── auto-rollback ─────────────────────────────────────────────────────────

def test_rollback_after_up_step_enters_danger(tmp_path):
    sf = tmp_path / "autotune.json"
    # Previously stepped UP 0.7→0.84; now the metric is in the danger zone.
    save_state({"params": {"K": 0.84},
                "changes": {"K": [{"ts": "2026-06-01T00:00:00", "from": 0.7, "to": 0.84,
                                   "dir": "up", "metric_before": -0.02}]},
                "last_tune": {"K": "2026-06-01T00:00:00"}}, sf)
    res = run_autotune(state_file=sf, now=datetime(2026, 6, 14),
                       tunables=[_knob(metric=0.12, n=10)])      # >= danger 0.10
    o = res.outcomes[0]
    assert o.action == "rollback" and o.new == 0.7
    assert load_state(sf)["params"]["K"] == 0.7


# ── store / consumer entry point ──────────────────────────────────────────

def test_get_tuned_respects_master_switch(tmp_path, monkeypatch):
    sf = tmp_path / "autotune.json"
    save_state({"params": {"CORTEX_GROUND_FRACTION": 0.5}, "changes": {}, "last_tune": {}}, sf)
    monkeypatch.setattr("core.config.AUTOTUNE_ENABLED", False)
    assert get_tuned("CORTEX_GROUND_FRACTION", 0.7, state_file=sf) == 0.7   # off → default
    monkeypatch.setattr("core.config.AUTOTUNE_ENABLED", True)
    assert get_tuned("CORTEX_GROUND_FRACTION", 0.7, state_file=sf) == 0.5   # on → live value


def test_get_tuned_preserves_type(tmp_path, monkeypatch):
    sf = tmp_path / "autotune.json"
    save_state({"params": {"N": 8.0}, "changes": {}, "last_tune": {}}, sf)
    monkeypatch.setattr("core.config.AUTOTUNE_ENABLED", True)
    v = get_tuned("N", 3, state_file=sf)        # default is int → return int
    assert v == 8 and isinstance(v, int)
