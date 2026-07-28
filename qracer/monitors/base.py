"""Shared base for the polling monitors run by the ``qracer serve`` tick loop.

Every monitor (alerts, tasks, autonomous, agents, briefing) had a byte-identical
monotonic-clock throttle. This collapses that into one base: subclasses call
``super().__init__(check_interval)`` and stamp ``self._mark_checked()`` at the top
of ``check()``. Override ``should_check`` to add extra gating (e.g. market hours),
delegating to ``super().should_check()`` for the throttle.
"""

from __future__ import annotations

import time


class PollingMonitor:
    """Monotonic-clock throttling for a ``should_check()`` / ``check()`` monitor."""

    def __init__(self, check_interval: float) -> None:
        self._check_interval = check_interval
        self._last_check: float | None = None

    def should_check(self) -> bool:
        """True on the first call, then once per ``check_interval`` seconds."""
        if self._last_check is None:
            return True
        return (time.monotonic() - self._last_check) >= self._check_interval

    def _mark_checked(self) -> None:
        """Record that a check just ran (call at the top of ``check()``)."""
        self._last_check = time.monotonic()


__all__ = ["PollingMonitor"]
