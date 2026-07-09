"""Agent runner — execute a custom agent (model + free-form prompt) via an LLM provider.

Each agent is an independent worker: its prompt is the standing instruction and its
``model`` selects which model answers. Execution goes straight through the
``LLMProvider.complete`` primitive with ``CompletionRequest.model`` set to the agent's
model (the OpenRouter adapter honors it), so no roles or market data are involved.

Failures are captured into :class:`AgentResult` rather than raised, so one failing
agent never blocks the others when several run concurrently.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Iterable

from qracer.agents_store import Agent
from qracer.llm.providers import CompletionRequest, LLMProvider, Message

logger = logging.getLogger(__name__)

# Sent as the user turn for autonomous runs, where there is no live user query.
DEFAULT_TRIGGER_MESSAGE = "Perform your task now and report the result."


@dataclass
class AgentResult:
    """The outcome of running a single agent."""

    agent_id: str
    name: str
    model: str
    output: str
    error: str | None = None
    output_tokens: int = 0

    @property
    def ok(self) -> bool:
        return self.error is None


async def run_agent(
    agent: Agent,
    provider: LLMProvider,
    *,
    user_input: str | None = None,
) -> AgentResult:
    """Run one agent and return its result (errors are captured, not raised)."""
    text = user_input or DEFAULT_TRIGGER_MESSAGE
    request = CompletionRequest(
        messages=[
            Message(role="system", content=agent.prompt),
            Message(role="user", content=text),
        ],
        model=agent.model,
    )
    try:
        response = await provider.complete(request)
    except Exception as exc:  # noqa: BLE001 - isolate failures per agent
        logger.warning("Agent %r failed: %s", agent.name, exc)
        return AgentResult(agent.id, agent.name, agent.model, "", error=str(exc))

    return AgentResult(
        agent_id=agent.id,
        name=agent.name,
        model=agent.model,
        output=response.content,
        output_tokens=response.output_tokens,
    )


async def run_agents(
    agents: Iterable[Agent],
    provider: LLMProvider,
    *,
    user_input: str | None = None,
) -> list[AgentResult]:
    """Run agents concurrently as independent workers; results keep input order."""
    tasks = [run_agent(a, provider, user_input=user_input) for a in agents]
    return list(await asyncio.gather(*tasks))


__all__ = ["AgentResult", "run_agent", "run_agents", "DEFAULT_TRIGGER_MESSAGE"]
