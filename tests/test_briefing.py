"""Tests for the AI-prioritized daily briefing (composer + scheduler)."""

from __future__ import annotations

import time
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, cast

import pytest

from qracer.briefing import BriefingComposer, BriefingScheduler
from qracer.config.models import AppConfig, Holding, PortfolioConfig
from qracer.notifications.providers import Notification, NotificationCategory
from qracer.watchlist import Watchlist
from tests.helpers import make_data_registry, make_llm_registry

if TYPE_CHECKING:
    from qracer.notifications.registry import NotificationRegistry

# ---------------------------------------------------------------------------
# Stubs for the scheduler
# ---------------------------------------------------------------------------


class StubComposer:
    def __init__(self, text: str | None) -> None:
        self.text = text
        self.calls = 0

    async def compose(self) -> str | None:
        self.calls += 1
        return self.text


class StubNotifications:
    def __init__(self, channels: list[str]) -> None:
        self.channels = channels
        self.sent: list[Notification] = []

    async def notify(self, notification: Notification) -> dict:
        self.sent.append(notification)
        return {c: True for c in self.channels}


def _sched(composer: object, notifications: object, cron: str, **kwargs: object):
    """Construct a BriefingScheduler with test stubs (cast to the declared types)."""
    return BriefingScheduler(
        cast(BriefingComposer, composer),
        cast("NotificationRegistry | None", notifications),
        cron,
        **kwargs,  # type: ignore[arg-type]
    )


def _watchlist(tmp_path: Path, *tickers: str) -> Watchlist:
    wl = Watchlist(tmp_path / "watchlist.json")
    for t in tickers:
        wl.add(t)
    return wl


# ---------------------------------------------------------------------------
# BriefingConfig
# ---------------------------------------------------------------------------


class TestBriefingConfig:
    def test_defaults(self) -> None:
        cfg = AppConfig()
        assert cfg.briefing.enabled is False
        assert cfg.briefing.top_n == 5
        assert cfg.briefing.schedule

    def test_nested_from_toml_shape(self) -> None:
        cfg = AppConfig.model_validate(
            {"briefing": {"enabled": True, "schedule": "0 9 * * *", "top_n": 3}}
        )
        assert cfg.briefing.enabled is True
        assert cfg.briefing.schedule == "0 9 * * *"
        assert cfg.briefing.top_n == 3


# ---------------------------------------------------------------------------
# BriefingComposer
# ---------------------------------------------------------------------------


class TestBriefingComposer:
    async def test_ranks_portfolio_and_watchlist(self, tmp_path: Path) -> None:
        llm_registry, fake = make_llm_registry(content="1. AAPL: watch earnings")
        data = make_data_registry()
        portfolio = PortfolioConfig(holdings=[Holding(ticker="AAPL", shares=10, avg_cost=100.0)])
        wl = _watchlist(tmp_path, "MSFT")

        composer = BriefingComposer(data, llm_registry, portfolio, wl, None, top_n=5)
        text = await composer.compose()

        assert text == "1. AAPL: watch earnings"
        # The LLM was handed grounded evidence with both sections.
        evidence = fake.calls[0].messages[1].content
        assert "PORTFOLIO" in evidence and "AAPL" in evidence
        assert "WATCHLIST" in evidence and "MSFT" in evidence

    async def test_watchlist_only_when_no_holdings(self, tmp_path: Path) -> None:
        llm_registry, fake = make_llm_registry(content="ok")
        data = make_data_registry()
        portfolio = PortfolioConfig(holdings=[])
        wl = _watchlist(tmp_path, "AAPL")

        composer = BriefingComposer(data, llm_registry, portfolio, wl, None)
        text = await composer.compose()

        assert text == "ok"
        evidence = fake.calls[0].messages[1].content
        assert "WATCHLIST" in evidence
        assert "PORTFOLIO" not in evidence

    async def test_no_strategist_provider_returns_none(self, tmp_path: Path) -> None:
        # Holdings exist (has content), but no LLM is registered → graceful None,
        # not an uncaught KeyError from LLMRegistry.get(STRATEGIST).
        from qracer.llm.registry import LLMRegistry

        data = make_data_registry()
        portfolio = PortfolioConfig(holdings=[Holding(ticker="AAPL", shares=10, avg_cost=100.0)])
        composer = BriefingComposer(data, LLMRegistry(), portfolio, _watchlist(tmp_path), None)
        assert await composer.compose() is None

    async def test_includes_open_theses_from_fact_store(self, tmp_path: Path) -> None:
        from qracer.memory.fact_store import FactStore
        from qracer.models.base import TradeThesis

        fact_store = FactStore(tmp_path / "facts.duckdb")
        try:
            fact_store.save_thesis(
                TradeThesis(
                    ticker="AAPL",
                    entry_zone=(100.0, 110.0),
                    target_price=150.0,
                    stop_loss=90.0,
                    risk_reward_ratio=2.0,
                    catalyst="Earnings beat",
                    catalyst_date="2026-05-01",
                    conviction=8,
                    summary="Long AAPL into earnings.",
                ),
                session_id="sess-1",
            )
            llm_registry, fake = make_llm_registry(content="ranked")
            portfolio = PortfolioConfig(
                holdings=[Holding(ticker="AAPL", shares=10, avg_cost=100.0)]
            )
            composer = BriefingComposer(
                make_data_registry(), llm_registry, portfolio, _watchlist(tmp_path), fact_store
            )
            text = await composer.compose()

            assert text == "ranked"
            evidence = fake.calls[0].messages[1].content
            assert "OPEN THESES" in evidence
            assert "AAPL LONG conviction 8/10" in evidence
            assert "Earnings beat" in evidence and "2026-05-01" in evidence
        finally:
            fact_store.close()

    async def test_empty_state_returns_none(self, tmp_path: Path) -> None:
        from qracer.data.registry import DataRegistry

        llm_registry, fake = make_llm_registry(content="should not be used")
        composer = BriefingComposer(
            DataRegistry(), llm_registry, PortfolioConfig(holdings=[]), _watchlist(tmp_path), None
        )
        assert await composer.compose() is None
        assert fake.calls == []  # LLM never called when there's nothing to rank


# ---------------------------------------------------------------------------
# BriefingScheduler
# ---------------------------------------------------------------------------


class TestBriefingScheduler:
    def test_invalid_cron_raises(self) -> None:
        with pytest.raises(Exception):
            _sched(StubComposer("x"), StubNotifications([]), "not a cron")

    def test_should_check_throttle(self) -> None:
        sched = _sched(StubComposer("x"), StubNotifications([]), "* * * * *", check_interval=10_000)
        assert sched.should_check() is True
        sched._last_check = time.monotonic()
        assert sched.should_check() is False

    async def test_no_fire_on_startup(self) -> None:
        comp = StubComposer("x")
        sched = _sched(comp, StubNotifications(["telegram"]), "0 8 * * *")
        # Fresh scheduler: _last_slot is already the current slot, so nothing new.
        assert await sched.check() is None
        assert comp.calls == 0

    async def test_fires_once_per_slot_and_pushes(self) -> None:
        comp = StubComposer("brief text")
        notif = StubNotifications(["telegram"])
        sched = _sched(comp, notif, "* * * * *")
        sched._last_slot = datetime(2000, 1, 1)  # force a newly-passed slot

        text = await sched.check()
        assert text == "brief text"
        assert len(notif.sent) == 1
        assert notif.sent[0].category == NotificationCategory.DAILY_BRIEFING

        # Same slot on the next check — no re-fire, no re-compose.
        assert await sched.check() is None
        assert comp.calls == 1

    async def test_no_channels_skips_push(self) -> None:
        comp = StubComposer("x")
        notif = StubNotifications([])
        sched = _sched(comp, notif, "* * * * *")
        sched._last_slot = datetime(2000, 1, 1)

        assert await sched.check() == "x"  # composed and returned
        assert notif.sent == []  # but not pushed

    async def test_empty_briefing_not_pushed(self) -> None:
        comp = StubComposer(None)
        notif = StubNotifications(["telegram"])
        sched = _sched(comp, notif, "* * * * *")
        sched._last_slot = datetime(2000, 1, 1)

        assert await sched.check() is None
        assert notif.sent == []
