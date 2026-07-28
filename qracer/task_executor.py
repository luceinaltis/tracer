"""TaskExecutor — checks and runs due scheduled tasks.

Follows the same should_check/check polling pattern as AlertMonitor.
Can be driven by the REPL heartbeat or the ``qracer heartbeat`` CLI command.
"""

from __future__ import annotations

import logging

from qracer.data.registry import DataRegistry
from qracer.llm.registry import LLMRegistry
from qracer.monitors.base import PollingMonitor
from qracer.tasks import (
    Task,
    TaskActionType,
    TaskResult,
    TaskScheduleType,
    TaskStore,
)
from qracer.tools import pipeline

logger = logging.getLogger(__name__)

DEFAULT_CHECK_INTERVAL = 30  # seconds


class TaskExecutor(PollingMonitor):
    """Evaluates due tasks and dispatches their actions.

    Usage::

        executor = TaskExecutor(store, data_registry, llm_registry)
        if executor.should_check():
            results = await executor.check()
    """

    def __init__(
        self,
        store: TaskStore,
        data_registry: DataRegistry,
        llm_registry: LLMRegistry,
        *,
        engine: object | None = None,
        check_interval: float = DEFAULT_CHECK_INTERVAL,
    ) -> None:
        super().__init__(check_interval)
        self._store = store
        self._data = data_registry
        self._llm = llm_registry
        self._engine = engine

    @property
    def store(self) -> TaskStore:
        return self._store

    async def check(self) -> list[TaskResult]:
        """Execute all due tasks and return results."""
        self._mark_checked()
        due = self._store.get_due()
        if not due:
            return []

        results: list[TaskResult] = []
        for task in due:
            result = await self._execute(task)
            results.append(result)
        return results

    async def _execute(self, task: Task) -> TaskResult:
        """Run a single task and update its status."""
        self._store.mark_running(task.id)

        try:
            output = await self._dispatch(task)
            self._store.mark_completed(task.id)

            if task.schedule_type == TaskScheduleType.RECURRING:
                self._store.advance_recurring(task.id)

            return TaskResult(task=task, success=True, output=output)
        except Exception as exc:
            error_msg = str(exc)
            self._store.mark_failed(task.id, error_msg)

            if task.schedule_type == TaskScheduleType.RECURRING:
                self._store.advance_recurring(task.id)

            logger.warning("Task %s failed: %s", task.id, error_msg)
            return TaskResult(task=task, success=False, output="", error=error_msg)

    async def _dispatch(self, task: Task) -> str:
        """Dispatch a task action and return a summary string."""
        params = task.action_params

        if task.action_type == TaskActionType.NEWS_SCAN:
            ticker = params.get("ticker", "")
            result = await pipeline.news(ticker, self._data)
            if not result.success:
                raise RuntimeError(result.error or "news scan failed")
            count = result.data.get("count", 0)
            return f"News scan for {ticker}: {count} articles"

        if task.action_type == TaskActionType.CROSS_MARKET_SCAN:
            tickers = params.get("tickers", [])
            result = await pipeline.cross_market(tickers, self._data)
            if not result.success:
                raise RuntimeError(result.error or "cross-market scan failed")
            return f"Cross-market scan: {len(tickers)} tickers"

        if task.action_type == TaskActionType.ANALYZE:
            ticker = params.get("ticker", "")
            if self._engine is not None and hasattr(self._engine, "query"):
                response = await self._engine.query(f"Analyze {ticker}")  # type: ignore[union-attr]
                return f"Analysis for {ticker}: conviction={response.analysis.confidence:.2f}"
            result = await pipeline.price_event(ticker, self._data)
            if not result.success:
                raise RuntimeError(result.error or "analysis failed")
            return f"Price check for {ticker}"

        if task.action_type == TaskActionType.PORTFOLIO_SNAPSHOT:
            if self._engine is not None and hasattr(self._engine, "query"):
                response = await self._engine.query("portfolio")  # type: ignore[union-attr]
                return "Portfolio snapshot taken"
            return "Portfolio snapshot (no engine available)"

        if task.action_type == TaskActionType.CUSTOM_QUERY:
            query = params.get("query", "")
            if self._engine is not None and hasattr(self._engine, "query"):
                response = await self._engine.query(query)  # type: ignore[union-attr]
                return f"Query completed: '{query[:50]}'"
            return f"Custom query skipped (no engine): '{query[:50]}'"

        return f"Unknown action type: {task.action_type}"
