import logging
import os
import re
from pathlib import Path

import yaml
from dotenv import load_dotenv

load_dotenv()

# ─── Static config ────────────────────────────────────────────────────

LLM_PROVIDER = os.getenv("LLM_PROVIDER", "vllm").lower()
AUTO_REPAIR = os.getenv("AUTO_REPAIR", "False").lower() == "true"
COMMAND_PREFIX = "@ling-"

CHUNK_SIZE = int(os.getenv("CHUNK_SIZE", "5000"))
CHUNK_OVERLAP = int(os.getenv("CHUNK_OVERLAP", "500"))

# ─── Paths ────────────────────────────────────────────────────────────

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


_FRONTMATTER_RE = re.compile(r'^---\s*\n(.*?)\n---\s*(?:\n|$)', re.DOTALL | re.MULTILINE)


class DynamicSettings:
    """Settings that can be reloaded at runtime from Scripture.md frontmatter."""

    # (yaml_key, attr_name, type_coercer)
    _BINDINGS: tuple[tuple[str, str, type], ...] = (
        ("be_a",           "AGENT_ROLE",       str),
        ("say",            "OUTPUT_LANGUAGE",  str),
        ("use_template",   "USE_TEMPLATE",     str),
        ("creativity",     "CREATIVITY",       float),
        ("max_output",     "MAX_OUTPUT",       int),
        ("memory_limit",   "MEMORY_LIMIT",     int),
        ("search_depth",   "SEARCH_DEPTH",     int),
        ("strict_mode",    "STRICT_MODE",      bool),
        ("digest_limit",   "DIGEST_LIMIT",     int),
        ("digest_overlap", "DIGEST_OVERLAP",   int),
        ("dreaming_from",  "DREAMING_FROM",    int),
        ("dreaming_to",    "DREAMING_TO",      int),
        ("self_healing",   "SELF_HEALING",     bool),
    )

    def __init__(self):
        self.AGENT_ROLE = "assistant"
        self.OUTPUT_LANGUAGE = "Traditional Chinese"
        self.USE_TEMPLATE: str | None = None
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
            content = SCRIPTURE_FILE.read_text(encoding="utf-8").strip()
            match = _FRONTMATTER_RE.search(content)
            if not match:
                logging.warning("Scripture.md: failed to find valid YAML frontmatter.")
                return
            yaml_data = yaml.safe_load(match.group(1))
            if not yaml_data:
                logging.warning("Scripture.md: frontmatter is empty.")
                return

            for key, attr, coercer in self._BINDINGS:
                if key not in yaml_data:
                    continue
                try:
                    value = coercer(yaml_data[key])
                except (TypeError, ValueError) as e:
                    logging.warning(f"Scripture.md: bad value for {key!r}: {e}")
                    continue
                if coercer is str:
                    value = value.lower() if attr == "AGENT_ROLE" else value
                setattr(self, attr, value)

            logging.info(
                f"Scripture says: Be a {'strict' if self.STRICT_MODE else 'chatty'} {self.AGENT_ROLE}. "
                f"Search Depth={self.SEARCH_DEPTH}. Read {self.DIGEST_LIMIT} chars. "
                f"Dreaming {self.DREAMING_FROM}-{self.DREAMING_TO}."
            )
        except Exception as e:
            logging.error(f"Failed to reload settings: {e}")


settings = DynamicSettings()


_MANAGED_DIRECTORIES = (
    CLIPPINGS_DIR, CONSOLIDATE_DIR, TO_LLM_DIR, FROM_LLM_DIR, PAGES_DIR, NOTES_DIR,
    EXCALIDRAW_DIR, ASSETS_DIR, RAW_CONSOLIDATE_DIR, RAW_PROMPTS_DIR,
    RAW_ASSETS_DIR, RAW_MERGED_DIR, SCRIPTURE_DIR, PERSONAS_DIR, GUIDELINES_DIR,
    SKILLS_DIR, BACKUPS_DIR, TEMPLATES_DIR, PROMPTS_DIR,
)


def ensure_directories():
    """Ensure all required directories exist."""
    for directory in _MANAGED_DIRECTORIES:
        directory.mkdir(parents=True, exist_ok=True)
