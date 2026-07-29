"""Tests for AlertMonitor."""

from __future__ import annotations

import pytest

from qracer.alert_monitor import AlertMonitor
from qracer.alerts import AlertCondition, AlertStore
from qracer.data.providers import PriceProvider
from qracer.data.registry import DataRegistry


class FakePriceProvider:
    """Fake price provider for testing."""

    def __init__(self, prices: dict[str, float]) -> None:
        self._prices = prices

    async def get_price(self, ticker: str) -> float:
        if ticker not in self._prices:
            raise KeyError(f"No price for {ticker}")
        return self._prices[ticker]

    async def get_ohlcv(self, ticker, start, end):
        return []


class FailingPriceProvider:
    """Price provider that always raises."""

    async def get_price(self, ticker: str) -> float:
        raise ConnectionError("API down")

    async def get_ohlcv(self, ticker, start, end):
        return []


def _make_registry(provider: object) -> DataRegistry:
    registry = DataRegistry()
    registry.register("fake", provider, [PriceProvider])
    return registry


@pytest.fixture
def store(tmp_path):
    return AlertStore(tmp_path / "alerts.json")


class TestAlertMonitor:
    async def test_check_triggers_above(self, store) -> None:
        store.create("AAPL", AlertCondition.ABOVE, 200.0)
        provider = FakePriceProvider({"AAPL": 210.0})
        monitor = AlertMonitor(store, _make_registry(provider), check_interval=0)

        results = await monitor.check()
        assert len(results) == 1
        assert results[0].triggered_price == 210.0
        assert "AAPL" in results[0].message
        # Alert should now be inactive
        assert len(store.get_active()) == 0

    async def test_check_no_trigger(self, store) -> None:
        store.create("AAPL", AlertCondition.ABOVE, 200.0)
        provider = FakePriceProvider({"AAPL": 190.0})
        monitor = AlertMonitor(store, _make_registry(provider), check_interval=0)

        results = await monitor.check()
        assert len(results) == 0
        assert len(store.get_active()) == 1

    async def test_check_below_trigger(self, store) -> None:
        store.create("TSLA", AlertCondition.BELOW, 150.0)
        provider = FakePriceProvider({"TSLA": 140.0})
        monitor = AlertMonitor(store, _make_registry(provider), check_interval=0)

        results = await monitor.check()
        assert len(results) == 1

    async def test_check_multiple_tickers(self, store) -> None:
        store.create("AAPL", AlertCondition.ABOVE, 200.0)
        store.create("TSLA", AlertCondition.BELOW, 150.0)
        provider = FakePriceProvider({"AAPL": 210.0, "TSLA": 140.0})
        monitor = AlertMonitor(store, _make_registry(provider), check_interval=0)

        results = await monitor.check()
        assert len(results) == 2

    async def test_check_no_active_alerts(self, store) -> None:
        provider = FakePriceProvider({})
        monitor = AlertMonitor(store, _make_registry(provider), check_interval=0)

        results = await monitor.check()
        assert len(results) == 0

    async def test_check_price_fetch_failure(self, store) -> None:
        store.create("AAPL", AlertCondition.ABOVE, 200.0)
        provider = FailingPriceProvider()
        monitor = AlertMonitor(store, _make_registry(provider), check_interval=0)

        results = await monitor.check()
        assert len(results) == 0
        # Alert stays active
        assert len(store.get_active()) == 1

    async def test_check_no_provider(self, store) -> None:
        store.create("AAPL", AlertCondition.ABOVE, 200.0)
        empty_registry = DataRegistry()
        monitor = AlertMonitor(store, empty_registry, check_interval=0)

        results = await monitor.check()
        assert len(results) == 0

    def test_should_check_respects_interval(self, store) -> None:
        provider = FakePriceProvider({})
        monitor = AlertMonitor(store, _make_registry(provider), check_interval=60)
        # First call should be true (last_check is 0)
        assert monitor.should_check() is True

    async def test_should_check_false_after_recent_check(self, store) -> None:
        provider = FakePriceProvider({})
        monitor = AlertMonitor(store, _make_registry(provider), check_interval=60)
        await monitor.check()
        assert monitor.should_check() is False

    async def test_multiple_alerts_same_ticker(self, store) -> None:
        store.create("AAPL", AlertCondition.ABOVE, 200.0)
        store.create("AAPL", AlertCondition.ABOVE, 180.0)
        provider = FakePriceProvider({"AAPL": 190.0})
        monitor = AlertMonitor(store, _make_registry(provider), check_interval=0)

        results = await monitor.check()
        # Only the 180 alert should trigger
        assert len(results) == 1
        assert len(store.get_active()) == 1


class TestEvaluatePrice:
    """Synchronous push-based alert evaluation used by the streaming adapter."""

    def test_triggers_matching_alert(self, store) -> None:
        store.create("AAPL", AlertCondition.ABOVE, 200.0)
        monitor = AlertMonitor(store, DataRegistry(), check_interval=0)

        results = monitor.evaluate_price("AAPL", 210.0)

        assert len(results) == 1
        assert results[0].triggered_price == 210.0
        # Triggered alert becomes inactive.
        assert len(store.get_active()) == 0

    def test_does_not_trigger_unmatched_ticker(self, store) -> None:
        store.create("AAPL", AlertCondition.ABOVE, 200.0)
        monitor = AlertMonitor(store, DataRegistry(), check_interval=0)

        results = monitor.evaluate_price("TSLA", 500.0)

        assert results == []
        assert len(store.get_active()) == 1

    def test_threshold_not_met(self, store) -> None:
        store.create("AAPL", AlertCondition.ABOVE, 200.0)
        monitor = AlertMonitor(store, DataRegistry(), check_interval=0)

        results = monitor.evaluate_price("AAPL", 199.0)

        assert results == []
        assert len(store.get_active()) == 1

    def test_inactive_alert_is_skipped(self, store) -> None:
        alert = store.create("AAPL", AlertCondition.ABOVE, 200.0)
        store.mark_triggered(alert.id, 205.0)
        monitor = AlertMonitor(store, DataRegistry(), check_interval=0)

        # Even with a matching price, an already-triggered alert stays
        # inactive and is not re-emitted.
        results = monitor.evaluate_price("AAPL", 210.0)
        assert results == []
