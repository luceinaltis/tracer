"""FinnhubWebSocketAdapter — real-time price and news streaming via Finnhub.

Opens a persistent WebSocket connection to ``wss://ws.finnhub.io`` and
dispatches incoming ``trade`` and ``news`` messages to registered
callbacks.  Intended to drive :class:`~qracer.alert_monitor.AlertMonitor`
without REST polling during a live session.

Implements the :class:`~qracer.data.providers.StreamingProvider` capability.

Example::

    adapter = FinnhubWebSocketAdapter(api_key="...")
    adapter.on_price(my_price_callback)
    await adapter.connect()
    await adapter.subscribe(["AAPL", "TSLA"])
    # ... receive callbacks as trades arrive ...
    await adapter.disconnect()
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from datetime import datetime
from typing import Any

from qracer.data.providers import NewsArticle, NewsCallback, PriceCallback

try:
    import websockets  # pyright: ignore[reportMissingImports]

    _HAS_WEBSOCKETS = True
except ImportError:
    _HAS_WEBSOCKETS = False

logger = logging.getLogger(__name__)

_WS_URL = "wss://ws.finnhub.io?token={api_key}"


class FinnhubWebSocketAdapter:
    """Real-time price and news streaming via Finnhub WebSocket.

    The adapter keeps an internal set of subscribed tickers so that it
    can replay subscriptions after a reconnect.  Callbacks registered
    via :meth:`on_price` and :meth:`on_news` are invoked in order for
    every matching message.
    """

    def __init__(self, api_key: str | None = None) -> None:
        if not _HAS_WEBSOCKETS:
            raise ImportError(
                "websockets is not installed. Install it with: uv add 'qracer[streaming]'"
            )
        if not api_key:
            raise ValueError("FINNHUB_API_KEY is required. Get one at https://finnhub.io/register")
        self._api_key = api_key
        self._ws: Any = None  # websockets client protocol — typed loosely to avoid stub issues.
        self._receive_task: asyncio.Task[None] | None = None
        self._subscribed: set[str] = set()
        self._price_callbacks: list[PriceCallback] = []
        self._news_callbacks: list[NewsCallback] = []
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def connect(self) -> None:
        """Open the WebSocket connection and start the receive loop.

        Raises:
            ConnectionError: if the initial handshake fails.
        """
        async with self._lock:
            if self._ws is not None:
                return
            try:
                self._ws = await websockets.connect(_WS_URL.format(api_key=self._api_key))
            except Exception as exc:
                self._ws = None
                raise ConnectionError(f"Finnhub WebSocket handshake failed: {exc}") from exc
            logger.info("Finnhub WebSocket connected")
            self._receive_task = asyncio.create_task(self._receive_loop())

            # Re-subscribe any tickers that were registered before connect().
            if self._subscribed:
                tickers = list(self._subscribed)
                self._subscribed.clear()
                await self._send_subscriptions(tickers)

    async def disconnect(self) -> None:
        """Close the WebSocket connection and stop the receive loop."""
        async with self._lock:
            if self._receive_task is not None:
                self._receive_task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await self._receive_task
                self._receive_task = None
            if self._ws is not None:
                with contextlib.suppress(Exception):
                    await self._ws.close()
                self._ws = None
            logger.info("Finnhub WebSocket disconnected")

    # ------------------------------------------------------------------
    # Subscriptions
    # ------------------------------------------------------------------

    async def subscribe(self, tickers: list[str]) -> None:
        """Subscribe to real-time updates for one or more tickers.

        Tickers registered before :meth:`connect` is called are remembered
        and sent automatically once the connection is established.
        """
        if not tickers:
            return
        if self._ws is None:
            # Defer until connect() establishes the session.
            self._subscribed.update(t.upper() for t in tickers)
            return
        new = [t.upper() for t in tickers if t.upper() not in self._subscribed]
        if new:
            await self._send_subscriptions(new)

    async def unsubscribe(self, tickers: list[str]) -> None:
        """Unsubscribe from real-time updates for one or more tickers."""
        if not tickers or self._ws is None:
            return
        for ticker in tickers:
            key = ticker.upper()
            if key not in self._subscribed:
                continue
            with contextlib.suppress(Exception):
                await self._ws.send(json.dumps({"type": "unsubscribe", "symbol": key}))
            self._subscribed.discard(key)

    async def _send_subscriptions(self, tickers: list[str]) -> None:
        """Send ``subscribe`` messages for the given tickers.

        Assumes ``self._ws`` is not ``None``.
        """
        for ticker in tickers:
            key = ticker.upper()
            try:
                await self._ws.send(json.dumps({"type": "subscribe", "symbol": key}))
                self._subscribed.add(key)
            except Exception as exc:
                logger.warning("Finnhub WebSocket subscribe failed for %s: %s", key, exc)

    # ------------------------------------------------------------------
    # Callback registration
    # ------------------------------------------------------------------

    def on_price(self, callback: PriceCallback) -> None:
        """Register a callback to be invoked on every trade update.

        The callback receives ``(ticker, price)`` and may be async.
        """
        self._price_callbacks.append(callback)

    def on_news(self, callback: NewsCallback) -> None:
        """Register a callback to be invoked on every news update.

        The callback receives a :class:`~qracer.data.providers.NewsArticle`
        and may be async.
        """
        self._news_callbacks.append(callback)

    # ------------------------------------------------------------------
    # Receive loop
    # ------------------------------------------------------------------

    async def _receive_loop(self) -> None:
        """Read messages from the socket until the connection closes.

        Errors from individual callbacks are logged but do not stop the
        loop.  The loop exits when the underlying connection is closed.
        """
        ws = self._ws
        if ws is None:
            return
        try:
            async for message in ws:
                try:
                    await self._dispatch_message(message)
                except Exception:
                    logger.exception("Finnhub WebSocket message dispatch failed")
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning("Finnhub WebSocket receive loop ended unexpectedly", exc_info=True)
        finally:
            # Mark the connection as dead so a later connect() can reopen.
            self._ws = None

    async def _dispatch_message(self, raw: str | bytes) -> None:
        """Parse a raw message and dispatch it to registered callbacks."""
        if isinstance(raw, (bytes, bytearray)):
            text = raw.decode("utf-8", errors="replace")
        else:
            text = raw
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            logger.debug("Finnhub WebSocket sent non-JSON message: %r", text[:120])
            return
        if not isinstance(payload, dict):
            return

        msg_type = payload.get("type")
        if msg_type == "trade":
            for trade in payload.get("data", []) or []:
                if not isinstance(trade, dict):
                    continue
                ticker = trade.get("s")
                price = trade.get("p")
                if not isinstance(ticker, str) or not isinstance(price, (int, float)):
                    continue
                await self._emit_price(ticker, float(price))
        elif msg_type == "news":
            for item in payload.get("data", []) or []:
                if not isinstance(item, dict):
                    continue
                article = _parse_news(item)
                if article is not None:
                    await self._emit_news(article)
        elif msg_type == "ping":
            # Finnhub pings periodically — nothing to do, ``websockets``
            # handles pong frames automatically.
            return
        elif msg_type == "error":
            logger.warning("Finnhub WebSocket error: %s", payload.get("msg"))

    async def _emit_price(self, ticker: str, price: float) -> None:
        for callback in list(self._price_callbacks):
            try:
                await callback(ticker, price)
            except Exception:
                logger.exception("Finnhub WebSocket price callback failed")

    async def _emit_news(self, article: NewsArticle) -> None:
        for callback in list(self._news_callbacks):
            try:
                await callback(article)
            except Exception:
                logger.exception("Finnhub WebSocket news callback failed")


def _parse_news(item: dict[str, Any]) -> NewsArticle | None:
    """Convert a raw Finnhub news message into a :class:`NewsArticle`."""
    headline = item.get("headline")
    if not isinstance(headline, str) or not headline:
        return None
    ts = item.get("datetime", 0)
    if isinstance(ts, (int, float)) and ts > 0:
        published_at = datetime.fromtimestamp(float(ts))
    else:
        published_at = datetime.now()
    return NewsArticle(
        title=headline,
        source=str(item.get("source", "finnhub")),
        published_at=published_at,
        url=str(item.get("url", "")),
        summary=str(item.get("summary", "")),
        sentiment=None,
    )
