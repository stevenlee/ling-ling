"""Telemetry collector for TUI: CPU, RAM, GPU, and Token usage.

Uses a background thread to poll DGX stats via SSH (to avoid blocking TUI).
Reads SQLite trace DB in read-only mode for token stats.
"""

import sqlite3
import subprocess
import threading
import time

from core.config import DATABASE_DIR

TRACE_DB = DATABASE_DIR / "llm_trace.sqlite"

# Rough context window limits (in K)
MODEL_LIMITS_K = {
    "gpt-4o": 128,
    "gpt-4-turbo": 128,
    "claude-3-opus": 200,
    "claude-3-sonnet": 200,
    "claude-3-5-sonnet": 200,
    "claude-3-haiku": 200,
    "gemini-1.5-pro": 2000,
    "gemini-1.5-flash": 1000,
}

# Global cache for DGX stats
_dgx_stats = {"cpu": "N/A", "ram": "N/A", "gpu": "N/A"}


def _poll_dgx_stats():
    """Background loop to fetch stats from DGX via SSH every 3 seconds."""
    while True:
        try:
            # -o LogLevel=QUIET to suppress "bind: Address already in use" from ControlMaster
            # timeout 2s for the ssh command itself to avoid hanging forever
            cmd = "ssh -o LogLevel=QUIET -o ConnectTimeout=2 dgx \"top -bn1 | grep 'Cpu(s)' && free -m && nvidia-smi --query-gpu=utilization.gpu --format=csv,noheader,nounits\""
            output = subprocess.check_output(cmd, shell=True, text=True, timeout=5)
            lines = [line.strip() for line in output.split("\n") if line.strip()]

            # Parse output
            # Expected format:
            # 1. %Cpu(s): 50.9 us,  2.7 sy,  0.0 ni, 46.5 id...
            # 2. total        used        free      shared  buff/cache   available
            # 3. Mem:          124610       52801       14382        6977       66121       71809
            # 4. Swap:          16383        1688       14695
            # 5. 85 (or multiple lines if multiple GPUs)

            cpu_val = "N/A"
            ram_val = "N/A"
            gpu_val = "N/A"

            for i, line in enumerate(lines):
                if line.startswith("%Cpu"):
                    # id is typically the 8th word: "%Cpu(s): 50.9 us,  2.7 sy,  0.0 ni, 46.5 id..."
                    try:
                        # Extract the idle percentage and subtract from 100
                        parts = line.split(",")
                        for p in parts:
                            if "id" in p:
                                idle = float(p.strip().split()[0])
                                cpu_val = f"{100.0 - idle:.1f}%"
                                break
                    except Exception:
                        pass
                elif line.startswith("Mem:"):
                    try:
                        parts = line.split()
                        total = float(parts[1])
                        used = float(parts[2])
                        ram_val = f"{(used / total) * 100:.1f}%"
                    except Exception:
                        pass

            # The remaining lines are GPU utilizations (nvidia-smi). Take the max or average.
            gpu_lines = [l for l in lines if l.isdigit()]
            if gpu_lines:
                try:
                    gpu_usages = [int(g) for g in gpu_lines]
                    max_gpu = max(gpu_usages)
                    gpu_val = f"{max_gpu}%"
                except Exception:
                    pass

            _dgx_stats["cpu"] = cpu_val
            _dgx_stats["ram"] = ram_val
            _dgx_stats["gpu"] = gpu_val

        except Exception:
            # On failure, keep the old values or show N/A
            pass

        time.sleep(3)


# Start background thread once when module is imported
_t = threading.Thread(target=_poll_dgx_stats, daemon=True)
_t.start()


def get_model_limit_k(model_name: str) -> int:
    if not model_name:
        return 128  # default fallback
    name = model_name.lower()
    for key, limit in MODEL_LIMITS_K.items():
        if key in name:
            return limit
    return 128


def format_tokens(n: int) -> str:
    if n is None:
        return "0"
    if n >= 1000:
        return f"{n / 1000:.1f}k"
    return str(n)


def get_telemetry_string(current_run_id: str | None, provider: str = "ollama") -> str:
    # 1. System Metrics (from DGX or Cloud)
    if provider.lower() in ("ollama", "vllm", "llama.cpp", "local"):
        cpu_str = _dgx_stats["cpu"]
        ram_str = _dgx_stats["ram"]
        gpu_str = _dgx_stats["gpu"]
        sys_str = f"CPU: {cpu_str} | RAM: {ram_str} | GPU: {gpu_str} (dgx)"
    else:
        # It's a cloud provider (OpenAI, Anthropic, Gemini, etc.)
        sys_str = f"☁️ Cloud API: {provider.capitalize()} (dgx idle)"

    # 2. Token Metrics
    run_in = 0
    run_out = 0
    ctx_str = "0 / 128k"

    if TRACE_DB.exists():
        try:
            con = sqlite3.connect(f"file:{TRACE_DB}?mode=ro", uri=True, timeout=0.5)
            con.row_factory = sqlite3.Row
            try:
                # Latest context (ignore in-flight calls where tokens is null)
                row_ctx = con.execute(
                    "SELECT prompt_tokens, model FROM llm_calls WHERE prompt_tokens IS NOT NULL AND prompt_tokens > 0 ORDER BY ts DESC LIMIT 1"
                ).fetchone()
                if row_ctx:
                    p_tok = row_ctx["prompt_tokens"] or 0
                    mod = row_ctx["model"] or ""
                    limit_k = get_model_limit_k(mod)
                    ctx_str = f"{format_tokens(p_tok)} / {limit_k}k"

                # Current Run Total
                if current_run_id:
                    row_run = con.execute(
                        "SELECT SUM(prompt_tokens) as p_sum, SUM(completion_tokens) as c_sum "
                        "FROM llm_calls WHERE run_id = ?",
                        (current_run_id,),
                    ).fetchone()
                    if row_run:
                        run_in = row_run["p_sum"] or 0
                        run_out = row_run["c_sum"] or 0
            finally:
                con.close()
        except Exception:
            pass

    token_str = (
        f"Tokens: {format_tokens(run_in)} In / {format_tokens(run_out)} Out | 🪟 Ctx: {ctx_str}"
    )

    return f"{sys_str}  ||  {token_str}"
