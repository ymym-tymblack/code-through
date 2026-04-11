from types import SimpleNamespace
from unittest.mock import patch


def test_cmd_chat_forwards_workspace():
    from hermes_cli.main import cmd_chat

    args = SimpleNamespace(
        continue_last=None,
        resume=None,
        model=None,
        provider=None,
        toolsets=None,
        verbose=False,
        quiet=False,
        query=None,
        worktree=False,
        checkpoints=False,
        pass_session_id=False,
        workspace="/tmp/workspace",
    )

    with (
        patch("hermes_cli.main._has_any_provider_configured", return_value=True),
        patch("cli.main") as mock_cli_main,
        patch("tools.skills_sync.sync_skills"),
    ):
        cmd_chat(args)

    assert mock_cli_main.call_args.kwargs["workspace"] == "/tmp/workspace"
