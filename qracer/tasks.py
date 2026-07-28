"""Task scheduler — persistent task storage with schedule parsing.

Tasks are stored as ``tasks.json`` in the user's ``~/.qracer/`` directory.
Supports one-time and recurring schedules without external dependencies.
"""

from __future__ import annotations

import logging
import re
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any

from qracer.storage.json_store import JsonListStore

logger = logging.getLogger(__name__)

_WEEKDAYS = {
    "monday": 0,
    "tuesday": 1,
    "wednesday": 2,
    "thursday": 3,
    "friday": 4,
    "saturday": 5,
    "sunday": 6,
}


class TaskStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


class TaskScheduleType(str, Enum):
    ONCE = "once"
    RECURRING = "recurring"


class TaskActionType(str, Enum):
    ANALYZE = "analyze"
    PORTFOLIO_SNAPSHOT = "portfolio_snapshot"
    NEWS_SCAN = "news_scan"
    CROSS_MARKET_SCAN = "cross_market_scan"
    CUSTOM_QUERY = "custom_query"


# ---------------------------------------------------------------------------
# Schedule parsing
# ---------------------------------------------------------------------------

_INTERVAL_RE = re.compile(r"^every\s+(\d+)\s*([mhd])$", re.IGNORECASE)
_DAILY_RE = re.compile(r"^daily\s+(\d{1,2}):(\d{2})$", re.IGNORECASE)
_WEEKLY_RE = re.compile(r"^weekly\s+(\w+)\s+(\d{1,2}):(\d{2})$", re.IGNORECASE)


def parse_schedule(spec: str, after: datetime | None = None) -> datetime:
    """Parse a schedule spec and return the next run time.

    Supported formats:
      - ISO datetime: ``"2026-04-08T09:30:00"``
      - Interval: ``"every 1h"``, ``"every 30m"``, ``"every 1d"``
      - Daily: ``"daily 09:30"``
      - Weekly: ``"weekly monday 09:00"``

    Args:
        spec: The schedule specification string.
        after: Reference time (defaults to ``datetime.now(timezone.utc)``).

    Returns:
        The next execution time as a UTC datetime.

    Raises:
        ValueError: If the spec cannot be parsed.
    """
    now = after or datetime.now(timezone.utc)

    # ISO datetime
    try:
        dt = datetime.fromisoformat(spec)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except ValueError:
        pass

    # Interval: "every 30m", "every 1h", "every 1d"
    m = _INTERVAL_RE.match(spec)
    if m:
        amount = int(m.group(1))
        unit = m.group(2).lower()
        if unit == "m":
            return now + timedelta(minutes=amount)
        if unit == "h":
            return now + timedelta(hours=amount)
        if unit == "d":
            return now + timedelta(days=amount)

    # Daily: "daily 09:30"
    m = _DAILY_RE.match(spec)
    if m:
        hour, minute = int(m.group(1)), int(m.group(2))
        candidate = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if candidate <= now:
            candidate += timedelta(days=1)
        return candidate

    # Weekly: "weekly monday 09:00"
    m = _WEEKLY_RE.match(spec)
    if m:
        day_name = m.group(1).lower()
        hour, minute = int(m.group(2)), int(m.group(3))
        if day_name not in _WEEKDAYS:
            raise ValueError(f"Unknown weekday: {day_name}")
        target_weekday = _WEEKDAYS[day_name]
        days_ahead = (target_weekday - now.weekday()) % 7
        candidate = now.replace(hour=hour, minute=minute, second=0, microsecond=0) + timedelta(
            days=days_ahead
        )
        if candidate <= now:
            candidate += timedelta(weeks=1)
        return candidate

    raise ValueError(f"Cannot parse schedule spec: {spec!r}")


def schedule_type_from_spec(spec: str) -> TaskScheduleType:
    """Infer schedule type from a spec string."""
    if _INTERVAL_RE.match(spec) or _DAILY_RE.match(spec) or _WEEKLY_RE.match(spec):
        return TaskScheduleType.RECURRING
    return TaskScheduleType.ONCE


# ---------------------------------------------------------------------------
# Task model
# ---------------------------------------------------------------------------


@dataclass
class Task:
    """A scheduled task."""

    id: str
    action_type: TaskActionType
    action_params: dict[str, Any]
    schedule_type: TaskScheduleType
    schedule_spec: str
    status: TaskStatus
    created_at: str
    next_run_at: str | None = None
    last_run_at: str | None = None
    run_count: int = 0
    last_error: str | None = None
    enabled: bool = True

    def is_due(self, now: datetime | None = None) -> bool:
        """Return True if the task should execute now."""
        if not self.enabled or self.status != TaskStatus.PENDING:
            return False
        if self.next_run_at is None:
            return False
        ref = now or datetime.now(timezone.utc)
        next_dt = datetime.fromisoformat(self.next_run_at)
        if next_dt.tzinfo is None:
            next_dt = next_dt.replace(tzinfo=timezone.utc)
        return next_dt <= ref

    def compute_next_run(self, after: datetime | None = None) -> str:
        """Compute and return the next run time as ISO string."""
        return parse_schedule(self.schedule_spec, after=after).isoformat()

    def describe(self) -> str:
        """Return a human-readable description."""
        action = self.action_type.value.replace("_", " ")
        params = ""
        if "ticker" in self.action_params:
            params = f" {self.action_params['ticker']}"
        elif "tickers" in self.action_params:
            params = f" {', '.join(self.action_params['tickers'])}"
        elif "query" in self.action_params:
            q = self.action_params["query"]
            params = f" '{q[:40]}{'...' if len(q) > 40 else ''}'"
        sched = self.schedule_spec
        return f"{action}{params} ({sched})"


@dataclass
class TaskResult:
    """The outcome of an executed task."""

    task: Task
    success: bool
    output: str
    error: str | None = None


# ---------------------------------------------------------------------------
# Persistent store
# ---------------------------------------------------------------------------


class TaskStore(JsonListStore[Task]):
    """File-backed storage for scheduled tasks.

    Usage::

        store = TaskStore(Path("~/.qracer/tasks.json"))
        task = store.create(TaskActionType.ANALYZE, {"ticker": "AAPL"}, "every 1h")
        due = store.get_due()
    """

    _label = "tasks"

    @property
    def tasks(self) -> list[Task]:
        return list(self._items)

    def create(
        self,
        action_type: TaskActionType,
        action_params: dict[str, Any],
        schedule_spec: str,
    ) -> Task:
        """Create and persist a new task."""
        sched_type = schedule_type_from_spec(schedule_spec)
        now = datetime.now(timezone.utc)
        next_run = parse_schedule(schedule_spec, after=now).isoformat()

        task = Task(
            id=uuid.uuid4().hex[:8],
            action_type=action_type,
            action_params=action_params,
            schedule_type=sched_type,
            schedule_spec=schedule_spec,
            status=TaskStatus.PENDING,
            created_at=now.isoformat(),
            next_run_at=next_run,
        )
        self._items.append(task)
        self._save()
        return task

    def get_due(self, now: datetime | None = None) -> list[Task]:
        """Return tasks that are due for execution."""
        self._maybe_reload()
        return [t for t in self._items if t.is_due(now)]

    def get_active(self) -> list[Task]:
        """Return tasks that are enabled and not completed."""
        self._maybe_reload()
        return [t for t in self._items if t.enabled and t.status != TaskStatus.COMPLETED]

    def get_all(self) -> list[Task]:
        self._maybe_reload()
        return list(self._items)

    def mark_running(self, task_id: str) -> bool:
        for t in self._items:
            if t.id == task_id:
                t.status = TaskStatus.RUNNING
                self._save()
                return True
        return False

    def mark_completed(self, task_id: str) -> bool:
        for t in self._items:
            if t.id == task_id:
                t.status = TaskStatus.COMPLETED
                t.last_run_at = datetime.now(timezone.utc).isoformat()
                t.run_count += 1
                t.last_error = None
                self._save()
                return True
        return False

    def mark_failed(self, task_id: str, error: str) -> bool:
        for t in self._items:
            if t.id == task_id:
                t.status = TaskStatus.FAILED
                t.last_run_at = datetime.now(timezone.utc).isoformat()
                t.run_count += 1
                t.last_error = error
                self._save()
                return True
        return False

    def advance_recurring(self, task_id: str) -> bool:
        """Recompute next_run for a recurring task and reset to PENDING."""
        for t in self._items:
            if t.id == task_id and t.schedule_type == TaskScheduleType.RECURRING:
                now = datetime.now(timezone.utc)
                t.next_run_at = parse_schedule(t.schedule_spec, after=now).isoformat()
                t.status = TaskStatus.PENDING
                self._save()
                return True
        return False

    def cancel(self, task_id: str) -> bool:
        """Disable a task. Returns True if found."""
        for t in self._items:
            if t.id == task_id:
                t.enabled = False
                t.status = TaskStatus.COMPLETED
                self._save()
                return True
        return False

    def remove(self, task_id: str) -> bool:
        """Remove a task entirely. Returns True if found."""
        for i, t in enumerate(self._items):
            if t.id == task_id:
                self._items.pop(i)
                self._save()
                return True
        return False

    def _serialize(self, task: Task) -> dict[str, Any]:
        d = asdict(task)
        d["action_type"] = task.action_type.value
        d["schedule_type"] = task.schedule_type.value
        d["status"] = task.status.value
        return d

    def _deserialize(self, data: dict[str, Any]) -> Task:
        return Task(
            id=data["id"],
            action_type=TaskActionType(data["action_type"]),
            action_params=data.get("action_params", {}),
            schedule_type=TaskScheduleType(data["schedule_type"]),
            schedule_spec=data["schedule_spec"],
            status=TaskStatus(data["status"]),
            created_at=data["created_at"],
            next_run_at=data.get("next_run_at"),
            last_run_at=data.get("last_run_at"),
            run_count=data.get("run_count", 0),
            last_error=data.get("last_error"),
            enabled=data.get("enabled", True),
        )
