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

from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import (
    Button, Checkbox, Footer, Header, Input, Label, OptionList, RichLog, Select, Static,
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
                    placeholder = "DocA DocB (空白分隔)" if f.kind == "links" else (f.help or "")
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
    #activity { height: 60%; border: round $secondary; }
    #results { height: 1fr; border: round $secondary; }
    #compose-box { width: 70%; height: auto; max-height: 90%; padding: 1 2; border: thick $primary; background: $surface; }
    #compose-buttons { height: auto; padding-top: 1; }
    .dim { color: $text-muted; }
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
                yield RichLog(id="activity", wrap=True, markup=True, highlight=False)
                yield RichLog(id="results", wrap=False, markup=True, highlight=False)
        yield Footer()

    def on_mount(self) -> None:
        self.title = "Ling-Ling 玲玲小老師 — TUI"
        self.refresh_status()
        self.refresh_activity()
        self.set_interval(2.0, self.refresh_status)
        self.set_interval(4.0, self.refresh_activity)

    # ── live panels ──────────────────────────────────────────────────

    @staticmethod
    def _fmt_ts(run: dict) -> str:
        ts = (run.get("ended_at") or run.get("started_at") or "")
        return ts[5:19].replace("T", " ")

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
        act = self.query_one("#activity", RichLog)
        act.clear()
        act.write("[b]最近活動 (trace)[/]")
        for r in trace_reader.recent_runs(12):
            color = {"running": "yellow", "succeeded": "green", "failed": "red"}.get(r["status"], "white")
            started = (r.get("started_at") or "")[5:19].replace("T", " ")
            act.write(f"[{color}]{r['status']:9}[/] {started}  {r['intent']}")
        for line in trace_reader.tail_maintenance_log(6):
            act.write(f"[dim]{line.lstrip('# ').strip()}[/]")

        res = self.query_one("#results", RichLog)
        res.clear()
        res.write("[b]最新產出 (fromLingLing/)[/]")
        for item in trace_reader.recent_results(15):
            res.write(item["name"])

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
