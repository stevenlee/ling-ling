"""Cortex Phase 5 F1 defense 5: echo-chamber canary."""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.absolute()))
os.environ.setdefault("LLM_PROVIDER", "vllm")

from maintenance.echo_canary import run_echo_canary


def _insight(d: Path, name: str, novelty: float, groundedness: float, grounded: bool):
    fm = [
        "---",
        "signals:",
        f"  novelty: {novelty}",
        f"  groundedness: {groundedness}",
    ]
    if grounded:
        fm += ["grounded_on:", "- cortex-abc"]
    fm += ["---", "", "body"]
    (d / name).write_text("\n".join(fm), encoding="utf-8")


def _run(tmp_path):
    ins = tmp_path / "Insights"; ins.mkdir()
    return ins, lambda: run_echo_canary(
        insights_dir=ins, report_dir=tmp_path / "rep", log_path=tmp_path / "log.md"
    )


def test_insufficient_samples(tmp_path):
    ins, run = _run(tmp_path)
    for i in range(3):  # below _MIN_PER_GROUP
        _insight(ins, f"g{i}.md", 0.3, 0.8, grounded=True)
        _insight(ins, f"c{i}.md", 0.5, 0.6, grounded=False)
    r = run()
    assert r.status == "insufficient"
    assert r.report_path.exists()


def test_alarm_on_novelty_collapse(tmp_path):
    ins, run = _run(tmp_path)
    for i in range(6):
        _insight(ins, f"g{i}.md", 0.20, 0.85, grounded=True)   # grounded: low novelty
        _insight(ins, f"c{i}.md", 0.55, 0.60, grounded=False)  # cold: high novelty
    r = run()
    assert r.status == "alarm"                                  # echo-chamber signature
    assert "同溫層" in r.message
    log = (tmp_path / "log.md").read_text(encoding="utf-8")
    assert "alarm" in log


def test_ok_when_novelty_holds(tmp_path):
    ins, run = _run(tmp_path)
    for i in range(6):
        _insight(ins, f"g{i}.md", 0.52, 0.85, grounded=True)   # grounded: novelty holds,
        _insight(ins, f"c{i}.md", 0.55, 0.60, grounded=False)  #   groundedness even higher
    r = run()
    assert r.status == "ok"
    assert r.stats["grounded_groundedness"] > r.stats["cold_groundedness"]  # the intended benefit


def test_empty_insights_is_insufficient(tmp_path):
    _, run = _run(tmp_path)
    r = run()
    assert r.status == "insufficient" and r.grounded_n == 0
