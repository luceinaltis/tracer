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
    """The `config` command is isolated via QRACER_CONFIG_DIR, which both the
    loader (reads) and the writer (writes) honor."""

    def _env(self, monkeypatch, tmp_path: Path) -> Path:
        d = tmp_path / ".qracer"
        d.mkdir()
        monkeypatch.setenv("QRACER_CONFIG_DIR", str(d))
        return d

    def test_config_show_lists_groups_and_providers(self, monkeypatch, tmp_path: Path) -> None:
        self._env(monkeypatch, tmp_path)
        result = CliRunner().invoke(main, ["config"])
        assert result.exit_code == 0
        assert "General:" in result.output
        assert "default_mode" in result.output
        assert "Providers:" in result.output

    def test_config_set_nested_and_seeds_file(self, monkeypatch, tmp_path: Path) -> None:
        d = self._env(monkeypatch, tmp_path)  # no config.toml yet
        result = CliRunner().invoke(main, ["config", "set", "briefing.schedule", "0 9 * * *"])
        assert result.exit_code == 0, result.output
        assert "Set briefing.schedule = 0 9 * * *" in result.output
        text = (d / "config.toml").read_text(encoding="utf-8")
        assert "[briefing]" in text and 'schedule = "0 9 * * *"' in text

    def test_config_set_compat_flag(self, monkeypatch, tmp_path: Path) -> None:
        d = self._env(monkeypatch, tmp_path)
        result = CliRunner().invoke(main, ["config", "--set", "default_mode=deep"])
        assert result.exit_code == 0
        assert 'default_mode = "deep"' in (d / "config.toml").read_text(encoding="utf-8")

    def test_config_get(self, monkeypatch, tmp_path: Path) -> None:
        self._env(monkeypatch, tmp_path)
        CliRunner().invoke(main, ["config", "set", "max_iterations", "9"])
        result = CliRunner().invoke(main, ["config", "get", "max_iterations"])
        assert result.exit_code == 0
        assert result.output.strip() == "9"

    def test_config_set_invalid_choice(self, monkeypatch, tmp_path: Path) -> None:
        self._env(monkeypatch, tmp_path)
        result = CliRunner().invoke(main, ["config", "set", "default_mode", "sideways"])
        assert result.exit_code != 0
        assert "must be one of" in result.output

    def test_config_set_unknown_key(self, monkeypatch, tmp_path: Path) -> None:
        self._env(monkeypatch, tmp_path)
        result = CliRunner().invoke(main, ["config", "set", "bogus", "1"])
        assert result.exit_code != 0
        assert "Unknown setting" in result.output

    def test_config_set_bad_compat_format(self, monkeypatch, tmp_path: Path) -> None:
        self._env(monkeypatch, tmp_path)
        result = CliRunner().invoke(main, ["config", "--set", "noequals"])
        assert result.exit_code != 0

    def test_config_providers_enables_and_sets_key(self, monkeypatch, tmp_path: Path) -> None:
        d = self._env(monkeypatch, tmp_path)
        # Configure 'dart': enable = yes, enter a key, then blank to finish.
        result = CliRunner().invoke(main, ["config", "providers"], input="dart\ny\nMY_DART_KEY\n\n")
        assert result.exit_code == 0, result.output
        providers = (d / "providers.toml").read_text(encoding="utf-8")
        assert "[providers.dart]" in providers
        assert "enabled = true" in providers.split("[providers.dart]", 1)[1]
        creds = (d / "credentials.env").read_text(encoding="utf-8")
        assert "DART_API_KEY=MY_DART_KEY" in creds
