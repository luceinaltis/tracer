"""Tests for the REPL custom-agent trigger helper (`/run`), in qracer.repl."""

from __future__ import annotations

from pathlib import Path

from qracer.agents_store import AgentStore
from qracer.repl import _handle_run_agents
from tests.helpers import FakeLLM


class TestHandleRunAgents:
    async def test_runs_enabled_agents_and_records(self, tmp_path: Path, capsys) -> None:
        store = AgentStore(tmp_path / "agents.json")
        a = store.create("Alice", "openai/gpt-4o", "be helpful")
        store.create("Bob", "m", "p", enabled=False)  # disabled — skipped

        await _handle_run_agents("/run", store, FakeLLM(content="hi there"))

        out = capsys.readouterr().out
        assert "Alice" in out
        assert "hi there" in out
        assert "Bob" not in out  # disabled agent not run

        updated = store.get(a.id)
        assert updated is not None
        assert updated.last_output == "hi there"
        assert updated.run_count == 1

    async def test_passes_query_as_user_input(self, tmp_path: Path) -> None:
        store = AgentStore(tmp_path / "agents.json")
        store.create("Alice", "m", "sys prompt")
        fake = FakeLLM(content="ok")

        await _handle_run_agents("/run AAPL 지금 살까?", store, fake)

        assert fake.calls[0].messages[1].content == "AAPL 지금 살까?"

    async def test_no_agents_message(self, tmp_path: Path, capsys) -> None:
        store = AgentStore(tmp_path / "agents.json")
        await _handle_run_agents("/run", store, FakeLLM())
        assert "No enabled agents" in capsys.readouterr().out

    async def test_unavailable_without_provider(self, tmp_path: Path, capsys) -> None:
        store = AgentStore(tmp_path / "agents.json")
        store.create("Alice", "m", "p")
        await _handle_run_agents("/run", store, None)
        assert "unavailable" in capsys.readouterr().out.lower()
