import threading
import os
from pathlib import Path

class BusyState:
    """
    Manages a simple thread-safe flag to indicate if the system is processing something.
    """
    def __init__(self):
        self._busy = False
        self._lock = threading.Lock()
        self.project_root = Path(__file__).parent.parent.parent.absolute()
        self.lock_file = self.project_root / ".kb_lock"

    def set_busy(self, status: bool):
        with self._lock:
            self._busy = status

    def is_busy(self) -> bool:
        # If KB is locked for maintenance, consider the system "busy"
        if os.path.exists(str(self.lock_file)):
            return True
            
        with self._lock:
            return self._busy

global_busy_state = BusyState()
