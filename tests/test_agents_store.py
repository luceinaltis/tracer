"""Tests for the custom-agent store — Agent model, AgentStore, cron/continuous triggers."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from qracer.agents_store import (
    Agent,
    AgentStore,
    TriggerType,
    compute_cron_next,
    validate_cron,
)


def _store(tmp_path: Path) -> AgentStore:
    return AgentStore(tmp_path / "agents.json")


# ---------------------------------------------------------------------------
# cron helpers
# ---------------------------------------------------------------------------


class TestCronHelpers:
    def test_validate_cron_valid(self) -> None:
        assert validate_cron("*/5 * * * *")
        assert validate_cron("0 9 * * 1-5")

    def test_validate_cron_invalid(self) -> None:
        assert not validate_cron("not a cron")
        assert not validate_cron("")

    def test_compute_cron_next(self) -> None:
        base = datetime(2026, 1, 1, 8, 0, tzinfo=timezone.utc)
        nxt = compute_cron_next("0 9 * * *", after=base)
        parsed = datetime.fromisoformat(nxt)
        assert parsed.hour == 9
        assert parsed.minute == 0


# ---------------------------------------------------------------------------
# Agent.is_due
# ---------------------------------------------------------------------------


class TestAgentIsDue:
    def test_manual_never_due(self) -> None:
        a = Agent(id="x", name="m", model="mod", prompt="p", trigger_type=TriggerType.MANUAL)
        assert not a.is_due()

    def test_disabled_never_due(self) -> None:
        now = datetime(2026, 1, 1, tzinfo=timezone.utc)
        a = Agent(
            id="x",
            name="c",
            model="mod",
            prompt="p",
            trigger_type=TriggerType.CONTINUOUS,
            enabled=False,
        )
        assert not a.is_due(now)

    def test_cron_due_when_next_run_passed(self) -> None:
        now = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
        past = (now - timedelta(minutes=1)).isoformat()
        a = Agent(
            id="x",
            name="c",
            model="mod",
            prompt="p",
            trigger_type=TriggerType.CRON,
            cron="*/5 * * * *",
            next_run_at=past,
        )
        assert a.is_due(now)

    def test_cron_not_due_when_next_run_future(self) -> None:
        now = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
        future = (now + timedelta(minutes=1)).isoformat()
        a = Agent(
            id="x",
            name="c",
            model="mod",
            prompt="p",
            trigger_type=TriggerType.CRON,
            cron="*/5 * * * *",
            next_run_at=future,
        )
        assert not a.is_due(now)

    def test_continuous_due_first_time(self) -> None:
        now = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
        a = Agent(id="x", name="c", model="mod", prompt="p", trigger_type=TriggerType.CONTINUOUS)
        assert a.is_due(now)

    def test_continuous_respects_cooldown(self) -> None:
        now = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
        recent = (now - timedelta(seconds=10)).isoformat()
        a = Agent(
            id="x",
            name="c",
            model="mod",
            prompt="p",
            trigger_type=TriggerType.CONTINUOUS,
            last_run_at=recent,
        )
        assert not a.is_due(now, continuous_cooldown=60)
        assert a.is_due(now, continuous_cooldown=5)


# ---------------------------------------------------------------------------
# AgentStore CRUD + scheduling
# ---------------------------------------------------------------------------


class TestAgentStore:
    def test_create_manual(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        a = store.create("Watcher", "openai/gpt-4o", "do stuff")
        assert a.id
        assert a.trigger_type == TriggerType.MANUAL
        assert a.next_run_at is None
        assert store.get(a.id) is not None

    def test_create_cron_computes_next_run(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        a = store.create("Cronny", "m", "p", trigger_type=TriggerType.CRON, cron="*/5 * * * *")
        assert a.next_run_at is not None

    def test_create_cron_invalid_raises(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        with pytest.raises(ValueError):
            store.create("Bad", "m", "p", trigger_type=TriggerType.CRON, cron="nope")

    def test_update_fields(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        a = store.create("A", "m", "p")
        updated = store.update(a.id, name="B", model="m2", prompt="p2", enabled=False)
        assert updated is not None
        assert updated.name == "B"
        assert updated.model == "m2"
        assert updated.enabled is False

    def test_update_switch_to_cron_sets_next_run(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        a = store.create("A", "m", "p")
        updated = store.update(a.id, trigger_type=TriggerType.CRON, cron="0 * * * *")
        assert updated is not None
        assert updated.next_run_at is not None

    def test_update_unrelated_edit_preserves_cron_next_run(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        a = store.create("A", "m", "p", trigger_type=TriggerType.CRON, cron="0 9 * * *")
        before = store.get(a.id).next_run_at  # type: ignore[union-attr]
        # Editing name/enabled must NOT push back the schedule.
        store.update(a.id, name="renamed", enabled=False)
        assert store.get(a.id).next_run_at == before  # type: ignore[union-attr]

    def test_update_changing_cron_recomputes_next_run(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        a = store.create("A", "m", "p", trigger_type=TriggerType.CRON, cron="0 9 * * *")
        before = store.get(a.id).next_run_at  # type: ignore[union-attr]
        store.update(a.id, cron="30 14 * * *")
        assert store.get(a.id).next_run_at != before  # type: ignore[union-attr]

    def test_update_switch_away_from_cron_clears_next_run(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        a = store.create("A", "m", "p", trigger_type=TriggerType.CRON, cron="0 * * * *")
        updated = store.update(a.id, trigger_type=TriggerType.MANUAL)
        assert updated is not None
        assert updated.next_run_at is None

    def test_update_missing_returns_none(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        assert store.update("nope", name="x") is None

    def test_remove(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        a = store.create("A", "m", "p")
        assert store.remove(a.id) is True
        assert store.get(a.id) is None
        assert store.remove(a.id) is False

    def test_reorder(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        a = store.create("A", "m", "p")
        b = store.create("B", "m", "p")
        c = store.create("C", "m", "p")
        store.reorder([c.id, a.id, b.id])
        assert [x.id for x in store.agents] == [c.id, a.id, b.id]

    def test_find_by_name_case_insensitive(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        store.create("News Watcher", "m", "p")
        assert store.find_by_name("news watcher") is not None
        assert store.find_by_name("missing") is None

    def test_due_selects_cron_and_continuous(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        now = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
        store.create("Manual", "m", "p")
        cron = store.create("Cron", "m", "p", trigger_type=TriggerType.CRON, cron="*/5 * * * *")
        # force the cron agent to be due
        store.update(cron.id)  # no-op, keeps next_run in future
        cont = store.create("Cont", "m", "p", trigger_type=TriggerType.CONTINUOUS)

        # Make cron due by rewriting next_run_at to the past directly.
        store.get(cron.id)
        for a in store._agents:  # type: ignore[attr-defined]
            if a.id == cron.id:
                a.next_run_at = (now - timedelta(minutes=1)).isoformat()

        due_ids = {a.id for a in store.due(now)}
        assert cont.id in due_ids
        assert cron.id in due_ids

    def test_record_run_advances_cron(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        a = store.create("Cron", "m", "p", trigger_type=TriggerType.CRON, cron="*/5 * * * *")
        store.record_run(a.id, output="hello")
        updated = store.get(a.id)
        assert updated is not None
        assert updated.run_count == 1
        assert updated.last_output == "hello"
        assert updated.last_error is None
        assert updated.last_run_at is not None
        # next_run_at is recomputed to a time strictly after this run.
        assert updated.next_run_at is not None
        assert datetime.fromisoformat(updated.next_run_at) > datetime.fromisoformat(
            updated.last_run_at
        )

    def test_record_run_error(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        a = store.create("A", "m", "p")
        store.record_run(a.id, error="boom")
        updated = store.get(a.id)
        assert updated is not None
        assert updated.last_error == "boom"

    def test_record_run_missing_returns_false(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        assert store.record_run("nope", output="x") is False

    def test_persistence_round_trip(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        a = store.create(
            "A",
            "openai/gpt-4o",
            "prompt text",
            trigger_type=TriggerType.CRON,
            cron="0 9 * * *",
        )
        reloaded = AgentStore(tmp_path / "agents.json")
        got = reloaded.get(a.id)
        assert got is not None
        assert got.name == "A"
        assert got.model == "openai/gpt-4o"
        assert got.trigger_type == TriggerType.CRON
        assert got.cron == "0 9 * * *"

    def test_hot_reload_picks_up_external_writes(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        store.create("A", "m", "p")

        external = AgentStore(tmp_path / "agents.json")
        external.create("B", "m", "p")

        # Original store should see the new agent after external write.
        names = {a.name for a in store.agents}
        assert names == {"A", "B"}

    def test_serialize_shape(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        store.create("A", "m", "p", trigger_type=TriggerType.CRON, cron="0 * * * *")
        raw = json.loads((tmp_path / "agents.json").read_text(encoding="utf-8"))
        assert isinstance(raw, list)
        assert raw[0]["trigger_type"] == "cron"
