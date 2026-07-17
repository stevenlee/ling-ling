"""Auto-tune state store — Metacognition M4.

Holds the LIVE values of auto-tuned numeric knobs (seeded from config defaults,
nudged by `maintenance/autotune.py`, read by consumers). Mirrors how Cortex
decay keeps its calibrated `base_days` in cortex_decay_state.json and consumers
read it via `load_params`.

`get_tuned(name, default)` is the consumer entry point: it returns the live
override ONLY when AUTOTUNE_ENABLED — so flipping the master switch off cleanly
reverts every knob to its config default, with no code change at the call site.

State shape (autotune_state.json):
    {
      "params":   {"<KNOB>": <number>},          # current live value
      "changes":  {"<KNOB>": [                    # audit trail / rollback basis
          {"ts","from","to","dir","metric_before","reason"}]},
      "last_tune": {"<KNOB>": "<iso>"},           # interval gate
    }
"""

from __future__ import annotations

import json
import logging
from pathlib import Path


def _state_path(state_file: Path | None) -> Path:
    if state_file is not None:
        return state_file
    from core.config import AUTOTUNE_STATE_FILE

    return AUTOTUNE_STATE_FILE


def load_state(state_file: Path | None = None) -> dict:
    path = _state_path(state_file)
    base = {"params": {}, "changes": {}, "last_tune": {}}
    try:
        if path.exists():
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                base.update({k: data.get(k, base[k]) for k in base})
    except Exception as e:
        logging.warning(f"autotune_store: failed to load {path.name}: {e}")
    return base


def save_state(state: dict, state_file: Path | None = None) -> None:
    path = _state_path(state_file)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(path)
    except Exception as e:
        logging.warning(f"autotune_store: failed to save {path.name}: {e}")


def get_tuned(
    name: str,
    default,
    *,
    state_file: Path | None = None,
    min_value=None,
    max_value=None,
):
    """Live value for a knob, or `default`. Returns the override only when
    AUTOTUNE_ENABLED — so turning the master switch off reverts to config.
    Fail-open: any error → default (the tuner can never break a consumer)."""
    try:
        from core.config import AUTOTUNE_ENABLED

        if not AUTOTUNE_ENABLED:
            return default
        val = load_state(state_file).get("params", {}).get(name)
        if val is None:
            return default
        value = type(default)(val)  # keep the consumer's expected type (int/float)
        # Persisted state can outlive the code/config that established its
        # bounds. Enforce safety at the consumer seam even during cooldown or
        # insufficient-sample periods, when the tuner itself does not write.
        if min_value is not None:
            value = max(type(default)(min_value), value)
        if max_value is not None:
            value = min(type(default)(max_value), value)
        return value
    except Exception as e:
        logging.debug(f"autotune_store.get_tuned({name}) failed, using default: {e}")
        return default
