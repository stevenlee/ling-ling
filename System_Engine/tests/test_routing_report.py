from maintenance.routing_report import run_routing_report
from maintenance.template_audit import run_template_audit
from services.profile_manager import render_profile_markdown


class FakeTraceStore:
    def __init__(self, decisions=None, llm_calls=None):
        self.decisions = decisions or []
        self.llm_calls = llm_calls or []

    def query_artifacts(self, artifact_type, since_days=7):
        assert artifact_type == "routing_decision"
        return self.decisions

    def query_llm_calls(self, stage, since_days=7):
        assert stage == "select_profile"
        return self.llm_calls


def _decision(layer, profile=None, fallback=False):
    return {"metadata": {"layer": layer, "profile": profile, "fellback_to_default": fallback}}


def _dirs(tmp_path):
    profiles = tmp_path / "Profiles"
    pending = profiles / "_pending"
    report = tmp_path / "fromLingLing"
    log = tmp_path / "maintenance.log.md"
    profiles.mkdir(parents=True)
    return profiles, pending, report, log


class TestRoutingReport:
    def test_skipped_when_no_data(self, tmp_path):
        profiles, pending, report, log = _dirs(tmp_path)
        result = run_routing_report(
            FakeTraceStore(),
            profiles_dir=profiles,
            pending_dir=pending,
            report_dir=report,
            log_path=log,
        )
        assert result.status == "skipped"
        assert not log.exists()

    def test_healthy_week_logs_but_no_report(self, tmp_path):
        profiles, pending, report, log = _dirs(tmp_path)
        (profiles / "patent.md").write_text(
            render_profile_markdown(persona="p", template="t"), encoding="utf-8"
        )
        store = FakeTraceStore(
            decisions=[
                _decision("llm_selection", "patent"),
                _decision("frontmatter_profile", "patent"),
            ]
        )
        result = run_routing_report(
            store,
            profiles_dir=profiles,
            pending_dir=pending,
            report_dir=report,
            log_path=log,
        )
        assert result.status == "succeeded"
        assert result.fallback_rate == 0.0
        assert log.exists()
        assert result.report_path is None  # nothing actionable

    def test_high_fallback_rate_writes_report(self, tmp_path):
        profiles, pending, report, log = _dirs(tmp_path)
        (profiles / "patent.md").write_text(
            render_profile_markdown(persona="p", template="t"), encoding="utf-8"
        )
        (profiles / "ghost.md").write_text(
            render_profile_markdown(persona="p2", template="t2"), encoding="utf-8"
        )
        store = FakeTraceStore(
            decisions=[
                _decision("default_profile", "default", fallback=True),
                _decision("default_profile", "default", fallback=True),
                _decision("llm_selection", "patent"),
            ]
        )
        result = run_routing_report(
            store,
            profiles_dir=profiles,
            pending_dir=pending,
            report_dir=report,
            log_path=log,
            fallback_alert_rate=0.5,
        )
        assert result.fallback_rate > 0.5
        assert result.report_path is not None and result.report_path.exists()
        text = result.report_path.read_text(encoding="utf-8")
        assert "ghost" in text  # unused profile surfaced

    def test_pending_drafts_always_actionable(self, tmp_path):
        profiles, pending, report, log = _dirs(tmp_path)
        (pending / "diary").mkdir(parents=True)
        result = run_routing_report(
            FakeTraceStore(),
            profiles_dir=profiles,
            pending_dir=pending,
            report_dir=report,
            log_path=log,
        )
        assert result.status == "succeeded"
        assert result.pending_drafts == ["diary"]
        assert result.report_path is not None
        assert "approve diary" in result.report_path.read_text(encoding="utf-8")


class TestTemplateAudit:
    def _template(self, dir_path, name, version):
        dir_path.mkdir(parents=True, exist_ok=True)
        (dir_path / f"{name}.md").write_text(
            f"---\nversion: {version}\n---\n\n# Template body\n", encoding="utf-8"
        )

    def _page(self, dir_path, name, template, version):
        dir_path.mkdir(parents=True, exist_ok=True)
        (dir_path / f"{name}.md").write_text(
            f"---\ntitle: {name}\ntemplate: {template}\ntemplate_version: {version}\n---\n\nbody\n",
            encoding="utf-8",
        )

    def test_skipped_without_versioned_templates(self, tmp_path):
        result = run_template_audit(
            pages_dir=tmp_path / "pages",
            templates_dir=tmp_path / "Templates",
            report_dir=tmp_path / "out",
            log_path=tmp_path / "log.md",
        )
        assert result.status == "skipped"

    def test_detects_outdated_pages(self, tmp_path):
        templates = tmp_path / "Templates"
        pages = tmp_path / "pages"
        self._template(templates, "wiki-note", 2)
        self._page(pages / "A", "A (Synthesis)", "wiki-note", 1)  # outdated
        self._page(pages / "B", "B (Synthesis)", "wiki-note", 2)  # current
        (pages / "C.md").write_text("---\ntitle: C\n---\nno stamp\n", encoding="utf-8")

        result = run_template_audit(
            pages_dir=pages,
            templates_dir=templates,
            report_dir=tmp_path / "out",
            log_path=tmp_path / "log.md",
        )
        assert result.status == "succeeded"
        assert result.scanned == 3
        assert result.stamped == 2
        assert len(result.outdated["wiki-note"]) == 1
        assert result.report_path is not None
        assert "wiki-note" in result.report_path.read_text(encoding="utf-8")

    def test_no_report_when_all_current(self, tmp_path):
        templates = tmp_path / "Templates"
        pages = tmp_path / "pages"
        self._template(templates, "wiki-note", 1)
        self._page(pages / "A", "A (Synthesis)", "wiki-note", 1)

        result = run_template_audit(
            pages_dir=pages,
            templates_dir=templates,
            report_dir=tmp_path / "out",
            log_path=tmp_path / "log.md",
        )
        assert result.outdated == {}
        assert result.report_path is None
