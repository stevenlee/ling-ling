import logging
from pathlib import Path
from core.config import PROJECT_ROOT
from maintenance.wiki_linter import WikiLinter
from services.rag_manager import RAGManager

# Setup logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

def main():
    logging.info("🛠️ DB Repair Agent: Starting maintenance...")
    rag = RAGManager()
    linter = WikiLinter(PROJECT_ROOT, rag_manager=rag)
    report = linter.perform_repair()
    
    # Save a summary to log.md or similar? Linter.perform_repair returns the report.
    print(report)

if __name__ == "__main__":
    main()
