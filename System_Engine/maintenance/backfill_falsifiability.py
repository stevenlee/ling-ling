"""One-off script to backfill falsifiability for existing Cortex pages."""

import logging
from pathlib import Path

from core.config import CORTEX_DIR, PROJECT_ROOT
from services.cortex_store import load_all_pages, save_cortex_page
from services.llm_client import LlmClient


def main():
    logging.basicConfig(level=logging.INFO)
    cortex_dir = CORTEX_DIR
    
    llm = LlmClient()
    pages = load_all_pages(cortex_dir)
    logging.info(f"Found {len(pages)} Cortex pages.")

    updated_count = 0
    for page in pages:
        if page.falsifiability is not None:
            logging.info(f"Skipping {page.claim_id}, already has falsifiability.")
            continue

        logging.info(f"Assessing {page.claim_id}: {page.claim[:50]}...")
        result = llm.assess_falsifiability(page.claim)
        score = result.get("score")
        falsifier = result.get("falsifier", "")
        
        # NOTE: Do NOT modify confidence retroactively.
        page.falsifiability = score
        page.falsifier = falsifier
        
        # Save without bumping S or updating updated/last_reinforced_at
        # unless required by rules, but we want to avoid side effects.
        # Wait, if we call save_cortex_page, it just writes it.
        save_cortex_page(page)
        updated_count += 1

    logging.info(f"Backfilled {updated_count} pages.")

if __name__ == "__main__":
    main()
