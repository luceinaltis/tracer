"""Pipeline tool wrappers — thin async functions that fetch data and return ToolResult.

Each tool wraps a DataRegistry capability lookup, calls the provider, and
normalises the output into the uniform ToolResult contract consumed by the
AnalysisLoop.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from datetime import date, datetime, timedelta
from typing import Any

from qracer.config.models import PortfolioConfig
from qracer.data.providers import (
    AlternativeProvider,
    FundamentalProvider,
    MacroProvider,
    NewsProvider,
    PriceProvider,
)
from qracer.data.registry import DataRegistry
from qracer.llm.providers import CompletionRequest, Message, Role
from qracer.llm.registry import LLMRegistry
from qracer.memory.fact_store import FactStore
from qracer.memory.memory_searcher import MemorySearcher
from qracer.models import ToolResult, TradeThesis

logger = logging.getLogger(__name__)

# How many days of price history to fetch by default.
_DEFAULT_LOOKBACK_DAYS = 30
# Default staleness threshold (hours).
_STALENESS_HOURS = 24


def configure(*, lookback_days: int | None = None, staleness_hours: int | None = None) -> None:
    """Override pipeline defaults from config.  Called once at startup."""
    global _DEFAULT_LOOKBACK_DAYS, _STALENESS_HOURS  # noqa: PLW0603
    if lookback_days is not None:
        _DEFAULT_LOOKBACK_DAYS = lookback_days
    if staleness_hours is not None:
        _STALENESS_HOURS = staleness_hours


def _is_stale(fetched_at: datetime, threshold_hours: int = _STALENESS_HOURS) -> bool:
    return (datetime.now() - fetched_at).total_seconds() > threshold_hours * 3600


# ---------------------------------------------------------------------------
# Shared wrapper — eliminates try/except + ToolResult boilerplate
# ---------------------------------------------------------------------------


async def _run_tool(
    tool_name: str,
    source: str,
    fetch: Callable[[], Awaitable[dict[str, Any]]],
    *,
    label: str = "",
    stale_check: bool = True,
) -> ToolResult:
    """Execute *fetch*, wrap the result in a ToolResult.

    *fetch* should be a zero-arg async callable that returns the data dict.
    On exception the tool returns a failed ToolResult instead of raising.
    """
    try:
        data = await fetch()
        now = datetime.now()
        return ToolResult(
            tool=tool_name,
            success=True,
            data=data,
            source=source,
            fetched_at=now,
            is_stale=_is_stale(now) if stale_check else False,
        )
    except Exception as exc:
        logger.warning("%s failed for %s: %s", tool_name, label, exc)
        return ToolResult(
            tool=tool_name,
            success=False,
            data={},
            source=source,
            error=str(exc),
        )


# ---------------------------------------------------------------------------
# Simple data tools
# ---------------------------------------------------------------------------


async def price_event(ticker: str, registry: DataRegistry) -> ToolResult:
    """Fetch recent price/OHLCV data for a ticker."""

    async def _fetch() -> dict[str, Any]:
        end = date.today()
        start = end - timedelta(days=_DEFAULT_LOOKBACK_DAYS)
        bars = await registry.async_get_with_fallback(
            PriceProvider, "get_ohlcv", ticker, start, end
        )
        current_price = await registry.async_get_with_fallback(PriceProvider, "get_price", ticker)
        return {
            "ticker": ticker,
            "current_price": current_price,
            "bars": len(bars),
            "period_start": start.isoformat(),
            "period_end": end.isoformat(),
            "ohlcv": [
                {
                    "date": b.date.isoformat(),
                    "open": b.open,
                    "high": b.high,
                    "low": b.low,
                    "close": b.close,
                    "volume": b.volume,
                }
                for b in bars
            ],
        }

    return await _run_tool("price_event", "PriceProvider", _fetch, label=ticker)


async def news(ticker: str, registry: DataRegistry, limit: int = 10) -> ToolResult:
    """Fetch recent news articles for a ticker."""

    async def _fetch() -> dict[str, Any]:
        articles = await registry.async_get_with_fallback(
            NewsProvider, "get_news", ticker, limit=limit
        )
        return {
            "ticker": ticker,
            "count": len(articles),
            "articles": [
                {
                    "title": a.title,
                    "source": a.source,
                    "published_at": a.published_at.isoformat(),
                    "url": a.url,
                    "summary": a.summary,
                    "sentiment": a.sentiment,
                }
                for a in articles
            ],
        }

    return await _run_tool("news", "NewsProvider", _fetch, label=ticker)


async def insider(ticker: str, registry: DataRegistry) -> ToolResult:
    """Fetch insider trading data for a ticker."""

    async def _fetch() -> dict[str, Any]:
        records = await registry.async_get_with_fallback(
            AlternativeProvider, "get_alternative", ticker, record_type="insider_trades"
        )
        return {
            "ticker": ticker,
            "count": len(records),
            "records": [
                {
                    "record_type": r.record_type,
                    "data": r.data,
                    "source": r.source,
                    "date": r.date.isoformat(),
                }
                for r in records
            ],
        }

    return await _run_tool("insider", "AlternativeProvider", _fetch, label=ticker)


async def macro(indicator: str, registry: DataRegistry) -> ToolResult:
    """Fetch a macroeconomic indicator."""

    async def _fetch() -> dict[str, Any]:
        data_point = await registry.async_get_with_fallback(
            MacroProvider, "get_indicator", indicator
        )
        return {
            "name": data_point.name,
            "value": data_point.value,
            "date": data_point.date.isoformat(),
            "source": data_point.source,
            "unit": data_point.unit,
        }

    return await _run_tool("macro", "MacroProvider", _fetch, label=indicator)


async def fundamentals(ticker: str, registry: DataRegistry) -> ToolResult:
    """Fetch fundamental financial data for a ticker."""

    async def _fetch() -> dict[str, Any]:
        data = await registry.async_get_with_fallback(
            FundamentalProvider, "get_fundamentals", ticker
        )
        return {
            "ticker": data.ticker,
            "pe_ratio": data.pe_ratio,
            "market_cap": data.market_cap,
            "revenue": data.revenue,
            "earnings": data.earnings,
            "dividend_yield": data.dividend_yield,
        }

    return await _run_tool("fundamentals", "FundamentalProvider", _fetch, label=ticker)


# ---------------------------------------------------------------------------
# Composite tools (custom logic, not suited for _run_tool)
# ---------------------------------------------------------------------------


async def cross_market(tickers: list[str], registry: DataRegistry) -> ToolResult:
    """Fetch price data across multiple tickers for cross-market comparison."""
    try:
        end = date.today()
        start = end - timedelta(days=_DEFAULT_LOOKBACK_DAYS)
        result_data: dict[str, Any] = {"period_start": start.isoformat(), "tickers": {}}

        for ticker in tickers:
            try:
                bars = await registry.async_get_with_fallback(
                    PriceProvider, "get_ohlcv", ticker, start, end
                )
                current_price = await registry.async_get_with_fallback(
                    PriceProvider, "get_price", ticker
                )
                result_data["tickers"][ticker] = {
                    "current_price": current_price,
                    "bars": len(bars),
                }
            except Exception as inner_exc:
                result_data["tickers"][ticker] = {"error": str(inner_exc)}

        now = datetime.now()
        return ToolResult(
            tool="cross_market",
            success=True,
            data=result_data,
            source="PriceProvider",
            fetched_at=now,
            is_stale=_is_stale(now),
        )
    except Exception as exc:
        logger.warning("cross_market failed: %s", exc)
        return ToolResult(
            tool="cross_market",
            success=False,
            data={},
            source="PriceProvider",
            error=str(exc),
        )


_TRADE_THESIS_SYSTEM = (
    "You are a senior investment strategist. Given the analysis results for a ticker, "
    "generate a structured trade thesis.\n\n"
    "Return ONLY valid JSON with these exact keys:\n"
    '- "entry_zone": [low, high] (price range for entry)\n'
    '- "target_price": float\n'
    '- "stop_loss": float\n'
    '- "catalyst": string (what drives the move)\n'
    '- "catalyst_date": string or null (expected timing, e.g. "Q2 2026")\n'
    '- "conviction": integer 1-10\n'
    '- "summary": string (one-paragraph thesis)\n\n'
    "Base entry zone on current price data. Ensure stop_loss < entry_zone[0] < "
    "entry_zone[1] < target_price for long trades."
)


async def trade_thesis(
    ticker: str, analysis_results: list[ToolResult], llm_registry: LLMRegistry
) -> ToolResult:
    """Generate a structured trade thesis from prior analysis results (step 7)."""
    import json

    evidence_parts: list[str] = []
    for r in analysis_results:
        if r.success:
            evidence_parts.append(f"[{r.tool}] source={r.source}\n{json.dumps(r.data, indent=2)}")

    evidence = "\n\n".join(evidence_parts) if evidence_parts else "(no data)"
    user_msg = f"Ticker: {ticker}\n\nAnalysis results:\n{evidence}"

    try:
        provider = llm_registry.get(Role.STRATEGIST)
        response = await provider.complete(
            CompletionRequest(
                messages=[
                    Message(role="system", content=_TRADE_THESIS_SYSTEM),
                    Message(role="user", content=user_msg),
                ],
                max_tokens=1024,
                temperature=0.2,
            )
        )

        parsed = json.loads(response.content)

        entry_zone = (float(parsed["entry_zone"][0]), float(parsed["entry_zone"][1]))
        target_price = float(parsed["target_price"])
        stop_loss = float(parsed["stop_loss"])
        entry_mid = (entry_zone[0] + entry_zone[1]) / 2
        denominator = entry_mid - stop_loss
        risk_reward_ratio = (target_price - entry_mid) / denominator if denominator > 0 else 0.0

        thesis = TradeThesis(
            ticker=ticker,
            entry_zone=entry_zone,
            target_price=target_price,
            stop_loss=stop_loss,
            risk_reward_ratio=round(risk_reward_ratio, 2),
            catalyst=parsed["catalyst"],
            catalyst_date=parsed.get("catalyst_date"),
            conviction=int(parsed["conviction"]),
            summary=parsed["summary"],
        )

        now = datetime.now()
        return ToolResult(
            tool="trade_thesis",
            success=True,
            data={
                "thesis": {
                    "ticker": thesis.ticker,
                    "entry_zone": list(thesis.entry_zone),
                    "target_price": thesis.target_price,
                    "stop_loss": thesis.stop_loss,
                    "risk_reward_ratio": thesis.risk_reward_ratio,
                    "catalyst": thesis.catalyst,
                    "catalyst_date": thesis.catalyst_date,
                    "conviction": thesis.conviction,
                    "summary": thesis.summary,
                }
            },
            source="LLM/strategist",
            fetched_at=now,
            is_stale=False,
        )
    except (json.JSONDecodeError, KeyError, ValueError, TypeError) as exc:
        logger.warning("trade_thesis parsing failed for %s: %s", ticker, exc)
        return ToolResult(
            tool="trade_thesis",
            success=False,
            data={},
            source="LLM/strategist",
            error=f"Failed to parse LLM response: {exc}",
        )
    except Exception as exc:
        logger.warning("trade_thesis failed for %s: %s", ticker, exc)
        return ToolResult(
            tool="trade_thesis",
            success=False,
            data={},
            source="LLM/strategist",
            error=str(exc),
        )


async def risk_check(
    ticker: str,
    thesis: TradeThesis,
    registry: DataRegistry,
    config: PortfolioConfig,
) -> ToolResult:
    """Run risk check on a proposed trade (step 8).

    Builds a portfolio snapshot from current prices, sizes the proposed
    position using conviction from the thesis, and checks portfolio limits.
    """
    from qracer.risk.calculator import RiskCalculator, SectorResolver

    try:
        sector_resolver = SectorResolver(data_registry=registry)
        calculator = RiskCalculator(config, sector_resolver=sector_resolver)

        # Gather current prices for all holdings.
        prices: dict[str, float] = {}
        for holding in config.holdings:
            try:
                prices[holding.ticker] = await registry.async_get_with_fallback(
                    PriceProvider, "get_price", holding.ticker
                )
            except Exception as exc:
                logger.warning("Could not fetch price for %s: %s", holding.ticker, exc)

        snapshot = calculator.build_snapshot(prices)
        exposure = await calculator.build_exposure_async(snapshot)

        # Size the proposed position.
        allocation_pct = calculator.size_position(ticker, thesis.conviction, snapshot)

        # Check limits (including the hypothetical new position).
        breached = calculator.check_limits(snapshot, exposure)

        # Flag missing data so the report can mention it.
        caveats: list[str] = []
        missing_prices = [h.ticker for h in config.holdings if h.ticker not in prices]
        if missing_prices:
            caveats.append(f"Price unavailable for: {', '.join(missing_prices)}")
        if exposure.correlation_data_unavailable:
            caveats.append(
                "Correlation/beta data unavailable — sizing without correlation adjustment"
            )

        now = datetime.now()
        return ToolResult(
            tool="risk_check",
            success=True,
            data={
                "ticker": ticker,
                "conviction": thesis.conviction,
                "allocation_pct": allocation_pct,
                "portfolio_value": snapshot.total_value,
                "holdings_count": len(snapshot.holdings),
                "top_sector": exposure.top_sector,
                "top_sector_pct": exposure.top_sector_pct,
                "limits_breached": breached,
                "caveats": caveats,
                "sized_recommendation": (
                    f"Allocate {allocation_pct:.2f}% of portfolio to {ticker}"
                    if not breached
                    else f"Allocate {allocation_pct:.2f}% of portfolio to {ticker} "
                    f"(WARNING: {len(breached)} limit(s) breached)"
                ),
            },
            source="RiskCalculator",
            fetched_at=now,
            is_stale=False,
        )
    except Exception as exc:
        logger.warning("risk_check failed for %s: %s", ticker, exc)
        return ToolResult(
            tool="risk_check",
            success=False,
            data={},
            source="RiskCalculator",
            error=str(exc),
        )


def _structured_lookup(fact_store: FactStore, tickers: list[str]) -> dict[str, Any]:
    """Fetch structured theses and findings for *tickers* from the fact store.

    Findings lookup is gated on ``hasattr`` so the pipeline stays compatible
    with builds of :class:`FactStore` that do not yet expose ``get_findings``
    (see issue #157 / Phase 2).  Any store-side exception is swallowed — the
    caller will then fall back to free-text search.
    """
    theses: list[dict[str, Any]] = []
    findings: list[dict[str, Any]] = []

    try:
        open_theses = fact_store.get_open_theses(tickers)
    except Exception:
        logger.debug("fact_store.get_open_theses failed", exc_info=True)
        open_theses = []

    for t in open_theses:
        theses.append(
            {
                "id": t.id,
                "ticker": t.ticker,
                "entry_zone": [t.entry_zone_low, t.entry_zone_high],
                "target_price": t.target_price,
                "stop_loss": t.stop_loss,
                "risk_reward_ratio": t.risk_reward_ratio,
                "catalyst": t.catalyst,
                "catalyst_date": t.catalyst_date,
                "conviction": t.conviction,
                "summary": t.summary,
                "status": t.status.value,
                "session_id": t.session_id,
            }
        )

    get_findings = getattr(fact_store, "get_findings", None)
    if callable(get_findings):
        for ticker in tickers:
            try:
                raw_findings: Any = get_findings(ticker)
                ticker_findings: list[Any] = list(raw_findings or [])
            except Exception:
                logger.debug("fact_store.get_findings failed for %s", ticker, exc_info=True)
                continue
            for f in ticker_findings:
                findings.append(
                    {
                        "entity": getattr(f, "entity", ticker),
                        "statement": getattr(f, "statement", ""),
                        "confidence": getattr(f, "confidence", 0.0),
                        "source_tool": getattr(f, "source_tool", ""),
                        "session_id": getattr(f, "session_id", ""),
                        "event_date": getattr(f, "event_date", None),
                    }
                )

    return {"theses": theses, "findings": findings}


async def memory_search(
    query: str,
    searcher: MemorySearcher | None = None,
    *,
    fact_store: FactStore | None = None,
    tickers: list[str] | None = None,
    **kwargs: object,
) -> ToolResult:
    """Search past analyses stored in session memory.

    Two-stage retrieval:

    1. When a :class:`FactStore` is available and *tickers* is non-empty,
       run a structured lookup first (open theses + findings).  If any
       structured records are found they are returned directly — this is the
       deterministic path for ticker-scoped queries ("how's my AAPL thesis
       going?").
    2. Otherwise fall back to the existing :class:`MemorySearcher` free-text
       search over compacted session summaries.

    If neither backend is available (nor produces results), an empty
    ``results`` list is returned so the caller degrades gracefully.
    """

    async def _fetch() -> dict[str, Any]:
        # Stage 1: structured FactStore lookup.
        if fact_store is not None and tickers:
            structured = _structured_lookup(fact_store, tickers)
            if structured["theses"] or structured["findings"]:
                return {
                    "query": query,
                    "source": "fact_store",
                    "results": [],
                    "theses": structured["theses"],
                    "findings": structured["findings"],
                }

        # Stage 2: free-text fallback.
        if searcher is None:
            return {"query": query, "source": "none", "results": []}

        try:
            results = searcher.search(query, limit=5)
            return {
                "query": query,
                "source": "memory_searcher",
                "results": [
                    {
                        "session_id": r.session_id,
                        "summary": r.summary[:500],
                        "score": r.score,
                    }
                    for r in results
                ],
            }
        except Exception:
            logger.debug("memory_search query failed for '%s'", query, exc_info=True)
            return {"query": query, "source": "none", "results": []}

    return await _run_tool("memory_search", "SessionMemory", _fetch, label=query, stale_check=False)
