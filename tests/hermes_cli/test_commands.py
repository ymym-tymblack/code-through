"""Tests for shared slash command definitions and autocomplete."""

from pathlib import Path

from prompt_toolkit.completion import CompleteEvent
from prompt_toolkit.document import Document

from hermes_cli.commands import COMMANDS, SlashCommandCompleter


# All commands that must be present in the shared COMMANDS dict.
EXPECTED_COMMANDS = {
    "/help", "/tools", "/toolsets", "/model", "/provider", "/prompt",
    "/personality", "/clear", "/history", "/new", "/reset", "/retry",
    "/undo", "/save", "/config", "/cron", "/skills", "/platforms",
    "/verbose", "/reasoning", "/language", "/compress", "/title", "/usage", "/insights", "/paste",
    "/reload-mcp", "/rollback", "/background", "/review", "/explain", "/flow",
    "/skin", "/voice", "/quit",
}


def _completions(completer: SlashCommandCompleter, text: str):
    return list(
        completer.get_completions(
            Document(text=text),
            CompleteEvent(completion_requested=True),
        )
    )


class TestCommands:
    def test_shared_commands_include_cli_specific_entries(self):
        """Entries that previously only existed in cli.py are now in the shared dict."""
        assert COMMANDS["/paste"] == "Check clipboard for an image and attach it"
        assert COMMANDS["/reload-mcp"] == "Reload MCP servers from config.yaml"

    def test_all_expected_commands_present(self):
        """Regression guard — every known command must appear in the shared dict."""
        assert set(COMMANDS.keys()) == EXPECTED_COMMANDS

    def test_every_command_has_nonempty_description(self):
        for cmd, desc in COMMANDS.items():
            assert isinstance(desc, str) and len(desc) > 0, f"{cmd} has empty description"


class TestSlashCommandCompleter:
    # -- basic prefix completion -----------------------------------------

    def test_builtin_prefix_completion_uses_shared_registry(self):
        completions = _completions(SlashCommandCompleter(), "/re")
        texts = {item.text for item in completions}

        assert "reset" in texts
        assert "retry" in texts
        assert "reload-mcp" in texts

    def test_builtin_completion_display_meta_shows_description(self):
        completions = _completions(SlashCommandCompleter(), "/help")
        assert len(completions) == 1
        assert completions[0].display_meta_text == "Show this help message"

    # -- exact-match trailing space --------------------------------------

    def test_exact_match_completion_adds_trailing_space(self):
        completions = _completions(SlashCommandCompleter(), "/help")

        assert [item.text for item in completions] == ["help "]

    def test_partial_match_does_not_add_trailing_space(self):
        completions = _completions(SlashCommandCompleter(), "/hel")

        assert [item.text for item in completions] == ["help"]

    # -- non-slash input returns nothing ---------------------------------

    def test_no_completions_for_non_slash_input(self):
        assert _completions(SlashCommandCompleter(), "help") == []

    def test_no_completions_for_empty_input(self):
        assert _completions(SlashCommandCompleter(), "") == []

    # -- skill commands via provider ------------------------------------

    def test_skill_commands_are_completed_from_provider(self):
        completer = SlashCommandCompleter(
            skill_commands_provider=lambda: {
                "/gif-search": {"description": "Search for GIFs across providers"},
            }
        )

        completions = _completions(completer, "/gif")

        assert len(completions) == 1
        assert completions[0].text == "gif-search"
        assert completions[0].display_text == "/gif-search"
        assert completions[0].display_meta_text == "⚡ Search for GIFs across providers"

    def test_skill_exact_match_adds_trailing_space(self):
        completer = SlashCommandCompleter(
            skill_commands_provider=lambda: {
                "/gif-search": {"description": "Search for GIFs"},
            }
        )

        completions = _completions(completer, "/gif-search")

        assert len(completions) == 1
        assert completions[0].text == "gif-search "

    def test_no_skill_provider_means_no_skill_completions(self):
        """Default (None) provider should not blow up or add completions."""
        completer = SlashCommandCompleter()
        completions = _completions(completer, "/gif")
        # /gif doesn't match any builtin command
        assert completions == []

    def test_skill_provider_exception_is_swallowed(self):
        """A broken provider should not crash autocomplete."""
        completer = SlashCommandCompleter(
            skill_commands_provider=lambda: (_ for _ in ()).throw(RuntimeError("boom")),
        )
        # Should return builtin matches only, no crash
        completions = _completions(completer, "/he")
        texts = {item.text for item in completions}
        assert "help" in texts

    def test_skill_description_truncated_at_50_chars(self):
        long_desc = "A" * 80
        completer = SlashCommandCompleter(
            skill_commands_provider=lambda: {
                "/long-skill": {"description": long_desc},
            }
        )
        completions = _completions(completer, "/long")
        assert len(completions) == 1
        meta = completions[0].display_meta_text
        # "⚡ " prefix + 50 chars + "..."
        assert meta == f"⚡ {'A' * 50}..."

    def test_skill_missing_description_uses_fallback(self):
        completer = SlashCommandCompleter(
            skill_commands_provider=lambda: {
                "/no-desc": {},
            }
        )
        completions = _completions(completer, "/no-desc")
        assert len(completions) == 1
        assert "Skill command" in completions[0].display_meta_text

    # -- builtin option completion ----------------------------------------

    def test_command_option_completion_suggests_review_actions(self):
        completions = _completions(SlashCommandCompleter(), "/review ")

        texts = {item.text for item in completions}
        assert {"on", "off", "status", "last", "apply"}.issubset(texts)
        assert any(item.display_meta_text == "Show review watcher status" for item in completions)

    def test_command_option_completion_filters_by_prefix(self):
        completions = _completions(SlashCommandCompleter(), "/reasoning sh")

        assert [item.text for item in completions] == ["show"]
        assert completions[0].display_meta_text == "Show model reasoning in output"

    def test_language_command_suggests_supported_languages(self):
        completions = _completions(SlashCommandCompleter(), "/language j")

        assert [item.text for item in completions] == ["ja"]
        assert completions[0].display_meta_text == "Japanese natural-language output"

    def test_exact_option_match_adds_trailing_space(self):
        completions = _completions(SlashCommandCompleter(), "/voice tts")

        assert [item.text for item in completions] == ["tts "]

    def test_prompt_command_suggests_clear_option(self):
        completions = _completions(SlashCommandCompleter(), "/prompt c")

        assert [item.text for item in completions] == ["clear"]
        assert completions[0].display_meta_text == "Remove the custom system prompt"

    def test_personality_command_uses_dynamic_options_provider(self):
        completer = SlashCommandCompleter(
            command_options_provider=lambda command: {
                "/personality": (
                    ("none", "Disable personality overlay"),
                    ("teacher", "Explain concepts clearly with examples"),
                )
            }.get(command, ()),
        )

        completions = _completions(completer, "/personality t")

        assert [item.text for item in completions] == ["teacher"]
        assert completions[0].display_meta_text == "Explain concepts clearly with examples"

    def test_skin_command_uses_dynamic_options_provider(self):
        completer = SlashCommandCompleter(
            command_options_provider=lambda command: {
                "/skin": (
                    ("default", "Classic Hermes gold/kawaii"),
                    ("slate", "Cool blue developer-focused theme"),
                )
            }.get(command, ()),
        )

        completions = _completions(completer, "/skin s")

        assert [item.text for item in completions] == ["slate"]
        assert completions[0].display_meta_text == "Cool blue developer-focused theme"

    def test_explain_command_completes_workspace_paths(self, tmp_path):
        docs = tmp_path / "docs"
        docs.mkdir()
        guide = docs / "guide.md"
        guide.write_text("hello", encoding="utf-8")

        completer = SlashCommandCompleter(workspace_root_provider=lambda: tmp_path)
        completions = _completions(completer, "/explain do")

        assert [item.text for item in completions] == ["docs/"]
        assert completions[0].display_meta_text == "Directory"

    def test_flow_command_completes_optional_path_argument(self, tmp_path):
        package = tmp_path / "pkg"
        package.mkdir()
        module = package / "worker.py"
        module.write_text("def run():\n    return 1\n", encoding="utf-8")

        completer = SlashCommandCompleter(workspace_root_provider=lambda: tmp_path)
        completions = _completions(completer, "/flow run pk")

        assert [item.text for item in completions] == ["pkg/"]
        assert completions[0].display_meta_text == "Directory"

    def test_command_without_registered_options_does_not_suggest_arguments(self):
        assert _completions(SlashCommandCompleter(), "/help ") == []

    def test_only_first_argument_gets_option_completion(self):
        assert _completions(SlashCommandCompleter(), "/review apply now") == []

    def test_non_path_second_argument_for_flow_does_not_complete(self):
        assert _completions(SlashCommandCompleter(), "/flow symbol other extra") == []
