import logging
from rich.console import Console
from rich.logging import RichHandler
from rich.live import Live
from rich.panel import Panel
from rich.text import Text
from rich.spinner import Spinner
import threading

# Initialize Rich Console
console = Console()

# Custom UI Emojis
IDLE_EMOJI = "🍵"
BUSY_EMOJI = "🔥"
SUCCESS_EMOJI = "(ﾉ>ω<)ﾉ 🎻"
ERROR_EMOJI = "😿"
MAINTENANCE_EMOJI = "🧹"
SYNC_EMOJI = "🔄"

class LingLingUI:
    _instance = None
    _live = None
    _status_text = "Ling Ling is ready!(๑•̀ㅂ•́)و✧"
    _is_busy = False
    _lock = threading.Lock()

    def __new__(cls):
        with cls._lock:
            if cls._instance is None:
                cls._instance = super(LingLingUI, cls).__new__(cls)
            return cls._instance

    def _get_status_renderable(self):
        """Generates the content for the fixed bottom status line."""
        from rich.table import Table
        if self._is_busy:
            grid = Table.grid(expand=False)
            grid.add_column()
            grid.add_column()
            grid.add_row(
                Text(f"{BUSY_EMOJI} ", style="bold magenta"),
                Spinner("dots", text=Text(f"{self._status_text}...", style="bold magenta"))
            )
            return grid
        else:
            return Text(f"{IDLE_EMOJI} {self._status_text}", style="bold green")

    def start(self, version):
        """Initializes the UI with a banner and starts the live status bar."""
        self.banner(version)
        self._live = Live(self._get_status_renderable(), console=console, refresh_per_second=10, transient=False)
        self._live.start()

    def banner(self, version):
        banner_text = Text()
        banner_text.append(f"\n🌸 Ling-Ling Mentor System v{version} 🌸\n", style="bold magenta")
        banner_text.append("玲玲小老師上線囉！(๑˃̵ᴗ˂̵)و\n", style="italic cyan")
        console.print(Panel(banner_text, border_style="bright_magenta"))

    def info(self, message):
        # Use console.print instead of logging to keep it above the live status
        console.print(f"[bold cyan]INFO[/bold cyan] | {message}")

    def success(self, message):
        console.print(f"[bold green]{SUCCESS_EMOJI} SUCCESS[/bold green] | {message}")

    def error(self, message):
        console.print(f"[bold red]{ERROR_EMOJI} ERROR[/bold red] | {message}")

    def cmd_received(self, cmd_name):
        console.print(f"\n[bold yellow]📥 收到指令：[/bold yellow][white on blue] {cmd_name} [/white on blue]")

    def set_status(self, message, is_busy=True):
        """Update the persistent status line text and busy state."""
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
    # We use a custom handler to prevent logs from breaking the Live display
    logging.basicConfig(
        level="INFO",
        format="%(message)s",
        datefmt="[%X]",
        handlers=[RichHandler(console=console, rich_tracebacks=True, show_path=False)]
    )
