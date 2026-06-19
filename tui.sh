#!/bin/bash
# Launch the Ling-Ling TUI cockpit — a separate companion process.
# It drops @ling-* commands into toLingLing/ and reads daemon status read-only;
# it never opens ChromaDB, so it is safe to run alongside the daemon.

set -e
cd "$(dirname "$0")"

if [ ! -d "venv" ]; then
    echo "初始化虛擬環境 (venv)..."
    python3 -m venv venv
    ./venv/bin/pip install -r requirements.txt
fi

# Install the TUI deps (textual) once, on first run.
if ! ./venv/bin/python -c "import textual" 2>/dev/null; then
    echo "安裝 TUI 依賴 (textual)..."
    ./venv/bin/pip install -r requirements-tui.txt
fi

exec env PYTHONPATH="$PWD/System_Engine" ./venv/bin/python -m tui
