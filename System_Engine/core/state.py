import logging
import threading
import time
from pathlib import Path

# A callback running longer than this gets a warning: while it runs, ALL idle
# work (queued prompts, clippings, pumps) is blocked. Legitimate long passes
# (daydream generation on a local model) can take minutes — the warning is a
# breadcrumb for diagnosing a hung daemon, not an error.
_SLOW_CALLBACK_SECONDS = 600.0


class BusyState:
    """Thread-safe busy flag with on-idle callbacks.

    Callbacks fire when state transitions busy→idle. They run while the state
    is held busy (so additional set_busy(False) calls during draining are
    no-ops), and re-run if any callback returned a positive int (signalling
    more queued work).
    """

    def __init__(self):
        self._busy = False
        self._lock = threading.Lock()
        self._idle_callbacks: list = []
        self._firing_callbacks = False
        self.project_root = Path(__file__).parent.parent.parent.absolute()
        self.lock_file = self.project_root / ".kb_lock"

    def register_idle_callback(self, callback):
        with self._lock:
            self._idle_callbacks.append(callback)

    def set_busy(self, status: bool, fire_callbacks: bool = True):
        should_fire = False
        with self._lock:
            was_busy = self._busy
            if fire_callbacks and was_busy and not status and not self._firing_callbacks:
                should_fire = True
                self._firing_callbacks = True
                # Hold the system busy while callbacks drain queued filesystem work.
                self._busy = True
            else:
                self._busy = status

        if not should_fire:
            if not status:
                try:
                    from core.ui import ui

                    ui.set_status(
                        "Ling Ling is waiting... (๑´ㅂ`๑)zZ... (Ctrl-C to Quit)", is_busy=False
                    )
                except Exception:
                    pass
            return

        try:
            while True:
                processed = 0
                for cb in self._idle_callbacks:
                    started = time.monotonic()
                    try:
                        result = cb()
                    except Exception as e:
                        logging.error(f"Idle callback error: {e}")
                        continue
                    finally:
                        elapsed = time.monotonic() - started
                        if elapsed > _SLOW_CALLBACK_SECONDS:
                            name = getattr(cb, "__qualname__", None) or repr(cb)
                            logging.warning(
                                f"Idle callback {name} ran {elapsed:.0f}s "
                                f"(>{_SLOW_CALLBACK_SECONDS:.0f}s); it blocks all other "
                                f"idle work (queued prompts/clippings/pumps) while running."
                            )
                    if isinstance(result, int):
                        processed += result
                if processed == 0:
                    break
        finally:
            with self._lock:
                self._busy = False
                self._firing_callbacks = False
            try:
                from core.ui import ui

                ui.set_status(
                    "Ling Ling is waiting... (๑´ㅂ`๑)zZ... (Ctrl-C to Quit)", is_busy=False
                )
            except Exception:
                pass

    def is_busy(self) -> bool:
        if self.lock_file.exists():
            return True
        with self._lock:
            return self._busy

    def try_set_busy(self) -> bool:
        """Atomically check if busy; if not busy, set busy to True and return True.
        If already busy (or lock file exists), return False.
        """
        if self.lock_file.exists():
            return False
        with self._lock:
            if self._busy:
                return False
            self._busy = True
            return True


global_busy_state = BusyState()
