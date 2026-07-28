"""Tests for QuickPath template formatting."""

from __future__ import annotations

import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from qracer.alerts import AlertCondition, AlertStore
from qracer.conversation.intent import Intent, IntentType
from qracer.conversation.quickpath import format_quickpath, generate_briefing
from qracer.data.providers import PriceProvider
from qracer.data.registry import DataRegistry
from qracer.models import ToolResult
from qracer.tasks import TaskActionType, TaskStore
from qracer.watchlist import Watchlist


def _price_result(ticker: str = "AAPL", price: float = 178.52) -> ToolResult:
    return ToolResult(
        tool="price_event",
        success=True,
        data={
            "ticker": ticker,
            "current_price": price,
            "bars": 1,
            "ohlcv": [
                {
                    "date": "2026-04-06",
                    "open": 176.20,
                    "high": 179.10,
                    "low": 175.50,
                    "close": 178.52,
                    "volume": 52_000_000,
                }
            ],
        },
        source="PriceProvider",
    )


def _news_result(ticker: str = "AAPL") -> ToolResult:
    return ToolResult(
        tool="news",
        success=True,
        data={
            "ticker": ticker,
            "count": 2,
            "articles": [
                {
                    "title": "AAPL beats earnings estimates",
                    "source": "Reuters",
                    "sentiment": 0.8,
                },
                {
                    "title": "iPhone sales slow in China",
                    "source": "Bloomberg",
                    "sentiment": -0.5,
                },
            ],
        },
        source="NewsProvider",
    )


class TestPriceCheck:
    def test_basic_price(self) -> None:
        intent = Intent(IntentType.PRICE_CHECK, tickers=["AAPL"], raw_query="What's AAPL at?")
        text = format_quickpath(intent, [_price_result()])
        assert "AAPL" in text
        assert "$178.52" in text

    def test_includes_change_and_volume(self) -> None:
        intent = Intent(IntentType.PRICE_CHECK, tickers=["AAPL"], raw_query="AAPL price")
        text = format_quickpath(intent, [_price_result()])
        assert "%" in text
        assert "Vol:" in text
        assert "Range:" in text

    def test_price_unavailable(self) -> None:
        intent = Intent(IntentType.PRICE_CHECK, tickers=["AAPL"], raw_query="AAPL price")
        failed = ToolResult(
            tool="price_event", success=False, data={}, source="test", error="no provider"
        )
        text = format_quickpath(intent, [failed])
        assert "unavailable" in text


class TestQuickNews:
    def test_basic_news(self) -> None:
        intent = Intent(IntentType.QUICK_NEWS, tickers=["AAPL"], raw_query="News on AAPL")
        text = format_quickpath(intent, [_news_result()])
        assert "AAPL" in text
        assert "beats earnings" in text
        assert "Reuters" in text

    def test_sentiment_indicators(self) -> None:
        intent = Intent(IntentType.QUICK_NEWS, tickers=["AAPL"], raw_query="News AAPL")
        text = format_quickpath(intent, [_news_result()])
        assert "[+]" in text  # positive sentiment
        assert "[-]" in text  # negative sentiment

    def test_no_news(self) -> None:
        intent = Intent(IntentType.QUICK_NEWS, tickers=["AAPL"], raw_query="News AAPL")
        failed = ToolResult(tool="news", success=False, data={}, source="test", error="no provider")
        text = format_quickpath(intent, [failed])
        assert "no news" in text


# ---------------------------------------------------------------------------
# i18n tests
# ---------------------------------------------------------------------------


class TestQuickPathI18n:
    """Verify quickpath templates render in non-English languages."""

    def test_price_check_korean(self) -> None:
        intent = Intent(IntentType.PRICE_CHECK, tickers=["AAPL"], raw_query="AAPL 가격")
        text = format_quickpath(intent, [_price_result()], language="ko")
        assert "AAPL" in text
        assert "$178.52" in text

    def test_price_unavailable_korean(self) -> None:
        intent = Intent(IntentType.PRICE_CHECK, tickers=["AAPL"], raw_query="AAPL 가격")
        failed = ToolResult(
            tool="price_event", success=False, data={}, source="test", error="no provider"
        )
        text = format_quickpath(intent, [failed], language="ko")
        assert "가격 정보 없음" in text

    def test_price_unavailable_japanese(self) -> None:
        intent = Intent(IntentType.PRICE_CHECK, tickers=["TSLA"], raw_query="TSLA price")
        failed = ToolResult(
            tool="price_event", success=False, data={}, source="test", error="no provider"
        )
        text = format_quickpath(intent, [failed], language="ja")
        assert "価格情報なし" in text

    def test_news_header_korean(self) -> None:
        intent = Intent(IntentType.QUICK_NEWS, tickers=["AAPL"], raw_query="AAPL 뉴스")
        text = format_quickpath(intent, [_news_result()], language="ko")
        assert "뉴스" in text
        assert "2건" in text

    def test_no_news_japanese(self) -> None:
        intent = Intent(IntentType.QUICK_NEWS, tickers=["AAPL"], raw_query="AAPL news")
        failed = ToolResult(tool="news", success=False, data={}, source="test", error="no provider")
        text = format_quickpath(intent, [failed], language="ja")
        assert "ニュースなし" in text

    def test_no_data_korean(self) -> None:
        intent = Intent(IntentType.ALPHA_HUNT, tickers=["AAPL"], raw_query="test")
        text = format_quickpath(intent, [], language="ko")
        assert "데이터 없음" in text

    def test_english_default_preserved(self) -> None:
        """Default language=en should produce identical output to no language arg."""
        intent = Intent(IntentType.PRICE_CHECK, tickers=["AAPL"], raw_query="AAPL price")
        failed = ToolResult(
            tool="price_event", success=False, data={}, source="test", error="no provider"
        )
        text_default = format_quickpath(intent, [failed])
        text_en = format_quickpath(intent, [failed], language="en")
        assert text_default == text_en
        assert "unavailable" in text_en


class TestKeywordDetection:
    def test_price_check_keywords(self) -> None:
        from unittest.mock import AsyncMock

        from qracer.conversation.intent import IntentParser, IntentType
        from qracer.llm.providers import Role
        from qracer.llm.registry import LLMRegistry

        # Use failing LLM to trigger keyword fallback
        mock = AsyncMock()
        mock.complete.side_effect = RuntimeError("fail")
        registry = LLMRegistry()
        registry.register("mock", mock, [Role.RESEARCHER])
        parser = IntentParser(registry)

        import asyncio

        intent = asyncio.run(parser.parse("What's AAPL at?"))
        assert intent.intent_type == IntentType.PRICE_CHECK

    def test_quick_news_keywords(self) -> None:
        from unittest.mock import AsyncMock

        from qracer.conversation.intent import IntentParser, IntentType
        from qracer.llm.providers import Role
        from qracer.llm.registry import LLMRegistry

        mock = AsyncMock()
        mock.complete.side_effect = RuntimeError("fail")
        registry = LLMRegistry()
        registry.register("mock", mock, [Role.RESEARCHER])
        parser = IntentParser(registry)

        import asyncio

        intent = asyncio.run(parser.parse("Any news on TSLA?"))
        assert intent.intent_type == IntentType.QUICK_NEWS


# ---------------------------------------------------------------------------
# Briefing helpers
# ---------------------------------------------------------------------------


class _FakePriceProvider:
    """Async price provider returning canned values."""

    def __init__(self, prices: dict[str, float]) -> None:
        self._prices = prices

    async def get_price(self, ticker: str) -> float:
        if ticker not in self._prices:
            raise KeyError(f"No price for {ticker}")
        return self._prices[ticker]

    async def get_ohlcv(self, ticker, start, end):  # pragma: no cover - unused
        return []


def _make_price_registry(prices: dict[str, float]) -> DataRegistry:
    registry = DataRegistry()
    registry.register("fake", _FakePriceProvider(prices), [PriceProvider])
    return registry


def _make_previous_session(sessions_dir: Path, *, name: str = "prev.jsonl") -> Path:
    """Create a previous session file with an mtime well before "now"."""
    sessions_dir.mkdir(parents=True, exist_ok=True)
    prev = sessions_dir / name
    prev.write_text("{}\n", encoding="utf-8")
    # Backdate so any "since last session" datetime comparisons are unambiguous.
    past = time.time() - 3600
    os.utime(prev, (past, past))
    return prev


class TestGenerateBriefing:
    async def test_returns_none_without_previous_session(self, tmp_path: Path) -> None:
        sessions_dir = tmp_path / "sessions"
        sessions_dir.mkdir()
        watchlist = Watchlist(tmp_path / "watchlist.json")
        registry = _make_price_registry({})
        alert_store = AlertStore(tmp_path / "alerts.json")
        task_store = TaskStore(tmp_path / "tasks.json")

        result = await generate_briefing(watchlist, registry, alert_store, task_store, sessions_dir)
        assert result is None

    async def test_excludes_current_session_from_history(self, tmp_path: Path) -> None:
        sessions_dir = tmp_path / "sessions"
        sessions_dir.mkdir()
        current = sessions_dir / "current.jsonl"
        current.write_text("", encoding="utf-8")
        watchlist = Watchlist(tmp_path / "watchlist.json")
        registry = _make_price_registry({})
        alert_store = AlertStore(tmp_path / "alerts.json")
        task_store = TaskStore(tmp_path / "tasks.json")

        # Only the current session exists -> nothing to compare against.
        result = await generate_briefing(
            watchlist,
            registry,
            alert_store,
            task_store,
            sessions_dir,
            current_session=current,
        )
        assert result is None

    async def test_returns_none_when_nothing_to_report(self, tmp_path: Path) -> None:
        sessions_dir = tmp_path / "sessions"
        _make_previous_session(sessions_dir)
        watchlist = Watchlist(tmp_path / "watchlist.json")  # empty
        registry = _make_price_registry({})
        alert_store = AlertStore(tmp_path / "alerts.json")  # empty
        task_store = TaskStore(tmp_path / "tasks.json")  # empty

        result = await generate_briefing(watchlist, registry, alert_store, task_store, sessions_dir)
        assert result is None

    async def test_includes_watchlist_prices(self, tmp_path: Path) -> None:
        sessions_dir = tmp_path / "sessions"
        _make_previous_session(sessions_dir)
        watchlist = Watchlist(tmp_path / "watchlist.json")
        watchlist.add("AAPL")
        watchlist.add("MSFT")
        registry = _make_price_registry({"AAPL": 178.52, "MSFT": 412.10})
        alert_store = AlertStore(tmp_path / "alerts.json")
        task_store = TaskStore(tmp_path / "tasks.json")

        result = await generate_briefing(watchlist, registry, alert_store, task_store, sessions_dir)
        assert result is not None
        assert "Session Briefing" in result
        assert "Watchlist:" in result
        assert "AAPL" in result and "$178.52" in result
        assert "MSFT" in result and "$412.10" in result

    async def test_skips_unavailable_prices(self, tmp_path: Path) -> None:
        sessions_dir = tmp_path / "sessions"
        _make_previous_session(sessions_dir)
        watchlist = Watchlist(tmp_path / "watchlist.json")
        watchlist.add("AAPL")
        watchlist.add("UNKNOWN")
        registry = _make_price_registry({"AAPL": 100.0})  # UNKNOWN raises
        alert_store = AlertStore(tmp_path / "alerts.json")
        task_store = TaskStore(tmp_path / "tasks.json")

        result = await generate_briefing(watchlist, registry, alert_store, task_store, sessions_dir)
        assert result is not None
        assert "AAPL" in result
        assert "UNKNOWN" not in result

    async def test_includes_recent_triggered_alerts(self, tmp_path: Path) -> None:
        sessions_dir = tmp_path / "sessions"
        _make_previous_session(sessions_dir)
        watchlist = Watchlist(tmp_path / "watchlist.json")
        registry = _make_price_registry({})
        alert_store = AlertStore(tmp_path / "alerts.json")
        task_store = TaskStore(tmp_path / "tasks.json")

        # Two alerts: one triggered after the previous session, one before.
        recent = alert_store.create("AAPL", AlertCondition.ABOVE, 200.0)
        stale = alert_store.create("MSFT", AlertCondition.BELOW, 300.0)
        alert_store.mark_triggered(recent.id, 210.0)
        # Backdate the stale alert's triggered_at so it falls before the
        # previous session timestamp.
        for alert in alert_store.alerts:
            if alert.id == stale.id:
                alert.active = False
                alert.triggered_at = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
        alert_store._save()  # type: ignore[attr-defined]

        result = await generate_briefing(watchlist, registry, alert_store, task_store, sessions_dir)
        assert result is not None
        assert "Triggered Alerts (1)" in result
        assert "AAPL goes above 200.0" in result
        assert "MSFT" not in result

    async def test_includes_pending_tasks(self, tmp_path: Path) -> None:
        sessions_dir = tmp_path / "sessions"
        _make_previous_session(sessions_dir)
        watchlist = Watchlist(tmp_path / "watchlist.json")
        registry = _make_price_registry({})
        alert_store = AlertStore(tmp_path / "alerts.json")
        task_store = TaskStore(tmp_path / "tasks.json")
        task_store.create(TaskActionType.NEWS_SCAN, {"ticker": "AAPL"}, "every 1h")

        result = await generate_briefing(watchlist, registry, alert_store, task_store, sessions_dir)
        assert result is not None
        assert "Pending Tasks (1)" in result
        assert "news scan" in result
        assert "AAPL" in result

    async def test_truncates_pending_tasks_over_five(self, tmp_path: Path) -> None:
        sessions_dir = tmp_path / "sessions"
        _make_previous_session(sessions_dir)
        watchlist = Watchlist(tmp_path / "watchlist.json")
        registry = _make_price_registry({})
        alert_store = AlertStore(tmp_path / "alerts.json")
        task_store = TaskStore(tmp_path / "tasks.json")
        for i in range(7):
            task_store.create(TaskActionType.NEWS_SCAN, {"ticker": f"T{i}"}, "every 1h")

        result = await generate_briefing(watchlist, registry, alert_store, task_store, sessions_dir)
        assert result is not None
        assert "Pending Tasks (7)" in result
        assert "and 2 more" in result
