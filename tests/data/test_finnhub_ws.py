"""Tests for FinnhubWebSocketAdapter."""

from __future__ import annotations

import asyncio
import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from qracer.data.providers import NewsArticle


class _FakeWebSocket:
    """Minimal async-iterable WebSocket double for unit tests.

    ``feed()`` queues outgoing messages that the adapter's receive loop
    will see; ``close()`` terminates the iteration.  ``send()`` records
    every outgoing subscribe/unsubscribe frame.
    """

    def __init__(self) -> None:
        self.sent: list[str] = []
        self._queue: asyncio.Queue[str | None] = asyncio.Queue()
        self.closed = False

    async def send(self, message: str) -> None:
        self.sent.append(message)

    async def close(self) -> None:
        self.closed = True
        await self._queue.put(None)

    def __aiter__(self) -> _FakeWebSocket:
        return self

    async def __anext__(self) -> str:
        item = await self._queue.get()
        if item is None:
            raise StopAsyncIteration
        return item

    async def feed(self, message: str) -> None:
        await self._queue.put(message)


class TestFinnhubWebSocketAdapterInit:
    """Constructor validation."""

    @patch("qracer.data.finnhub_ws._HAS_WEBSOCKETS", False)
    def test_missing_websockets_raises(self) -> None:
        from qracer.data.finnhub_ws import FinnhubWebSocketAdapter

        with pytest.raises(ImportError, match="websockets is not installed"):
            FinnhubWebSocketAdapter(api_key="test")

    @patch("qracer.data.finnhub_ws._HAS_WEBSOCKETS", True)
    def test_missing_api_key_raises(self) -> None:
        from qracer.data.finnhub_ws import FinnhubWebSocketAdapter

        with pytest.raises(ValueError, match="FINNHUB_API_KEY is required"):
            FinnhubWebSocketAdapter(api_key=None)

    @patch("qracer.data.finnhub_ws._HAS_WEBSOCKETS", True)
    def test_empty_api_key_raises(self) -> None:
        from qracer.data.finnhub_ws import FinnhubWebSocketAdapter

        with pytest.raises(ValueError, match="FINNHUB_API_KEY is required"):
            FinnhubWebSocketAdapter(api_key="")

    @patch("qracer.data.finnhub_ws._HAS_WEBSOCKETS", True)
    def test_valid_init(self) -> None:
        from qracer.data.finnhub_ws import FinnhubWebSocketAdapter

        adapter = FinnhubWebSocketAdapter(api_key="key")
        assert adapter._api_key == "key"
        assert adapter._ws is None
        assert adapter._subscribed == set()


class TestConnectAndSubscribe:
    """Connect / subscribe / receive loop integration."""

    @patch("qracer.data.finnhub_ws._HAS_WEBSOCKETS", True)
    async def test_connect_starts_receive_loop(self) -> None:
        from qracer.data.finnhub_ws import FinnhubWebSocketAdapter

        fake_ws = _FakeWebSocket()
        with patch("qracer.data.finnhub_ws.websockets", create=True) as mock_ws:
            mock_ws.connect = AsyncMock(return_value=fake_ws)
            adapter = FinnhubWebSocketAdapter(api_key="k")
            await adapter.connect()
            try:
                assert adapter._ws is fake_ws
                assert adapter._receive_task is not None
                mock_ws.connect.assert_awaited_once()
            finally:
                await adapter.disconnect()
        assert fake_ws.closed

    @patch("qracer.data.finnhub_ws._HAS_WEBSOCKETS", True)
    async def test_connect_handshake_failure_raises(self) -> None:
        from qracer.data.finnhub_ws import FinnhubWebSocketAdapter

        with patch("qracer.data.finnhub_ws.websockets", create=True) as mock_ws:
            mock_ws.connect = AsyncMock(side_effect=OSError("boom"))
            adapter = FinnhubWebSocketAdapter(api_key="k")
            with pytest.raises(ConnectionError, match="handshake failed"):
                await adapter.connect()
            assert adapter._ws is None

    @patch("qracer.data.finnhub_ws._HAS_WEBSOCKETS", True)
    async def test_subscribe_before_connect_is_deferred(self) -> None:
        from qracer.data.finnhub_ws import FinnhubWebSocketAdapter

        fake_ws = _FakeWebSocket()
        with patch("qracer.data.finnhub_ws.websockets", create=True) as mock_ws:
            mock_ws.connect = AsyncMock(return_value=fake_ws)
            adapter = FinnhubWebSocketAdapter(api_key="k")
            await adapter.subscribe(["aapl", "tsla"])
            assert adapter._subscribed == {"AAPL", "TSLA"}
            assert fake_ws.sent == []

            await adapter.connect()
            try:
                # Subscriptions queued before connect should now be sent.
                sent = [json.loads(m) for m in fake_ws.sent]
                symbols = {m["symbol"] for m in sent if m["type"] == "subscribe"}
                assert symbols == {"AAPL", "TSLA"}
                assert adapter._subscribed == {"AAPL", "TSLA"}
            finally:
                await adapter.disconnect()

    @patch("qracer.data.finnhub_ws._HAS_WEBSOCKETS", True)
    async def test_subscribe_after_connect_dedupes(self) -> None:
        from qracer.data.finnhub_ws import FinnhubWebSocketAdapter

        fake_ws = _FakeWebSocket()
        with patch("qracer.data.finnhub_ws.websockets", create=True) as mock_ws:
            mock_ws.connect = AsyncMock(return_value=fake_ws)
            adapter = FinnhubWebSocketAdapter(api_key="k")
            await adapter.connect()
            try:
                await adapter.subscribe(["AAPL"])
                await adapter.subscribe(["aapl", "MSFT"])
                subs = [json.loads(m)["symbol"] for m in fake_ws.sent if '"subscribe"' in m]
                assert subs == ["AAPL", "MSFT"]
            finally:
                await adapter.disconnect()

    @patch("qracer.data.finnhub_ws._HAS_WEBSOCKETS", True)
    async def test_unsubscribe_sends_frame(self) -> None:
        from qracer.data.finnhub_ws import FinnhubWebSocketAdapter

        fake_ws = _FakeWebSocket()
        with patch("qracer.data.finnhub_ws.websockets", create=True) as mock_ws:
            mock_ws.connect = AsyncMock(return_value=fake_ws)
            adapter = FinnhubWebSocketAdapter(api_key="k")
            await adapter.connect()
            try:
                await adapter.subscribe(["AAPL"])
                fake_ws.sent.clear()
                await adapter.unsubscribe(["AAPL", "GOOG"])
                assert len(fake_ws.sent) == 1
                frame = json.loads(fake_ws.sent[0])
                assert frame == {"type": "unsubscribe", "symbol": "AAPL"}
                assert adapter._subscribed == set()
            finally:
                await adapter.disconnect()


class TestMessageDispatch:
    """Price and news message parsing + callback dispatch."""

    @patch("qracer.data.finnhub_ws._HAS_WEBSOCKETS", True)
    async def test_trade_message_invokes_price_callback(self) -> None:
        from qracer.data.finnhub_ws import FinnhubWebSocketAdapter

        fake_ws = _FakeWebSocket()
        received: list[tuple[str, float]] = []

        async def on_price(ticker: str, price: float) -> None:
            received.append((ticker, price))

        with patch("qracer.data.finnhub_ws.websockets", create=True) as mock_ws:
            mock_ws.connect = AsyncMock(return_value=fake_ws)
            adapter = FinnhubWebSocketAdapter(api_key="k")
            adapter.on_price(on_price)
            await adapter.connect()
            try:
                await fake_ws.feed(
                    json.dumps(
                        {
                            "type": "trade",
                            "data": [
                                {"s": "AAPL", "p": 182.5, "t": 1, "v": 100},
                                {"s": "TSLA", "p": 250.0, "t": 2, "v": 200},
                            ],
                        }
                    )
                )
                await asyncio.sleep(0)  # let the receive loop run
                await asyncio.sleep(0)
            finally:
                await adapter.disconnect()

        assert ("AAPL", 182.5) in received
        assert ("TSLA", 250.0) in received

    @patch("qracer.data.finnhub_ws._HAS_WEBSOCKETS", True)
    async def test_news_message_invokes_news_callback(self) -> None:
        from qracer.data.finnhub_ws import FinnhubWebSocketAdapter

        fake_ws = _FakeWebSocket()
        received: list[NewsArticle] = []

        async def on_news(article: NewsArticle) -> None:
            received.append(article)

        with patch("qracer.data.finnhub_ws.websockets", create=True) as mock_ws:
            mock_ws.connect = AsyncMock(return_value=fake_ws)
            adapter = FinnhubWebSocketAdapter(api_key="k")
            adapter.on_news(on_news)
            await adapter.connect()
            try:
                await fake_ws.feed(
                    json.dumps(
                        {
                            "type": "news",
                            "data": [
                                {
                                    "headline": "AAPL beats earnings",
                                    "source": "Reuters",
                                    "datetime": 1_700_000_000,
                                    "url": "https://example.com/a",
                                    "summary": "Apple crushed expectations.",
                                }
                            ],
                        }
                    )
                )
                await asyncio.sleep(0)
                await asyncio.sleep(0)
            finally:
                await adapter.disconnect()

        assert len(received) == 1
        assert received[0].title == "AAPL beats earnings"
        assert received[0].source == "Reuters"
        assert received[0].url == "https://example.com/a"

    @patch("qracer.data.finnhub_ws._HAS_WEBSOCKETS", True)
    async def test_callback_error_does_not_stop_loop(self) -> None:
        from qracer.data.finnhub_ws import FinnhubWebSocketAdapter

        fake_ws = _FakeWebSocket()
        good = MagicMock()

        async def bad_callback(ticker: str, price: float) -> None:
            raise RuntimeError("boom")

        async def good_callback(ticker: str, price: float) -> None:
            good(ticker, price)

        with patch("qracer.data.finnhub_ws.websockets", create=True) as mock_ws:
            mock_ws.connect = AsyncMock(return_value=fake_ws)
            adapter = FinnhubWebSocketAdapter(api_key="k")
            adapter.on_price(bad_callback)
            adapter.on_price(good_callback)
            await adapter.connect()
            try:
                await fake_ws.feed(json.dumps({"type": "trade", "data": [{"s": "X", "p": 1.0}]}))
                await asyncio.sleep(0)
                await asyncio.sleep(0)
            finally:
                await adapter.disconnect()

        good.assert_called_once_with("X", 1.0)

    @patch("qracer.data.finnhub_ws._HAS_WEBSOCKETS", True)
    async def test_non_json_message_is_ignored(self) -> None:
        from qracer.data.finnhub_ws import FinnhubWebSocketAdapter

        fake_ws = _FakeWebSocket()

        calls: list[Any] = []

        async def on_price(ticker: str, price: float) -> None:
            calls.append((ticker, price))

        with patch("qracer.data.finnhub_ws.websockets", create=True) as mock_ws:
            mock_ws.connect = AsyncMock(return_value=fake_ws)
            adapter = FinnhubWebSocketAdapter(api_key="k")
            adapter.on_price(on_price)
            await adapter.connect()
            try:
                await fake_ws.feed("not json")
                await fake_ws.feed(json.dumps({"type": "ping"}))
                await fake_ws.feed(json.dumps({"type": "error", "msg": "limit"}))
                await asyncio.sleep(0)
                await asyncio.sleep(0)
            finally:
                await adapter.disconnect()

        assert calls == []


class TestParseNews:
    """Unit tests for the raw news-payload parser."""

    def test_parse_news_basic(self) -> None:
        from qracer.data.finnhub_ws import _parse_news

        article = _parse_news(
            {
                "headline": "TSLA hits new high",
                "source": "Bloomberg",
                "datetime": 1_700_000_000,
                "url": "https://example.com/n",
                "summary": "Shares surged 5%.",
            }
        )
        assert article is not None
        assert article.title == "TSLA hits new high"
        assert article.source == "Bloomberg"
        assert article.url == "https://example.com/n"
        assert article.summary == "Shares surged 5%."

    def test_parse_news_missing_headline_returns_none(self) -> None:
        from qracer.data.finnhub_ws import _parse_news

        assert _parse_news({"source": "Reuters", "url": "x"}) is None
        assert _parse_news({"headline": ""}) is None
