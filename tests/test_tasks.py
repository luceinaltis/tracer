"""Tests for task scheduler — Task model, TaskStore, parse_schedule."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from qracer.tasks import (
    Task,
    TaskActionType,
    TaskScheduleType,
    TaskStatus,
    TaskStore,
    parse_schedule,
    schedule_type_from_spec,
)

# ---------------------------------------------------------------------------
# parse_schedule
# ---------------------------------------------------------------------------


class TestParseSchedule:
    def test_iso_datetime(self) -> None:
        dt = parse_schedule("2026-04-08T09:30:00")
        assert dt.year == 2026
        assert dt.month == 4
        assert dt.hour == 9
        assert dt.minute == 30

    def test_iso_datetime_with_tz(self) -> None:
        dt = parse_schedule("2026-04-08T09:30:00+09:00")
        assert dt.tzinfo is not None

    def test_interval_minutes(self) -> None:
        now = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
        dt = parse_schedule("every 30m", after=now)
        assert dt == now + timedelta(minutes=30)

    def test_interval_hours(self) -> None:
        now = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
        dt = parse_schedule("every 2h", after=now)
        assert dt == now + timedelta(hours=2)

    def test_interval_days(self) -> None:
        now = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
        dt = parse_schedule("every 1d", after=now)
        assert dt == now + timedelta(days=1)

    def test_daily_future_time(self) -> None:
        now = datetime(2026, 1, 1, 8, 0, tzinfo=timezone.utc)
        dt = parse_schedule("daily 09:30", after=now)
        assert dt.hour == 9
        assert dt.minute == 30
        assert dt.day == 1  # same day

    def test_daily_past_time_goes_to_next_day(self) -> None:
        now = datetime(2026, 1, 1, 10, 0, tzinfo=timezone.utc)
        dt = parse_schedule("daily 09:30", after=now)
        assert dt.day == 2  # next day

    def test_weekly(self) -> None:
        # 2026-01-05 is a Monday
        now = datetime(2026, 1, 5, 8, 0, tzinfo=timezone.utc)
        dt = parse_schedule("weekly monday 09:00", after=now)
        assert dt.hour == 9
        assert dt.weekday() == 0  # Monday

    def test_weekly_past_time_goes_to_next_week(self) -> None:
        # 2026-01-05 is a Monday, 10:00 already past 09:00
        now = datetime(2026, 1, 5, 10, 0, tzinfo=timezone.utc)
        dt = parse_schedule("weekly monday 09:00", after=now)
        assert dt.day == 12  # next Monday

    def test_invalid_spec_raises(self) -> None:
        with pytest.raises(ValueError, match="Cannot parse"):
            parse_schedule("gibberish")

    def test_unknown_weekday_raises(self) -> None:
        with pytest.raises(ValueError, match="Unknown weekday"):
            parse_schedule("weekly notaday 09:00")


class TestScheduleTypeFromSpec:
    def test_interval_is_recurring(self) -> None:
        assert schedule_type_from_spec("every 1h") == TaskScheduleType.RECURRING

    def test_daily_is_recurring(self) -> None:
        assert schedule_type_from_spec("daily 09:30") == TaskScheduleType.RECURRING

    def test_weekly_is_recurring(self) -> None:
        assert schedule_type_from_spec("weekly monday 09:00") == TaskScheduleType.RECURRING

    def test_iso_datetime_is_once(self) -> None:
        assert schedule_type_from_spec("2026-04-08T09:30:00") == TaskScheduleType.ONCE


# ---------------------------------------------------------------------------
# Task
# ---------------------------------------------------------------------------


class TestTask:
    def _make_task(self, **overrides) -> Task:
        defaults = {
            "id": "abc12345",
            "action_type": TaskActionType.ANALYZE,
            "action_params": {"ticker": "AAPL"},
            "schedule_type": TaskScheduleType.ONCE,
            "schedule_spec": "2026-04-08T09:30:00",
            "status": TaskStatus.PENDING,
            "created_at": "2026-04-07T12:00:00+00:00",
            "next_run_at": "2026-04-08T09:30:00+00:00",
            "enabled": True,
        }
        defaults.update(overrides)
        return Task(**defaults)

    def test_is_due_when_past_next_run(self) -> None:
        task = self._make_task(next_run_at="2026-04-07T08:00:00+00:00")
        now = datetime(2026, 4, 7, 9, 0, tzinfo=timezone.utc)
        assert task.is_due(now) is True

    def test_not_due_when_before_next_run(self) -> None:
        task = self._make_task(next_run_at="2026-04-08T09:30:00+00:00")
        now = datetime(2026, 4, 7, 9, 0, tzinfo=timezone.utc)
        assert task.is_due(now) is False

    def test_not_due_when_disabled(self) -> None:
        task = self._make_task(next_run_at="2026-04-07T08:00:00+00:00", enabled=False)
        now = datetime(2026, 4, 7, 9, 0, tzinfo=timezone.utc)
        assert task.is_due(now) is False

    def test_not_due_when_not_pending(self) -> None:
        task = self._make_task(
            next_run_at="2026-04-07T08:00:00+00:00",
            status=TaskStatus.COMPLETED,
        )
        now = datetime(2026, 4, 7, 9, 0, tzinfo=timezone.utc)
        assert task.is_due(now) is False

    def test_not_due_without_next_run(self) -> None:
        task = self._make_task(next_run_at=None)
        assert task.is_due() is False

    def test_describe_analyze(self) -> None:
        task = self._make_task()
        assert "analyze" in task.describe()
        assert "AAPL" in task.describe()

    def test_describe_custom_query(self) -> None:
        task = self._make_task(
            action_type=TaskActionType.CUSTOM_QUERY,
            action_params={"query": "What is the macro outlook?"},
        )
        assert "custom query" in task.describe()
        assert "What is the macro" in task.describe()


# ---------------------------------------------------------------------------
# TaskStore
# ---------------------------------------------------------------------------


@pytest.fixture
def store(tmp_path) -> TaskStore:
    return TaskStore(tmp_path / "tasks.json")


class TestTaskStore:
    def test_create_task(self, store: TaskStore) -> None:
        task = store.create(TaskActionType.ANALYZE, {"ticker": "AAPL"}, "every 1h")
        assert task.id
        assert task.action_type == TaskActionType.ANALYZE
        assert task.schedule_type == TaskScheduleType.RECURRING
        assert task.status == TaskStatus.PENDING
        assert task.next_run_at is not None
        assert len(store) == 1

    def test_create_once_task(self, store: TaskStore) -> None:
        task = store.create(
            TaskActionType.NEWS_SCAN,
            {"ticker": "TSLA"},
            "2030-01-01T09:00:00",
        )
        assert task.schedule_type == TaskScheduleType.ONCE

    def test_get_due(self, store: TaskStore) -> None:
        store.create(TaskActionType.ANALYZE, {"ticker": "AAPL"}, "every 1h")
        # Nothing due yet (next_run is 1h from now)
        assert len(store.get_due()) == 0

        # Manually set next_run to past
        store._items[0].next_run_at = "2020-01-01T00:00:00+00:00"
        assert len(store.get_due()) == 1

    def test_get_active(self, store: TaskStore) -> None:
        store.create(TaskActionType.ANALYZE, {"ticker": "AAPL"}, "every 1h")
        store.create(TaskActionType.NEWS_SCAN, {"ticker": "TSLA"}, "every 30m")
        assert len(store.get_active()) == 2

        store.mark_completed(store._items[0].id)
        assert len(store.get_active()) == 1

    def test_mark_running(self, store: TaskStore) -> None:
        task = store.create(TaskActionType.ANALYZE, {"ticker": "AAPL"}, "every 1h")
        assert store.mark_running(task.id)
        assert store._items[0].status == TaskStatus.RUNNING

    def test_mark_completed(self, store: TaskStore) -> None:
        task = store.create(TaskActionType.ANALYZE, {"ticker": "AAPL"}, "every 1h")
        assert store.mark_completed(task.id)
        assert store._items[0].status == TaskStatus.COMPLETED
        assert store._items[0].run_count == 1
        assert store._items[0].last_run_at is not None

    def test_mark_failed(self, store: TaskStore) -> None:
        task = store.create(TaskActionType.ANALYZE, {"ticker": "AAPL"}, "every 1h")
        assert store.mark_failed(task.id, "connection error")
        assert store._items[0].status == TaskStatus.FAILED
        assert store._items[0].last_error == "connection error"

    def test_advance_recurring(self, store: TaskStore) -> None:
        task = store.create(TaskActionType.ANALYZE, {"ticker": "AAPL"}, "every 1h")
        old_next = task.next_run_at
        store.mark_completed(task.id)
        assert store.advance_recurring(task.id)
        assert store._items[0].status == TaskStatus.PENDING
        assert store._items[0].next_run_at != old_next

    def test_advance_recurring_fails_for_once(self, store: TaskStore) -> None:
        task = store.create(TaskActionType.ANALYZE, {"ticker": "AAPL"}, "2030-01-01T09:00:00")
        assert not store.advance_recurring(task.id)

    def test_cancel(self, store: TaskStore) -> None:
        task = store.create(TaskActionType.ANALYZE, {"ticker": "AAPL"}, "every 1h")
        assert store.cancel(task.id)
        assert store._items[0].enabled is False
        assert store._items[0].status == TaskStatus.COMPLETED

    def test_remove(self, store: TaskStore) -> None:
        task = store.create(TaskActionType.ANALYZE, {"ticker": "AAPL"}, "every 1h")
        assert store.remove(task.id)
        assert len(store) == 0

    def test_persistence(self, tmp_path) -> None:
        path = tmp_path / "tasks.json"
        store1 = TaskStore(path)
        store1.create(TaskActionType.ANALYZE, {"ticker": "AAPL"}, "every 1h")

        store2 = TaskStore(path)
        assert len(store2) == 1
        assert store2._items[0].action_type == TaskActionType.ANALYZE

    def test_corrupt_file(self, tmp_path) -> None:
        path = tmp_path / "tasks.json"
        path.write_text("not json", encoding="utf-8")
        store = TaskStore(path)
        assert len(store) == 0

    def test_missing_file(self, tmp_path) -> None:
        store = TaskStore(tmp_path / "nonexistent.json")
        assert len(store) == 0

    def test_hot_reload_picks_up_external_changes(self, tmp_path) -> None:
        """When another process modifies tasks.json, store should reload."""
        import json
        import time

        path = tmp_path / "tasks.json"
        store = TaskStore(path)
        store.create(TaskActionType.ANALYZE, {"ticker": "AAPL"}, "every 1h")
        assert len(store.get_all()) == 1

        # Simulate external process adding a task
        time.sleep(0.05)  # Ensure mtime differs
        data = json.loads(path.read_text(encoding="utf-8"))
        data.append(
            {
                "id": "external1",
                "action_type": "news_scan",
                "action_params": {"ticker": "TSLA"},
                "schedule_type": "recurring",
                "schedule_spec": "every 30m",
                "status": "pending",
                "created_at": "2026-01-01T00:00:00+00:00",
                "next_run_at": "2026-01-01T00:30:00+00:00",
            }
        )
        path.write_text(json.dumps(data, indent=2), encoding="utf-8")

        # Store should detect the change
        assert len(store.get_all()) == 2
