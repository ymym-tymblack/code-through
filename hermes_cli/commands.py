"""Slash command definitions and autocomplete for the Hermes CLI.

Contains the shared built-in ``COMMANDS`` dict and ``SlashCommandCompleter``.
The completer can optionally include dynamic skill slash commands supplied by the
interactive CLI.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from pathlib import Path
from typing import Any

from prompt_toolkit.completion import Completer, Completion


# Commands organized by category for better help display
COMMANDS_BY_CATEGORY = {
    "Session": {
        "/new": "Start a new session (fresh session ID + history)",
        "/reset": "Start a new session (alias for /new)",
        "/clear": "Clear screen and start a new session",
        "/history": "Show conversation history",
        "/save": "Save the current conversation",
        "/retry": "Retry the last message (resend to agent)",
        "/undo": "Remove the last user/assistant exchange",
        "/title": "Set a title for the current session (usage: /title My Session Name)",
        "/compress": "Manually compress conversation context (flush memories + summarize)",
        "/rollback": "List or restore filesystem checkpoints (usage: /rollback [number])",
        "/background": "Run a prompt in the background (usage: /background <prompt>)",
        "/review": "Manage automatic diff reviews (usage: /review [on|off|status|last|apply|promote])",
        "/explain": "Explain a file in natural language (usage: /explain <path>)",
        "/flow": "Explain a symbol or flow (usage: /flow <symbol> [path])",
        "/commit": "Generate a commit message for the current git diff (usage: /commit [extra guidance])",
        "/promote": "Queue a memory/skill promotion from the latest analysis (usage: /promote [last|review|explain|flow] [memory|skill] [index])",
    },
    "Configuration": {
        "/config": "Show current configuration",
        "/model": "Show or change the current model",
        "/provider": "Show available providers and current provider",
        "/prompt": "View/set custom system prompt",
        "/personality": "Set a predefined personality",
        "/verbose": "Cycle tool progress display: off → new → all → verbose",
        "/reasoning": "Manage reasoning effort and display (usage: /reasoning [level|show|hide])",
        "/language": "Show or change the natural-language output for explain/review commands",
        "/skin": "Show or change the display skin/theme",
        "/voice": "Toggle voice mode (Ctrl+B to record). Usage: /voice [on|off|tts|status]",
    },
    "Tools & Skills": {
        "/tools": "List available tools",
        "/toolsets": "List available toolsets",
        "/skills": "Search, install, inspect, or manage skills from online registries",
        "/cron": "Manage scheduled tasks (list, add, remove)",
        "/reload-mcp": "Reload MCP servers from config.yaml",
    },
    "Info": {
        "/help": "Show this help message",
        "/usage": "Show token usage for the current session",
        "/insights": "Show usage insights and analytics (last 30 days)",
        "/platforms": "Show gateway/messaging platform status",
        "/paste": "Check clipboard for an image and attach it",
    },
    "Exit": {
        "/quit": "Exit the CLI (also: /exit, /q)",
    },
}

# Flat dict for backwards compatibility and autocomplete
COMMANDS = {}
for category_commands in COMMANDS_BY_CATEGORY.values():
    COMMANDS.update(category_commands)


COMMAND_OPTIONS: dict[str, tuple[tuple[str, str], ...]] = {
    "/cron": (
        ("list", "List scheduled jobs"),
        ("add", "Create a scheduled job"),
        ("remove", "Remove a scheduled job by ID"),
        ("rm", "Alias for remove"),
        ("delete", "Alias for remove"),
    ),
    "/review": (
        ("on", "Enable automatic diff review watcher"),
        ("off", "Disable automatic diff review watcher"),
        ("status", "Show review watcher status"),
        ("last", "Show the latest diff review"),
        ("apply", "Queue an implementation prompt from the latest diff review"),
    ),
    "/promote": (
        ("last", "Use the most recent review/explain/flow output"),
        ("review", "Use the latest diff review output"),
        ("explain", "Use the latest explain output"),
        ("flow", "Use the latest flow output"),
        ("memory", "Promote into persistent memory"),
        ("skill", "Promote into a reusable skill"),
    ),
    "/reasoning": (
        ("none", "Disable reasoning effort"),
        ("low", "Set reasoning effort to low"),
        ("minimal", "Alias for low reasoning effort"),
        ("medium", "Set reasoning effort to medium"),
        ("high", "Set reasoning effort to high"),
        ("xhigh", "Set reasoning effort to xhigh"),
        ("show", "Show model reasoning in output"),
        ("on", "Alias for showing model reasoning"),
        ("hide", "Hide model reasoning in output"),
        ("off", "Alias for hiding model reasoning"),
    ),
    "/voice": (
        ("on", "Enable voice mode"),
        ("off", "Disable voice mode"),
        ("tts", "Toggle text-to-speech playback"),
        ("status", "Show voice mode status"),
    ),
    "/language": (
        ("en", "English natural-language output"),
        ("ja", "Japanese natural-language output"),
    ),
    "/prompt": (
        ("clear", "Remove the custom system prompt"),
    ),
    "/insights": (
        ("--days", "Set lookback window in days"),
        ("--source", "Filter insights by source/platform"),
    ),
}


CommandOption = tuple[str, str]
CommandArgContext = tuple[str, list[str], int, str]


class SlashCommandCompleter(Completer):
    """Autocomplete for built-in slash commands and optional skill commands."""

    def __init__(
        self,
        skill_commands_provider: Callable[[], Mapping[str, dict[str, Any]]] | None = None,
        command_options_provider: Callable[[str], Iterable[CommandOption]] | None = None,
        workspace_root_provider: Callable[[], str | Path] | None = None,
    ) -> None:
        self._skill_commands_provider = skill_commands_provider
        self._command_options_provider = command_options_provider
        self._workspace_root_provider = workspace_root_provider

    def _iter_skill_commands(self) -> Mapping[str, dict[str, Any]]:
        if self._skill_commands_provider is None:
            return {}
        try:
            return self._skill_commands_provider() or {}
        except Exception:
            return {}

    def _iter_dynamic_command_options(self, command: str) -> tuple[CommandOption, ...]:
        if self._command_options_provider is None:
            return ()
        try:
            options = self._command_options_provider(command) or ()
        except Exception:
            return ()
        return tuple((str(option), str(desc)) for option, desc in options)

    def _iter_command_options(self, command: str) -> tuple[CommandOption, ...]:
        seen: set[str] = set()
        combined: list[CommandOption] = []
        for option, desc in (*COMMAND_OPTIONS.get(command, ()), *self._iter_dynamic_command_options(command)):
            if option in seen:
                continue
            seen.add(option)
            combined.append((option, desc))
        return tuple(combined)

    def _workspace_root(self) -> Path | None:
        if self._workspace_root_provider is None:
            return None
        try:
            return Path(self._workspace_root_provider()).expanduser().resolve()
        except Exception:
            return None

    @staticmethod
    def _completion_text(cmd_name: str, word: str) -> str:
        """Return replacement text for a completion.

        When the user has already typed the full command exactly (``/help``),
        returning ``help`` would be a no-op and prompt_toolkit suppresses the
        menu. Appending a trailing space keeps the dropdown visible and makes
        backspacing retrigger it naturally.
        """
        return f"{cmd_name} " if cmd_name == word else cmd_name

    @staticmethod
    def _parse_argument_context(text: str) -> CommandArgContext | None:
        """Return (command, args, arg_index, current_arg) for slash command arguments."""
        body = text[1:]
        if not body or body.lstrip() != body or " " not in body:
            return None

        tokens = body.split(" ")
        if not tokens or not tokens[0]:
            return None

        command = f"/{tokens[0]}"
        args = tokens[1:]
        if not args:
            return None
        return command, args, len(args) - 1, args[-1]

    @staticmethod
    def _looks_like_path_prefix(value: str) -> bool:
        return not value or any(sep in value for sep in ("/", ".", "-", "_")) or value.islower()

    @staticmethod
    def _iter_path_completions(workspace_root: Path, prefix: str) -> tuple[CommandOption, ...]:
        normalized = prefix.replace("\\", "/")
        if normalized.startswith("/"):
            return ()

        prefix_path = Path(normalized) if normalized else Path()
        if normalized.endswith("/"):
            parent_rel = prefix_path
            name_prefix = ""
        elif prefix_path.parent == Path("."):
            parent_rel = Path()
            name_prefix = prefix_path.name
        else:
            parent_rel = prefix_path.parent
            name_prefix = prefix_path.name

        search_root = (workspace_root / parent_rel).resolve()
        try:
            search_root.relative_to(workspace_root)
        except ValueError:
            return ()
        if not search_root.is_dir():
            return ()

        completions: list[CommandOption] = []
        for child in sorted(search_root.iterdir(), key=lambda item: (not item.is_dir(), item.name.lower())):
            if name_prefix and not child.name.startswith(name_prefix):
                continue
            rel_path = child.relative_to(workspace_root).as_posix()
            if child.is_dir():
                completions.append((f"{rel_path}/", "Directory"))
            else:
                completions.append((rel_path, "File"))
        return tuple(completions)

    def _iter_argument_completions(self, context: CommandArgContext) -> tuple[CommandOption, ...]:
        command, _args, arg_index, current_arg = context
        if arg_index == 0:
            options = self._iter_command_options(command)
            if options:
                return options
            if command == "/explain":
                workspace_root = self._workspace_root()
                if workspace_root is not None:
                    return self._iter_path_completions(workspace_root, current_arg)
            return ()

        if command == "/flow" and arg_index == 1 and self._looks_like_path_prefix(current_arg):
            workspace_root = self._workspace_root()
            if workspace_root is not None:
                return self._iter_path_completions(workspace_root, current_arg)
        return ()

    def get_completions(self, document, complete_event):
        text = document.text_before_cursor
        if not text.startswith("/"):
            return

        argument_context = self._parse_argument_context(text)
        if argument_context is not None:
            _command, _args, _arg_index, current_arg = argument_context
            for option, desc in self._iter_argument_completions(argument_context):
                if option.startswith(current_arg):
                    yield Completion(
                        self._completion_text(option, current_arg),
                        start_position=-len(current_arg),
                        display=option,
                        display_meta=desc,
                    )
            return

        word = text[1:]

        for cmd, desc in COMMANDS.items():
            cmd_name = cmd[1:]
            if cmd_name.startswith(word):
                yield Completion(
                    self._completion_text(cmd_name, word),
                    start_position=-len(word),
                    display=cmd,
                    display_meta=desc,
                )

        for cmd, info in self._iter_skill_commands().items():
            cmd_name = cmd[1:]
            if cmd_name.startswith(word):
                description = str(info.get("description", "Skill command"))
                short_desc = description[:50] + ("..." if len(description) > 50 else "")
                yield Completion(
                    self._completion_text(cmd_name, word),
                    start_position=-len(word),
                    display=cmd,
                    display_meta=f"⚡ {short_desc}",
                )
