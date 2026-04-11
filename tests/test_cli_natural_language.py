from unittest.mock import patch

from cli import HermesCLI


def _make_cli_stub() -> HermesCLI:
    cli = HermesCLI.__new__(HermesCLI)
    cli.review_natural_language = "en"
    return cli


def test_handle_language_command_shows_current_language(capsys):
    cli = _make_cli_stub()

    cli._handle_language_command("/language")

    output = capsys.readouterr().out
    assert "Natural-language output: English (en)" in output
    assert "Usage: /language <en|ja>" in output


def test_handle_language_command_updates_language_and_saves_config(capsys):
    cli = _make_cli_stub()

    with patch("cli.save_config_value", return_value=True) as save_mock:
        cli._handle_language_command("/language ja")

    output = capsys.readouterr().out
    assert "Natural-language output set to: Japanese (ja) (saved)" in output
    assert cli.review_natural_language == "ja"
    save_mock.assert_called_once_with("review.natural_language", "ja")
