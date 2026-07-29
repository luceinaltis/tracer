"""Tests for the structured, comment-preserving config writer."""

from __future__ import annotations

from pathlib import Path

from qracer.config import writer


class TestConfigValue:
    def test_seeds_from_template_and_preserves_comments(self, tmp_path: Path) -> None:
        writer.set_config_value("max_iterations", 7, config_dir=tmp_path)
        text = (tmp_path / "config.toml").read_text(encoding="utf-8")
        assert "max_iterations = 7" in text
        assert "# Analysis loop tuning" in text  # template comment preserved
        assert 'default_mode = "quick"' in text  # untouched key survives

    def test_sets_nested_briefing_key(self, tmp_path: Path) -> None:
        writer.set_config_value("briefing.schedule", "0 9 * * *", config_dir=tmp_path)
        text = (tmp_path / "config.toml").read_text(encoding="utf-8")
        assert "[briefing]" in text
        assert 'schedule = "0 9 * * *"' in text
        assert "top_n = 5" in text  # other briefing keys intact

    def test_types_serialize_correctly(self, tmp_path: Path) -> None:
        writer.set_config_value("autonomous_enabled", False, config_dir=tmp_path)
        writer.set_config_value("confidence_threshold", 0.85, config_dir=tmp_path)
        text = (tmp_path / "config.toml").read_text(encoding="utf-8")
        assert "autonomous_enabled = false" in text
        assert "confidence_threshold = 0.85" in text


class TestPortfolioValue:
    def test_sets_currency_and_nested_limit(self, tmp_path: Path) -> None:
        writer.set_portfolio_value("currency", "KRW", config_dir=tmp_path)
        writer.set_portfolio_value("limits.max_sector_pct", 55.0, config_dir=tmp_path)
        text = (tmp_path / "portfolio.toml").read_text(encoding="utf-8")
        assert 'currency = "KRW"' in text
        assert "max_sector_pct = 55.0" in text


class TestProviderField:
    def test_enables_existing_block_preserving_others(self, tmp_path: Path) -> None:
        writer.set_provider_field("dart", "enabled", True, config_dir=tmp_path)
        text = (tmp_path / "providers.toml").read_text(encoding="utf-8")
        assert "[providers.dart]" in text
        dart_block = text.split("[providers.dart]", 1)[1].split("[providers.")[0]
        assert "enabled = true" in dart_block
        assert 'api_key_env = "DART_API_KEY"' in dart_block  # schema field preserved
        assert "[providers.yfinance]" in text  # other providers untouched

    def test_creates_missing_block_from_schema(self, tmp_path: Path) -> None:
        # A providers.toml that lacks the dart block entirely.
        (tmp_path / "providers.toml").write_text(
            '[providers.yfinance]\nenabled = true\nkind = "data"\n', encoding="utf-8"
        )
        writer.set_provider_field("dart", "enabled", True, config_dir=tmp_path)
        text = (tmp_path / "providers.toml").read_text(encoding="utf-8")
        assert "[providers.dart]" in text
        assert "enabled = true" in text.split("[providers.dart]", 1)[1]


class TestCredentials:
    def test_upsert_and_read(self, tmp_path: Path) -> None:
        writer.set_credential("DART_API_KEY", "abc", config_dir=tmp_path)
        writer.set_credential("DART_API_KEY", "xyz", config_dir=tmp_path)  # update in place
        writer.set_credential("FINNHUB_API_KEY", "fk", config_dir=tmp_path)
        assert writer.read_credentials(config_dir=tmp_path) == {
            "DART_API_KEY": "xyz",
            "FINNHUB_API_KEY": "fk",
        }

    def test_preserves_comments_and_blank_lines(self, tmp_path: Path) -> None:
        (tmp_path / "credentials.env").write_text("# my keys\nEXISTING=1\n", encoding="utf-8")
        writer.set_credential("NEW_KEY", "2", config_dir=tmp_path)
        text = (tmp_path / "credentials.env").read_text(encoding="utf-8")
        assert "# my keys" in text
        assert "EXISTING=1" in text
        assert "NEW_KEY=2" in text

    def test_unset(self, tmp_path: Path) -> None:
        writer.set_credential("A", "1", config_dir=tmp_path)
        writer.set_credential("B", "2", config_dir=tmp_path)
        writer.unset_credential("A", config_dir=tmp_path)
        assert writer.read_credentials(config_dir=tmp_path) == {"B": "2"}

    def test_unset_missing_is_noop(self, tmp_path: Path) -> None:
        writer.unset_credential("NOPE", config_dir=tmp_path)  # no file yet
        assert writer.read_credentials(config_dir=tmp_path) == {}

    def test_read_missing_returns_empty(self, tmp_path: Path) -> None:
        assert writer.read_credentials(config_dir=tmp_path) == {}
