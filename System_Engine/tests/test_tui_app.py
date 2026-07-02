"""Headless smoke test for the Textual app: it mounts, the status panel
refreshes without error, and composing a command drops a file into toLingLing/."""

import asyncio
import os

os.environ.setdefault("LLM_PROVIDER", "vllm")

import tui.app as app_mod


def _isolate(monkeypatch, tmp_path):
    monkeypatch.setattr(app_mod, "TO_LLM_DIR", tmp_path)
    # Keep the live panels from touching the real daemon state.
    monkeypatch.setattr(
        app_mod.trace_reader,
        "status_summary",
        lambda: {
            "alive": True,
            "busy": False,
            "message": None,
            "last": None,
            "provider": "vllm",
            "role": "translator",
            "dreaming": "1-5",
            "daydream": True,
        },
    )
    monkeypatch.setattr(app_mod.trace_reader, "recent_runs", lambda *a, **k: [])
    monkeypatch.setattr(app_mod.trace_reader, "tail_maintenance_log", lambda *a, **k: [])
    monkeypatch.setattr(app_mod.trace_reader, "recent_results", lambda *a, **k: [])


def test_app_mounts_and_drops_fieldless_command(tmp_path, monkeypatch):
    _isolate(monkeypatch, tmp_path)
    spec = next(s for s in app_mod.COMMANDS if s.intent == "consolidate")

    async def run():
        app = app_mod.LingLingTUI()
        async with app.run_test(size=(120, 40)) as pilot:
            assert app.query_one("#status")  # mounted
            app._open_compose(spec)
            await pilot.pause()
            app.screen._submit()  # fieldless → writes immediately
            await pilot.pause()

    asyncio.run(run())
    files = list(tmp_path.iterdir())
    assert len(files) == 1
    assert files[0].name.startswith("@ling-consolidate-")
    assert files[0].name.endswith(".md")


def test_compose_with_targets_writes_wikilinks(tmp_path, monkeypatch):
    _isolate(monkeypatch, tmp_path)
    spec = next(s for s in app_mod.COMMANDS if s.intent == "resynthesize")

    async def run():
        app = app_mod.LingLingTUI()
        async with app.run_test(size=(120, 40)) as pilot:
            app._open_compose(spec)
            await pilot.pause()
            app.screen._widgets["targets"].value = "MyDoc"
            app.screen._submit()
            await pilot.pause()

    asyncio.run(run())
    written = next(tmp_path.iterdir()).read_text(encoding="utf-8")
    assert "[[MyDoc]]" in written
