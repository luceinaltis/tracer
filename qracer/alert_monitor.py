"""AlertMonitor — periodic price alert checking.

Checks active alerts against live prices using the DataRegistry's
PriceProvider capability. Designed to be called from the REPL heartbeat.
"""

from __future__ import annotations

import logging

from qracer.alerts import AlertResult, AlertStore
from qracer.data.providers import PriceProvider
from qracer.data.registry import DataRegistry
from qracer.monitors.base import PollingMonitor

logger = logging.getLogger(__name__)

# Minimum seconds between full alert-check sweeps.
DEFAULT_CHECK_INTERVAL = 5


class AlertMonitor(PollingMonitor):
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
        super().__init__(check_interval)
        self._store = store
        self._data_registry = data_registry

    @property
    def store(self) -> AlertStore:
        return self._store

    async def check(self) -> list[AlertResult]:
        """Evaluate all active alerts and return any that triggered.

        Triggered alerts are automatically marked inactive in the store.
        """
        self._mark_checked()
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
