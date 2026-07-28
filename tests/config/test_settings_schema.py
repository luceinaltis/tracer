"""Tests for the declarative settings registry."""

from __future__ import annotations

import pytest

from qracer.config import settings_schema as ss
from qracer.config.models import (
    AppConfig,
    BriefingConfig,
    PortfolioConfig,
    ProviderConfig,
    ProvidersConfig,
    QracerConfig,
)


class TestRegistry:
    def test_find_and_groups(self) -> None:
        assert ss.find("default_mode") is not None
        assert ss.find("nonexistent") is None
        # Groups are ordered and de-duplicated.
        assert ss.groups() == ["General", "Analysis", "Autonomous", "Briefing", "Portfolio"]

    def test_every_setting_has_a_valid_target(self) -> None:
        assert all(s.target in ("config", "portfolio") for s in ss.APP_SETTINGS)


class TestGetCurrent:
    def test_reads_across_app_briefing_and_portfolio(self) -> None:
        cfg = QracerConfig(
            app=AppConfig(default_mode="deep", briefing=BriefingConfig(top_n=9)),
            portfolio=PortfolioConfig(currency="KRW"),
        )
        assert ss.get_current(cfg, ss.require("default_mode")) == "deep"
        assert ss.get_current(cfg, ss.require("briefing.top_n")) == 9
        assert ss.get_current(cfg, ss.require("currency")) == "KRW"
        assert ss.get_current(cfg, ss.require("limits.max_sector_pct")) == 40.0  # default


class TestCoerce:
    @pytest.mark.parametrize("raw,expected", [("true", True), ("off", False), ("1", True)])
    def test_bool(self, raw: str, expected: bool) -> None:
        assert ss.coerce(ss.require("autonomous_enabled"), raw) is expected

    def test_bool_invalid_raises(self) -> None:
        with pytest.raises(ValueError, match="boolean"):
            ss.coerce(ss.require("autonomous_enabled"), "maybe")

    def test_int_and_float(self) -> None:
        assert ss.coerce(ss.require("max_iterations"), "5") == 5
        assert ss.coerce(ss.require("confidence_threshold"), "0.9") == 0.9

    def test_str_passthrough(self) -> None:
        assert ss.coerce(ss.require("language"), "ko") == "ko"


class TestValidate:
    def test_choice_ok_and_rejected(self) -> None:
        ss.validate(ss.require("default_mode"), "quick")  # no raise
        with pytest.raises(ValueError, match="must be one of"):
            ss.validate(ss.require("default_mode"), "sideways")

    def test_cron_validator(self) -> None:
        ss.validate(ss.require("briefing.schedule"), "0 8 * * 1-5")  # no raise
        with pytest.raises(ValueError, match="Invalid cron"):
            ss.validate(ss.require("briefing.schedule"), "not a cron")


class TestProviderSettings:
    def _cfg(self) -> QracerConfig:
        return QracerConfig(
            providers=ProvidersConfig(
                providers={
                    "dart": ProviderConfig(
                        enabled=True, priority=40, api_key_env="DART_API_KEY", kind="data"
                    ),
                }
            )
        )

    def test_overlays_config_over_schema(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # has_key falls back to os.environ, so neutralize any real key in the env
        # to keep the assertion hermetic.
        monkeypatch.delenv("FINNHUB_API_KEY", raising=False)
        rows = ss.provider_settings(self._cfg(), credentials={"DART_API_KEY": "x"})
        by_name = {r.name: r for r in rows}
        # dart reflects the user's config + credential presence.
        assert by_name["dart"].enabled is True
        assert by_name["dart"].priority == 40
        assert by_name["dart"].has_key is True
        # A provider only in the schema (not in cfg) still appears, from schema defaults.
        assert "finnhub" in by_name
        assert by_name["finnhub"].enabled is False
        assert by_name["finnhub"].api_key_env == "FINNHUB_API_KEY"
        assert by_name["finnhub"].has_key is False

    def test_kinds_are_populated(self) -> None:
        rows = ss.provider_settings(self._cfg(), credentials={})
        by_name = {r.name: r for r in rows}
        assert by_name["dart"].kind == "data"
        assert by_name["claude"].kind == "llm"
