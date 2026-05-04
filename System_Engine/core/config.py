import os
import logging
from pathlib import Path
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

# Setup logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

import yaml
import re

# Constants
LLM_PROVIDER = os.getenv("LLM_PROVIDER", "vllm").lower()
AUTO_REPAIR = os.getenv("AUTO_REPAIR", "False").lower() == "true"
COMMAND_PREFIX = "@ling-"

# Chunking Settings
CHUNK_SIZE = int(os.getenv("CHUNK_SIZE", "5000"))
CHUNK_OVERLAP = int(os.getenv("CHUNK_OVERLAP", "500"))

# Paths
PROJECT_ROOT = Path(__file__).parent.parent.parent.absolute()
SYSTEM_ENGINE_DIR = PROJECT_ROOT / "System_Engine"
PID_FILE = SYSTEM_ENGINE_DIR / "daemon.pid"

WIKI_VAULT_DIR = PROJECT_ROOT / "lings-desktop"
INDEX_FILE = WIKI_VAULT_DIR / "index.md"
LOG_FILE = WIKI_VAULT_DIR / "log.md"
SCRIPTURE_DIR = WIKI_VAULT_DIR / "Scripture"
SCRIPTURE_FILE = SCRIPTURE_DIR / "Scripture.md"
PERSONAS_DIR = SCRIPTURE_DIR / "Personas"
GUIDELINES_DIR = SCRIPTURE_DIR / "Guidelines"
TEMPLATES_DIR = WIKI_VAULT_DIR / "Templates"
PROMPTS_DIR = TEMPLATES_DIR / "Prompts"
PAGES_DIR = WIKI_VAULT_DIR / "pages"
NOTES_DIR = WIKI_VAULT_DIR / "Notes"
TAG_MAP_FILE = PAGES_DIR / "_tagScrapbook.md"

class DynamicSettings:
    """Manages settings that can be reloaded at runtime."""
    def __init__(self):
        self.AGENT_ROLE = "assistant"
        self.OUTPUT_LANGUAGE = "Traditional Chinese"
        self.DIGEST_LIMIT = 5000
        self.DIGEST_OVERLAP = 500
        self.DREAMING_FROM = 1
        self.DREAMING_TO = 5
        self.SELF_HEALING = True
        self.CREATIVITY = 0.4
        self.MAX_OUTPUT = 4096
        self.MEMORY_LIMIT = 32768
        self.SEARCH_DEPTH = 3
        self.STRICT_MODE = True

    def reload(self):
        if not SCRIPTURE_FILE.exists():
            return
        
        try:
            content = SCRIPTURE_FILE.read_text(encoding='utf-8').strip()
            match = re.search(r'^---\s*\n(.*?)\n---\s*(\n|$)', content, re.DOTALL | re.MULTILINE)
            if match:
                yaml_data = yaml.safe_load(match.group(1))
                if yaml_data:
                    # Persona & Language
                    if 'be_a' in yaml_data: self.AGENT_ROLE = str(yaml_data['be_a']).lower()
                    if 'say' in yaml_data: self.OUTPUT_LANGUAGE = str(yaml_data['say'])
                    if 'creativity' in yaml_data: self.CREATIVITY = float(yaml_data['creativity'])
                    if 'max_output' in yaml_data: self.MAX_OUTPUT = int(yaml_data['max_output'])
                    if 'memory_limit' in yaml_data: self.MEMORY_LIMIT = int(yaml_data['memory_limit'])
                    if 'search_depth' in yaml_data: self.SEARCH_DEPTH = int(yaml_data['search_depth'])
                    if 'strict_mode' in yaml_data: self.STRICT_MODE = bool(yaml_data['strict_mode'])
                    
                    # Digestion
                    if 'digest_limit' in yaml_data: self.DIGEST_LIMIT = int(yaml_data['digest_limit'])
                    if 'digest_overlap' in yaml_data: self.DIGEST_OVERLAP = int(yaml_data['digest_overlap'])
                    
                    # Dreaming
                    if 'dreaming_from' in yaml_data: self.DREAMING_FROM = int(yaml_data['dreaming_from'])
                    if 'dreaming_to' in yaml_data: self.DREAMING_TO = int(yaml_data['dreaming_to'])
                    if 'self_healing' in yaml_data: self.SELF_HEALING = bool(yaml_data['self_healing'])
                    
                    logging.info(
                        f"Scripture says: Be a {'strict' if self.STRICT_MODE else 'chatty'} {self.AGENT_ROLE}. "
                        f"Search Depth={self.SEARCH_DEPTH}. Read {self.DIGEST_LIMIT} chars. "
                        f"Dreaming {self.DREAMING_FROM}-{self.DREAMING_TO}."
                    )
                else:
                    logging.warning("Scripture.md: Frontmatter is empty.")
            else:
                logging.warning("Scripture.md: Failed to find valid YAML frontmatter.")
        except Exception as e:
            logging.error(f"Failed to reload settings: {e}")

settings = DynamicSettings()

CLIPPINGS_DIR = WIKI_VAULT_DIR / "Clippings"
CONSOLIDATE_DIR = WIKI_VAULT_DIR / "Consolidate"
TO_LLM_DIR = WIKI_VAULT_DIR / "toLingLing"
FROM_LLM_DIR = WIKI_VAULT_DIR / "fromLingLing"
EXCALIDRAW_DIR = WIKI_VAULT_DIR / "Excalidraw"
ASSETS_DIR = WIKI_VAULT_DIR / "Assets"
SKILLS_DIR = WIKI_VAULT_DIR / "Skills"
BACKUPS_DIR = PROJECT_ROOT / "Backups"

DATABASE_DIR = WIKI_VAULT_DIR / "Database"
RAW_DIR = WIKI_VAULT_DIR / "raw"
RAW_CONSOLIDATE_DIR = RAW_DIR / "consolidate"
RAW_PROMPTS_DIR = RAW_DIR / "prompts"
RAW_ASSETS_DIR = RAW_DIR / "assets"
RAW_MERGED_DIR = RAW_DIR / "merged"

def ensure_directories():
    """Ensure all required directories exist."""
    directories = [
        CLIPPINGS_DIR, CONSOLIDATE_DIR, TO_LLM_DIR, FROM_LLM_DIR, PAGES_DIR, NOTES_DIR,
        EXCALIDRAW_DIR, ASSETS_DIR, RAW_CONSOLIDATE_DIR, RAW_PROMPTS_DIR, 
        RAW_ASSETS_DIR, RAW_MERGED_DIR, SCRIPTURE_DIR, PERSONAS_DIR, GUIDELINES_DIR, 
        SKILLS_DIR, BACKUPS_DIR, TEMPLATES_DIR, PROMPTS_DIR
    ]
    for directory in directories:
        directory.mkdir(parents=True, exist_ok=True)
