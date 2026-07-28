"""Unit tests for the web Settings section's pure apply helpers (no NiceGUI)."""

from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("nicegui")

from qracer.config import settings_schema as ss  # noqa: E402
from qracer.web import settings_ui  # noqa: E402


class TestCoerceWidget:
    def test_int_from_number_float(self) -> None:
        assert settings_ui._coerce_widget(ss.require("max_iterations"), 9.0) == 9

    def test_bool(self) -> None:
        assert settings_ui._coerce_widget(ss.require("autonomous_enabled"), True) is True

    def test_str_from_none(self) -> None:
        assert settings_ui._coerce_widget(ss.require("briefing.timezone"), None) == ""


class TestApplyScalar:
    def test_writes_config_target(self, tmp_path: Path) -> None:
        stored = settings_ui.apply_scalar(ss.require("max_iterations"), 7.0, config_dir=tmp_path)
        assert stored == 7
        assert "max_iterations = 7" in (tmp_path / "config.toml").read_text(encoding="utf-8")

    def test_writes_nested_config_target(self, tmp_path: Path) -> None:
        settings_ui.apply_scalar(ss.require("briefing.schedule"), "0 9 * * *", config_dir=tmp_path)
        text = (tmp_path / "config.toml").read_text(encoding="utf-8")
        assert "[briefing]" in text and 'schedule = "0 9 * * *"' in text

    def test_writes_portfolio_target(self, tmp_path: Path) -> None:
        settings_ui.apply_scalar(ss.require("currency"), "KRW", config_dir=tmp_path)
        assert 'currency = "KRW"' in (tmp_path / "portfolio.toml").read_text(encoding="utf-8")

    def test_invalid_choice_raises_and_does_not_write(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="must be one of"):
            settings_ui.apply_scalar(ss.require("default_mode"), "sideways", config_dir=tmp_path)
        assert not (tmp_path / "config.toml").exists()

    def test_invalid_cron_raises(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="Invalid cron"):
            settings_ui.apply_scalar(ss.require("briefing.schedule"), "nope", config_dir=tmp_path)

    def test_blank_numeric_raises_valueerror_not_typeerror(self, tmp_path: Path) -> None:
        # A cleared ui.number yields None; must surface as ValueError, not TypeError.
        with pytest.raises(ValueError, match="must be a number"):
            settings_ui.apply_scalar(ss.require("max_iterations"), None, config_dir=tmp_path)


class TestApplyGroup:
    def test_writes_all_when_valid(self, tmp_path: Path) -> None:
        items = [
            (ss.require("briefing.top_n"), 9.0),
            (ss.require("briefing.schedule"), "0 9 * * *"),
        ]
        settings_ui.apply_group(items, config_dir=tmp_path)
        text = (tmp_path / "config.toml").read_text(encoding="utf-8")
        assert "top_n = 9" in text and 'schedule = "0 9 * * *"' in text

    def test_atomic_no_partial_write_on_validation_error(self, tmp_path: Path) -> None:
        # Second item is invalid; nothing (not even the valid first item) is written.
        items = [
            (ss.require("briefing.top_n"), 7),
            (ss.require("briefing.schedule"), "not a cron"),
        ]
        with pytest.raises(ValueError, match="Invalid cron"):
            settings_ui.apply_group(items, config_dir=tmp_path)
        assert not (tmp_path / "config.toml").exists()


class TestApplyProviderValidation:
    def test_blank_priority_raises_and_writes_nothing(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="Priority must be a number"):
            settings_ui.apply_provider("dart", True, None, "DART_API_KEY", "K", config_dir=tmp_path)
        assert not (tmp_path / "providers.toml").exists()
        assert not (tmp_path / "credentials.env").exists()


class TestApplyProvider:
    def test_enables_sets_priority_and_key(self, tmp_path: Path) -> None:
        settings_ui.apply_provider("dart", True, 40, "DART_API_KEY", "MYKEY", config_dir=tmp_path)
        providers = (tmp_path / "providers.toml").read_text(encoding="utf-8")
        dart = providers.split("[providers.dart]", 1)[1]
        assert "enabled = true" in dart
        assert "priority = 40" in dart
        from qracer.config import writer

        assert writer.read_credentials(config_dir=tmp_path) == {"DART_API_KEY": "MYKEY"}

    def test_blank_key_leaves_credentials_untouched(self, tmp_path: Path) -> None:
        settings_ui.apply_provider("dart", False, 40, "DART_API_KEY", "", config_dir=tmp_path)
        from qracer.config import writer

        assert writer.read_credentials(config_dir=tmp_path) == {}
        dart = (
            (tmp_path / "providers.toml")
            .read_text(encoding="utf-8")
            .split("[providers.dart]", 1)[1]
        )
        assert "enabled = false" in dart
