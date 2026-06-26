"""Ling-Ling TUI — companion cockpit (separate process, file-drop + read-only).

Layout:
  ┌ status header (busy/idle · current run · provider/role/dreaming) ┐
  ├ commands (left) ─────────────┬ activity feed (recent runs+log) ─┤
  │                              ├ results (newest fromLingLing/)    │
  └──────────────────────────────┴───────────────────────────────────┘

Selecting a command opens a compose form; submitting drops an @ling-*.md into
toLingLing/ — the exact same channel Obsidian uses. Nothing here touches the DB.
"""

from __future__ import annotations

from datetime import datetime

from rich.table import Table
from rich.text import Text
from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import (
    Button, Checkbox, Footer, Header, Input, Label, OptionList, Select, Static,
)
from textual.widgets.option_list import Option

from core.config import TO_LLM_DIR
from tui import trace_reader
from tui.command_specs import COMMANDS, build_command_file


def _spec_options() -> tuple[list, dict]:
    """Build OptionList items grouped by spec.group, plus an id→spec index."""
    items: list = []
    index: dict[str, object] = {}
    last_group = None
    for spec in COMMANDS:
        if spec.group != last_group:
            items.append(Option(f"── {spec.group} ──", disabled=True))
            last_group = spec.group
        items.append(Option(f"  {spec.label}", id=spec.trigger))
        index[spec.trigger] = spec
    return items, index


class ComposeScreen(ModalScreen[str | None]):
    """A form for one command's fields. On submit, writes the file and dismisses
    with the filename; on cancel, dismisses with None."""

    BINDINGS = [("escape", "cancel", "取消")]

    def __init__(self, spec):
        super().__init__()
        self.spec = spec
        self._widgets: dict[str, object] = {}

    def compose(self) -> ComposeResult:
        with Vertical(id="compose-box"):
            yield Label(f"[b]{self.spec.label}[/b]  ({self.spec.trigger})")
            if self.spec.help:
                yield Label(self.spec.help, classes="dim")
            for f in self.spec.fields:
                yield Label(f.label + (" *" if f.required else ""))
                if f.kind == "flag":
                    w = Checkbox(f.label, value=False, id=f"f_{f.key}")
                elif f.kind == "choice":
                    opts = [(c, c) for c in f.choices]
                    w = Select(opts, prompt="(不指定)", allow_blank=True, id=f"f_{f.key}")
                else:  # links / text
                    placeholder = (
                        "整段=一個標題（含空白OK）；多個用 [[A]] [[B]]"
                        if f.kind == "links" else (f.help or "")
                    )
                    w = Input(placeholder=placeholder, id=f"f_{f.key}")
                self._widgets[f.key] = w
                yield w
            with Horizontal(id="compose-buttons"):
                yield Button("送出 (Enter)", variant="success", id="send")
                yield Button("取消 (Esc)", id="cancel")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "send":
            self._submit()
        else:
            self.dismiss(None)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self._submit()

    def action_cancel(self) -> None:
        self.dismiss(None)

    def _submit(self) -> None:
        values: dict = {}
        for key, w in self._widgets.items():
            if isinstance(w, Checkbox):
                values[key] = w.value
            elif isinstance(w, Select):
                values[key] = "" if w.value is Select.BLANK else w.value
            else:
                values[key] = w.value
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        filename, content = build_command_file(self.spec, values, stamp=stamp)
        try:
            TO_LLM_DIR.mkdir(parents=True, exist_ok=True)
            (TO_LLM_DIR / filename).write_text(content, encoding="utf-8")
        except Exception as e:  # surface, don't crash the TUI
            self.app.notify(f"寫入失敗：{e}", severity="error")
            return
        self.dismiss(filename)


class LingLingTUI(App):
    CSS = """
    #status { height: 3; padding: 0 1; background: $boost; }
    #body { height: 1fr; }
    #commands { width: 38%; border: round $primary; }
    #right { width: 1fr; }
    #activity-box { height: 60%; border: round $secondary; }
    #results-box { height: 1fr; border: round $secondary; }
    #compose-box { width: 70%; height: auto; max-height: 90%; padding: 1 2; border: thick $primary; background: $surface; }
    #compose-buttons { height: auto; padding-top: 1; }
    .dim { color: $text-muted; }
    #telemetry { height: 1; padding: 0 1; background: $surface; }
    """

    BINDINGS = [
        ("q", "quit", "離開"),
        ("r", "refresh", "刷新"),
    ]

    def __init__(self):
        super().__init__()
        self._options, self._index = _spec_options()

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        yield Static(id="status")
        with Horizontal(id="body"):
            yield OptionList(*self._options, id="commands")
            with Vertical(id="right"):
                with VerticalScroll(id="activity-box"):
                    yield Static(id="activity")
                with VerticalScroll(id="results-box"):
                    yield Static(id="results")
        yield Static(id="telemetry")
        yield Footer()

    def on_mount(self) -> None:
        self.title = "Ling-Ling 玲玲小老師 — TUI"
        self.query_one("#commands", OptionList).border_title = "命令"
        self.query_one("#activity-box", VerticalScroll).border_title = "最近活動（新→舊）"
        self.query_one("#results-box", VerticalScroll).border_title = "最新產出 fromLingLing/"
        self.refresh_status()
        self.refresh_activity()
        self.refresh_telemetry()
        self.set_interval(2.0, self.refresh_status)
        self.set_interval(4.0, self.refresh_activity)
        self.set_interval(3.0, self.refresh_telemetry)

    # ── live panels ──────────────────────────────────────────────────

    @staticmethod
    def _fmt_ts(run: dict) -> str:
        ts = (run.get("ended_at") or run.get("started_at") or "")
        return ts[5:19].replace("T", " ")

    def refresh_telemetry(self) -> None:
        try:
            from tui.telemetry import get_telemetry_string
            from tui.trace_reader import current_run, status_summary
            c_run = current_run()
            c_run_id = c_run.get("run_id") if c_run else None
            status = status_summary()
            provider = status.get("provider", "unknown")
            t_str = get_telemetry_string(c_run_id, provider)
            self.query_one("#telemetry", Static).update(t_str)
        except Exception as e:
            self.query_one("#telemetry", Static).update(f"Telemetry error: {e}")

    def refresh_status(self) -> None:
        s = trace_reader.status_summary()
        if not s.get("alive"):
            head = "[b red]✖ daemon 未執行[/] · 命令會留在 toLingLing/ 等它啟動"
        elif s["busy"]:
            head = "[b yellow]● 忙碌中[/]"
            if s.get("message"):
                head += f" · [white]{s['message']}[/]"
        else:
            head = "[b green]○ 閒置[/]"
            last = s.get("last")
            if last:
                color = {"succeeded": "green", "failed": "red", "running": "yellow"}.get(
                    last.get("status"), "white")
                head += (
                    f" · 最後：[cyan]{last['intent']}[/] "
                    f"[{color}]{last.get('status')}[/] {self._fmt_ts(last)}"
                )
        dd = "on" if s.get("daydream") else "off"
        line = (
            f"{head}\n"
            f"provider [cyan]{s['provider']}[/] · role [yellow]{s['role']}[/] · "
            f"做夢窗口 {s['dreaming']} · daydream {dd}"
        )
        self.query_one("#status", Static).update(line)

    def refresh_activity(self) -> None:
        # Snapshots, newest at TOP. A scrollable Static (not a streaming log) so
        # the view doesn't jump to the oldest line.
        runs = trace_reader.recent_runs(25)
        # Only ONE run can truly be live (the busy lock serialises work): the
        # newest 'running' row, and only while the daemon is busy. Any other
        # 'running' row is an orphan (daemon died mid-run) — show it dim as 中斷?
        # so the feed doesn't read like a dozen things are executing at once.
        live_id = (
            runs[0]["run_id"]
            if runs and trace_reader.is_busy() and runs[0].get("status") == "running"
            else None
        )
        t = Table(expand=True, show_edge=False, pad_edge=False, box=None)
        t.add_column("時間", no_wrap=True, width=11, style="dim")
        t.add_column("狀態", no_wrap=True, width=10)
        t.add_column("動作", overflow="ellipsis", no_wrap=True)
        for r in runs:
            ts = (r.get("started_at") or "")[5:16].replace("T", " ")
            st = r.get("status") or ""
            if st == "running" and r.get("run_id") != live_id:
                label, color = "中斷?", "grey50"
            elif st == "interrupted":
                label, color = "中斷", "grey50"
            else:
                label = st
                color = {"running": "yellow", "succeeded": "green", "failed": "red"}.get(st, "white")
            t.add_row(ts, Text(label, style=color), r.get("intent") or "")
        if not runs:
            t.add_row("", "", "（暫無紀錄）")
        self.query_one("#activity", Static).update(t)

        files = trace_reader.recent_results(30)
        rt = Table(expand=True, show_header=False, show_edge=False, pad_edge=False, box=None)
        rt.add_column("檔名", overflow="ellipsis", no_wrap=True)
        for item in files:
            rt.add_row(item["name"])
        if not files:
            rt.add_row("（暫無產出）")
        self.query_one("#results", Static).update(rt)

    # ── actions ──────────────────────────────────────────────────────

    def action_refresh(self) -> None:
        self.refresh_status()
        self.refresh_activity()

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        spec = self._index.get(event.option.id) if event.option.id else None
        if spec:
            self._open_compose(spec)

    def _open_compose(self, spec) -> None:
        def _done(filename: str | None) -> None:
            if filename:
                self.notify(f"已投遞：{filename}", title="toLingLing/")
                self.refresh_activity()
        self.push_screen(ComposeScreen(spec), _done)


def main() -> None:
    LingLingTUI().run()
