"""Pydantic v2 models for .qracer/ configuration files."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class BriefingConfig(BaseModel):
    """config.toml [briefing] — AI-prioritized daily briefing pushed by `qracer serve`."""

    enabled: bool = False
    # cron expression (e.g. "0 8 * * 1-5" = weekday 08:00). Sent once per occurrence.
    schedule: str = "0 8 * * 1-5"
    timezone: str | None = None  # IANA tz (e.g. "America/New_York"); None = local
    top_n: int = 5  # how many prioritized items to surface


class AppConfig(BaseModel):
    """config.toml — global application settings."""

    default_mode: Literal["quick", "deep"] = "quick"
    llm_provider: str = "claude"
    llm_model: str = "claude-sonnet-4-20250514"
    language: str = "en"

    # Analysis loop tuning
    max_iterations: int = 3
    confidence_threshold: float = 0.7

    # Pipeline defaults
    lookback_days: int = 30
    staleness_hours: int = 24

    # Autonomous monitoring
    autonomous_enabled: bool = True
    price_move_threshold_pct: float = 2.0
    alert_cooldown_minutes: int = 30

    # Daily briefing ([briefing] table)
    briefing: BriefingConfig = Field(default_factory=BriefingConfig)


class ProviderConfig(BaseModel):
    """Single provider entry inside providers.toml."""

    enabled: bool = True
    priority: int = 100
    tier: Literal["hot", "warm", "cold"] = "warm"
    api_key_env: str | None = None
    kind: Literal["data", "llm"] = "data"


class ProvidersConfig(BaseModel):
    """providers.toml — mapping of provider name to its config."""

    providers: dict[str, ProviderConfig] = Field(default_factory=dict)


class Holding(BaseModel):
    """A single portfolio holding."""

    ticker: str
    shares: float
    avg_cost: float


class PortfolioLimits(BaseModel):
    """Risk limits for the portfolio."""

    max_single_position_pct: float = 15.0
    max_sector_pct: float = 40.0
    max_drawdown_alert_pct: float = 10.0


class PortfolioConfig(BaseModel):
    """portfolio.toml — portfolio settings."""

    currency: str = "USD"
    holdings: list[Holding] = Field(default_factory=list)
    limits: PortfolioLimits = Field(default_factory=PortfolioLimits)


class QracerConfig(BaseModel):
    """Merged top-level configuration combining all TOML files."""

    app: AppConfig = Field(default_factory=AppConfig)
    providers: ProvidersConfig = Field(default_factory=ProvidersConfig)
    portfolio: PortfolioConfig = Field(default_factory=PortfolioConfig)
    credentials: dict[str, str] = Field(default_factory=dict)
