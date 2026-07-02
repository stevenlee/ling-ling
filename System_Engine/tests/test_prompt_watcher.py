import os

os.environ.setdefault("LLM_PROVIDER", "vllm")

import threading
from types import SimpleNamespace
from unittest.mock import MagicMock

from watchers.prompt_watcher import PromptWatcher


class TestPromptWatcherPlannerFlags:
    def test_planner_mode_keyword_sets_preview_flag(self):
        flags = PromptWatcher._detect_planner_flags("@ling-insight planner-mode compare notes")
        assert flags["planner_mode"] is True
        assert flags["execute_plan"] is False

    def test_planner_slash_alias_sets_preview_flag(self):
        flags = PromptWatcher._detect_planner_flags("@ling-insight /planner compare notes")
        assert flags["planner_mode"] is True
        assert flags["execute_plan"] is False

    def test_execute_flag_is_detected_but_does_not_imply_planner(self):
        flags = PromptWatcher._detect_planner_flags("@ling-insight /execute compare notes")
        assert flags["planner_mode"] is False
        assert flags["execute_plan"] is True

    def test_execution_alias_sets_execute_flag(self):
        flags = PromptWatcher._detect_planner_flags(
            "@ling-insight planner-mode /execution compare notes"
        )
        assert flags["planner_mode"] is True
        assert flags["execute_plan"] is True


class TestPromptWatcherProcessPrompt:
    def test_default_qa_resolves_bracket_links(self, monkeypatch, tmp_path):
        from unittest.mock import MagicMock

        # 1. Mock llm_client
        mock_llm = MagicMock()
        mock_llm.provider = "vllm"
        mock_llm.model = "fake-model"
        mock_llm.answer_query.return_value = "mock_answer"
        mock_llm.current_trace_ids.return_value = []
        mock_llm.current_run_id.return_value = None

        # 2. Mock rag_manager
        mock_rag = MagicMock()
        mock_rag.query_similar_notes.return_value = ["similar note content"]

        # 3. Mock _resolve_source_paths
        mock_path = MagicMock()
        mock_path.read_text.return_value = "referenced file content"
        mock_resolve = MagicMock(return_value=[(mock_path, "direct")])

        monkeypatch.setattr("services.command_dispatcher._resolve_source_paths", mock_resolve)

        # 4. Create prompt file
        prompt_file = tmp_path / "test_prompt.md"
        prompt_file.write_text("Hello, what is in [[MyDoc]]?", encoding="utf-8")

        # 5. Instantiate PromptWatcher
        watcher = PromptWatcher(mock_llm, mock_rag)

        # Patch output path writing & archiving to avoid side effects
        monkeypatch.setattr(watcher, "_archive_raw", MagicMock())

        # Mock FROM_LLM_DIR
        mock_from_llm = tmp_path / "from_llm"
        mock_from_llm.mkdir()
        monkeypatch.setattr("services.command_dispatcher.FROM_LLM_DIR", mock_from_llm)

        # 6. Execute process_prompt
        watcher.process_prompt(prompt_file)

        # 7. Assertions
        mock_resolve.assert_called_once_with("MyDoc")

        # Check context argument passed to answer_query
        assert mock_llm.answer_query.call_count == 1
        called_args = mock_llm.answer_query.call_args
        query_content, context = called_args[0]

        assert "Hello, what is in [[MyDoc]]?" in query_content
        assert "## Source: MyDoc" in context
        assert "referenced file content" in context
        assert "similar note content" in context

    def test_default_qa_truncates_large_bracket_link_sources(self, monkeypatch, tmp_path):
        from unittest.mock import MagicMock

        mock_llm = MagicMock()
        mock_llm.provider = "vllm"
        mock_llm.model = "fake-model"
        mock_llm.answer_query.return_value = "mock_answer"
        mock_llm.current_trace_ids.return_value = []
        mock_llm.current_run_id.return_value = None

        mock_rag = MagicMock()
        mock_rag.query_similar_notes.return_value = ["similar note content"]

        mock_path = MagicMock()
        mock_path.read_text.return_value = "abcdefg"
        mock_resolve = MagicMock(return_value=[(mock_path, "direct")])
        monkeypatch.setattr("services.command_dispatcher._resolve_source_paths", mock_resolve)
        monkeypatch.setattr("services.command_dispatcher.LOAD_SOURCES_MAX_CHARS_PER_SOURCE", 4)

        prompt_file = tmp_path / "test_prompt.md"
        prompt_file.write_text("Summarize [[BigDoc]]", encoding="utf-8")

        watcher = PromptWatcher(mock_llm, mock_rag)
        monkeypatch.setattr(watcher, "_archive_raw", MagicMock())

        mock_from_llm = tmp_path / "from_llm"
        mock_from_llm.mkdir()
        monkeypatch.setattr("services.command_dispatcher.FROM_LLM_DIR", mock_from_llm)

        watcher.process_prompt(prompt_file)

        _, context = mock_llm.answer_query.call_args[0]
        assert "## Source: BigDoc" in context
        assert "abcd" in context
        assert "efg" not in context
        assert "truncated by PromptWatcher default Q&A" in context
        assert "similar note content" in context

    def test_default_qa_supports_template(self, monkeypatch, tmp_path):
        from unittest.mock import MagicMock

        # The model returns a complete template-shaped document with its own
        # YAML frontmatter.
        template_output = (
            '---\ntitle: "Invention Disclosure: Foo"\n'
            'tags: ["patent", "disclosure", "software"]\ntype: "disclosure"\n---\n\n'
            "# Invention Disclosure (Software & Algorithm)\n"
        )
        mock_llm = MagicMock()
        mock_llm.provider = "vllm"
        mock_llm.model = "fake-model"
        mock_llm.answer_query.return_value = template_output
        mock_llm.current_trace_ids.return_value = []
        mock_llm.current_run_id.return_value = None

        mock_rag = MagicMock()
        mock_rag.query_similar_notes.return_value = []

        monkeypatch.setattr(
            "services.command_dispatcher._resolve_source_paths",
            MagicMock(return_value=[]),
        )

        prompt_file = tmp_path / "disclosure_req.md"
        prompt_file.write_text(
            "請根據 [[Automatic Case Investigator]] 的內容填入專利揭露書。\n"
            "/template sw-inv-disclosure-rpt",
            encoding="utf-8",
        )

        watcher = PromptWatcher(mock_llm, mock_rag)
        monkeypatch.setattr(watcher, "_archive_raw", MagicMock())

        mock_from_llm = tmp_path / "from_llm"
        mock_from_llm.mkdir()
        monkeypatch.setattr("services.command_dispatcher.FROM_LLM_DIR", mock_from_llm)

        watcher.process_prompt(prompt_file)

        # The parsed /template name is forwarded as forced_template.
        assert mock_llm.answer_query.call_count == 1
        assert mock_llm.answer_query.call_args.kwargs["forced_template"] == "sw-inv-disclosure-rpt"

        # Output is the template document verbatim — no chat envelope, no
        # blockquoted query, no double frontmatter.
        outputs = list(mock_from_llm.iterdir())
        assert len(outputs) == 1
        written = outputs[0].read_text(encoding="utf-8")
        assert written.startswith('---\ntitle: "Invention Disclosure: Foo"')
        assert "type: chat" not in written
        assert "> 請根據" not in written
        assert written.count("---\n") == 2  # only the template's own frontmatter


# ── R7-G: processing runs on a worker, not the watchdog dispatch thread ──


class TestPromptWatcherBrainOps:
    def test_detect_intent_routes_brain_ops(self):
        w = PromptWatcher(MagicMock(), MagicMock())
        cases = {
            "@ling-dream.md": "dream",
            "@ling-consolidate.md": "consolidate",
            "@ling-decay.md": "decay",
            "@ling-ledger.md": "ledger",
            "@ling-assess.md": "assess",
            "@ling-resynthesize.md": "resynthesize",
        }
        for name, intent in cases.items():
            assert w._detect_intent(name.lower(), "") == intent

    def test_consolidate_command_invokes_run_consolidation(self, monkeypatch, tmp_path):
        import maintenance.cortex_consolidation as cc

        calls = []

        def fake(llm, rag, **kw):
            calls.append((llm, rag))
            return SimpleNamespace(status="succeeded", message="2 new claim(s)")

        monkeypatch.setattr(cc, "run_consolidation", fake)

        mock_llm = MagicMock()
        mock_llm.provider = "vllm"
        mock_llm.model = "m"
        w = PromptWatcher(mock_llm, MagicMock())
        monkeypatch.setattr(w, "_archive_raw", MagicMock())
        out = tmp_path / "from"
        out.mkdir()
        monkeypatch.setattr("services.command_dispatcher.FROM_LLM_DIR", out)

        f = tmp_path / "@ling-consolidate.md"
        f.write_text("go", encoding="utf-8")
        w.process_prompt(f)

        assert len(calls) == 1  # ran the real maintenance fn
        reports = list(out.iterdir())
        assert reports and "sys-admin" in reports[0].name
        assert "2 new claim(s)" in reports[0].read_text(encoding="utf-8")

    def test_resynthesize_copies_source_and_images_into_consolidate(self, monkeypatch, tmp_path):
        raw = tmp_path / "raw_consolidate"
        (raw / "images" / "MyDoc").mkdir(parents=True)
        (raw / "MyDoc.md").write_text("# doc", encoding="utf-8")
        (raw / "images" / "MyDoc" / "a.jpeg").write_bytes(b"\xff\xd8\xff")
        cons = tmp_path / "Consolidate"
        monkeypatch.setattr("core.config.RAW_CONSOLIDATE_DIR", raw)
        monkeypatch.setattr("core.config.CONSOLIDATE_DIR", cons)

        w = PromptWatcher(MagicMock(), MagicMock())
        msg = w._resynthesize(["MyDoc"])

        assert (cons / "MyDoc.md").exists()  # source re-queued
        assert (cons / "images" / "MyDoc" / "a.jpeg").exists()  # sidecar restored
        assert "MyDoc" in msg

    def test_resynthesize_reports_missing_source(self, monkeypatch, tmp_path):
        raw = tmp_path / "raw_consolidate"
        raw.mkdir()
        monkeypatch.setattr("core.config.RAW_CONSOLIDATE_DIR", raw)
        monkeypatch.setattr("core.config.CONSOLIDATE_DIR", tmp_path / "Consolidate")
        w = PromptWatcher(MagicMock(), MagicMock())
        msg = w._resynthesize(["Ghost"])
        assert "找不到" in msg and "Ghost" in msg


class TestPromptWatcherWorker:
    def _watcher(self):
        return PromptWatcher(MagicMock(), MagicMock())

    def test_handle_event_enqueues_without_processing(self, tmp_path):
        w = self._watcher()
        drained = []
        w._drain_queue = lambda: drained.append(1)
        f = tmp_path / "cmd.md"
        f.write_text("hi", encoding="utf-8")

        w._handle_event(SimpleNamespace(is_directory=False, src_path=str(f)))

        assert str(f) in w._queued_paths  # enqueued
        assert w._wake.is_set()  # worker signaled
        assert drained == []  # NOT processed on the dispatch thread

    def test_non_prompt_suffix_ignored(self, tmp_path):
        w = self._watcher()
        f = tmp_path / "image.png"
        f.write_text("x", encoding="utf-8")
        w._handle_event(SimpleNamespace(is_directory=False, src_path=str(f)))
        assert str(f) not in w._queued_paths
        assert not w._wake.is_set()

    def test_worker_drains_off_thread(self, tmp_path):
        w = self._watcher()
        w._stability_delay = 0
        drained = threading.Event()
        w._drain_queue = lambda: drained.set()
        w.start()
        try:
            f = tmp_path / "cmd.md"
            f.write_text("hi", encoding="utf-8")
            w._handle_event(SimpleNamespace(is_directory=False, src_path=str(f)))
            assert drained.wait(timeout=3)  # worker picked it up and drained
        finally:
            w.stop()
        assert not w._worker.is_alive()  # stop() joined the worker
