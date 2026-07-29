"""Shared best-effort price fetching.

`fetch_prices` concurrently resolves the latest price for many tickers via the
capability registry, skipping any that fail. Previously this exact ``asyncio.gather``
loop was copy-pasted into the briefing composer and the web dashboard.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from qracer.data.providers import PriceProvider

if TYPE_CHECKING:
    from qracer.data.registry import DataRegistry


async def fetch_prices(
    data_registry: "DataRegistry | None", tickers: list[str]
) -> dict[str, float]:
    """Return ``{ticker: price}`` for the tickers that resolve; skip failures.

    Best-effort and concurrent: each ticker is fetched via the registry's
    capability fallback, and any error (or a None registry / empty list) simply
    omits that ticker from the result.
    """
    if data_registry is None or not tickers:
        return {}

    async def one(ticker: str) -> tuple[str, float | None]:
        try:
            price = await data_registry.async_get_with_fallback(PriceProvider, "get_price", ticker)
            return ticker, float(price)
        except Exception:  # noqa: BLE001 - best-effort per ticker
            return ticker, None

    results = await asyncio.gather(*(one(t) for t in tickers))
    return {t: p for t, p in results if p is not None}


__all__ = ["fetch_prices"]
