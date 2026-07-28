"""Contract tests for the PollingMonitor throttle base."""

from __future__ import annotations

import time

from qracer.monitors.base import PollingMonitor


class TestPollingMonitor:
    def test_first_check_is_always_due(self) -> None:
        m = PollingMonitor(check_interval=1000)
        assert m.should_check() is True

    def test_throttles_after_mark(self) -> None:
        m = PollingMonitor(check_interval=1000)
        m._mark_checked()
        assert m.should_check() is False  # just checked, not due again

    def test_due_again_once_interval_elapsed(self) -> None:
        m = PollingMonitor(check_interval=1000)
        m._mark_checked()
        # Simulate the interval having passed.
        m._last_check = time.monotonic() - 1001
        assert m.should_check() is True
