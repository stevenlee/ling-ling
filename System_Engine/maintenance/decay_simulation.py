"""Decay backtest — replay reinforcement history through the S/R model.

Phase 3's hard prerequisite (plan §3.1): before live calibration data
exists (revival rate is a months-scale signal), pick base/growth by
replaying each page's KNOWN reinforcement history — page creation,
every evidence entry (rediscoveries), and trace-store retrieval hits —
through a (base, growth) grid and looking at where pages would sit
today and how often they'd cross state boundaries.

What to look for in the output:
- a column where most pages sit dormant on day one → base too short;
- a column with zero transitions ever → base too long to matter;
- pick the knee. Run: ../venv/bin/python -m maintenance.decay_simulation
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from core.config import CORTEX_DIR
from services.cortex_decay import derive_status, retrievability
from services.cortex_store import load_all_pages

GRID_BASE_DAYS = (14.0, 21.0, 30.0)
GRID_GROWTH = (1.5, 1.8, 2.2)


@dataclass
class SimulationCell:
    base_days: float
    growth: float
    status_counts: dict
    transition_count: int
    mean_r: float


def _page_events(page, extra_events: dict) -> list[datetime]:
    """Chronological reinforcement events reconstructed from history."""
    events = []
    for stamp in [page.created] + [e.get("date", "") for e in page.evidence[1:]]:
        try:
            events.append(datetime.fromisoformat(str(stamp)))
        except (TypeError, ValueError):
            continue
    events.extend(extra_events.get(page.claim_id, []))
    return sorted(set(events))


def simulate(
    pages,
    *,
    base_days: float,
    growth: float,
    now: datetime = None,
    extra_events: dict = None,
    gain: float = 1.0,
) -> SimulationCell:
    """Replay each page's events with the spacing rule; report end state."""
    now = now or datetime.now()
    extra_events = extra_events or {}
    status_counts = {"active": 0, "fading": 0, "dormant": 0}
    transitions = 0
    r_values = []

    for page in pages:
        events = _page_events(page, extra_events)
        if not events:
            continue
        S = 1.0
        last = events[0]
        status = "active"
        for event in events[1:]:
            r_event = retrievability(
                S, last.isoformat(), base_days=base_days, growth=growth, now=event,
            )
            new_status = derive_status(status, r_event)
            if new_status != status:
                transitions += 1
                status = new_status
            S += gain * (1.0 - r_event)
            last = event

        # Walk daily from the final event to now to count boundary crossings.
        cursor = last
        while cursor < now:
            cursor = min(now, cursor.replace(hour=23, minute=59, second=59))
            r_cursor = retrievability(
                S, last.isoformat(), base_days=base_days, growth=growth, now=cursor,
            )
            new_status = derive_status(status, r_cursor)
            if new_status != status:
                transitions += 1
                status = new_status
            if cursor >= now:
                break
            cursor = datetime.fromordinal(cursor.toordinal() + 1)

        r_final = retrievability(
            S, last.isoformat(), base_days=base_days, growth=growth, now=now,
        )
        r_values.append(r_final)
        status_counts[status] = status_counts.get(status, 0) + 1

    return SimulationCell(
        base_days=base_days,
        growth=growth,
        status_counts=status_counts,
        transition_count=transitions,
        mean_r=round(sum(r_values) / len(r_values), 3) if r_values else 0.0,
    )


def run_grid(pages, *, now: datetime = None, extra_events: dict = None) -> list[SimulationCell]:
    return [
        simulate(pages, base_days=b, growth=g, now=now, extra_events=extra_events)
        for b in GRID_BASE_DAYS
        for g in GRID_GROWTH
    ]


def main():
    pages = load_all_pages(CORTEX_DIR)
    if not pages:
        print("No cortex pages to simulate.")
        return
    print(f"Simulating {len(pages)} pages over the (base, growth) grid:\n")
    print(f"{'base':>6} {'growth':>7} {'active':>7} {'fading':>7} {'dormant':>8} "
          f"{'trans':>6} {'mean_R':>7}")
    for cell in run_grid(pages):
        c = cell.status_counts
        print(f"{cell.base_days:>6.0f} {cell.growth:>7.1f} {c.get('active', 0):>7} "
              f"{c.get('fading', 0):>7} {c.get('dormant', 0):>8} "
              f"{cell.transition_count:>6} {cell.mean_r:>7.3f}")


if __name__ == "__main__":
    main()
