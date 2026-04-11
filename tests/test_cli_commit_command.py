"""Tests for the `/diff` slash command and /commit compatibility alias."""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import MagicMock

from cli import HermesCLI


class TestCommitCommand:
    def _make_cli(self, workspace_root: Path):
        cli_obj = HermesCLI.__new__(HermesCLI)
        cli_obj.workspace_root = workspace_root
        cli_obj.review_natural_language = "en"
        cli_obj._run_review_prompt = MagicMock()
        return cli_obj

    def _init_repo(self, tmp_path: Path) -> Path:
        subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True, text=True)
        subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=tmp_path, check=True)
        subprocess.run(["git", "config", "user.name", "Test User"], cwd=tmp_path, check=True)
        (tmp_path / "demo.txt").write_text("hello\n", encoding="utf-8")
        subprocess.run(["git", "add", "demo.txt"], cwd=tmp_path, check=True)
        subprocess.run(["git", "commit", "-m", "initial"], cwd=tmp_path, check=True, capture_output=True, text=True)
        return tmp_path

    def test_commit_command_prints_message_outside_git_repo(self, tmp_path, capsys):
        cli_obj = self._make_cli(tmp_path)

        HermesCLI._handle_diff_command(cli_obj, "/diff")

        output = capsys.readouterr().out
        assert "git repository" in output.lower()
        cli_obj._run_review_prompt.assert_not_called()

    def test_commit_command_prints_message_when_repo_is_clean(self, tmp_path, capsys):
        repo = self._init_repo(tmp_path)
        cli_obj = self._make_cli(repo)

        HermesCLI._handle_diff_command(cli_obj, "/diff")

        output = capsys.readouterr().out
        assert "no git changes" in output.lower()
        cli_obj._run_review_prompt.assert_not_called()

    def test_commit_command_builds_prompt_from_current_diff(self, tmp_path):
        repo = self._init_repo(tmp_path)
        (repo / "demo.txt").write_text("hello\nworld\n", encoding="utf-8")
        cli_obj = self._make_cli(repo)

        HermesCLI._handle_diff_command(cli_obj, "/diff")

        cli_obj._run_review_prompt.assert_called_once()
        prompt = cli_obj._run_review_prompt.call_args.kwargs["prompt"]
        metadata = cli_obj._run_review_prompt.call_args.kwargs["metadata"]
        assert "demo.txt" in prompt
        assert "world" in prompt
        assert metadata["changed_paths"] == ["demo.txt"]
        assert cli_obj._run_review_prompt.call_args.kwargs["command_name"] == "diff"
        assert cli_obj._run_review_prompt.call_args.kwargs["title"] == "Diff"

    def test_commit_command_includes_extra_instruction(self, tmp_path):
        repo = self._init_repo(tmp_path)
        (repo / "demo.txt").write_text("hello\nworld\n", encoding="utf-8")
        cli_obj = self._make_cli(repo)

        HermesCLI._handle_diff_command(cli_obj, "/diff emphasize the user-facing behavior")

        prompt = cli_obj._run_review_prompt.call_args.kwargs["prompt"]
        assert "emphasize the user-facing behavior" in prompt
