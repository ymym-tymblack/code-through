from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from rich.console import Console

from cli import (
    COMPACT_BANNER,
    HERMES_AGENT_LOGO,
    HermesCLI,
    _build_compact_banner,
    _rich_text_from_ansi,
    build_welcome_banner,
)
from hermes_cli.skin_engine import get_active_skin, set_active_skin


def _make_cli_stub():
    cli = HermesCLI.__new__(HermesCLI)
    cli._sudo_state = None
    cli._secret_state = None
    cli._approval_state = None
    cli._clarify_state = None
    cli._clarify_freetext = False
    cli._command_running = False
    cli._agent_running = False
    cli._voice_recording = False
    cli._voice_processing = False
    cli._voice_mode = False
    cli._command_spinner_frame = lambda: "⟳"
    cli._tui_style_base = {
        "prompt": "#fff",
        "input-area": "#fff",
        "input-rule": "#aaa",
        "prompt-working": "#888 italic",
    }
    cli._app = SimpleNamespace(style=None)
    cli._invalidate = MagicMock()
    return cli


class TestCliSkinPromptIntegration:
    def test_default_prompt_fragments_use_default_symbol(self):
        cli = _make_cli_stub()

        set_active_skin("default")
        assert cli._get_tui_prompt_fragments() == [("class:prompt", "❯ ")]

    def test_ares_prompt_fragments_use_skin_symbol(self):
        cli = _make_cli_stub()

        set_active_skin("ares")
        assert cli._get_tui_prompt_fragments() == [("class:prompt", "⚔ ❯ ")]

    def test_secret_prompt_fragments_preserve_secret_state(self):
        cli = _make_cli_stub()
        cli._secret_state = {"response_queue": object()}

        set_active_skin("ares")
        assert cli._get_tui_prompt_fragments() == [("class:sudo-prompt", "🔑 ❯ ")]

    def test_icon_only_skin_symbol_still_visible_in_special_states(self):
        cli = _make_cli_stub()
        cli._secret_state = {"response_queue": object()}

        with patch("hermes_cli.skin_engine.get_active_prompt_symbol", return_value="⚔ "):
            assert cli._get_tui_prompt_fragments() == [("class:sudo-prompt", "🔑 ⚔ ")]

    def test_build_tui_style_dict_uses_skin_overrides(self):
        cli = _make_cli_stub()

        set_active_skin("ares")
        skin = get_active_skin()
        style_dict = cli._build_tui_style_dict()

        assert style_dict["prompt"] == skin.get_color("prompt")
        assert style_dict["input-rule"] == skin.get_color("input_rule")
        assert style_dict["prompt-working"] == f"{skin.get_color('banner_dim')} italic"
        assert style_dict["approval-title"] == f"{skin.get_color('ui_warn')} bold"

    def test_apply_tui_skin_style_updates_running_app(self):
        cli = _make_cli_stub()

        set_active_skin("ares")
        assert cli._apply_tui_skin_style() is True
        assert cli._app.style is not None
        cli._invalidate.assert_called_once_with(min_interval=0.0)

    def test_handle_skin_command_refreshes_live_tui(self, capsys):
        cli = _make_cli_stub()

        with patch("cli.save_config_value", return_value=True):
            cli._handle_skin_command("/skin ares")

        output = capsys.readouterr().out
        assert "Skin set to: ares (saved)" in output
        assert "Prompt + TUI colors updated." in output
        assert cli._app.style is not None


class TestBannerBrandingIntegration:
    def test_build_welcome_banner_uses_ex_branding_in_title(self, monkeypatch):
        set_active_skin("default")
        console = Console(record=True, width=120)

        monkeypatch.setattr("model_tools.check_tool_availability", lambda quiet=True: ([], []))
        monkeypatch.setattr("cli._get_available_skills", lambda: {})

        build_welcome_banner(
            console,
            model="anthropic/claude-opus-4.1",
            cwd="/tmp",
            tools=[],
            enabled_toolsets=[],
            session_id=None,
            context_length=None,
        )

        assert "HERMES-AGENT-Ex v" in console.export_text()

    def test_logo_markup_uses_ex_name_with_inverted_gold_palette(self):
        assert "HERMES-AGENT-Ex" in HERMES_AGENT_LOGO
        assert "███████╗██╗  ██╗" in HERMES_AGENT_LOGO
        assert "#CD7F32" in HERMES_AGENT_LOGO
        assert "#FFD700" in HERMES_AGENT_LOGO
        assert "#7FDBFF" not in HERMES_AGENT_LOGO
        assert "#4DA3FF" not in HERMES_AGENT_LOGO

    def test_compact_banner_uses_ex_name_with_inverted_gold_palette(self):
        assert "HERMES-AGENT-Ex" in COMPACT_BANNER
        assert "#CD7F32" in COMPACT_BANNER
        assert "#FFD700" in COMPACT_BANNER
        assert "#7FDBFF" not in COMPACT_BANNER
        assert "#4DA3FF" not in COMPACT_BANNER

    def test_dynamic_compact_banner_uses_ex_name_with_inverted_gold_palette(self, monkeypatch):
        monkeypatch.setattr("shutil.get_terminal_size", lambda *args, **kwargs: SimpleNamespace(columns=80))
        banner = _build_compact_banner()
        assert "HERMES-AGENT-Ex" in banner
        assert "#CD7F32" in banner
        assert "#FFD700" in banner
        assert "#7FDBFF" not in banner
        assert "#4DA3FF" not in banner


class TestAnsiRichTextHelper:
    def test_preserves_literal_brackets(self):
        text = _rich_text_from_ansi("[notatag] literal")
        assert text.plain == "[notatag] literal"

    def test_strips_ansi_but_keeps_plain_text(self):
        text = _rich_text_from_ansi("\x1b[31mred\x1b[0m")
        assert text.plain == "red"
