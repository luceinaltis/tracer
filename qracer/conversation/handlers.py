"""Intent handlers — extracted from ConversationEngine for single-responsibility.

Each handler processes one category of intent and returns a HandlerResult.
The Engine takes care of history, session logging, and compaction.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

from qracer.config.models import PortfolioConfig
from qracer.conversation.analysis_loop import AnalysisLoop, AnalysisResult
from qracer.conversation.dispatcher import invoke_tools
from qracer.conversation.intent import Intent, IntentType
from qracer.conversation.quickpath import format_portfolio, format_quickpath
from qracer.conversation.synthesizer import ComparisonSynthesizer, ResponseSynthesizer
from qracer.data.providers import PriceProvider
from qracer.data.registry import DataRegistry
from qracer.llm.registry import LLMRegistry
from qracer.memory.fact_models import PersistedThesis
from qracer.memory.fact_store import FactStore
from qracer.memory.memory_searcher import MemorySearcher
from qracer.models import ToolResult, TradeThesis
from qracer.risk.calculator import RiskCalculator
from qracer.risk.models import RebalanceAction
from qracer.tools import pipeline

logger = logging.getLogger(__name__)


@dataclass
class HandlerResult:
    """Return type for all intent handlers."""

    text: str
    analysis: AnalysisResult


class PortfolioHandler:
    """Handles PORTFOLIO_CHECK intent — fetch prices, show P&L summary."""

    def __init__(
        self,
        data_registry: DataRegistry,
        portfolio_config: PortfolioConfig,
        *,
        language: str = "en",
    ) -> None:
        self._data = data_registry
        self._portfolio_config = portfolio_config
        self._language = language

    async def handle(self, intent: Intent) -> HandlerResult:
        if not self._portfolio_config.holdings:
            text = (
                "No holdings configured.\n"
                "Add holdings to ~/.qracer/portfolio.toml to use portfolio tracking."
            )
            return HandlerResult(text=text, analysis=AnalysisResult(confidence=1.0, iterations=0))

        prices: dict[str, float] = {}
        for holding in self._portfolio_config.holdings:
            try:
                price = await self._data.async_get_with_fallback(
                    PriceProvider, "get_price", holding.ticker
                )
                prices[holding.ticker] = price
            except Exception:
                logger.warning("Could not fetch price for %s", holding.ticker)

        calculator = RiskCalculator(self._portfolio_config)
        snapshot = calculator.build_snapshot(prices)
        exposure = calculator.build_exposure(snapshot)
        breached = calculator.check_limits(snapshot, exposure)

        text = format_portfolio(snapshot, language=self._language)

        if breached:
            suggestions = calculator.suggest_rebalance(snapshot, exposure)
            if suggestions:
                text += "\n\n" + _format_rebalance_suggestions(suggestions)

        return HandlerResult(text=text, analysis=AnalysisResult(confidence=1.0, iterations=0))


class QuickPathHandler:
    """Handles QuickPath intents — template-based response, no LLM."""

    def __init__(
        self,
        data_registry: DataRegistry,
        memory_searcher: MemorySearcher | None = None,
        *,
        language: str = "en",
        fact_store: FactStore | None = None,
    ) -> None:
        self._data = data_registry
        self._memory_searcher = memory_searcher
        self._language = language
        self._fact_store = fact_store

    async def handle(self, intent: Intent) -> HandlerResult:
        results = await invoke_tools(
            intent.tools,
            intent,
            self._data,
            memory_searcher=self._memory_searcher,
            fact_store=self._fact_store,
        )
        text = format_quickpath(intent, results, language=self._language)

        if self._fact_store is not None and intent.tickers:
            open_theses = self._fact_store.get_open_theses(intent.tickers)
            if open_theses:
                text += "\n\n" + _format_thesis_reminder(open_theses[0])

        return HandlerResult(
            text=text, analysis=AnalysisResult(results=results, confidence=1.0, iterations=0)
        )


class ComparisonHandler:
    """Handles COMPARISON intent — per-ticker analysis + comparison table."""

    def __init__(
        self,
        data_registry: DataRegistry,
        synthesizer: ComparisonSynthesizer,
        memory_searcher: MemorySearcher | None = None,
        *,
        fact_store: FactStore | None = None,
    ) -> None:
        self._data = data_registry
        self._synthesizer = synthesizer
        self._memory_searcher = memory_searcher
        self._fact_store = fact_store

    async def handle(self, intent: Intent) -> HandlerResult:
        single_intents = [
            Intent(
                intent_type=IntentType.COMPARISON,
                tickers=[ticker],
                tools=intent.tools,
                raw_query=intent.raw_query,
            )
            for ticker in intent.tickers
        ]
        gathered = await asyncio.gather(
            *[
                invoke_tools(
                    si.tools,
                    si,
                    self._data,
                    memory_searcher=self._memory_searcher,
                    fact_store=self._fact_store,
                )
                for si in single_intents
            ]
        )
        per_ticker_results: dict[str, list[ToolResult]] = dict(zip(intent.tickers, gathered))
        all_results = [r for results in per_ticker_results.values() for r in results]
        analysis = AnalysisResult(results=all_results, confidence=0.7, iterations=1)
        text = await self._synthesizer.synthesize(intent, per_ticker_results)
        return HandlerResult(text=text, analysis=analysis)


class StandardHandler:
    """Handles standard DeepPath analysis — full pipeline with trade thesis + risk check."""

    def __init__(
        self,
        data_registry: DataRegistry,
        llm_registry: LLMRegistry,
        analysis_loop: AnalysisLoop,
        synthesizer: ResponseSynthesizer,
        portfolio_config: PortfolioConfig,
        memory_searcher: MemorySearcher | None = None,
        *,
        fact_store: FactStore | None = None,
    ) -> None:
        self._data = data_registry
        self._llm = llm_registry
        self._analysis_loop = analysis_loop
        self._synthesizer = synthesizer
        self._portfolio_config = portfolio_config
        self._memory_searcher = memory_searcher
        self._fact_store = fact_store

    async def handle(self, intent: Intent) -> HandlerResult:
        # Invoke initial pipeline tools.
        initial_results = await invoke_tools(
            intent.tools,
            intent,
            self._data,
            memory_searcher=self._memory_searcher,
            fact_store=self._fact_store,
        )

        # Inject open theses from fact store as prior evidence.
        if self._fact_store is not None and intent.tickers:
            open_theses = self._fact_store.get_open_theses(intent.tickers)
            if open_theses:
                initial_results.append(
                    ToolResult(
                        tool="prior_thesis",
                        success=True,
                        data={"theses": [_thesis_to_dict(t) for t in open_theses]},
                        source="FactStore",
                    )
                )

        # Run analysis loop.
        analysis = await self._analysis_loop.run(intent, initial_results)
        logger.info(
            "Analysis complete: confidence=%.2f iterations=%d",
            analysis.confidence,
            analysis.iterations,
        )

        # Trade thesis generation (step 7) — only when tickers are present.
        if intent.tickers:
            thesis_result = await pipeline.trade_thesis(
                intent.tickers[0], analysis.results, self._llm
            )
            analysis.results.append(thesis_result)
            if thesis_result.success and thesis_result.data.get("thesis"):
                td = thesis_result.data["thesis"]
                try:
                    analysis.trade_thesis = TradeThesis(
                        ticker=td["ticker"],
                        entry_zone=tuple(td["entry_zone"]),  # type: ignore[arg-type]
                        target_price=td["target_price"],
                        stop_loss=td["stop_loss"],
                        risk_reward_ratio=td["risk_reward_ratio"],
                        catalyst=td["catalyst"],
                        catalyst_date=td.get("catalyst_date"),
                        conviction=td["conviction"],
                        summary=td["summary"],
                    )
                except (KeyError, ValueError, TypeError):
                    logger.warning("Failed to reconstruct TradeThesis from result")
            elif not thesis_result.success:
                logger.warning(
                    "Trade thesis generation failed for %s: %s",
                    intent.tickers[0],
                    thesis_result.error,
                )
                reason = thesis_result.error or "unknown error"
                if analysis.early_exit_reason:
                    analysis.early_exit_reason += f"; Trade thesis failed: {reason}"
                else:
                    analysis.early_exit_reason = f"Trade thesis failed: {reason}"

        # Risk check (step 8) — only when a trade thesis was produced.
        if analysis.trade_thesis is not None and self._portfolio_config.holdings:
            risk_result = await pipeline.risk_check(
                analysis.trade_thesis.ticker,
                analysis.trade_thesis,
                self._data,
                self._portfolio_config,
            )
            analysis.results.append(risk_result)

        # Synthesize response.
        text = await self._synthesizer.synthesize(intent, analysis)
        return HandlerResult(text=text, analysis=analysis)


def _format_rebalance_suggestions(suggestions: list[RebalanceAction]) -> str:
    """Format rebalancing suggestions for display."""
    lines = ["Rebalancing Suggestions:"]
    for s in suggestions:
        if s.action == "reduce":
            lines.append(f"  REDUCE {s.ticker}: sell {abs(s.shares_delta):.0f} shares — {s.reason}")
        else:
            lines.append(f"  ADD {s.ticker} — {s.reason}")
    return "\n".join(lines)


def _thesis_to_dict(t: PersistedThesis) -> dict:
    """Convert a PersistedThesis to a dict for ToolResult.data."""
    return {
        "ticker": t.ticker,
        "entry_zone": [t.entry_zone_low, t.entry_zone_high],
        "target_price": t.target_price,
        "stop_loss": t.stop_loss,
        "risk_reward_ratio": t.risk_reward_ratio,
        "catalyst": t.catalyst,
        "catalyst_date": t.catalyst_date,
        "conviction": t.conviction,
        "status": t.status.value,
        "session_id": t.session_id,
    }


def _format_thesis_reminder(t: PersistedThesis) -> str:
    """One-line reminder of an active thesis for QuickPath output."""
    entry_mid = (t.entry_zone_low + t.entry_zone_high) / 2
    direction = "LONG" if t.target_price > entry_mid else "SHORT"
    line = (
        f"  Active thesis: {direction} {t.ticker} "
        f"(conviction {t.conviction}/10, "
        f"target ${t.target_price:.2f}, stop ${t.stop_loss:.2f}"
    )
    if t.catalyst:
        line += f", catalyst: {t.catalyst}"
        if t.catalyst_date:
            line += f" ({t.catalyst_date})"
    line += ")"
    return line
