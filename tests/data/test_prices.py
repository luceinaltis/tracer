"""Tests for the shared best-effort price fetcher."""

from __future__ import annotations

from qracer.data.prices import fetch_prices


class TestFetchPrices:
    async def test_none_registry_returns_empty(self) -> None:
        assert await fetch_prices(None, ["AAPL"]) == {}

    async def test_no_tickers_returns_empty(self, data_registry) -> None:
        assert await fetch_prices(data_registry, []) == {}

    async def test_fetches_prices(self, data_registry) -> None:
        prices = await fetch_prices(data_registry, ["AAPL", "MSFT"])
        assert prices == {"AAPL": 150.0, "MSFT": 150.0}
