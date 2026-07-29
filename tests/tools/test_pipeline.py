"""Tests for pipeline tool wrappers."""

from __future__ import annotations

from datetime import date, datetime
from unittest.mock import AsyncMock

from qracer.data.providers import (
    OHLCV,
    AlternativeProvider,
    AlternativeRecord,
    FundamentalData,
    FundamentalProvider,
    MacroIndicator,
    MacroProvider,
    NewsArticle,
    NewsProvider,
    PriceProvider,
)
from qracer.data.registry import DataRegistry
from qracer.tools.pipeline import (
    cross_market,
    fundamentals,
    insider,
    macro,
    memory_search,
    news,
    price_event,
)


def _registry_with(capability: type, mock: object) -> DataRegistry:
    reg = DataRegistry()
    reg.register("mock", mock, [capability])
    return reg


# ---------------------------------------------------------------------------
# price_event
# ---------------------------------------------------------------------------


class TestPriceEvent:
    async def test_success(self) -> None:
        mock = AsyncMock(spec=PriceProvider)
        mock.get_price.return_value = 185.0
        mock.get_ohlcv.return_value = [
            OHLCV(
                date=date(2024, 1, 1),
                open=180.0,
                high=186.0,
                low=179.0,
                close=185.0,
                volume=1000,
            ),
        ]
        result = await price_event("AAPL", _registry_with(PriceProvider, mock))
        assert result.success is True
        assert result.tool == "price_event"
        assert result.data["ticker"] == "AAPL"
        assert result.data["current_price"] == 185.0
        assert result.error is None

    async def test_failure(self) -> None:
        mock = AsyncMock(spec=PriceProvider)
        mock.get_ohlcv.side_effect = RuntimeError("timeout")
        result = await price_event("AAPL", _registry_with(PriceProvider, mock))
        assert result.success is False
        assert result.error is not None


# ---------------------------------------------------------------------------
# news
# ---------------------------------------------------------------------------


class TestNews:
    async def test_success(self) -> None:
        mock = AsyncMock(spec=NewsProvider)
        mock.get_news.return_value = [
            NewsArticle(
                title="AAPL earnings",
                source="Reuters",
                published_at=datetime(2024, 1, 15),
                url="https://example.com",
                summary="Beat estimates",
                sentiment=0.8,
            ),
        ]
        result = await news("AAPL", _registry_with(NewsProvider, mock))
        assert result.success is True
        assert result.data["count"] == 1
        assert result.data["articles"][0]["title"] == "AAPL earnings"

    async def test_failure(self) -> None:
        mock = AsyncMock(spec=NewsProvider)
        mock.get_news.side_effect = RuntimeError("rate limit")
        result = await news("AAPL", _registry_with(NewsProvider, mock))
        assert result.success is False


# ---------------------------------------------------------------------------
# insider
# ---------------------------------------------------------------------------


class TestInsider:
    async def test_success(self) -> None:
        mock = AsyncMock(spec=AlternativeProvider)
        mock.get_alternative.return_value = [
            AlternativeRecord(
                record_type="insider_trades",
                ticker="AAPL",
                data={"shares": 10000, "direction": "buy"},
                source="SEC",
                date=date(2024, 1, 10),
            ),
        ]
        result = await insider("AAPL", _registry_with(AlternativeProvider, mock))
        assert result.success is True
        assert result.data["count"] == 1

    async def test_failure(self) -> None:
        mock = AsyncMock(spec=AlternativeProvider)
        mock.get_alternative.side_effect = KeyError("no adapter")
        result = await insider("AAPL", _registry_with(AlternativeProvider, mock))
        assert result.success is False


# ---------------------------------------------------------------------------
# macro
# ---------------------------------------------------------------------------


class TestMacro:
    async def test_success(self) -> None:
        mock = AsyncMock(spec=MacroProvider)
        mock.get_indicator.return_value = MacroIndicator(
            name="fed_funds_rate",
            value=5.25,
            date=date(2024, 1, 1),
            source="FRED",
            unit="%",
        )
        result = await macro("fed_funds_rate", _registry_with(MacroProvider, mock))
        assert result.success is True
        assert result.data["value"] == 5.25

    async def test_failure(self) -> None:
        mock = AsyncMock(spec=MacroProvider)
        mock.get_indicator.side_effect = RuntimeError("unavailable")
        result = await macro("cpi", _registry_with(MacroProvider, mock))
        assert result.success is False


# ---------------------------------------------------------------------------
# fundamentals
# ---------------------------------------------------------------------------


class TestFundamentals:
    async def test_success(self) -> None:
        mock = AsyncMock(spec=FundamentalProvider)
        mock.get_fundamentals.return_value = FundamentalData(
            ticker="AAPL",
            pe_ratio=28.5,
            market_cap=3_000_000_000_000,
            revenue=380_000_000_000,
            earnings=95_000_000_000,
            dividend_yield=0.005,
        )
        result = await fundamentals("AAPL", _registry_with(FundamentalProvider, mock))
        assert result.success is True
        assert result.data["pe_ratio"] == 28.5

    async def test_failure(self) -> None:
        mock = AsyncMock(spec=FundamentalProvider)
        mock.get_fundamentals.side_effect = RuntimeError("fail")
        result = await fundamentals("AAPL", _registry_with(FundamentalProvider, mock))
        assert result.success is False


# ---------------------------------------------------------------------------
# cross_market
# ---------------------------------------------------------------------------


class TestCrossMarket:
    async def test_success(self) -> None:
        mock = AsyncMock(spec=PriceProvider)
        mock.get_price.return_value = 100.0
        mock.get_ohlcv.return_value = []
        result = await cross_market(["AAPL", "MSFT"], _registry_with(PriceProvider, mock))
        assert result.success is True
        assert "AAPL" in result.data["tickers"]
        assert "MSFT" in result.data["tickers"]

    async def test_partial_failure(self) -> None:
        """Individual ticker failure should not fail the whole tool."""
        mock = AsyncMock(spec=PriceProvider)
        mock.get_price.side_effect = [100.0, RuntimeError("fail")]
        mock.get_ohlcv.return_value = []
        result = await cross_market(["AAPL", "MSFT"], _registry_with(PriceProvider, mock))
        assert result.success is True
        assert "error" in result.data["tickers"]["MSFT"]

    async def test_registry_failure(self) -> None:
        """If the registry has no providers, per-ticker errors are recorded."""
        registry = DataRegistry()  # empty — no providers
        result = await cross_market(["AAPL"], registry)
        # cross_market handles per-ticker failures gracefully — the outer
        # result succeeds but each ticker carries an error entry.
        assert result.success is True
        assert "error" in result.data["tickers"]["AAPL"]


# ---------------------------------------------------------------------------
# memory_search
# ---------------------------------------------------------------------------


class TestMemorySearch:
    async def test_placeholder(self) -> None:
        result = await memory_search("previous AAPL analysis")
        assert result.success is True
        assert result.tool == "memory_search"
        assert result.data["results"] == []

    async def test_fact_store_ticker_lookup_returns_theses(self) -> None:
        """Structured theses from FactStore short-circuit the free-text search."""
        from qracer.memory.fact_store import FactStore
        from qracer.models import TradeThesis

        store = FactStore()
        try:
            thesis = TradeThesis(
                ticker="AAPL",
                entry_zone=(175.0, 180.0),
                target_price=200.0,
                stop_loss=165.0,
                risk_reward_ratio=2.5,
                catalyst="AI revenue",
                catalyst_date="Q2 2026",
                conviction=8,
                summary="Long AAPL.",
            )
            store.save_thesis(thesis, session_id="sess_001")

            result = await memory_search(
                "how's my AAPL thesis?",
                searcher=None,
                fact_store=store,
                tickers=["AAPL"],
            )

            assert result.success is True
            assert result.data["source"] == "fact_store"
            assert len(result.data["theses"]) == 1
            assert result.data["theses"][0]["ticker"] == "AAPL"
            assert result.data["theses"][0]["conviction"] == 8
            assert result.data["results"] == []
        finally:
            store.close()

    async def test_fact_store_empty_falls_back_to_searcher(self) -> None:
        """When FactStore has no ticker hits, free-text search runs instead."""
        from unittest.mock import MagicMock

        from qracer.memory.fact_store import FactStore
        from qracer.memory.memory_searcher import SearchResult

        store = FactStore()
        searcher = MagicMock()
        searcher.search.return_value = [
            SearchResult(
                session_id="sess_42",
                summary="Prior AAPL analysis notes.",
                score=1.5,
                indexed_at=datetime(2026, 1, 1),
            )
        ]
        try:
            result = await memory_search(
                "AAPL earnings",
                searcher=searcher,
                fact_store=store,
                tickers=["AAPL"],
            )

            assert result.success is True
            assert result.data["source"] == "memory_searcher"
            assert result.data["results"][0]["session_id"] == "sess_42"
            searcher.search.assert_called_once_with("AAPL earnings", limit=5)
        finally:
            store.close()

    async def test_no_tickers_uses_searcher(self) -> None:
        """Without tickers the FactStore path is skipped entirely."""
        from unittest.mock import MagicMock

        from qracer.memory.fact_store import FactStore

        store = FactStore()
        searcher = MagicMock()
        searcher.search.return_value = []
        try:
            result = await memory_search(
                "some macro query",
                searcher=searcher,
                fact_store=store,
                tickers=[],
            )

            assert result.data["source"] == "memory_searcher"
            searcher.search.assert_called_once()
        finally:
            store.close()

    async def test_fact_store_exception_falls_back(self) -> None:
        """A broken FactStore does not bubble up — we fall back to the searcher."""
        from unittest.mock import MagicMock

        broken_store = MagicMock()
        broken_store.get_open_theses.side_effect = RuntimeError("db gone")

        searcher = MagicMock()
        searcher.search.return_value = []

        result = await memory_search(
            "AAPL",
            searcher=searcher,
            fact_store=broken_store,
            tickers=["AAPL"],
        )

        assert result.success is True
        assert result.data["source"] == "memory_searcher"
        searcher.search.assert_called_once()

    async def test_fact_store_findings_included_when_available(self) -> None:
        """If the FactStore exposes get_findings, findings are included."""
        from unittest.mock import MagicMock

        from qracer.memory.fact_models import Finding

        store = MagicMock()
        store.get_open_theses.return_value = []
        store.get_findings.return_value = [
            Finding(
                id=1,
                entity="AAPL",
                statement="P/E ratio 29",
                confidence=0.9,
                source_tool="fundamentals",
                session_id="sess_001",
            )
        ]

        result = await memory_search(
            "AAPL valuation",
            searcher=None,
            fact_store=store,
            tickers=["AAPL"],
        )

        assert result.data["source"] == "fact_store"
        assert len(result.data["findings"]) == 1
        assert result.data["findings"][0]["statement"] == "P/E ratio 29"


class TestConfigure:
    def test_configure_lookback_days(self) -> None:
        from qracer.tools import pipeline as mod

        original = mod._DEFAULT_LOOKBACK_DAYS
        try:
            mod.configure(lookback_days=60)
            assert mod._DEFAULT_LOOKBACK_DAYS == 60
        finally:
            mod._DEFAULT_LOOKBACK_DAYS = original

    def test_configure_staleness_hours(self) -> None:
        from qracer.tools import pipeline as mod

        original = mod._STALENESS_HOURS
        try:
            mod.configure(staleness_hours=48)
            assert mod._STALENESS_HOURS == 48
        finally:
            mod._STALENESS_HOURS = original

    def test_configure_none_no_change(self) -> None:
        from qracer.tools import pipeline as mod

        original_lb = mod._DEFAULT_LOOKBACK_DAYS
        original_sh = mod._STALENESS_HOURS
        mod.configure()
        assert mod._DEFAULT_LOOKBACK_DAYS == original_lb
        assert mod._STALENESS_HOURS == original_sh
