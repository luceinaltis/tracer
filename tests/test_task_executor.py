"""Tests for TaskExecutor."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from qracer.data.providers import PriceProvider
from qracer.data.registry import DataRegistry
from qracer.llm.registry import LLMRegistry
from qracer.task_executor import TaskExecutor
from qracer.tasks import TaskActionType, TaskStatus, TaskStore


class FakePriceProvider:
    async def get_price(self, ticker: str) -> float:
        return 150.0

    async def get_ohlcv(self, ticker, start, end):
        return []


class FakeNewsProvider:
    async def get_news(self, ticker: str, limit: int = 10):
        return []


def _make_registry() -> DataRegistry:
    registry = DataRegistry()
    registry.register("fake", FakePriceProvider(), [PriceProvider])
    return registry


@pytest.fixture
def store(tmp_path) -> TaskStore:
    return TaskStore(tmp_path / "tasks.json")


@pytest.fixture
def executor(store: TaskStore) -> TaskExecutor:
    return TaskExecutor(store, _make_registry(), LLMRegistry(), check_interval=0)


class TestTaskExecutor:
    def test_should_check_first_call(self, executor: TaskExecutor) -> None:
        assert executor.should_check() is True

    async def test_should_check_false_after_recent(self, store: TaskStore) -> None:
        executor = TaskExecutor(store, _make_registry(), LLMRegistry(), check_interval=60)
        await executor.check()
        assert executor.should_check() is False

    async def test_check_no_due_tasks(self, executor: TaskExecutor) -> None:
        results = await executor.check()
        assert results == []

    async def test_check_executes_due_task(self, executor: TaskExecutor, store: TaskStore) -> None:
        store.create(TaskActionType.ANALYZE, {"ticker": "AAPL"}, "every 1h")
        # Force it to be due now
        store._items[0].next_run_at = "2020-01-01T00:00:00+00:00"
        store._items[0].status = TaskStatus.PENDING

        results = await executor.check()
        assert len(results) == 1
        assert results[0].success is True
        assert "AAPL" in results[0].output

    async def test_recurring_task_advances(self, executor: TaskExecutor, store: TaskStore) -> None:
        store.create(TaskActionType.ANALYZE, {"ticker": "AAPL"}, "every 1h")
        store._items[0].next_run_at = "2020-01-01T00:00:00+00:00"

        await executor.check()

        # After execution, recurring task should be PENDING again with new next_run
        assert store._items[0].status == TaskStatus.PENDING
        assert store._items[0].run_count == 1
        assert store._items[0].next_run_at != "2020-01-01T00:00:00+00:00"

    async def test_once_task_completed(self, executor: TaskExecutor, store: TaskStore) -> None:
        store.create(TaskActionType.ANALYZE, {"ticker": "AAPL"}, "2020-01-01T09:00:00")
        # Already due (past date)

        results = await executor.check()
        assert len(results) == 1
        assert store._items[0].status == TaskStatus.COMPLETED

    async def test_failed_task(self, store: TaskStore) -> None:
        # Registry with no providers — will fail
        empty_registry = DataRegistry()
        executor = TaskExecutor(store, empty_registry, LLMRegistry(), check_interval=0)

        store.create(TaskActionType.NEWS_SCAN, {"ticker": "AAPL"}, "every 1h")
        store._items[0].next_run_at = "2020-01-01T00:00:00+00:00"

        results = await executor.check()
        assert len(results) == 1
        assert results[0].success is False
        assert results[0].error is not None

    async def test_failed_recurring_still_advances(self, store: TaskStore) -> None:
        empty_registry = DataRegistry()
        executor = TaskExecutor(store, empty_registry, LLMRegistry(), check_interval=0)

        store.create(TaskActionType.NEWS_SCAN, {"ticker": "AAPL"}, "every 1h")
        store._items[0].next_run_at = "2020-01-01T00:00:00+00:00"

        await executor.check()

        # Even on failure, recurring task should advance
        assert store._items[0].status == TaskStatus.PENDING
        assert store._items[0].next_run_at != "2020-01-01T00:00:00+00:00"

    async def test_custom_query_with_engine(self, store: TaskStore) -> None:
        mock_engine = MagicMock()
        mock_response = MagicMock()
        mock_response.analysis.confidence = 0.85
        mock_engine.query = AsyncMock(return_value=mock_response)

        executor = TaskExecutor(
            store,
            _make_registry(),
            LLMRegistry(),
            engine=mock_engine,
            check_interval=0,
        )

        store.create(TaskActionType.CUSTOM_QUERY, {"query": "macro outlook"}, "every 1d")
        store._items[0].next_run_at = "2020-01-01T00:00:00+00:00"

        results = await executor.check()
        assert len(results) == 1
        assert results[0].success is True
        mock_engine.query.assert_called_once_with("macro outlook")

    async def test_analyze_with_engine(self, store: TaskStore) -> None:
        mock_engine = MagicMock()
        mock_response = MagicMock()
        mock_response.analysis.confidence = 0.9
        mock_engine.query = AsyncMock(return_value=mock_response)

        executor = TaskExecutor(
            store,
            _make_registry(),
            LLMRegistry(),
            engine=mock_engine,
            check_interval=0,
        )

        store.create(TaskActionType.ANALYZE, {"ticker": "TSLA"}, "every 2h")
        store._items[0].next_run_at = "2020-01-01T00:00:00+00:00"

        results = await executor.check()
        assert results[0].success is True
        assert "TSLA" in results[0].output
        mock_engine.query.assert_called_once_with("Analyze TSLA")

    async def test_portfolio_snapshot_with_engine(self, store: TaskStore) -> None:
        mock_engine = MagicMock()
        mock_engine.query = AsyncMock(return_value=MagicMock())

        executor = TaskExecutor(
            store,
            _make_registry(),
            LLMRegistry(),
            engine=mock_engine,
            check_interval=0,
        )

        store.create(TaskActionType.PORTFOLIO_SNAPSHOT, {}, "every 1d")
        store._items[0].next_run_at = "2020-01-01T00:00:00+00:00"

        results = await executor.check()
        assert results[0].success is True
        assert "Portfolio" in results[0].output
        mock_engine.query.assert_called_once_with("portfolio")

    async def test_portfolio_snapshot_without_engine(self, store: TaskStore) -> None:
        executor = TaskExecutor(store, _make_registry(), LLMRegistry(), check_interval=0)

        store.create(TaskActionType.PORTFOLIO_SNAPSHOT, {}, "every 1d")
        store._items[0].next_run_at = "2020-01-01T00:00:00+00:00"

        results = await executor.check()
        assert results[0].success is True
        assert "no engine" in results[0].output

    async def test_custom_query_without_engine(self, store: TaskStore) -> None:
        executor = TaskExecutor(store, _make_registry(), LLMRegistry(), check_interval=0)

        store.create(TaskActionType.CUSTOM_QUERY, {"query": "test"}, "every 1h")
        store._items[0].next_run_at = "2020-01-01T00:00:00+00:00"

        results = await executor.check()
        assert results[0].success is True
        assert "skipped" in results[0].output

    async def test_cross_market_scan(self, store: TaskStore) -> None:
        executor = TaskExecutor(store, _make_registry(), LLMRegistry(), check_interval=0)

        store.create(
            TaskActionType.CROSS_MARKET_SCAN,
            {"tickers": ["AAPL", "TSLA"]},
            "every 1d",
        )
        store._items[0].next_run_at = "2020-01-01T00:00:00+00:00"

        results = await executor.check()
        assert len(results) == 1
        assert results[0].success is True
        assert "2 tickers" in results[0].output
