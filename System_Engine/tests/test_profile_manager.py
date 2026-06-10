import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.absolute()))

from services.profile_manager import ProfileManager, render_profile_markdown


def _write_profile(dir_path: Path, name: str, **kwargs):
    dir_path.mkdir(parents=True, exist_ok=True)
    (dir_path / f"{name}.md").write_text(render_profile_markdown(**kwargs), encoding="utf-8")


class TestProfileScan:
    def test_scan_parses_frontmatter(self, tmp_path):
        _write_profile(
            tmp_path, "cookery",
            persona="cookery-curator", template="cookery-recipe-card",
            description="食譜", applicable_when="Recipes and cooking",
            operations=["digest_sources", "synthesize"],
        )
        pm = ProfileManager(tmp_path)
        spec = pm.get("cookery")
        assert spec is not None
        assert spec.persona == "cookery-curator"
        assert spec.template == "cookery-recipe-card"
        assert spec.operations == ("digest_sources", "synthesize")
        assert "Recipes" in spec.selection_hint()

    def test_get_normalizes_case_and_whitespace(self, tmp_path):
        _write_profile(tmp_path, "patent", persona="p", template="t")
        pm = ProfileManager(tmp_path)
        assert pm.get(" Patent ") is not None
        assert pm.get(None) is None
        assert pm.get("missing") is None

    def test_scan_skips_pending_localized_and_invalid(self, tmp_path):
        _write_profile(tmp_path, "good", persona="p", template="t")
        # Localized variant and underscore files must be ignored.
        (tmp_path / "good.zh.md").write_text("---\npersona: x\ntemplate: y\n---\n", encoding="utf-8")
        (tmp_path / "_notes.md").write_text("---\npersona: x\ntemplate: y\n---\n", encoding="utf-8")
        # Missing template → invalid, skipped with a warning.
        (tmp_path / "broken.md").write_text("---\npersona: only\n---\n", encoding="utf-8")
        # No frontmatter at all.
        (tmp_path / "plain.md").write_text("just text\n", encoding="utf-8")
        pending = tmp_path / "_pending"
        _write_profile(pending, "draft", persona="p", template="t")

        pm = ProfileManager(tmp_path)
        assert [s.name for s in pm.all()] == ["good"]

    def test_empty_dir_is_empty(self, tmp_path):
        pm = ProfileManager(tmp_path / "nonexistent")
        assert pm.is_empty()
        assert pm.selection_options() == []


class TestDocTypeMigration:
    DOCTYPE = (
        "# Document Type Mappings\n\n"
        "| Category | Persona | Template | Description |\n"
        "| -------- | ------- | -------- | ----------- |\n"
        "| patent   | patent-expert | patent-rpt | Patent report |\n"
        "| paper    | researcher    | research-rpt | Academic papers |\n"
    )

    def test_migration_creates_profiles_once(self, tmp_path):
        doctype = tmp_path / "DocType.md"
        doctype.write_text(self.DOCTYPE, encoding="utf-8")
        profiles_dir = tmp_path / "Profiles"

        pm = ProfileManager(profiles_dir)
        assert pm.migrate_from_doctype(doctype) == 2
        assert pm.get("patent").persona == "patent-expert"
        assert pm.get("paper").template == "research-rpt"

        # Second migration is a no-op (existing files untouched).
        assert pm.migrate_from_doctype(doctype) == 0

    def test_migration_does_not_overwrite_edited_profile(self, tmp_path):
        doctype = tmp_path / "DocType.md"
        doctype.write_text(self.DOCTYPE, encoding="utf-8")
        profiles_dir = tmp_path / "Profiles"
        _write_profile(profiles_dir, "patent", persona="my-custom", template="my-rpt")

        pm = ProfileManager(profiles_dir)
        pm.migrate_from_doctype(doctype)
        assert pm.get("patent").persona == "my-custom"

    def test_migration_without_doctype_is_noop(self, tmp_path):
        pm = ProfileManager(tmp_path / "Profiles")
        assert pm.migrate_from_doctype(tmp_path / "DocType.md") == 0


class TestPendingQueue:
    def test_queue_pending_writes_bundle_and_notice(self, tmp_path):
        profiles_dir = tmp_path / "Profiles"
        notify_dir = tmp_path / "fromLingLing"
        pm = ProfileManager(profiles_dir)

        bundle = pm.queue_pending(
            profile_name="diary",
            persona_name="diary-companion",
            persona_content="# Persona\nbody",
            template_name="diary-entry",
            template_content="# Template\nbody",
            description="Personal diary entries",
            notify_dir=notify_dir,
        )

        assert (bundle / "diary-companion.md").exists()
        assert (bundle / "diary-entry.md").exists()
        profile_file = bundle / "diary.md"
        assert profile_file.exists()
        assert "persona: diary-companion" in profile_file.read_text(encoding="utf-8")
        assert pm.has_pending("diary")

        notices = list(notify_dir.glob("*.md"))
        assert len(notices) == 1
        notice_text = notices[0].read_text(encoding="utf-8")
        assert "diary" in notice_text and "_pending" in notice_text

    def test_approve_pending_activates_bundle(self, tmp_path):
        profiles_dir = tmp_path / "Profiles"
        personas_dir = tmp_path / "Personas"
        templates_dir = tmp_path / "Templates"
        notify_dir = tmp_path / "fromLingLing"
        pm = ProfileManager(profiles_dir)
        pm.queue_pending(
            profile_name="diary",
            persona_name="diary-companion", persona_content="# P",
            template_name="diary-entry", template_content="# T",
            notify_dir=notify_dir,
        )

        result = pm.approve_pending(
            "diary", personas_dir=personas_dir, templates_dir=templates_dir,
            notify_dir=notify_dir,
        )

        assert result["ok"], result["errors"]
        assert (personas_dir / "diary-companion.md").exists()
        assert (templates_dir / "diary-entry.md").exists()
        assert pm.get("diary") is not None              # active after reload
        assert not (profiles_dir / "_pending" / "diary").exists()  # bundle removed
        assert list(notify_dir.glob("*.md")) == []      # notice cleaned up

    def test_approve_pending_refuses_overwrite(self, tmp_path):
        profiles_dir = tmp_path / "Profiles"
        personas_dir = tmp_path / "Personas"
        personas_dir.mkdir()
        (personas_dir / "diary-companion.md").write_text("existing", encoding="utf-8")
        pm = ProfileManager(profiles_dir)
        pm.queue_pending(
            profile_name="diary",
            persona_name="diary-companion", persona_content="# P",
            template_name="diary-entry", template_content="# T",
        )

        result = pm.approve_pending(
            "diary", personas_dir=personas_dir, templates_dir=tmp_path / "Templates",
        )

        assert not result["ok"]
        assert any("not overwriting" in e for e in result["errors"])
        # Nothing moved; bundle intact; existing file untouched.
        assert pm.has_pending("diary")
        assert (personas_dir / "diary-companion.md").read_text(encoding="utf-8") == "existing"

    def test_approve_missing_bundle_reports_error(self, tmp_path):
        pm = ProfileManager(tmp_path / "Profiles")
        result = pm.approve_pending(
            "ghost", personas_dir=tmp_path / "P", templates_dir=tmp_path / "T",
        )
        assert not result["ok"]
        assert "No pending bundle" in result["errors"][0]

    def test_pending_bundle_is_not_active(self, tmp_path):
        profiles_dir = tmp_path / "Profiles"
        pm = ProfileManager(profiles_dir)
        pm.queue_pending(
            profile_name="diary",
            persona_name="diary-companion", persona_content="x",
            template_name="diary-entry", template_content="y",
        )
        pm.reload()
        assert pm.get("diary") is None
