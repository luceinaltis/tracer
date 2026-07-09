"""ConversationEngine — top-level orchestrator for the conversational pipeline.

Wires together IntentParser, dispatcher, AnalysisLoop, and synthesizers
to process natural-language queries end-to-end.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from qracer.config.models import PortfolioConfig
from qracer.conversation.analysis_loop import (
    CONFIDENCE_THRESHOLD,
    MAX_ITERATIONS,
    AnalysisLoop,
    AnalysisResult,
)
from qracer.conversation.context import (
    ConversationContext,
    extract_context,
    is_stale,
    resolve_pronoun,
)
from qracer.conversation.handlers import (
    ComparisonHandler,
    PortfolioHandler,
    QuickPathHandler,
    StandardHandler,
)
from qracer.conversation.intent import QUICKPATH_INTENTS, Intent, IntentParser, IntentType
from qracer.conversation.report_exporter import ReportExporter
from qracer.conversation.synthesizer import ComparisonSynthesizer, ResponseSynthesizer
from qracer.data.registry import DataRegistry
from qracer.llm.registry import LLMRegistry
from qracer.memory.fact_store import FactStore
from qracer.memory.finding_extractor import extract_findings
from qracer.memory.memory_searcher import MemorySearcher
from qracer.memory.session_compactor import SessionCompactor
from qracer.memory.session_logger import SessionLogger, TurnRecord

logger = logging.getLogger(__name__)


@dataclass
class EngineResponse:
    """Final response from the ConversationEngine."""

    text: str
    intent: Intent
    analysis: AnalysisResult
    generated_at: datetime = field(default_factory=datetime.now)


class ConversationEngine:
    """Top-level orchestrator that wires together the conversational pipeline.

    Flow: IntentParser -> dispatcher -> AnalysisLoop -> Synthesizer
    """

    def __init__(
        self,
        llm_registry: LLMRegistry,
        data_registry: DataRegistry,
        portfolio_config: PortfolioConfig | None = None,
        *,
        max_iterations: int = MAX_ITERATIONS,
        confidence_threshold: float = CONFIDENCE_THRESHOLD,
        session_logger: SessionLogger | None = None,
        report_dir: Path | None = None,
        memory_searcher: MemorySearcher | None = None,
        language: str = "en",
        summaries_dir: Path | None = None,
        fact_store: FactStore | None = None,
    ) -> None:
        self._llm = llm_registry
        self._data = data_registry
        self._intent_parser = IntentParser(llm_registry)
        self._portfolio_config = portfolio_config or PortfolioConfig()
        self._memory_searcher = memory_searcher
        self._language = language
        self._summaries_dir = summaries_dir
        self._fact_store = fact_store

        analysis_loop = AnalysisLoop(
            llm_registry,
            data_registry,
            max_iterations=max_iterations,
            confidence_threshold=confidence_threshold,
        )
        synthesizer = ResponseSynthesizer(llm_registry, language=language)
        comparison_synthesizer = ComparisonSynthesizer(llm_registry, language=language)

        # Intent handlers — each owns one branch of the query flow.
        self._portfolio_handler = PortfolioHandler(
            data_registry, self._portfolio_config, language=language
        )
        self._quickpath_handler = QuickPathHandler(
            data_registry, memory_searcher, language=language, fact_store=fact_store
        )
        self._comparison_handler = ComparisonHandler(
            data_registry, comparison_synthesizer, memory_searcher
        )
        self._standard_handler = StandardHandler(
            data_registry,
            llm_registry,
            analysis_loop,
            synthesizer,
            self._portfolio_config,
            memory_searcher,
            fact_store=fact_store,
        )

        self._history: list[dict] = []
        self._session_logger = session_logger
        self._session_id = session_logger.path.stem if session_logger else "unknown"
        self._compactor = SessionCompactor(llm_registry) if session_logger else None
        self._report_exporter = ReportExporter(report_dir) if report_dir else None
        self._context: ConversationContext = ConversationContext()
        self._turn_counter = 0
        self._last_response: EngineResponse | None = None
        self._config_version = 0

    def update_registries(
        self,
        llm_registry: LLMRegistry,
        data_registry: DataRegistry,
        portfolio_config: PortfolioConfig | None = None,
    ) -> None:
        """Hot-swap registries and optionally portfolio config at runtime."""
        self._llm = llm_registry
        self._data = data_registry
        self._intent_parser = IntentParser(llm_registry)

        if portfolio_config is not None:
            self._portfolio_config = portfolio_config

        analysis_loop = AnalysisLoop(
            llm_registry,
            data_registry,
            max_iterations=self._standard_handler._analysis_loop._max_iterations,
            confidence_threshold=self._standard_handler._analysis_loop._confidence_threshold,
        )
        lang = self._language
        synthesizer = ResponseSynthesizer(llm_registry, language=lang)
        comparison_synthesizer = ComparisonSynthesizer(llm_registry, language=lang)

        self._portfolio_handler = PortfolioHandler(
            data_registry, self._portfolio_config, language=lang
        )
        self._quickpath_handler = QuickPathHandler(
            data_registry,
            self._memory_searcher,
            language=lang,
            fact_store=self._fact_store,
        )
        self._comparison_handler = ComparisonHandler(
            data_registry, comparison_synthesizer, self._memory_searcher
        )
        self._standard_handler = StandardHandler(
            data_registry,
            llm_registry,
            analysis_loop,
            synthesizer,
            self._portfolio_config,
            self._memory_searcher,
            fact_store=self._fact_store,
        )
        self._config_version += 1

    @property
    def history(self) -> list[dict]:
        """Turn history for the current session."""
        return list(self._history)

    def save_last_report(self, fmt: str = "md") -> Path | None:
        """Save the last analysis result as a report file.

        Args:
            fmt: ``"md"`` for Markdown, ``"json"`` for JSON, ``"pdf"`` for PDF.

        Returns:
            Path to the saved file, or None if no report exporter is
            configured or no previous response exists.
        """
        if self._report_exporter is None or self._last_response is None:
            return None
        resp = self._last_response
        if fmt == "json":
            return self._report_exporter.save_json(resp.intent, resp.analysis, resp.text)
        if fmt == "pdf":
            return self._report_exporter.save_pdf(resp.intent, resp.analysis, resp.text)
        return self._report_exporter.save_markdown(resp.intent, resp.analysis, resp.text)

    def _log_turn(self, role: str, content: str, **kwargs: object) -> None:
        """Append a turn to the session log if a logger is configured."""
        if self._session_logger is None:
            return
        self._turn_counter += 1
        self._session_logger.append(
            TurnRecord(turn=self._turn_counter, role=role, content=content, **kwargs)  # type: ignore[arg-type]
        )

    async def _maybe_compact(self) -> None:
        """Trigger compaction if the session log exceeds the token threshold.

        When a ``summaries_dir`` is configured the compacted summary is also
        persisted to disk (Tier 2) and, if a ``memory_searcher`` is present,
        indexed into the search index (Tier 3) so future sessions can find
        it.
        """
        if self._compactor is None or self._session_logger is None:
            return
        if not self._compactor.needs_compaction(self._session_logger):
            return
        try:
            if self._summaries_dir is not None:
                result = await self._compactor.compact_and_save(
                    self._session_logger, self._summaries_dir
                )
                if self._memory_searcher is not None:
                    session_id = self._session_logger.path.stem
                    self._memory_searcher.index_summary(session_id, result.summary)
            else:
                result = await self._compactor.compact(self._session_logger)
            logger.info(
                "Session compacted: %d turns → %d tokens summary",
                result.turn_count,
                result.output_tokens,
            )
        except Exception:
            logger.warning("Session compaction failed", exc_info=True)

    async def query(self, user_input: str) -> EngineResponse:
        """Process a user query through the full pipeline."""
        self._history.append({"role": "user", "content": user_input})
        self._log_turn("user", user_input)

        # 0. Extract conversation context from session log.
        if self._session_logger is not None:
            turns = self._session_logger.read_all()[-50:]
            self._context = extract_context(turns)

        # 0b. Check for stale context — notify user if returning after timeout.
        if is_stale(self._context) and self._context.current_topic:
            stale_msg = (
                f"(Welcome back. Last topic was {self._context.current_topic}. "
                f"Continuing from there.)"
            )
            self._history.append({"role": "system", "content": stale_msg})
            logger.info("Stale context detected, topic=%s", self._context.current_topic)

        # 1. Parse intent (context-aware: resolves pronouns during parsing).
        intent = await self._intent_parser.parse(user_input, context=self._context)

        # 1a. Handle ambiguous intents — ask for clarification.
        if intent.ambiguous and intent.candidates:
            candidates_str = ", ".join(intent.candidates[:3])
            clarification = f"I'm not sure what you mean. Did you want: {candidates_str}?"
            if self._context.topic_stack:
                topics = self._context.topic_stack[:3]
                clarification += f" (Recent topics: {', '.join(topics)})"
            analysis = AnalysisResult(confidence=0.0, iterations=0)
            self._history.append({"role": "assistant", "content": clarification})
            self._log_turn("assistant", clarification)
            return EngineResponse(text=clarification, intent=intent, analysis=analysis)

        # 1b. If no tickers in intent, try resolving from conversation context.
        if not intent.tickers and self._context.current_topic:
            resolved = resolve_pronoun(user_input.strip(), self._context)
            if resolved is None:
                resolved = self._context.current_topic
            intent = Intent(
                intent_type=intent.intent_type,
                tickers=[resolved],
                tools=intent.tools,
                raw_query=intent.raw_query,
                confidence=intent.confidence,
            )

        # 1c. If still no tickers and intent needs them, return clarification.
        needs_tickers = intent.intent_type not in (
            IntentType.MACRO_QUERY,
            IntentType.ALPHA_HUNT,
            IntentType.FOLLOW_UP,
        )
        if not intent.tickers and needs_tickers:
            topics = self._context.topic_stack[:3]
            if topics:
                clarification = (
                    f"Which ticker are you referring to? Recent topics: {', '.join(topics)}"
                )
            else:
                clarification = (
                    "Which ticker would you like me to analyze? "
                    "Example: 'Analyze AAPL' or 'Why did TSLA spike?'"
                )
            analysis = AnalysisResult(confidence=0.0, iterations=0)
            self._history.append({"role": "assistant", "content": clarification})
            self._log_turn("assistant", clarification)
            return EngineResponse(text=clarification, intent=intent, analysis=analysis)

        logger.info(
            "Parsed intent: %s tickers=%s tools=%s",
            intent.intent_type.value,
            intent.tickers,
            intent.tools,
        )

        # 2. Route to the appropriate handler.
        if intent.intent_type == IntentType.PORTFOLIO_CHECK:
            result = await self._portfolio_handler.handle(intent)
        elif intent.intent_type in QUICKPATH_INTENTS:
            result = await self._quickpath_handler.handle(intent)
        elif intent.intent_type == IntentType.COMPARISON and len(intent.tickers) >= 2:
            result = await self._comparison_handler.handle(intent)
        else:
            result = await self._standard_handler.handle(intent)

        # 3. Common post-processing: history, logging, compaction.
        self._history.append({"role": "assistant", "content": result.text})
        self._log_turn("assistant", result.text)
        await self._maybe_compact()

        response = EngineResponse(text=result.text, intent=intent, analysis=result.analysis)
        self._last_response = response
        self._persist_facts(result.analysis)
        return response

    def _persist_facts(self, analysis: AnalysisResult) -> None:
        """Extract and persist structured facts from analysis results."""
        if self._fact_store is None:
            return
        if analysis.trade_thesis is not None:
            try:
                self._fact_store.save_thesis(analysis.trade_thesis, self._session_id)
            except Exception:
                logger.warning("Failed to persist thesis to fact store", exc_info=True)

        # Extract and persist discrete findings from every successful tool
        # result.  Each tool-level failure is isolated so a bad payload for
        # one tool never prevents findings from other tools being saved.
        for result in analysis.results:
            for draft in extract_findings(result):
                try:
                    self._fact_store.save_finding(
                        entity=draft.entity,
                        statement=draft.statement,
                        confidence=draft.confidence,
                        source_tool=draft.source_tool,
                        session_id=self._session_id,
                        event_date=draft.event_date,
                    )
                except Exception:
                    logger.warning(
                        "Failed to persist finding (tool=%s entity=%s)",
                        draft.source_tool,
                        draft.entity,
                        exc_info=True,
                    )
