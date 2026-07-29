"""Autonomous market monitoring during trading hours.

Watches watchlist tickers for significant price moves and breaking news,
sending notifications when thresholds are breached.  Designed to run
inside the ``Server`` tick loop alongside AlertMonitor and TaskExecutor.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from zoneinfo import ZoneInfo

from qracer.data.providers import NewsProvider, PriceProvider
from qracer.data.registry import DataRegistry
from qracer.monitors.base import PollingMonitor
from qracer.watchlist import Watchlist

logger = logging.getLogger(__name__)

US_EASTERN = ZoneInfo("America/New_York")

# Defaults
DEFAULT_CHECK_INTERVAL = 60  # seconds between autonomous checks
DEFAULT_PRICE_THRESHOLD_PCT = 2.0
DEFAULT_COOLDOWN_MINUTES = 30


class TriggerType(str, Enum):
    PRICE_MOVE = "price_move"
    BREAKING_NEWS = "breaking_news"
    VOLUME_SPIKE = "volume_spike"


class Severity(str, Enum):
    INFO = "info"
    WARNING = "warning"
    CRITICAL = "critical"


@dataclass(frozen=True)
class AutonomousAlert:
    """A monitoring alert produced by the autonomous scanner."""

    ticker: str
    trigger_type: TriggerType
    summary: str
    severity: Severity
    data: dict[str, object] = field(default_factory=dict)
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


def is_market_hours(now: datetime | None = None) -> bool:
    """Return True if *now* falls within US equity market hours.

    Market hours: 09:30–16:00 ET, Monday–Friday.
    """
    now = now or datetime.now(timezone.utc)
    et = now.astimezone(US_EASTERN)
    # Weekday: 0=Mon … 4=Fri
    if et.weekday() > 4:
        return False
    market_open = et.replace(hour=9, minute=30, second=0, microsecond=0)
    market_close = et.replace(hour=16, minute=0, second=0, microsecond=0)
    return market_open <= et <= market_close


def _severity_for_pct(pct: float) -> Severity:
    """Map absolute percentage change to a severity level."""
    abs_pct = abs(pct)
    if abs_pct >= 5.0:
        return Severity.CRITICAL
    if abs_pct >= 3.0:
        return Severity.WARNING
    return Severity.INFO


class AutonomousMonitor(PollingMonitor):
    """Monitors watchlist tickers for price moves and breaking news.

    Follows the same ``should_check()`` / ``check()`` polling pattern
    used by :class:`AlertMonitor` and :class:`TaskExecutor`.

    Usage::

        monitor = AutonomousMonitor(watchlist, data_registry)
        if monitor.should_check():
            alerts = await monitor.check()
    """

    def __init__(
        self,
        watchlist: Watchlist,
        data_registry: DataRegistry,
        *,
        check_interval: float = DEFAULT_CHECK_INTERVAL,
        price_threshold_pct: float = DEFAULT_PRICE_THRESHOLD_PCT,
        cooldown_minutes: int = DEFAULT_COOLDOWN_MINUTES,
    ) -> None:
        super().__init__(check_interval)
        self._watchlist = watchlist
        self._data = data_registry
        self._price_threshold_pct = price_threshold_pct
        self._cooldown_seconds = cooldown_minutes * 60
        # ticker → monotonic timestamp of last alert
        self._cooldowns: dict[str, float] = {}
        # ticker → last known price (for change detection)
        self._baseline_prices: dict[str, float] = {}

    def should_check(self) -> bool:
        """Return True when a check is due and the market is open."""
        if not is_market_hours():
            return False
        return super().should_check()

    async def check(self) -> list[AutonomousAlert]:
        """Scan watchlist tickers and return any triggered alerts."""
        self._mark_checked()
        tickers = self._watchlist.tickers
        if not tickers:
            return []

        alerts: list[AutonomousAlert] = []
        price_alerts = await self._check_price_moves(tickers)
        alerts.extend(price_alerts)

        news_alerts = await self._check_breaking_news(tickers)
        alerts.extend(news_alerts)

        return alerts

    async def _check_price_moves(self, tickers: list[str]) -> list[AutonomousAlert]:
        """Detect significant price moves relative to the baseline."""
        try:
            provider: PriceProvider = self._data.get(PriceProvider)
        except KeyError:
            logger.debug("No PriceProvider — skipping price move check")
            return []

        alerts: list[AutonomousAlert] = []
        for ticker in tickers:
            if self._is_cooling_down(ticker):
                continue
            try:
                price = await provider.get_price(ticker)
            except Exception:
                logger.debug("Price fetch failed for %s", ticker)
                continue

            baseline = self._baseline_prices.get(ticker)
            self._baseline_prices[ticker] = price

            if baseline is None or baseline == 0:
                continue

            pct = ((price - baseline) / baseline) * 100
            if abs(pct) >= self._price_threshold_pct:
                direction = "up" if pct > 0 else "down"
                severity = _severity_for_pct(pct)
                alert = AutonomousAlert(
                    ticker=ticker,
                    trigger_type=TriggerType.PRICE_MOVE,
                    summary=(
                        f"{ticker} moved {direction} {abs(pct):.1f}%"
                        f" (${baseline:.2f} → ${price:.2f})"
                    ),
                    severity=severity,
                    data={"baseline": baseline, "current": price, "pct_change": round(pct, 2)},
                )
                alerts.append(alert)
                self._set_cooldown(ticker)
                # Reset baseline after alert
                self._baseline_prices[ticker] = price

        return alerts

    async def _check_breaking_news(self, tickers: list[str]) -> list[AutonomousAlert]:
        """Detect breaking news with strong sentiment."""
        try:
            provider: NewsProvider = self._data.get(NewsProvider)
        except KeyError:
            logger.debug("No NewsProvider — skipping news check")
            return []

        alerts: list[AutonomousAlert] = []
        for ticker in tickers:
            if self._is_cooling_down(ticker):
                continue
            try:
                articles = await provider.get_news(ticker, limit=3)
            except Exception:
                logger.debug("News fetch failed for %s", ticker)
                continue

            for article in articles:
                if article.sentiment is not None and abs(article.sentiment) >= 0.7:
                    tone = "positive" if article.sentiment > 0 else "negative"
                    severity = Severity.WARNING if abs(article.sentiment) >= 0.9 else Severity.INFO
                    alert = AutonomousAlert(
                        ticker=ticker,
                        trigger_type=TriggerType.BREAKING_NEWS,
                        summary=f"{ticker}: {article.title} (sentiment: {tone})",
                        severity=severity,
                        data={
                            "title": article.title,
                            "source": article.source,
                            "sentiment": article.sentiment,
                            "url": article.url,
                        },
                    )
                    alerts.append(alert)
                    self._set_cooldown(ticker)
                    break  # one news alert per ticker per cycle

        return alerts

    def _is_cooling_down(self, ticker: str) -> bool:
        """Return True if *ticker* was alerted recently."""
        last = self._cooldowns.get(ticker)
        if last is None:
            return False
        return (time.monotonic() - last) < self._cooldown_seconds

    def _set_cooldown(self, ticker: str) -> None:
        """Record the current time as the last alert for *ticker*."""
        self._cooldowns[ticker] = time.monotonic()
