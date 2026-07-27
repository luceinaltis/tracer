"""AI-prioritized daily briefing.

`BriefingComposer` gathers the user's actual state — portfolio P&L, watchlist
prices, open theses + upcoming catalysts, and recent news — and asks an LLM to rank,
**by impact on the user's current returns**, the few things they should look at first.
It is grounded: the model is told to use only the supplied data and never invent
numbers or news.

`BriefingScheduler` fires the composer once per scheduled cron occurrence (independent
of market hours) and pushes the result through the notification registry. It plugs
into the `qracer serve` tick loop, mirroring `AgentMonitor`.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING

from croniter import croniter

from qracer.data.providers import PriceProvider
from qracer.llm.providers import CompletionRequest, Message, Role
from qracer.notifications.providers import Notification, NotificationCategory

if TYPE_CHECKING:
    from zoneinfo import ZoneInfo

    from qracer.config.models import PortfolioConfig
    from qracer.data.registry import DataRegistry
    from qracer.llm.registry import LLMRegistry
    from qracer.memory.fact_store import FactStore
    from qracer.notifications.registry import NotificationRegistry
    from qracer.watchlist import Watchlist

logger = logging.getLogger(__name__)

DEFAULT_CHECK_INTERVAL = 30.0  # seconds between scheduler due-checks
_MAX_NEWS_TICKERS = 6

_SYSTEM_PROMPT = """\
You are the user's personal investment briefing assistant. From the DATA below — their
portfolio holdings with P&L, watchlist prices, open theses with catalysts, and recent
news — produce a SHORT briefing that ranks, by impact on THIS user's current returns
(P&L), the few things they should look at first right now.

Rules:
- Use ONLY the data provided. Never invent prices, numbers, or news.
- Lead with the single most important item for their returns.
- Be specific: name the ticker, the relevant number, why it matters, and what to
  consider — one or two lines each.
- Surface at most {top_n} items. If nothing is material, say so in one line.
- Plain text, no preamble. Suitable for a phone notification."""


@dataclass
class BriefingState:
    """Gathered, grounded inputs for the briefing."""

    portfolio_lines: list[str] = field(default_factory=list)
    watchlist_lines: list[str] = field(default_factory=list)
    thesis_lines: list[str] = field(default_factory=list)
    news_lines: list[str] = field(default_factory=list)

    def has_any(self) -> bool:
        return bool(
            self.portfolio_lines or self.watchlist_lines or self.thesis_lines or self.news_lines
        )

    def to_evidence(self) -> str:
        sections: list[str] = []
        if self.portfolio_lines:
            sections.append("PORTFOLIO (holdings + P&L):\n" + "\n".join(self.portfolio_lines))
        if self.watchlist_lines:
            sections.append("WATCHLIST (current prices):\n" + "\n".join(self.watchlist_lines))
        if self.thesis_lines:
            sections.append("OPEN THESES / CATALYSTS:\n" + "\n".join(self.thesis_lines))
        if self.news_lines:
            sections.append("RECENT NEWS:\n" + "\n".join(self.news_lines))
        return "\n\n".join(sections)


class BriefingComposer:
    """Gathers state and asks an LLM to rank it by impact on the user's returns."""

    def __init__(
        self,
        data_registry: "DataRegistry",
        llm_registry: "LLMRegistry",
        portfolio_config: "PortfolioConfig",
        watchlist: "Watchlist",
        fact_store: "FactStore | None" = None,
        *,
        top_n: int = 5,
    ) -> None:
        self._data = data_registry
        self._llm = llm_registry
        self._portfolio = portfolio_config
        self._watchlist = watchlist
        self._fact_store = fact_store
        self._top_n = top_n

    async def compose(self) -> str | None:
        """Return a prioritized briefing string, or None if there is nothing to say."""
        state = await self._gather()
        if not state.has_any():
            return None
        return await self._rank(state)

    # -- gathering ----------------------------------------------------------

    async def _gather(self) -> BriefingState:
        state = BriefingState()

        holdings = list(self._portfolio.holdings)
        watch = list(self._watchlist.tickers)
        all_tickers = list(dict.fromkeys([h.ticker for h in holdings] + watch))

        prices = await self._fetch_prices(all_tickers)

        # Portfolio snapshot (fall back to avg_cost so the snapshot always builds).
        if holdings:
            from qracer.risk.calculator import RiskCalculator

            price_map = {h.ticker: prices.get(h.ticker, h.avg_cost) for h in holdings}
            try:
                snapshot = RiskCalculator(self._portfolio).build_snapshot(price_map)
                for h in snapshot.holdings:
                    sign = "+" if h.unrealized_pnl >= 0 else "-"
                    state.portfolio_lines.append(
                        f"{h.ticker}: {h.shares:g} sh @ ${h.current_price:,.2f} "
                        f"(weight {h.weight_pct:.1f}%, P&L {sign}${abs(h.unrealized_pnl):,.0f} "
                        f"/ {sign}{abs(h.unrealized_pnl_pct):.1f}%)"
                    )
                state.portfolio_lines.append(f"Total value: ${snapshot.total_value:,.0f}")
            except Exception:  # noqa: BLE001 - never let one section break the briefing
                logger.debug("portfolio snapshot failed", exc_info=True)

        # Watchlist prices (only tickers not already shown as holdings).
        held = {h.ticker for h in holdings}
        for ticker in watch:
            if ticker in held:
                continue
            price = prices.get(ticker)
            if price is not None:
                state.watchlist_lines.append(f"{ticker}: ${price:,.2f}")

        # Open theses + upcoming catalysts.
        if self._fact_store is not None:
            state.thesis_lines = self._thesis_lines()

        # Recent news for held + watched tickers (capped).
        state.news_lines = await self._news_lines(all_tickers[:_MAX_NEWS_TICKERS])

        return state

    async def _fetch_prices(self, tickers: list[str]) -> dict[str, float]:
        if not tickers:
            return {}

        async def one(ticker: str) -> tuple[str, float | None]:
            try:
                price = await self._data.async_get_with_fallback(PriceProvider, "get_price", ticker)
                return ticker, float(price)
            except Exception:  # noqa: BLE001 - best-effort per ticker
                return ticker, None

        results = await asyncio.gather(*(one(t) for t in tickers))
        return {t: p for t, p in results if p is not None}

    def _thesis_lines(self) -> list[str]:
        assert self._fact_store is not None
        theses = self._fact_store.get_upcoming_catalysts(days_ahead=14)
        if not theses:
            theses = self._fact_store.get_open_theses()[:5]
        lines: list[str] = []
        for t in theses[:5]:
            direction = "LONG" if t.target_price >= t.entry_zone_high else "SHORT"
            catalyst = f" — catalyst: {t.catalyst}" if t.catalyst else ""
            when = f" ({t.catalyst_date})" if t.catalyst_date else ""
            lines.append(
                f"{t.ticker} {direction} conviction {t.conviction}/10, "
                f"target ${t.target_price:,.2f} / stop ${t.stop_loss:,.2f}{catalyst}{when}"
            )
        return lines

    async def _news_lines(self, tickers: list[str]) -> list[str]:
        if not tickers:
            return []
        from qracer.tools import pipeline

        async def one(ticker: str) -> list[str]:
            try:
                result = await pipeline.news(ticker, self._data, limit=3)
            except Exception:  # noqa: BLE001
                return []
            if not result.success:
                return []
            articles = result.data.get("articles", [])
            return [f"{ticker}: {a.get('title', '')} ({a.get('source', '')})" for a in articles[:2]]

        grouped = await asyncio.gather(*(one(t) for t in tickers))
        return [line for lines in grouped for line in lines]

    # -- ranking ------------------------------------------------------------

    async def _rank(self, state: BriefingState) -> str:
        provider = self._llm.get(Role.STRATEGIST)
        request = CompletionRequest(
            messages=[
                Message(role="system", content=_SYSTEM_PROMPT.format(top_n=self._top_n)),
                Message(role="user", content=state.to_evidence()),
            ],
            temperature=0.2,
            max_tokens=1024,
        )
        response = await provider.complete(request)
        return response.content.strip()


class BriefingScheduler:
    """Fires the composer once per scheduled cron occurrence and pushes the result."""

    def __init__(
        self,
        composer: BriefingComposer,
        notifications: "NotificationRegistry | None",
        cron: str,
        *,
        timezone: "ZoneInfo | None" = None,
        check_interval: float = DEFAULT_CHECK_INTERVAL,
    ) -> None:
        # croniter raises on an invalid expression — validate eagerly.
        croniter(cron, self._now(timezone))
        self._composer = composer
        self._notifications = notifications
        self._cron = cron
        self._tz = timezone
        self._check_interval = check_interval
        self._last_check: float | None = None
        # Don't backfill on startup: only fire on the NEXT occurrence.
        self._last_slot: datetime = self._current_slot()

    @staticmethod
    def _now(tz: "ZoneInfo | None") -> datetime:
        return datetime.now(tz) if tz else datetime.now()

    def _current_slot(self) -> datetime:
        """Most recent scheduled datetime at or before now."""
        return croniter(self._cron, self._now(self._tz)).get_prev(datetime)

    def should_check(self) -> bool:
        if self._last_check is None:
            return True
        return (time.monotonic() - self._last_check) >= self._check_interval

    async def check(self) -> str | None:
        """If a scheduled slot has newly passed, compose and push the briefing."""
        self._last_check = time.monotonic()
        slot = self._current_slot()
        if slot == self._last_slot:
            return None
        self._last_slot = slot  # mark handled even if empty, so we fire once per slot

        text = await self._composer.compose()
        if not text:
            logger.info("Briefing slot reached but no material content")
            return None
        await self._push(text)
        return text

    async def _push(self, text: str) -> None:
        if self._notifications is None or not self._notifications.channels:
            logger.warning("Briefing ready but no notification channels configured")
            return
        await self._notifications.notify(
            Notification(
                category=NotificationCategory.DAILY_BRIEFING,
                title="Daily briefing",
                body=text,
            )
        )


__all__ = ["BriefingComposer", "BriefingScheduler", "BriefingState"]
