"""AgentMonitor — runs due custom agents autonomously inside the server loop.

Follows the same ``should_check()`` / ``check()`` polling contract used by
:class:`AlertMonitor`, :class:`TaskExecutor`, and :class:`AutonomousMonitor`, so it
plugs straight into the ``Server`` tick loop under ``qracer serve``.

On each cycle it asks the :class:`AgentStore` which agents are due (cron expired or
continuous off cooldown), runs them **concurrently** via :func:`run_agents` so a slow
model never blocks the others or the tick loop, then records each result back into the
store (which advances cron schedules). Results are surfaced via the store for the web
UI to display.
"""

from __future__ import annotations

import logging
import time

from qracer.agent_runner import AgentResult, run_agents
from qracer.agents_store import CONTINUOUS_COOLDOWN_SECONDS, AgentStore
from qracer.llm.providers import LLMProvider

logger = logging.getLogger(__name__)

DEFAULT_CHECK_INTERVAL = 5.0  # seconds between agent-due checks


class AgentMonitor:
    """Runs due custom agents concurrently and stores their results.

    Usage::

        monitor = AgentMonitor(agent_store, provider)
        if monitor.should_check():
            results = await monitor.check()
    """

    def __init__(
        self,
        store: AgentStore,
        provider: LLMProvider,
        *,
        check_interval: float = DEFAULT_CHECK_INTERVAL,
        continuous_cooldown: int = CONTINUOUS_COOLDOWN_SECONDS,
    ) -> None:
        self._store = store
        self._provider = provider
        self._check_interval = check_interval
        self._continuous_cooldown = continuous_cooldown
        self._last_check: float | None = None

    @property
    def store(self) -> AgentStore:
        return self._store

    def should_check(self) -> bool:
        """Return True when a due-check is warranted (monotonic throttle)."""
        if self._last_check is None:
            return True
        return (time.monotonic() - self._last_check) >= self._check_interval

    async def check(self) -> list[AgentResult]:
        """Run all due agents concurrently and persist their results."""
        self._last_check = time.monotonic()
        due = self._store.due(continuous_cooldown=self._continuous_cooldown)
        if not due:
            return []

        results = await run_agents(due, self._provider)
        for result in results:
            self._store.record_run(
                result.agent_id,
                output=result.output if result.ok else None,
                error=result.error,
            )
            if result.ok:
                logger.info("Agent %r ran (%d tokens)", result.name, result.output_tokens)
            else:
                logger.warning("Agent %r failed: %s", result.name, result.error)
        return results


__all__ = ["AgentMonitor", "DEFAULT_CHECK_INTERVAL"]
