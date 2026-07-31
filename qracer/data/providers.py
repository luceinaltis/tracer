"""Data layer protocols.

Each provider protocol defines a capability that data adapters can implement.
The DataRegistry routes requests by capability with fallback support.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import date, datetime
from typing import Protocol, runtime_checkable

# Callback signatures for streaming data updates. Both callbacks may be async.
PriceCallback = Callable[[str, float], Awaitable[None]]
NewsCallback = Callable[["NewsArticle"], Awaitable[None]]


@dataclass(frozen=True)
class OHLCV:
    """A single OHLCV bar."""

    date: date
    open: float
    high: float
    low: float
    close: float
    volume: int


@dataclass(frozen=True)
class FundamentalData:
    """Fundamental data snapshot for a security."""

    ticker: str
    pe_ratio: float | None = None
    market_cap: float | None = None
    revenue: float | None = None
    earnings: float | None = None
    dividend_yield: float | None = None
    sector: str | None = None
    fetched_at: datetime | None = None


@dataclass(frozen=True)
class MacroIndicator:
    """A macroeconomic indicator data point."""

    name: str
    value: float
    date: date
    source: str
    unit: str = ""


@dataclass(frozen=True)
class NewsArticle:
    """A news article with optional sentiment score."""

    title: str
    source: str
    published_at: datetime
    url: str
    summary: str = ""
    sentiment: float | None = None  # -1.0 to 1.0


@dataclass(frozen=True)
class AlternativeRecord:
    """Alternative data record (insider trades, congressional trades, etc.)."""

    record_type: str
    ticker: str
    data: dict[str, object]
    source: str
    date: date


@dataclass(frozen=True)
class FuturesQuote:
    """A single futures contract quote (e.g. a KOSPI 200 futures month)."""

    code: str
    price: float
    underlying: str | None = None
    expiry: str | None = None  # contract month, ``YYYYMM``
    change: float | None = None
    change_pct: float | None = None
    volume: int | None = None
    open_interest: int | None = None
    basis: float | None = None  # futures price minus spot, when known


@dataclass(frozen=True)
class OptionQuote:
    """A single option contract quote, with greeks when the source supplies them."""

    code: str
    right: str  # "C" (call) or "P" (put)
    strike: float
    price: float
    underlying: str | None = None
    expiry: str | None = None  # contract month, ``YYYYMM``
    change: float | None = None
    volume: int | None = None
    open_interest: int | None = None
    implied_vol: float | None = None
    delta: float | None = None
    gamma: float | None = None
    theta: float | None = None
    vega: float | None = None


@dataclass(frozen=True)
class OptionChain:
    """Calls and puts for one underlying at one expiry."""

    underlying: str
    expiry: str | None
    calls: list[OptionQuote]
    puts: list[OptionQuote]


@runtime_checkable
class PriceProvider(Protocol):
    """Capability: Price/OHLCV data retrieval."""

    async def get_price(self, ticker: str) -> float: ...

    async def get_ohlcv(self, ticker: str, start: date, end: date) -> list[OHLCV]: ...


@runtime_checkable
class FundamentalProvider(Protocol):
    """Capability: Fundamental financial data retrieval."""

    async def get_fundamentals(self, ticker: str) -> FundamentalData: ...


@runtime_checkable
class MacroProvider(Protocol):
    """Capability: Macroeconomic indicator retrieval."""

    async def get_indicator(self, name: str) -> MacroIndicator: ...


@runtime_checkable
class NewsProvider(Protocol):
    """Capability: News and sentiment data retrieval."""

    async def get_news(self, ticker: str, limit: int = 10) -> list[NewsArticle]: ...


@runtime_checkable
class AlternativeProvider(Protocol):
    """Capability: Alternative data retrieval (insider trades, etc.)."""

    async def get_alternative(self, ticker: str, record_type: str) -> list[AlternativeRecord]: ...


@runtime_checkable
class DerivativesProvider(Protocol):
    """Capability: exchange-traded derivatives (futures & option chains).

    Distinct from :class:`PriceProvider`: a derivative is identified by a
    contract code (or an underlying + expiry for a chain), and its quote
    carries derivative-specific fields — open interest, greeks, basis —
    that do not fit an OHLCV bar.
    """

    async def get_futures_quote(self, code: str) -> FuturesQuote: ...

    async def get_option_chain(self, underlying: str, expiry: str | None = None) -> OptionChain: ...


@runtime_checkable
class StreamingProvider(Protocol):
    """Capability: real-time data streaming via WebSocket.

    Implementations maintain a persistent connection and dispatch price
    and news updates to registered callbacks as messages arrive.  The
    caller is responsible for connecting before subscribing and
    disconnecting on shutdown.
    """

    async def connect(self) -> None: ...

    async def disconnect(self) -> None: ...

    async def subscribe(self, tickers: list[str]) -> None: ...

    async def unsubscribe(self, tickers: list[str]) -> None: ...

    def on_price(self, callback: PriceCallback) -> None: ...

    def on_news(self, callback: NewsCallback) -> None: ...
