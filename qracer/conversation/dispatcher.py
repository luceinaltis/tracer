"""Tool dispatcher — routes intent to pipeline tool functions."""

from __future__ import annotations

import asyncio

from qracer.conversation.intent import INTENT_TOOL_MAP, Intent
from qracer.data.registry import DataRegistry
from qracer.memory.fact_store import FactStore
from qracer.memory.memory_searcher import MemorySearcher
from qracer.models import ToolResult
from qracer.tools import pipeline

# Maps tool name → the kind of argument it expects.
TOOL_DISPATCH: dict[str, str] = {
    "price_event": "ticker",
    "news": "ticker",
    "insider": "ticker",
    "macro": "indicator",
    "fundamentals": "ticker",
    "cross_market": "tickers",
    "memory_search": "query",
}

# Validate that all tools referenced in INTENT_TOOL_MAP exist in TOOL_DISPATCH.
# This catches sync errors at import time rather than at runtime.
_all_intent_tools = {t for tools in INTENT_TOOL_MAP.values() for t in tools}
_unknown_tools = _all_intent_tools - set(TOOL_DISPATCH)
if _unknown_tools:
    raise RuntimeError(
        f"INTENT_TOOL_MAP references unknown tools not in TOOL_DISPATCH: {_unknown_tools}"
    )


async def invoke_tool(
    tool_name: str,
    intent: Intent,
    registry: DataRegistry,
    *,
    memory_searcher: MemorySearcher | None = None,
    fact_store: FactStore | None = None,
) -> ToolResult:
    """Invoke a single pipeline tool based on the intent context."""
    tickers = intent.tickers

    if tool_name == "price_event" and tickers:
        return await pipeline.price_event(tickers[0], registry)
    if tool_name == "news" and tickers:
        return await pipeline.news(tickers[0], registry)
    if tool_name == "insider" and tickers:
        return await pipeline.insider(tickers[0], registry)
    if tool_name == "fundamentals" and tickers:
        return await pipeline.fundamentals(tickers[0], registry)
    if tool_name == "cross_market" and tickers:
        return await pipeline.cross_market(tickers, registry)
    if tool_name == "macro":
        return await pipeline.macro(intent.raw_query, registry)
    if tool_name == "memory_search":
        return await pipeline.memory_search(
            intent.raw_query,
            searcher=memory_searcher,
            fact_store=fact_store,
            tickers=tickers,
        )

    return ToolResult(
        tool=tool_name,
        success=False,
        data={},
        source="dispatcher",
        error=f"Cannot invoke {tool_name}: no tickers in query",
    )


async def invoke_tools(
    tool_names: list[str],
    intent: Intent,
    registry: DataRegistry,
    *,
    memory_searcher: MemorySearcher | None = None,
    fact_store: FactStore | None = None,
) -> list[ToolResult]:
    """Invoke multiple pipeline tools concurrently."""
    if not tool_names:
        return []
    coros = [
        invoke_tool(
            name,
            intent,
            registry,
            memory_searcher=memory_searcher,
            fact_store=fact_store,
        )
        for name in tool_names
    ]
    return list(await asyncio.gather(*coros))
