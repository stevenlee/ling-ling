import os
import sys
import logging
from pathlib import Path
import atexit

def acquire_pid_lock(pid_file: Path):
    """
    Ensures only one instance of the script is running by using a PID file.
    """
    if pid_file.exists():
        try:
            with open(pid_file, 'r') as f:
                content = f.read().strip()
                if not content:
                    raise ValueError("Empty PID file")
                old_pid = int(content)
            
            # Use signal 0 to check if process exists
            os.kill(old_pid, 0)
            logging.error(f"❌ 偵測到重複啟動：系統已在運行中 (PID: {old_pid})。請先關閉舊程序: kill {old_pid}。")
            sys.exit(1)
        except (ValueError, ProcessLookupError, PermissionError, OSError):
            # ProcessLookupError: PID not found
            # ValueError: File content not a PID
            # OSError: General error checking PID
            logging.warning(f"⚠️ 發現過時的 PID 檔案（程序已不存在）。正在重新建立...")
            pid_file.unlink(missing_ok=True)
            
    # Write current PID
    try:
        pid_file.write_text(str(os.getpid()), encoding='utf-8')
        logging.info(f"🔒 One Ling Ling at a time! (๑˃̵ᴗ˂̵)و (PID: {os.getpid()})")
        
        # Register cleanup on exit
        def release_lock():
            if pid_file.exists():
                try:
                    # Only remove if it's still our PID (sanity check)
                    current_pid_in_file = pid_file.read_text().strip()
                    if current_pid_in_file == str(os.getpid()):
                        pid_file.unlink()
                except Exception:
                    pass
        
        atexit.register(release_lock)
    except Exception as e:
        logging.error(f"❌ 無法建立 PID 檔案: {e}")
        sys.exit(1)


def digest_value_to_text(value) -> str:
    """Recursively convert a digest value (str, list, dict, etc.) to a flat text string.

    Used by ClippingWatcher (digest appendix formatting) and LLMClient
    (part-digest-to-prompt serialisation). Centralised here to avoid
    three identical copies across the codebase.
    """
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, (list, tuple, set)):
        import json
        return "; ".join(digest_value_to_text(item) for item in value if digest_value_to_text(item))
    if isinstance(value, dict):
        import json
        return json.dumps(value, ensure_ascii=False)
    return str(value).strip()
