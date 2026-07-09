"""Tests for AgentMonitor — concurrent autonomous execution + result persistence."""

from __future__ import annotations

from pathlib import Path

from qracer.agent_monitor import AgentMonitor
from qracer.agents_store import AgentStore, TriggerType
from qracer.llm.providers import CompletionRequest, CompletionResponse
from tests.helpers import FakeLLM


class SelectiveLLM:
    """Succeeds unless the agent's system prompt is 'bad'."""

    async def complete(self, request: CompletionRequest) -> CompletionResponse:
        if request.messages[0].content == "bad":
            raise RuntimeError("boom")
        return CompletionResponse(
            content="done", model="fake", input_tokens=0, output_tokens=1, cost=0.0
        )


def _store(tmp_path: Path) -> AgentStore:
    return AgentStore(tmp_path / "agents.json")


class TestShouldCheck:
    def test_first_check_is_due(self, tmp_path: Path) -> None:
        monitor = AgentMonitor(_store(tmp_path), FakeLLM())
        assert monitor.should_check()

    async def test_throttled_after_check(self, tmp_path: Path) -> None:
        monitor = AgentMonitor(_store(tmp_path), FakeLLM(), check_interval=1000)
        await monitor.check()
        assert not monitor.should_check()


class TestCheck:
    async def test_runs_continuous_agent_and_records(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        agent = store.create("Cont", "m", "p", trigger_type=TriggerType.CONTINUOUS)
        monitor = AgentMonitor(store, FakeLLM(content="hello"))

        results = await monitor.check()

        assert len(results) == 1
        updated = store.get(agent.id)
        assert updated is not None
        assert updated.last_output == "hello"
        assert updated.run_count == 1
        assert updated.last_error is None

    async def test_manual_agent_never_runs(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        store.create("Manual", "m", "p")  # default MANUAL
        monitor = AgentMonitor(store, FakeLLM())
        assert await monitor.check() == []

    async def test_one_failure_does_not_block_others(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        good = store.create("Good", "m", "good", trigger_type=TriggerType.CONTINUOUS)
        bad = store.create("Bad", "m", "bad", trigger_type=TriggerType.CONTINUOUS)
        monitor = AgentMonitor(store, SelectiveLLM())

        results = await monitor.check()
        assert len(results) == 2

        good_agent = store.get(good.id)
        bad_agent = store.get(bad.id)
        assert good_agent is not None and good_agent.last_output == "done"
        assert good_agent.last_error is None
        assert bad_agent is not None and bad_agent.last_error is not None
        assert bad_agent.last_output is None

    async def test_continuous_cooldown_prevents_immediate_rerun(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        store.create("Cont", "m", "p", trigger_type=TriggerType.CONTINUOUS)
        # Long cooldown: after the first run the agent is not due again.
        monitor = AgentMonitor(store, FakeLLM(), continuous_cooldown=10_000)

        first = await monitor.check()
        assert len(first) == 1
        second = await monitor.check()
        assert second == []
