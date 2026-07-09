"""Tests for the Click CLI entrypoint."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from click.testing import CliRunner

from qracer.cli import main


class TestHelp:
    def test_no_args_shows_help(self) -> None:
        runner = CliRunner()
        result = runner.invoke(main, [])
        assert result.exit_code == 0
        assert "qracer" in result.output

    def test_help_flag(self) -> None:
        runner = CliRunner()
        result = runner.invoke(main, ["--help"])
        assert result.exit_code == 0
        assert "install" in result.output
        assert "status" in result.output
        assert "config" in result.output
        assert "repl" in result.output


class TestInstall:
    def test_install_creates_directory_and_files(self, tmp_path: Path) -> None:
        home_dir = tmp_path / ".qracer"
        runner = CliRunner()

        with patch("qracer.cli._user_dir", return_value=home_dir):
            # Choice=1 (Claude), skip API key, currency=USD
            result = runner.invoke(main, ["install"], input="1\n\nUSD\n")

        assert result.exit_code == 0
        assert home_dir.is_dir()
        assert (home_dir / "config.toml").exists()
        assert (home_dir / "providers.toml").exists()
        assert (home_dir / "portfolio.toml").exists()
        assert (home_dir / "credentials.env").exists()
        assert "Setup complete" in result.output

    def test_install_skips_existing_files(self, tmp_path: Path) -> None:
        home_dir = tmp_path / ".qracer"
        home_dir.mkdir()
        (home_dir / "config.toml").write_text("existing")

        runner = CliRunner()
        with patch("qracer.cli._user_dir", return_value=home_dir):
            result = runner.invoke(main, ["install"], input="1\n\nUSD\n")

        assert result.exit_code == 0
        assert "already exists" in result.output
        # Existing file should not be overwritten
        assert (home_dir / "config.toml").read_text() == "existing"

    def test_install_writes_credentials(self, tmp_path: Path) -> None:
        home_dir = tmp_path / ".qracer"
        runner = CliRunner()

        with patch("qracer.cli._user_dir", return_value=home_dir):
            # Choice=1 (Claude), enter API key
            result = runner.invoke(main, ["install"], input="1\nsk-ant-key123\nUSD\n")

        assert result.exit_code == 0
        creds = (home_dir / "credentials.env").read_text()
        assert "ANTHROPIC_API_KEY=sk-ant-key123" in creds

    def test_install_custom_currency(self, tmp_path: Path) -> None:
        home_dir = tmp_path / ".qracer"
        runner = CliRunner()

        with patch("qracer.cli._user_dir", return_value=home_dir):
            # Choice=1 (Claude), skip API key, currency=EUR
            result = runner.invoke(main, ["install"], input="1\n\nEUR\n")

        assert result.exit_code == 0
        portfolio = (home_dir / "portfolio.toml").read_text(encoding="utf-8")
        assert 'currency = "EUR"' in portfolio

    def test_install_selects_openai(self, tmp_path: Path) -> None:
        home_dir = tmp_path / ".qracer"
        runner = CliRunner()

        with patch("qracer.cli._user_dir", return_value=home_dir):
            # Choice=2 (OpenAI), enter API key
            result = runner.invoke(main, ["install"], input="2\nsk-openai-key\nUSD\n")

        assert result.exit_code == 0
        # Credentials should have OPENAI_API_KEY
        creds = (home_dir / "credentials.env").read_text()
        assert "OPENAI_API_KEY=sk-openai-key" in creds
        # providers.toml should have openai enabled, claude disabled
        assert "OpenAI" in result.output
        # Verify provider enablement via config loading
        import tomllib

        with open(home_dir / "providers.toml", "rb") as f:
            data = tomllib.load(f)
        assert data["providers"]["openai"]["enabled"] is True
        assert data["providers"]["claude"]["enabled"] is False

    def test_install_selects_openrouter_with_model(self, tmp_path: Path) -> None:
        home_dir = tmp_path / ".qracer"
        runner = CliRunner()

        from qracer.llm.openrouter_adapter import ModelInfo

        fake_models = [
            ModelInfo(
                id="anthropic/claude-3.5-sonnet",
                name="Claude 3.5 Sonnet",
                context_length=200000,
                prompt_price=3e-6,
                completion_price=1.5e-5,
            )
        ]

        with (
            patch("qracer.cli._user_dir", return_value=home_dir),
            patch("qracer.llm.openrouter_adapter.list_models", return_value=fake_models),
        ):
            # Choice=4 (OpenRouter), API key, search 'claude', pick #1, currency USD
            result = runner.invoke(main, ["install"], input="4\nsk-or-key\nclaude\n1\nUSD\n")

        assert result.exit_code == 0, result.output
        creds = (home_dir / "credentials.env").read_text()
        assert "OPENROUTER_API_KEY=sk-or-key" in creds

        import tomllib

        with open(home_dir / "providers.toml", "rb") as f:
            prov = tomllib.load(f)
        assert prov["providers"]["openrouter"]["enabled"] is True

        with open(home_dir / "config.toml", "rb") as f:
            cfg = tomllib.load(f)
        assert cfg["llm_provider"] == "openrouter"
        assert cfg["llm_model"] == "anthropic/claude-3.5-sonnet"


class TestStatus:
    def test_status_no_config(self) -> None:
        runner = CliRunner()
        with patch("qracer.cli.resolve_config_dirs", return_value=[]):
            result = runner.invoke(main, ["status"])

        assert result.exit_code == 0
        assert "No config directory found" in result.output

    def test_status_with_config(self, tmp_path: Path) -> None:
        config_dir = tmp_path / ".qracer"
        config_dir.mkdir()

        runner = CliRunner()
        with (
            patch("qracer.cli.resolve_config_dirs", return_value=[config_dir]),
            patch("qracer.cli.load_config") as mock_load,
        ):
            from qracer.config.models import (
                PortfolioConfig,
                ProviderConfig,
                ProvidersConfig,
                QracerConfig,
            )

            mock_load.return_value = QracerConfig(
                providers=ProvidersConfig(
                    providers={"yfinance": ProviderConfig(enabled=True, tier="hot")}
                ),
                portfolio=PortfolioConfig(currency="USD"),
                credentials={"FINNHUB_API_KEY": "abc123xyz"},
            )
            result = runner.invoke(main, ["status"])

        assert result.exit_code == 0
        assert "yfinance: enabled" in result.output
        assert "0 holdings" in result.output
        assert "FINNHUB_API_KEY" in result.output
        assert "abc123xyz" not in result.output  # should be masked


class TestConfig:
    def test_config_show(self) -> None:
        runner = CliRunner()
        with patch("qracer.cli.load_config") as mock_load:
            from qracer.config.models import QracerConfig

            mock_load.return_value = QracerConfig()
            result = runner.invoke(main, ["config"])

        assert result.exit_code == 0
        assert '"app"' in result.output

    def test_config_set(self, tmp_path: Path) -> None:
        home_dir = tmp_path / ".qracer"
        home_dir.mkdir()
        (home_dir / "config.toml").write_text('default_mode = "quick"\n')

        runner = CliRunner()
        with patch("qracer.cli._user_dir", return_value=home_dir):
            result = runner.invoke(main, ["config", "--set", "default_mode=deep"])

        assert result.exit_code == 0
        assert "Set default_mode=deep" in result.output
        content = (home_dir / "config.toml").read_text()
        assert "deep" in content

    def test_config_set_bad_format(self, tmp_path: Path) -> None:
        runner = CliRunner()
        home_dir = tmp_path / ".qracer"
        home_dir.mkdir()
        (home_dir / "config.toml").write_text("")

        with patch("qracer.cli._user_dir", return_value=home_dir):
            result = runner.invoke(main, ["config", "--set", "noequals"])

        assert result.exit_code != 0

    def test_config_set_no_config_file(self, tmp_path: Path) -> None:
        home_dir = tmp_path / ".qracer"
        home_dir.mkdir()  # No config.toml

        runner = CliRunner()
        with patch("qracer.cli._user_dir", return_value=home_dir):
            result = runner.invoke(main, ["config", "--set", "key=val"])

        assert result.exit_code != 0
