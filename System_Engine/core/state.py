import threading
import logging
import os
from pathlib import Path

class BusyState:
    """
    Manages a simple thread-safe flag to indicate if the system is processing something.
    Supports idle callbacks that fire on busy→idle transitions.
    """
    def __init__(self):
        self._busy = False
        self._lock = threading.Lock()
        self._idle_callbacks = []
        self._firing_callbacks = False
        self.project_root = Path(__file__).parent.parent.parent.absolute()
        self.lock_file = self.project_root / ".kb_lock"

    def register_idle_callback(self, callback):
        """Register a callback to fire when the system transitions from busy to idle."""
        with self._lock:
            self._idle_callbacks.append(callback)

    def set_busy(self, status: bool, fire_callbacks: bool = True):
        should_fire = False
        with self._lock:
            was_busy = self._busy
            # Fire callbacks on busy→idle, but not during callback execution
            if fire_callbacks and was_busy and not status and not self._firing_callbacks:
                should_fire = True
                self._firing_callbacks = True
                # Keep the system busy while callbacks drain queued filesystem work.
                self._busy = True
            else:
                self._busy = status

        if should_fire:
            try:
                while True:
                    processed = 0
                    for cb in self._idle_callbacks:
                        try:
                            result = cb()
                            if isinstance(result, int):
                                processed += result
                        except Exception as e:
                            logging.error(f"Idle callback error: {e}")
                    if processed == 0:
                        break
            finally:
                with self._lock:
                    self._busy = False
                    self._firing_callbacks = False

    def is_busy(self) -> bool:
        # If KB is locked for maintenance, consider the system "busy"
        if os.path.exists(str(self.lock_file)):
            return True
            
        with self._lock:
            return self._busy

global_busy_state = BusyState()
