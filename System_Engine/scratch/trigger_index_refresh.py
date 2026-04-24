import sys
from pathlib import Path

# Add project root to path
project_root = Path(__file__).parent.parent.parent.absolute()
sys.path.append(str(project_root / "System_Engine"))

from core.vault_utils import update_wiki_index

if __name__ == "__main__":
    update_wiki_index()
    print("Index refresh triggered successfully.")
