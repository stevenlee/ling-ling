import time
import threading
import logging
from datetime import datetime
from core.config import settings
from core.state import global_busy_state
from agents.insight_agent import InsightAgent

class InsightScheduler(threading.Thread):
    """
    Background thread that runs Insight updates during scheduled hours when system is not busy.
    """
    def __init__(self, project_root, llm, rag):
        super().__init__(daemon=True)
        self.project_root = project_root
        self.llm = llm
        self.rag = rag
        self.agent = InsightAgent(llm, rag)
        self.last_run_date = None

    def run(self):
        logging.info(f"InsightScheduler: Started. Window: {settings.DREAMING_FROM:02d}:00 - {settings.DREAMING_TO:02d}:00")
        while True:
            now = datetime.now()
            current_hour = now.hour
            current_date = now.date()

            # Check if within window and hasn't run today
            if settings.DREAMING_FROM <= current_hour < settings.DREAMING_TO:
                if self.last_run_date != current_date:
                    if not global_busy_state.is_busy():
                        try:
                            logging.info("InsightScheduler: System idle in window. Starting scheduled FULL insight report...")
                            self.agent.generate_full_insight(user_directive="Scheduled daily comprehensive reflection.")
                            self.last_run_date = current_date
                        except Exception as e:
                            logging.error(f"InsightScheduler: Error during execution: {e}")
                    else:
                        logging.debug("InsightScheduler: System busy, skipping check...")
            
            time.sleep(60) # Check every minute
