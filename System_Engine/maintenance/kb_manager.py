import os
import shutil
import zipfile
import hashlib
import logging
from pathlib import Path
from datetime import datetime
from core.config import PROJECT_ROOT, WIKI_VAULT_DIR, BACKUPS_DIR, LOG_FILE
from services.rag_manager import RAGManager
from maintenance.init_rag import init_rag_from_scratch

LOCK_FILE = PROJECT_ROOT / ".kb_lock"


class KBManager:
    def __init__(self, rag_manager=None):
        self.rag = rag_manager or RAGManager()
        BACKUPS_DIR.mkdir(parents=True, exist_ok=True)

    def _create_lock(self):
        if LOCK_FILE.exists():
            raise RuntimeError("Management operation already in progress (.kb_lock exists)")
        LOCK_FILE.touch()
        logging.info("KBManager: System locked for maintenance.")

    def _remove_lock(self):
        if LOCK_FILE.exists():
            LOCK_FILE.unlink()
            logging.info("KBManager: System unlocked.")

    def _get_file_hash(self, filepath: Path):
        hash_md5 = hashlib.md5()
        with open(filepath, "rb") as f:
            for chunk in iter(lambda: f.read(4096), b""):
                hash_md5.update(chunk)
        return hash_md5.hexdigest()

    def zip_kb(self, output_name: str = None) -> Path:
        """Archiving the knowledge base into a zip file."""
        self._create_lock()
        try:
            if not output_name:
                timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
                output_name = f"kb_backup_{timestamp}.zip"

            zip_path = BACKUPS_DIR / output_name
            print(f"📦 (1/1) Archiving Knowledge Base to {output_name}...")

            with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zipf:
                for root, dirs, files in os.walk(WIKI_VAULT_DIR):
                    # Exclude Backups, Database, and toLingLing (too large/unnecessary for portable backup)
                    if any(
                        x in root
                        for x in [
                            str(BACKUPS_DIR),
                            str(WIKI_VAULT_DIR / "toLingLing"),
                            str(WIKI_VAULT_DIR / "Database"),
                        ]
                    ):
                        continue

                    for file in files:
                        file_path = Path(root) / file
                        arcname = file_path.relative_to(WIKI_VAULT_DIR)
                        zipf.write(file_path, arcname)

            logging.info(f"KBManager: Backup created at {zip_path}")
            return zip_path
        finally:
            self._remove_lock()

    def reset_kb(self) -> str:
        """Safety reset: Backup first, then wipe everything."""
        # 1. Backup
        try:
            backup_path = self.zip_kb()
        except Exception as e:
            return f"💧 Reset failed: Could not create safety backup. {e}"

        self._create_lock()
        try:
            print("🧹 (1/3) Starting RESET operation...")
            # 2. Wipe core content folders and command intake
            content_dirs = ["pages", "Clippings", "Notes", "raw", "toLingLing"]
            for folder in content_dirs:
                target = WIKI_VAULT_DIR / folder
                if target.exists():
                    print(f"🧹 (2/3) Clearing {folder}...")
                    for item in target.iterdir():
                        if item.is_file():
                            item.unlink()
                        elif item.is_dir():
                            shutil.rmtree(item)

            # 3. Wipe database and log
            print("🧹 (3/3) Wiping RAG database and logs...")
            self.rag.wipe_collection()
            if LOG_FILE.exists():
                LOG_FILE.write_text(
                    f"# Knowledge Base Reset Log\nReset performed on {datetime.now()}\nBackup: {backup_path.name}\n",
                    encoding="utf-8",
                )

            logging.info("KBManager: Knowledge Base successfully reset.")
            return (
                f"✅ RESET COMPLETE. Safety backup created in Backups/ folder: {backup_path.name}"
            )
        finally:
            self._remove_lock()

    def unzip_kb(self, zip_filename: str = None) -> str:
        """Importing/Unzipping a KB archive with duplicate detection."""
        if not zip_filename:
            # Pick the latest backup automatically
            zips = list(BACKUPS_DIR.glob("*.zip"))
            if not zips:
                return "💧 Error: No backup files found in Backups/ directory."
            # Sort by modification time (latest first)
            zips.sort(key=lambda x: x.stat().st_mtime, reverse=True)
            zip_path = zips[0]
            zip_filename = zip_path.name
        else:
            # Clean up filename in case user included path or brackets
            clean_name = zip_filename.replace("[[", "").replace("]]", "").strip()
            if "/" in clean_name:
                clean_name = clean_name.split("/")[-1]

            # Auto-append .zip if missing
            if not clean_name.lower().endswith(".zip"):
                clean_name += ".zip"

            zip_path = BACKUPS_DIR / clean_name

        if not zip_path.exists():
            return f"💧 Error: Backup file {zip_filename} not found in Backups/ directory."

        self._create_lock()
        tmp_unzip = BACKUPS_DIR / "tmp_unzip"
        try:
            if tmp_unzip.exists():
                shutil.rmtree(tmp_unzip)
            tmp_unzip.mkdir()

            print(f"📂 (1/3) Unzipping {zip_filename} to temporary area...")
            with zipfile.ZipFile(zip_path, "r") as zipf:
                zipf.extractall(tmp_unzip)

            print("🔍 (2/3) Comparing files and importing...")
            files_to_index = []
            all_files = []
            for root, _, filenames in os.walk(tmp_unzip):
                for f in filenames:
                    all_files.append(Path(root) / f)

            total = len(all_files)
            for i, src_path in enumerate(all_files, 1):
                rel_path = src_path.relative_to(tmp_unzip)

                # Security: Never unzip files into toLingLing or Database
                # Use rel_path.parts to check the top-level directory safely
                if rel_path.parts and (
                    rel_path.parts[0] == "toLingLing" or rel_path.parts[0] == "Database"
                ):
                    continue

                dest_path = WIKI_VAULT_DIR / rel_path

                # Progress hint
                if i % 10 == 0 or i == total:
                    print(f"📦 Processing files... ({i}/{total})")

                # Ensure directory exists
                dest_path.parent.mkdir(parents=True, exist_ok=True)

                if dest_path.exists():
                    # MD5 Comparison
                    if self._get_file_hash(src_path) == self._get_file_hash(dest_path):
                        # Identical, skip
                        continue
                    else:
                        # Conflict -> rename to (twin)
                        new_name = f"{dest_path.stem} (twin){dest_path.suffix}"
                        dest_path = dest_path.parent / new_name

                shutil.copy2(src_path, dest_path)
                if dest_path.suffix == ".md" and "pages" in str(dest_path):
                    files_to_index.append(dest_path)

            print("⚡ (3/3) Re-indexing newly imported files...")
            # For simplicity, we trigger a full init_rag to be sure everything is consistent,
            # or just index the new ones. init_rag_from_scratch(wipe=False) works well.
            init_rag_from_scratch(wipe=False)

            return f"✅ IMPORT COMPLETE. Unzipped {zip_filename} and integrated knowledge."
        finally:
            if tmp_unzip.exists():
                shutil.rmtree(tmp_unzip)
            self._remove_lock()


if __name__ == "__main__":
    import sys

    logging.basicConfig(level=logging.INFO)
    manager = KBManager()
    # Simple CLI for testing
    if len(sys.argv) > 1:
        cmd = sys.argv[1]
        if cmd == "zip":
            manager.zip_kb()
        elif cmd == "reset":
            manager.reset_kb()
        elif cmd == "unzip" and len(sys.argv) > 2:
            manager.unzip_kb(sys.argv[2])
