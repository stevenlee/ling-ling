import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent.absolute()))

import json
from datetime import datetime, timedelta
from unittest.mock import MagicMock
import pytest

from maintenance.weekly_memoir import run_weekly_memoir, MemoirResult
from services.trace_store import TraceStore

def test_weekly_memoir_fail_open_all(tmp_path):
    """Test that when all dependencies fail or are missing, it fails open."""
    # A trace store that throws exceptions on queries
    mock_ts = MagicMock()
    mock_ts.recent_query_texts.side_effect = Exception("DB offline")
    mock_ts.query_artifacts.side_effect = Exception("DB offline")
    
    result = run_weekly_memoir(
        mock_ts,
        cortex_dir=tmp_path / "cortex",
        insights_dir=tmp_path / "insights",
        bench_history=tmp_path / "bench.json",
        report_dir=tmp_path / "reports",
        log_path=tmp_path / "maintenance.log.md"
    )
    
    assert result.status == "succeeded"
    assert result.report_path.exists()
    
    content = result.report_path.read_text(encoding="utf-8")
    assert "（本節資料不可用）" in content
    
def test_weekly_memoir_happy_path(tmp_path):
    # Setup mock trace store
    mock_ts = MagicMock()
    mock_ts.recent_query_texts.return_value = ["query 1", "query 2"]
    mock_ts.query_artifacts.return_value = [
        {"metadata": {"profile": "academic", "filename": "doc1.md"}},
        {"metadata": {"profile": "default", "filename": "doc2.md"}},
        {"metadata": {"profile": "academic", "filename": "doc3.md"}},
    ]
    
    # Setup directories
    cortex_dir = tmp_path / "cortex"
    insights_dir = tmp_path / "insights"
    bench_history = tmp_path / "bench.json"
    report_dir = tmp_path / "reports"
    log_path = tmp_path / "maintenance.log.md"
    
    cortex_dir.mkdir()
    insights_dir.mkdir()
    
    # Create fake insight
    insight_file = insights_dir / "insight1.md"
    insight_file.write_text("---\nsignals:\n  groundedness: 0.9\n  refute_verdict: false\n---\nContent", encoding="utf-8")
    
    # Create bench history
    bench_history.write_text(json.dumps([
        {"pass_rate": 0.8, "facet_lift": 0.05}
    ]), encoding="utf-8")
    
    # Create fake cortex page manually via object or mock load_all_pages
    # It's easier to mock load_all_pages inside the module
    from services import cortex_store
    from unittest.mock import patch
    
    fake_page = MagicMock()
    fake_page.claim = "Cats are liquid"
    fake_page.status = "falsified"
    fake_page.created = datetime.now().isoformat()
    fake_page.updated = datetime.now().isoformat()
    
    with patch("services.cortex_store.load_all_pages", return_value=[fake_page]):
        result = run_weekly_memoir(
            mock_ts,
            cortex_dir=cortex_dir,
            insights_dir=insights_dir,
            bench_history=bench_history,
            report_dir=report_dir,
            log_path=log_path
        )
        
    assert result.status == "succeeded"
    content = result.report_path.read_text(encoding="utf-8")
    
    assert "query 1" in content
    assert "query 2" in content
    assert "Profile 分佈" in content
    assert "`academic`: 2" in content
    assert "doc1.md" in content
    assert "insight1.md" in content
    assert "groundedness: 0.9" in content
    assert "Cats are liquid" in content
    assert "【Falsified】" in content
    assert "Pass Rate: 80.0%" in content
    assert "Facet Lift: +5.0%" in content


def test_weekly_memoir_corrupted_bench_history(tmp_path):
    mock_ts = MagicMock()
    mock_ts.recent_query_texts.return_value = []
    mock_ts.query_artifacts.return_value = []
    
    bench_history = tmp_path / "bench.json"
    bench_history.write_text("invalid json {", encoding="utf-8")
    
    result = run_weekly_memoir(
        mock_ts,
        cortex_dir=tmp_path / "cortex",
        insights_dir=tmp_path / "insights",
        bench_history=bench_history,
        report_dir=tmp_path / "reports",
        log_path=tmp_path / "maintenance.log.md"
    )
    
    assert result.status == "succeeded"
    content = result.report_path.read_text(encoding="utf-8")
    assert "（本節資料不可用）" in content
    # 空窗口的節（1-4）整節省略，而不是佔位文字
    assert "## 1." not in content
    assert "## 2." not in content
    assert "## 3." not in content
    assert "## 4." not in content
    # maintenance log 仍有摘要一行
    log_content = (tmp_path / "maintenance.log.md").read_text(encoding="utf-8")
    assert "Weekly Memoir" in log_content


def test_weekly_memoir_real_trace_store(tmp_path):
    import time

    ts = TraceStore(db_path=tmp_path / "traces.sqlite")

    with ts.run():
        ts.record_retrieval_event(query_text="What is cats?", top_k=0, options={}, results=[])
        time.sleep(0.002)
        ts.record_retrieval_event(query_text="What is cats?", top_k=0, options={}, results=[])
        time.sleep(0.002)
        ts.record_retrieval_event(query_text="Dogs are nice?", top_k=0, options={}, results=[])
        
    cortex_dir = tmp_path / "cortex"
    insights_dir = tmp_path / "insights"
    bench_history = tmp_path / "bench.json"
    report_dir = tmp_path / "reports"
    log_path = tmp_path / "maintenance.log.md"
    
    cortex_dir.mkdir()
    insights_dir.mkdir()
    
    result = run_weekly_memoir(
        ts,
        cortex_dir=cortex_dir,
        insights_dir=insights_dir,
        bench_history=bench_history,
        report_dir=report_dir,
        log_path=log_path
    )
    
    content = result.report_path.read_text(encoding="utf-8")
    # Should contain deduplicated queries
    assert "What is cats?" in content
    assert "Dogs are nice?" in content
    assert content.count("What is cats?") == 1
    # 最新優先：Dogs 是最後記錄的，應排在前面
    assert content.index("Dogs are nice?") < content.index("What is cats?")
