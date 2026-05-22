import logging
import threading

from rich.console import Console
from rich.live import Live
from rich.logging import RichHandler
from rich.panel import Panel
from rich.spinner import Spinner
from rich.table import Table
from rich.text import Text

console = Console()

IDLE_EMOJI = "🍵"
BUSY_EMOJI = "🔥"
SUCCESS_EMOJI = "(ﾉ>ω<)ﾉ 🎻"
ERROR_EMOJI = "😿"
MAINTENANCE_EMOJI = "🧹"
SYNC_EMOJI = "🔄"


class LingLingUI:
    _instance = None
    _lock = threading.Lock()

    def __new__(cls):
        with cls._lock:
            if cls._instance is None:
                cls._instance = super().__new__(cls)
                cls._instance._live = None
                cls._instance._status_text = "Ling Ling is ready!(๑•̀ㅂ•́)و✧"
                cls._instance._is_busy = False
            return cls._instance

    def _get_status_renderable(self):
        if self._is_busy:
            grid = Table.grid(expand=False)
            grid.add_column()
            grid.add_column()
            grid.add_row(
                Text(f"{BUSY_EMOJI} ", style="bold magenta"),
                Spinner("dots", text=Text(f"{self._status_text}...", style="bold magenta")),
            )
            return grid
        return Text(f"{IDLE_EMOJI} {self._status_text}", style="bold green")

    def start(self, version):
        self.banner(version)
        self._live = Live(
            self._get_status_renderable(),
            console=console,
            refresh_per_second=10,
            transient=False,
        )
        self._live.start()

    def banner(self, version):
        banner_text = Text()
        banner_text.append(f"\n🌸 Ling-Ling Mentor System v{version} 🌸\n", style="bold magenta")
        banner_text.append("玲玲小老師上線囉！(๑˃̵ᴗ˂̵)و\n", style="italic cyan")
        console.print(Panel(banner_text, border_style="bright_magenta"))

    def info(self, message):
        console.print(f"[bold cyan]INFO[/bold cyan] | {message}")

    def success(self, message):
        console.print(f"[bold green]{SUCCESS_EMOJI} SUCCESS[/bold green] | {message}")

    def error(self, message):
        console.print(f"[bold red]{ERROR_EMOJI} ERROR[/bold red] | {message}")

    def cmd_received(self, cmd_name):
        console.print(f"\n[bold yellow]📥 收到指令：[/bold yellow][white on blue] {cmd_name} [/white on blue]")

    def set_status(self, message, is_busy=True):
        with self._lock:
            self._status_text = message
            self._is_busy = is_busy
            if self._live:
                self._live.update(self._get_status_renderable())

    def stop(self):
        if self._live:
            self._live.stop()


ui = LingLingUI()


def setup_rich_logging():
    logging.basicConfig(
        level="INFO",
        format="%(message)s",
        datefmt="[%X]",
        handlers=[RichHandler(console=console, rich_tracebacks=True, show_path=False)],
    )
