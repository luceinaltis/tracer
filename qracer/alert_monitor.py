"""AlertMonitor — periodic price alert checking.

Checks active alerts against live prices using the DataRegistry's
PriceProvider capability. Designed to be called from the REPL heartbeat.
"""

from __future__ import annotations

import logging
import time

from qracer.alerts import AlertResult, AlertStore
from qracer.data.providers import PriceProvider
from qracer.data.registry import DataRegistry

logger = logging.getLogger(__name__)

# Minimum seconds between full alert-check sweeps.
DEFAULT_CHECK_INTERVAL = 5


class AlertMonitor:
    """Monitors active alerts and triggers them when conditions are met.

    Usage::

        monitor = AlertMonitor(alert_store, data_registry)
        triggered = await monitor.check()
        for result in triggered:
            print(result.message)
    """

    def __init__(
        self,
        store: AlertStore,
        data_registry: DataRegistry,
        check_interval: float = DEFAULT_CHECK_INTERVAL,
    ) -> None:
        self._store = store
        self._data_registry = data_registry
        self._check_interval = check_interval
        self._last_check: float | None = None

    @property
    def store(self) -> AlertStore:
        return self._store

    def should_check(self) -> bool:
        """Return True if enough time has elapsed since the last check."""
        if self._last_check is None:
            return True
        return (time.monotonic() - self._last_check) >= self._check_interval

    async def check(self) -> list[AlertResult]:
        """Evaluate all active alerts and return any that triggered.

        Triggered alerts are automatically marked inactive in the store.
        """
        self._last_check = time.monotonic()
        active = self._store.get_active()
        if not active:
            return []

        try:
            provider: PriceProvider = self._data_registry.get(PriceProvider)
        except KeyError:
            logger.warning("No PriceProvider registered — cannot check alerts")
            return []

        # Group alerts by ticker to minimise API calls.
        tickers: dict[str, list] = {}
        for alert in active:
            tickers.setdefault(alert.ticker, []).append(alert)

        results: list[AlertResult] = []
        for ticker, alerts in tickers.items():
            try:
                price = await provider.get_price(ticker)
            except Exception:
                logger.debug("Price fetch failed for %s, skipping alerts", ticker)
                continue

            for alert in alerts:
                if alert.evaluate(price):
                    self._store.mark_triggered(alert.id, price)
                    msg = f"Alert triggered: {alert.describe()} (price: {price})"
                    results.append(AlertResult(alert=alert, triggered_price=price, message=msg))
                    logger.info(msg)

        return results

    def evaluate_price(self, ticker: str, price: float) -> list[AlertResult]:
        """Evaluate alerts for *ticker* against a known *price*.

        Intended for push-based flows — for example, a WebSocket trade
        callback that already carries the latest price.  Unlike
        :meth:`check`, this method does not fetch from the data
        registry and only touches alerts for the given ticker.
        """
        results: list[AlertResult] = []
        for alert in self._store.get_by_ticker(ticker):
            if not alert.active:
                continue
            if alert.evaluate(price):
                self._store.mark_triggered(alert.id, price)
                msg = f"Alert triggered: {alert.describe()} (price: {price})"
                results.append(AlertResult(alert=alert, triggered_price=price, message=msg))
                logger.info(msg)
        return results
