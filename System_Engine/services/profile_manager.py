"""Profile registry — named persona + template (+ operations) bundles.

A profile is one markdown file in `Scripture/Profiles/` whose YAML
frontmatter declares a complete, validated routing decision. The router
selects a *profile*, never a persona/template pair independently, so the
two can no longer drift into conflicting combinations. Profiles replace
the retired `Scripture/DocType.md` table; `migrate_from_doctype()` converts
an existing table once, after which the table is no longer consulted.

Frontmatter contract (body is free-form notes for humans):

    ---
    persona: cookery-curator
    template: cookery-recipe-card
    operations: [digest_sources, synthesize]   # optional, planner use
    description: Recipes and cooking instruction documents
    applicable_when: Recipes, kitchen technique guides, cooking tutorials
    ---

The file stem is the canonical profile id. Files whose name starts with
an underscore (and anything under `_pending/`) are ignored by the scan.

Auto-generated assets are quality-gated: `queue_pending()` writes the new
persona/template/profile trio into `Scripture/Profiles/_pending/<name>/`
and drops a review notice into `fromLingLing/`. They activate only after
the user moves each file into its real directory.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import yaml


_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*(?:\n|$)", re.DOTALL)


@dataclass(frozen=True)
class ProfileSpec:
    """Parsed profile metadata. `name` is always the file stem."""

    name: str
    persona: str
    template: str
    source_path: Path
    operations: tuple[str, ...] = ()
    description: str = ""
    applicable_when: str = ""

    @property
    def valid(self) -> bool:
        return bool(self.persona and self.template)

    def selection_hint(self) -> str:
        """One line fed to the LLM profile selector."""
        hint = self.applicable_when or self.description or self.name
        return f"{self.name}: {hint}"


def _as_str_tuple(value) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        return (value,)
    if isinstance(value, (list, tuple)):
        return tuple(str(v) for v in value if v is not None)
    return ()


def _parse_profile_file(path: Path) -> ProfileSpec | None:
    """Parse one profile file. Returns None (with a warning) on bad files."""
    try:
        text = path.read_text(encoding="utf-8")
    except Exception as e:
        logging.warning(f"ProfileManager: cannot read {path.name}: {e}")
        return None

    match = _FRONTMATTER_RE.match(text)
    if not match:
        logging.warning(f"ProfileManager: no frontmatter in {path.name}; skipping")
        return None

    try:
        data = yaml.safe_load(match.group(1)) or {}
    except Exception as e:
        logging.warning(f"ProfileManager: bad YAML in {path.name}: {e}")
        return None
    if not isinstance(data, dict):
        logging.warning(f"ProfileManager: frontmatter in {path.name} is not a mapping")
        return None

    spec = ProfileSpec(
        name=path.stem,
        persona=str(data.get("persona") or "").strip(),
        template=str(data.get("template") or "").strip(),
        source_path=path,
        operations=_as_str_tuple(data.get("operations")),
        description=str(data.get("description") or "").strip(),
        applicable_when=str(data.get("applicable_when") or "").strip(),
    )
    if not spec.valid:
        logging.warning(
            f"ProfileManager: {path.name} is missing persona and/or template; skipping"
        )
        return None
    return spec


def render_profile_markdown(
    *,
    persona: str,
    template: str,
    description: str = "",
    applicable_when: str = "",
    operations: tuple[str, ...] | list[str] = (),
    note: str = "",
) -> str:
    """Render a profile file (frontmatter + human-readable body)."""
    frontmatter: dict = {"persona": persona, "template": template}
    if operations:
        frontmatter["operations"] = list(operations)
    if description:
        frontmatter["description"] = description
    if applicable_when:
        frontmatter["applicable_when"] = applicable_when
    yaml_block = yaml.safe_dump(
        frontmatter, allow_unicode=True, sort_keys=False, default_flow_style=False
    ).strip()
    body = note.strip() or (
        f"Routes matching documents to persona `{persona}` with template `{template}`."
    )
    return f"---\n{yaml_block}\n---\n\n{body}\n"


class ProfileManager:
    """Registry of routing profiles. Scan happens at construction."""

    def __init__(self, profiles_dir: Path, pending_dir: Path | None = None):
        self.profiles_dir = Path(profiles_dir)
        self.pending_dir = Path(pending_dir) if pending_dir else self.profiles_dir / "_pending"
        self._specs: dict[str, ProfileSpec] = {}
        self.reload()

    def reload(self) -> None:
        self._specs = {}
        if not self.profiles_dir.exists():
            return
        for path in sorted(self.profiles_dir.glob("*.md")):
            stem = path.stem
            # Skip localized variants (foo.zh.md) and underscore-prefixed files.
            if "." in stem or stem.startswith("_"):
                continue
            spec = _parse_profile_file(path)
            if spec:
                self._specs[spec.name] = spec

    def get(self, name: str | None) -> ProfileSpec | None:
        if not name:
            return None
        return self._specs.get(str(name).strip().lower())

    def all(self) -> list[ProfileSpec]:
        return list(self._specs.values())

    def is_empty(self) -> bool:
        return not self._specs

    def selection_options(self) -> list[dict]:
        """Options fed to LLMClient.select_profile()."""
        return [
            {"name": spec.name, "hint": spec.selection_hint()}
            for spec in self._specs.values()
        ]

    # ── One-time DocType.md migration ────────────────────────────────

    def migrate_from_doctype(self, doctype_file: Path) -> int:
        """Convert legacy DocType.md table rows into profile files.

        Only writes profiles that don't already exist. Returns the number
        of profiles written. The table itself is left untouched (and is
        never consulted again); the user deletes it when ready.
        """
        if not doctype_file.exists():
            return 0

        written = 0
        for category, persona, template, description in self._iter_doctype_rows(doctype_file):
            target = self.profiles_dir / f"{category}.md"
            if target.exists():
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(
                render_profile_markdown(
                    persona=persona,
                    template=template,
                    description=description,
                    applicable_when=description,
                    note=f"Migrated from DocType.md category `{category}`.",
                ),
                encoding="utf-8",
            )
            written += 1

        if written:
            logging.info(f"ProfileManager: migrated {written} DocType.md rows to profiles.")
            self.reload()
        return written

    @staticmethod
    def _iter_doctype_rows(doctype_file: Path):
        content = doctype_file.read_text(encoding="utf-8")
        for line in content.splitlines():
            line = line.strip()
            if not (line.startswith("|") and line.endswith("|")):
                continue
            parts = [p.strip() for p in line.split("|")[1:-1]]
            if len(parts) < 3:
                continue
            category = parts[0].lower()
            if not category or category == "category" or set(category) <= {"-", " "}:
                continue
            yield category, parts[1], parts[2], parts[3] if len(parts) > 3 else ""

    # ── Pending review queue ─────────────────────────────────────────

    def queue_pending(
        self,
        *,
        profile_name: str,
        persona_name: str,
        persona_content: str,
        template_name: str,
        template_content: str,
        description: str = "",
        notify_dir: Path | None = None,
    ) -> Path:
        """Write an auto-generated persona/template/profile trio to _pending/.

        Nothing under _pending/ is active. The notice tells the user which
        file goes where; files use their real target names so activation is
        three drag-and-drops in Obsidian.
        """
        bundle_dir = self.pending_dir / profile_name
        bundle_dir.mkdir(parents=True, exist_ok=True)

        (bundle_dir / f"{persona_name}.md").write_text(persona_content, encoding="utf-8")
        (bundle_dir / f"{template_name}.md").write_text(template_content, encoding="utf-8")
        (bundle_dir / f"{profile_name}.md").write_text(
            render_profile_markdown(
                persona=persona_name,
                template=template_name,
                description=description,
                applicable_when=description,
                note=f"Auto-generated for new document category `{profile_name}`. Review before activating.",
            ),
            encoding="utf-8",
        )

        if notify_dir is not None:
            self._write_review_notice(
                notify_dir, profile_name, persona_name, template_name, bundle_dir
            )
        return bundle_dir

    def has_pending(self, profile_name: str) -> bool:
        return (self.pending_dir / profile_name).exists()

    @staticmethod
    def _write_review_notice(
        notify_dir: Path,
        profile_name: str,
        persona_name: str,
        template_name: str,
        bundle_dir: Path,
    ) -> None:
        try:
            notify_dir.mkdir(parents=True, exist_ok=True)
            notice = notify_dir / f"[review] new profile - {profile_name}.md"
            stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
            notice.write_text(
                f"# 🧾 新 Profile 待審核：{profile_name}\n\n"
                f"Ling Ling 在 {stamp} 遇到無法分類的文件，自動草擬了一組新資產，"
                f"已放入 `Scripture/Profiles/_pending/{profile_name}/` 等你審核。\n\n"
                f"審核通過後，把檔案搬到正式位置即可生效：\n\n"
                f"1. `{persona_name}.md` → `Scripture/Personas/`\n"
                f"2. `{template_name}.md` → `Templates/`\n"
                f"3. `{profile_name}.md` → `Scripture/Profiles/`\n\n"
                f"在生效之前，同類文件會先以 `default` profile 處理。\n",
                encoding="utf-8",
            )
        except Exception as e:
            logging.warning(f"ProfileManager: failed to write review notice: {e}")
