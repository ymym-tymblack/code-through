"""Tests for user-defined quick commands that bypass the agent loop."""
import subprocess
from unittest.mock import MagicMock, patch, AsyncMock
from rich.text import Text
import pytest


# ── CLI tests ──────────────────────────────────────────────────────────────

class TestCLIQuickCommands:
    """Test quick command dispatch in HermesCLI.process_command."""

    @staticmethod
    def _printed_plain(call_arg):
        if isinstance(call_arg, Text):
            return call_arg.plain
        return str(call_arg)

    def _make_cli(self, quick_commands):
        from cli import HermesCLI
        cli = HermesCLI.__new__(HermesCLI)
        cli.config = {"quick_commands": quick_commands}
        cli.console = MagicMock()
        cli.agent = None
        cli.conversation_history = []
        return cli

    def test_exec_command_runs_and_prints_output(self):
        cli = self._make_cli({"dn": {"type": "exec", "command": "echo daily-note"}})
        result = cli.process_command("/dn")
        assert result is True
        cli.console.print.assert_called_once()
        printed = self._printed_plain(cli.console.print.call_args[0][0])
        assert printed == "daily-note"

    def test_exec_command_stderr_shown_on_no_stdout(self):
        cli = self._make_cli({"err": {"type": "exec", "command": "echo error >&2"}})
        result = cli.process_command("/err")
        assert result is True
        # stderr fallback — should print something
        cli.console.print.assert_called_once()

    def test_exec_command_no_output_shows_fallback(self):
        cli = self._make_cli({"empty": {"type": "exec", "command": "true"}})
        cli.process_command("/empty")
        cli.console.print.assert_called_once()
        args = cli.console.print.call_args[0][0]
        assert "no output" in args.lower()

    def test_unsupported_type_shows_error(self):
        cli = self._make_cli({"bad": {"type": "prompt", "command": "echo hi"}})
        cli.process_command("/bad")
        cli.console.print.assert_called_once()
        args = cli.console.print.call_args[0][0]
        assert "unsupported type" in args.lower()

    def test_missing_command_field_shows_error(self):
        cli = self._make_cli({"oops": {"type": "exec"}})
        cli.process_command("/oops")
        cli.console.print.assert_called_once()
        args = cli.console.print.call_args[0][0]
        assert "no command defined" in args.lower()

    def test_quick_command_takes_priority_over_skill_commands(self):
        """Quick commands must be checked before skill slash commands."""
        cli = self._make_cli({"mygif": {"type": "exec", "command": "echo overridden"}})
        with patch("cli._skill_commands", {"/mygif": {"name": "gif-search"}}):
            cli.process_command("/mygif")
        cli.console.print.assert_called_once()
        printed = self._printed_plain(cli.console.print.call_args[0][0])
        assert printed == "overridden"

    def test_unknown_command_still_shows_error(self):
        cli = self._make_cli({})
        cli.process_command("/nonexistent")
        cli.console.print.assert_called()
        args = cli.console.print.call_args_list[0][0][0]
        assert "unknown command" in args.lower()

    def test_timeout_shows_error(self):
        cli = self._make_cli({"slow": {"type": "exec", "command": "sleep 100"}})
        with patch("subprocess.run", side_effect=subprocess.TimeoutExpired("sleep", 30)):
            cli.process_command("/slow")
        cli.console.print.assert_called_once()
        args = cli.console.print.call_args[0][0]
        assert "timed out" in args.lower()

    def test_review_command_dispatches_to_handler(self):
        cli = self._make_cli({})
        cli._handle_review_command = MagicMock()
        cli.process_command("/review status")
        cli._handle_review_command.assert_called_once_with("/review status")

    def test_explain_command_dispatches_to_handler(self):
        cli = self._make_cli({})
        cli._handle_explain_command = MagicMock()
        cli.process_command("/explain cli.py")
        cli._handle_explain_command.assert_called_once_with("/explain cli.py")

    def test_flow_command_dispatches_to_handler(self):
        cli = self._make_cli({})
        cli._handle_flow_command = MagicMock()
        cli.process_command("/flow run")
        cli._handle_flow_command.assert_called_once_with("/flow run")

    def test_diff_command_dispatches_to_handler(self):
        cli = self._make_cli({})
        cli._handle_diff_command = MagicMock()
        cli.process_command("/diff left.py right.py")
        cli._handle_diff_command.assert_called_once_with("/diff left.py right.py")

    def test_promote_command_dispatches_to_handler(self):
        cli = self._make_cli({})
        cli._handle_promote_command = MagicMock()
        cli.process_command("/promote last")
        cli._handle_promote_command.assert_called_once_with("/promote last")

    def test_explain_command_handles_file_path(self, tmp_path):
        from cli import HermesCLI

        target = tmp_path / "sample.py"
        target.write_text("def run():\n    return 1\n", encoding="utf-8")

        cli = HermesCLI.__new__(HermesCLI)
        cli.workspace_root = tmp_path
        cli._run_review_prompt = MagicMock()
        cli._start_analysis_sync_watcher = MagicMock()

        cli._handle_explain_command("/explain sample.py")

        cli._run_review_prompt.assert_called_once()
        kwargs = cli._run_review_prompt.call_args.kwargs
        assert kwargs["title"] == "File Explain"
        assert kwargs["subtitle"] == "sample.py"
        cli._start_analysis_sync_watcher.assert_called_once()
        sync_kwargs = cli._start_analysis_sync_watcher.call_args.kwargs
        assert sync_kwargs["command_name"] == "explain"
        assert sync_kwargs["target_path"] == "sample.py"
        assert sync_kwargs["kind"] == "file"

    def test_explain_command_handles_directory_path(self, tmp_path):
        from cli import HermesCLI

        src_dir = tmp_path / "pkg"
        src_dir.mkdir()
        (src_dir / "__init__.py").write_text("", encoding="utf-8")
        (src_dir / "core.py").write_text("def run():\n    return 1\n", encoding="utf-8")

        cli = HermesCLI.__new__(HermesCLI)
        cli.workspace_root = tmp_path
        cli._run_review_prompt = MagicMock()
        cli._start_analysis_sync_watcher = MagicMock()

        cli._handle_explain_command("/explain pkg")

        cli._run_review_prompt.assert_called_once()
        kwargs = cli._run_review_prompt.call_args.kwargs
        assert kwargs["title"] == "Directory Explain"
        assert kwargs["subtitle"] == "pkg"
        cli._start_analysis_sync_watcher.assert_called_once()
        sync_kwargs = cli._start_analysis_sync_watcher.call_args.kwargs
        assert sync_kwargs["command_name"] == "explain"
        assert sync_kwargs["target_path"] == "pkg"
        assert sync_kwargs["kind"] == "directory"

    def test_flow_command_starts_sync_watcher(self, tmp_path):
        from cli import HermesCLI

        target = tmp_path / "sample.py"
        target.write_text("def run():\n    return 1\n", encoding="utf-8")

        cli = HermesCLI.__new__(HermesCLI)
        cli.workspace_root = tmp_path
        cli._run_review_prompt = MagicMock()
        cli._start_analysis_sync_watcher = MagicMock()

        cli._handle_flow_command("/flow run sample.py")

        cli._run_review_prompt.assert_called_once()
        kwargs = cli._run_review_prompt.call_args.kwargs
        assert kwargs["title"] == "Flow Explain"
        assert kwargs["subtitle"] == "run @ sample.py"
        cli._start_analysis_sync_watcher.assert_called_once()
        sync_kwargs = cli._start_analysis_sync_watcher.call_args.kwargs
        assert sync_kwargs["command_name"] == "flow"
        assert sync_kwargs["target_path"] == "sample.py"
        assert sync_kwargs["kind"] == "file"
        assert sync_kwargs["symbol"] == "run"

    def test_diff_command_runs_semantic_comparison(self, tmp_path):
        from cli import HermesCLI

        left = tmp_path / "left.py"
        left.write_text("def run(x):\n    return x + 1\n", encoding="utf-8")
        right = tmp_path / "right.py"
        right.write_text("def run(value):\n    return value + 1\n", encoding="utf-8")

        cli = HermesCLI.__new__(HermesCLI)
        cli.workspace_root = tmp_path
        cli._run_review_prompt = MagicMock()

        cli._handle_diff_command("/diff left.py right.py")

        cli._run_review_prompt.assert_called_once()
        kwargs = cli._run_review_prompt.call_args.kwargs
        assert kwargs["title"] == "Semantic Diff"
        assert kwargs["subtitle"] == "left.py <> right.py"
        assert kwargs["command_name"] == "diff"
        assert kwargs["metadata"]["left_path"] == "left.py"
        assert kwargs["metadata"]["right_path"] == "right.py"


    def test_promote_command_queues_followup_prompt(self):
        from cli import HermesCLI

        payload = {
            "command": "review",
            "title": "Diff Review",
            "subtitle": "cli.py",
            "content": {"text": "## Improvement Suggestions\n- Add a regression test for retry flow."},
            "metadata": {
                "promotion_candidates": [
                    {
                        "title": "Diff review workflow: Add a regression",
                        "summary": "Add a regression test for retry flow.",
                        "confidence": 0.84,
                        "source_paths": ["cli.py"],
                        "suggested_target": "skill",
                        "type": "skill",
                    }
                ]
            },
        }

        cli = HermesCLI.__new__(HermesCLI)
        cli._pending_input = MagicMock()
        cli.console = MagicMock()

        with patch("hermes_cli.codex_companion.HermesStore") as store_cls:
            store_cls.return_value.load_latest_output.return_value = payload
            cli._handle_promote_command("/promote review")

        cli._pending_input.put.assert_called_once()
        queued_prompt = cli._pending_input.put.call_args[0][0]
        assert "Create a new reusable Hermes skill" in queued_prompt
        assert "Add a regression test for retry flow." in queued_prompt

    def test_start_review_watcher_passes_exclude_globs(self):
        from cli import HermesCLI

        cli = HermesCLI.__new__(HermesCLI)
        cli._review_thread = None
        cli._ensure_runtime_credentials = MagicMock(return_value=True)
        cli.workspace_root = MagicMock()
        cli.api_key = "key"
        cli.base_url = "https://example.com/v1"
        cli.provider = "openrouter"
        cli.api_mode = "chat_completions"
        cli.review_natural_language = "en"
        cli._app = None
        cli.session_id = "sess"

        watcher_instance = MagicMock()
        with patch("cli.CLI_CONFIG", {"review": {"exclude_globs": ["memo/**", "notes/*.md"]}}), \
             patch("cli.threading.Thread") as thread_cls, \
             patch("hermes_cli.codex_companion.CodexCompanionWatcher", return_value=watcher_instance) as watcher_cls:
            thread_cls.return_value = MagicMock()
            assert cli._start_review_watcher() is True

        watcher_cls.assert_called_once()
        kwargs = watcher_cls.call_args.kwargs
        assert kwargs["ignore_globs"] == ["memo/**", "notes/*.md"]

    def test_review_exclude_add_updates_config_and_prints(self):
        from cli import HermesCLI

        cli = HermesCLI.__new__(HermesCLI)
        cli._review_thread = None
        cli.console = MagicMock()

        with patch("cli.CLI_CONFIG", {"review": {"exclude_globs": ["memo/**"]}}), \
             patch("cli.save_config_value", return_value=True) as save_mock, \
             patch("cli._cprint") as cprint_mock:
            cli._handle_review_command("/review exclude add notes/*.md")

        save_mock.assert_called_once_with("review.exclude_globs", ["memo/**", "notes/*.md"])
        printed = " ".join(str(call.args[0]) for call in cprint_mock.call_args_list)
        assert "Added review exclusion: notes/*.md" in printed

    def test_review_exclude_remove_updates_config_and_prints(self):
        from cli import HermesCLI

        cli = HermesCLI.__new__(HermesCLI)
        cli._review_thread = None
        cli.console = MagicMock()

        with patch("cli.CLI_CONFIG", {"review": {"exclude_globs": ["memo/**", "notes/*.md"]}}), \
             patch("cli.save_config_value", return_value=True) as save_mock, \
             patch("cli._cprint") as cprint_mock:
            cli._handle_review_command("/review exclude remove memo/**")

        save_mock.assert_called_once_with("review.exclude_globs", ["notes/*.md"])
        printed = " ".join(str(call.args[0]) for call in cprint_mock.call_args_list)
        assert "Removed review exclusion: memo/**" in printed

    def test_review_exclude_list_shows_patterns(self):
        from cli import HermesCLI

        cli = HermesCLI.__new__(HermesCLI)
        cli._review_thread = None
        cli.console = MagicMock()

        with patch("cli.CLI_CONFIG", {"review": {"exclude_globs": ["memo/**", "notes/*.md"]}}), \
             patch("cli._cprint") as cprint_mock:
            cli._handle_review_command("/review exclude list")

        printed = " ".join(str(call.args[0]) for call in cprint_mock.call_args_list)
        assert "Review exclusion globs" in printed
        assert "memo/**" in printed
        assert "notes/*.md" in printed


# ── Gateway tests ──────────────────────────────────────────────────────────

class TestGatewayQuickCommands:
    """Test quick command dispatch in GatewayRunner._handle_message."""

    def _make_event(self, command, args=""):
        event = MagicMock()
        event.get_command.return_value = command
        event.get_command_args.return_value = args
        event.text = f"/{command} {args}".strip()
        event.source = MagicMock()
        event.source.user_id = "test_user"
        event.source.user_name = "Test User"
        event.source.platform.value = "telegram"
        event.source.chat_type = "dm"
        event.source.chat_id = "123"
        return event

    @pytest.mark.asyncio
    async def test_exec_command_returns_output(self):
        from gateway.run import GatewayRunner
        runner = GatewayRunner.__new__(GatewayRunner)
        runner.config = {"quick_commands": {"limits": {"type": "exec", "command": "echo ok"}}}
        runner._running_agents = {}
        runner._pending_messages = {}
        runner._is_user_authorized = MagicMock(return_value=True)

        event = self._make_event("limits")
        result = await runner._handle_message(event)
        assert result == "ok"

    @pytest.mark.asyncio
    async def test_unsupported_type_returns_error(self):
        from gateway.run import GatewayRunner
        runner = GatewayRunner.__new__(GatewayRunner)
        runner.config = {"quick_commands": {"bad": {"type": "prompt", "command": "echo hi"}}}
        runner._running_agents = {}
        runner._pending_messages = {}
        runner._is_user_authorized = MagicMock(return_value=True)

        event = self._make_event("bad")
        result = await runner._handle_message(event)
        assert result is not None
        assert "unsupported type" in result.lower()

    @pytest.mark.asyncio
    async def test_timeout_returns_error(self):
        from gateway.run import GatewayRunner
        import asyncio
        runner = GatewayRunner.__new__(GatewayRunner)
        runner.config = {"quick_commands": {"slow": {"type": "exec", "command": "sleep 100"}}}
        runner._running_agents = {}
        runner._pending_messages = {}
        runner._is_user_authorized = MagicMock(return_value=True)

        event = self._make_event("slow")
        with patch("asyncio.wait_for", side_effect=asyncio.TimeoutError):
            result = await runner._handle_message(event)
        assert result is not None
        assert "timed out" in result.lower()

    @pytest.mark.asyncio
    async def test_gateway_config_object_supports_quick_commands(self):
        from gateway.config import GatewayConfig
        from gateway.run import GatewayRunner

        runner = GatewayRunner.__new__(GatewayRunner)
        runner.config = GatewayConfig(
            quick_commands={"limits": {"type": "exec", "command": "echo ok"}}
        )
        runner._running_agents = {}
        runner._pending_messages = {}
        runner._is_user_authorized = MagicMock(return_value=True)

        event = self._make_event("limits")
        result = await runner._handle_message(event)
        assert result == "ok"
