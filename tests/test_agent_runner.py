"""Tests for the agent runner — single + concurrent execution, model injection, isolation."""

from __future__ import annotations

from qracer.agent_runner import DEFAULT_TRIGGER_MESSAGE, run_agent, run_agents
from qracer.agents_store import Agent
from qracer.llm.providers import CompletionRequest, CompletionResponse
from tests.helpers import FakeLLM


class BoomLLM:
    """Provider that always raises, to test failure isolation."""

    async def complete(self, request: CompletionRequest) -> CompletionResponse:
        raise RuntimeError("model exploded")


def _agent(name: str = "A", model: str = "openai/gpt-4o", prompt: str = "do the thing") -> Agent:
    return Agent(id=name, name=name, model=model, prompt=prompt)


class TestRunAgent:
    async def test_sets_request_model_to_agent_model(self) -> None:
        fake = FakeLLM(content="result text")
        agent = _agent(model="anthropic/claude-3.5-sonnet")
        result = await run_agent(agent, fake)

        assert result.ok
        assert result.output == "result text"
        assert result.model == "anthropic/claude-3.5-sonnet"
        # the request carried the agent's model + prompt as system message
        assert fake.calls[0].model == "anthropic/claude-3.5-sonnet"
        assert fake.calls[0].messages[0].role == "system"
        assert fake.calls[0].messages[0].content == "do the thing"

    async def test_default_trigger_message_used(self) -> None:
        fake = FakeLLM(content="ok")
        await run_agent(_agent(), fake)
        assert fake.calls[0].messages[1].content == DEFAULT_TRIGGER_MESSAGE

    async def test_user_input_overrides_trigger_message(self) -> None:
        fake = FakeLLM(content="ok")
        await run_agent(_agent(), fake, user_input="AAPL 지금 살까?")
        assert fake.calls[0].messages[1].content == "AAPL 지금 살까?"

    async def test_failure_is_captured_not_raised(self) -> None:
        result = await run_agent(_agent(), BoomLLM())
        assert not result.ok
        assert result.error is not None
        assert "exploded" in result.error


class TestRunAgents:
    async def test_runs_all_and_preserves_order(self) -> None:
        fake = FakeLLM(content="x")
        agents = [_agent("A"), _agent("B"), _agent("C")]
        results = await run_agents(agents, fake)
        assert [r.name for r in results] == ["A", "B", "C"]
        assert all(r.ok for r in results)

    async def test_one_failure_does_not_block_others(self) -> None:
        # A provider that fails only for a specific agent prompt.
        class SelectiveLLM:
            async def complete(self, request: CompletionRequest) -> CompletionResponse:
                if request.messages[0].content == "bad":
                    raise RuntimeError("boom")
                return CompletionResponse(
                    content="fine", model="fake", input_tokens=0, output_tokens=3, cost=0.0
                )

        agents = [_agent("A", prompt="good"), _agent("B", prompt="bad"), _agent("C", prompt="good")]
        results = await run_agents(agents, SelectiveLLM())

        by_name = {r.name: r for r in results}
        assert by_name["A"].ok
        assert by_name["C"].ok
        assert not by_name["B"].ok
        assert by_name["A"].output_tokens == 3
