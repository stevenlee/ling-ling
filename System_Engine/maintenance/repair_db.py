import logging
from pathlib import Path

import sys

# Add System_Engine to sys.path so this script works when run directly.
sys.path.append(str(Path(__file__).parent.parent.absolute()))

from agents.linter_agent import LinterAgent
from services.rag_manager import RAGManager

# Setup logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

def main():
    logging.info("🛠️ DB Repair Agent: Starting maintenance...")
    rag = RAGManager()
    linter = LinterAgent(llm=None, rag_manager=rag)
    report = linter.perform_repair()
    
    # Save a summary to log.md or similar? Linter.perform_repair returns the report.
    print(report)

if __name__ == "__main__":
    main()
