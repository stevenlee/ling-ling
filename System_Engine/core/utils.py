import atexit
import json
import logging
import os
import sys
from pathlib import Path


class MtimeCache:
    """Tiny cache of file contents keyed by (path, mtime).

    Used by LLMClient (persona/template/guideline reads) and BaseAgent
    (prompt template reads) to avoid re-reading files that rarely change
    during a single session. Auto-invalidates when the file is edited.
    """

    def __init__(self):
        self._entries: dict[Path, tuple[float, str]] = {}

    def read(self, path: Path) -> str:
        if not path.exists():
            self._entries.pop(path, None)
            return ""
        mtime = path.stat().st_mtime
        cached = self._entries.get(path)
        if cached and cached[0] == mtime:
            return cached[1]
        try:
            content = path.read_text(encoding="utf-8")
        except Exception as e:
            logging.warning(f"MtimeCache: failed to read {path.name}: {e}")
            return ""
        self._entries[path] = (mtime, content)
        return content


def acquire_pid_lock(pid_file: Path):
    """Ensure only one instance is running by writing the PID to `pid_file`.

    If a stale PID file exists for a non-running process, it is replaced.
    Exits the process with code 1 on conflict or write failure.
    """
    if pid_file.exists():
        try:
            content = pid_file.read_text(encoding="utf-8").strip()
            if not content:
                raise ValueError("empty PID file")
            old_pid = int(content)
            os.kill(old_pid, 0)
        except (ValueError, ProcessLookupError, PermissionError, OSError):
            logging.warning("⚠️ 發現過時的 PID 檔案（程序已不存在）。正在重新建立...")
            pid_file.unlink(missing_ok=True)
        else:
            logging.error(
                f"❌ 偵測到重複啟動：系統已在運行中 (PID: {old_pid})。請先關閉舊程序: kill {old_pid}。"
            )
            sys.exit(1)

    try:
        pid_file.write_text(str(os.getpid()), encoding="utf-8")
    except Exception as e:
        logging.error(f"❌ 無法建立 PID 檔案: {e}")
        sys.exit(1)

    logging.info(f"🔒 One Ling Ling at a time! (๑˃̵ᴗ˂̵)و (PID: {os.getpid()})")

    def release_lock():
        if not pid_file.exists():
            return
        try:
            if pid_file.read_text(encoding="utf-8").strip() == str(os.getpid()):
                pid_file.unlink()
        except Exception:
            pass

    atexit.register(release_lock)


def digest_value_to_text(value) -> str:
    """Flatten a digest value (str/list/dict/...) into a single text string.

    Used by ClippingWatcher (digest appendix formatting) and LLMClient
    (part-digest-to-prompt serialisation).
    """
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, (list, tuple, set)):
        parts = (digest_value_to_text(item) for item in value)
        return "; ".join(p for p in parts if p)
    if isinstance(value, dict):
        return json.dumps(value, ensure_ascii=False)
    return str(value).strip()
