#!/bin/bash
# 自動建立虛擬環境並啟動精靈

if [ ! -d "venv" ]; then
    echo "初始化虛擬環境 (venv)..."
    python3 -m venv venv
    source venv/bin/activate
    echo "安裝依賴套件..."
    pip install -r requirements.txt
else
    source venv/bin/activate
fi

if [ "$1" == "--check" ]; then
    echo "🛡️  正在啟動系統健康檢查..."
    ./venv/bin/python3 System_Engine/maintenance/health_check.py
    exit $?
fi

echo "Available Agents: assistant, coder, patent-expert, researcher, translator"
python3 System_Engine/main.py
