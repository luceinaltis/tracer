"""Tests for ConversationEngine, AnalysisLoop, and ResponseSynthesizer."""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, patch

from helpers import failed_result as _failed_result
from helpers import make_mock_llm_registry as _mock_llm_registry
from helpers import ok_result as _ok_result

from qracer.config.models import Holding, PortfolioConfig
from qracer.conversation.analysis_loop import AnalysisLoop, AnalysisResult
from qracer.conversation.context import ConversationContext
from qracer.conversation.dispatcher import invoke_tool, invoke_tools
from qracer.conversation.engine import ConversationEngine, EngineResponse
from qracer.conversation.intent import Intent, IntentType
from qracer.conversation.synthesizer import ComparisonSynthesizer, ResponseSynthesizer
from qracer.data.registry import DataRegistry
from qracer.llm.providers import Role
from qracer.llm.registry import LLMRegistry

# ---------------------------------------------------------------------------
# invoke_tool / invoke_tools
# ---------------------------------------------------------------------------


class TestInvokeTool:
    async def test_price_event_with_ticker(self) -> None:
        intent = Intent(IntentType.EVENT_ANALYSIS, tickers=["AAPL"], raw_query="test")
        registry = DataRegistry()
        with patch("qracer.conversation.dispatcher.pipeline") as mock_pipeline:
            mock_pipeline.price_event = AsyncMock(return_value=_ok_result("price_event"))
            result = await invoke_tool("price_event", intent, registry)
            assert result.success
            mock_pipeline.price_event.assert_called_once_with("AAPL", registry)

    async def test_news_with_ticker(self) -> None:
        intent = Intent(IntentType.EVENT_ANALYSIS, tickers=["TSLA"], raw_query="test")
        registry = DataRegistry()
        with patch("qracer.conversation.dispatcher.pipeline") as mock_pipeline:
            mock_pipeline.news = AsyncMock(return_value=_ok_result("news"))
            result = await invoke_tool("news", intent, registry)
            assert result.success

    async def test_cross_market_passes_all_tickers(self) -> None:
        intent = Intent(IntentType.CROSS_MARKET, tickers=["AAPL", "TSLA"], raw_query="test")
        registry = DataRegistry()
        with patch("qracer.conversation.dispatcher.pipeline") as mock_pipeline:
            mock_pipeline.cross_market = AsyncMock(return_value=_ok_result("cross_market"))
            await invoke_tool("cross_market", intent, registry)
            mock_pipeline.cross_market.assert_called_once_with(["AAPL", "TSLA"], registry)

    async def test_macro_uses_raw_query(self) -> None:
        intent = Intent(IntentType.MACRO_QUERY, raw_query="inflation rate")
        registry = DataRegistry()
        with patch("qracer.conversation.dispatcher.pipeline") as mock_pipeline:
            mock_pipeline.macro = AsyncMock(return_value=_ok_result("macro"))
            await invoke_tool("macro", intent, registry)
            mock_pipeline.macro.assert_called_once_with("inflation rate", registry)

    async def test_memory_search(self) -> None:
        intent = Intent(IntentType.FOLLOW_UP, raw_query="what about before?")
        registry = DataRegistry()
        with patch("qracer.conversation.dispatcher.pipeline") as mock_pipeline:
            mock_pipeline.memory_search = AsyncMock(return_value=_ok_result("memory_search"))
            await invoke_tool("memory_search", intent, registry)
            mock_pipeline.memory_search.assert_called_once_with("what about before?", searcher=None)

    async def test_tool_without_tickers_returns_failure(self) -> None:
        intent = Intent(IntentType.EVENT_ANALYSIS, tickers=[], raw_query="test")
        registry = DataRegistry()
        result = await invoke_tool("price_event", intent, registry)
        assert not result.success
        assert result.error is not None and "no tickers" in result.error

    async def test_invoke_tools_concurrent(self) -> None:
        intent = Intent(IntentType.EVENT_ANALYSIS, tickers=["AAPL"], raw_query="test")
        registry = DataRegistry()
        with patch("qracer.conversation.dispatcher.pipeline") as mock_pipeline:
            mock_pipeline.price_event = AsyncMock(return_value=_ok_result("price_event"))
            mock_pipeline.news = AsyncMock(return_value=_ok_result("news"))
            results = await invoke_tools(["price_event", "news"], intent, registry)
            assert len(results) == 2
            assert all(r.success for r in results)

    async def test_invoke_tools_empty_list(self) -> None:
        intent = Intent(IntentType.FOLLOW_UP, raw_query="ok")
        results = await invoke_tools([], intent, DataRegistry())
        assert results == []


# ---------------------------------------------------------------------------
# AnalysisLoop
# ---------------------------------------------------------------------------


class TestAnalysisLoop:
    async def test_exits_on_high_confidence(self) -> None:
        """Loop should exit after first evaluation if confidence is high."""
        llm = _mock_llm_registry(
            {
                Role.ANALYST: json.dumps({"confidence": 0.9, "missing_tools": []}),
            }
        )
        loop = AnalysisLoop(llm, DataRegistry())
        intent = Intent(IntentType.EVENT_ANALYSIS, tickers=["AAPL"], raw_query="test")

        result = await loop.run(intent, [_ok_result("price_event")])
        assert result.confidence >= 0.7
        assert result.iterations == 1
        assert result.early_exit_reason is None

    async def test_fetches_missing_tools(self) -> None:
        """Loop should call additional tools when analyst suggests them."""
        responses = [
            json.dumps({"confidence": 0.4, "missing_tools": ["news"]}),
            json.dumps({"confidence": 0.85, "missing_tools": []}),
        ]
        llm = _mock_llm_registry({Role.ANALYST: responses})
        data = DataRegistry()
        loop = AnalysisLoop(llm, data)
        intent = Intent(IntentType.EVENT_ANALYSIS, tickers=["AAPL"], raw_query="test")

        with patch("qracer.conversation.handlers.invoke_tools") as mock_invoke:
            mock_invoke.return_value = [_ok_result("news")]
            result = await loop.run(intent, [_ok_result("price_event")])

        assert result.confidence >= 0.7
        assert result.iterations == 2
        # Should have the initial + fetched results
        assert len(result.results) == 2

    async def test_max_iterations_cap(self) -> None:
        """Loop should not exceed max_iterations."""
        low_conf = json.dumps({"confidence": 0.3, "missing_tools": ["news"]})
        llm = _mock_llm_registry({Role.ANALYST: [low_conf, low_conf, low_conf]})
        data = DataRegistry()
        loop = AnalysisLoop(llm, data, max_iterations=2)
        intent = Intent(IntentType.EVENT_ANALYSIS, tickers=["AAPL"], raw_query="test")

        with patch("qracer.conversation.handlers.invoke_tools") as mock_invoke:
            mock_invoke.return_value = [_ok_result("news")]
            result = await loop.run(intent, [_ok_result("price_event")])

        assert result.iterations <= 2

    async def test_early_exit_on_multiple_failures(self) -> None:
        """Loop should exit early if >=2 tools fail in first iteration."""
        llm = _mock_llm_registry(
            {
                Role.ANALYST: json.dumps({"confidence": 0.5, "missing_tools": []}),
            }
        )
        loop = AnalysisLoop(llm, DataRegistry())
        intent = Intent(IntentType.EVENT_ANALYSIS, tickers=["AAPL"], raw_query="test")

        result = await loop.run(intent, [_failed_result("price_event"), _failed_result("news")])
        assert result.early_exit_reason is not None
        assert "2 tools failed" in result.early_exit_reason

    async def test_llm_failure_returns_zero_confidence(self) -> None:
        """If the analyst LLM fails, confidence should be 0."""
        provider = AsyncMock()
        provider.complete.side_effect = RuntimeError("LLM down")
        llm = LLMRegistry()
        llm.register("mock", provider, [Role.ANALYST])

        loop = AnalysisLoop(llm, DataRegistry())
        intent = Intent(IntentType.EVENT_ANALYSIS, tickers=["AAPL"], raw_query="test")

        result = await loop.run(intent, [_ok_result("price_event")])
        assert result.confidence == 0.0

    async def test_no_missing_tools_exits(self) -> None:
        """If analyst says no missing tools but confidence is low, exit."""
        llm = _mock_llm_registry(
            {
                Role.ANALYST: json.dumps({"confidence": 0.5, "missing_tools": []}),
            }
        )
        loop = AnalysisLoop(llm, DataRegistry())
        intent = Intent(IntentType.EVENT_ANALYSIS, tickers=["AAPL"], raw_query="test")

        result = await loop.run(intent, [_ok_result("price_event")])
        assert result.early_exit_reason == "no additional tools suggested"


# ---------------------------------------------------------------------------
# ResponseSynthesizer
# ---------------------------------------------------------------------------


class TestResponseSynthesizer:
    async def test_synthesize_calls_strategist(self) -> None:
        llm = _mock_llm_registry(
            {
                Role.STRATEGIST: "[ANALYSIS: AAPL]\nConviction: 8/10\n...",
            }
        )
        synth = ResponseSynthesizer(llm)
        intent = Intent(IntentType.EVENT_ANALYSIS, tickers=["AAPL"], raw_query="test")
        analysis = AnalysisResult(results=[_ok_result("price_event")], confidence=0.8, iterations=1)

        text = await synth.synthesize(intent, analysis)
        assert "ANALYSIS" in text
        assert "AAPL" in text

    async def test_fallback_on_llm_failure(self) -> None:
        provider = AsyncMock()
        provider.complete.side_effect = RuntimeError("LLM down")
        llm = LLMRegistry()
        llm.register("mock", provider, [Role.STRATEGIST])

        synth = ResponseSynthesizer(llm)
        intent = Intent(IntentType.EVENT_ANALYSIS, tickers=["AAPL"], raw_query="test")
        analysis = AnalysisResult(
            results=[_ok_result("price_event"), _failed_result("news")],
            confidence=0.5,
            iterations=1,
        )

        text = await synth.synthesize(intent, analysis)
        assert "AAPL" in text
        assert "LLM error" in text

    async def test_includes_failed_tools_in_caveats(self) -> None:
        llm = _mock_llm_registry(
            {
                Role.STRATEGIST: "[ANALYSIS] with insider caveat",
            }
        )
        synth = ResponseSynthesizer(llm)
        intent = Intent(IntentType.EVENT_ANALYSIS, tickers=["AAPL"], raw_query="test")
        analysis = AnalysisResult(
            results=[_ok_result("price_event"), _failed_result("insider")],
            confidence=0.6,
            iterations=1,
        )

        # The synthesizer should pass failed tools info to the LLM.
        text = await synth.synthesize(intent, analysis)
        assert isinstance(text, str)


# ---------------------------------------------------------------------------
# ConversationEngine (integration)
# ---------------------------------------------------------------------------


class TestConversationEngine:
    async def test_full_pipeline(self) -> None:
        """End-to-end test: query → intent → tools → analysis → response."""
        intent_resp = json.dumps({"intent": "event_analysis", "tickers": ["AAPL"]})
        analysis_resp = json.dumps({"confidence": 0.85, "missing_tools": []})
        synth_resp = "[ANALYSIS: AAPL — 2025-01-01]\nConviction: 8/10\nTest response"

        llm = _mock_llm_registry(
            {
                Role.RESEARCHER: intent_resp,
                Role.ANALYST: analysis_resp,
                Role.STRATEGIST: synth_resp,
            }
        )
        data = DataRegistry()

        engine = ConversationEngine(llm, data)

        with patch("qracer.conversation.handlers.invoke_tools") as mock_invoke:
            mock_invoke.return_value = [_ok_result("price_event"), _ok_result("news")]
            response = await engine.query("Why did AAPL spike 5% today?")

        assert isinstance(response, EngineResponse)
        assert response.intent.intent_type == IntentType.EVENT_ANALYSIS
        assert response.intent.tickers == ["AAPL"]
        assert "AAPL" in response.text
        assert response.analysis.confidence >= 0.7

    async def test_history_tracking(self) -> None:
        """Engine should track user/assistant turns."""
        intent_resp = json.dumps({"intent": "macro_query", "tickers": []})
        analysis_resp = json.dumps({"confidence": 0.9, "missing_tools": []})

        llm = _mock_llm_registry(
            {
                Role.RESEARCHER: intent_resp,
                Role.ANALYST: analysis_resp,
                Role.STRATEGIST: "Macro analysis response",
            }
        )

        engine = ConversationEngine(llm, DataRegistry())

        with patch("qracer.conversation.handlers.invoke_tools") as mock_invoke:
            mock_invoke.return_value = [_ok_result("macro")]
            await engine.query("Where are we in the rate cycle?")

        assert len(engine.history) == 2
        assert engine.history[0]["role"] == "user"
        assert engine.history[1]["role"] == "assistant"

    async def test_risk_check_called_when_thesis_and_holdings(self) -> None:
        """Step 8: risk_check runs when trade thesis succeeds and portfolio has holdings."""
        intent_resp = json.dumps({"intent": "event_analysis", "tickers": ["AAPL"]})
        analysis_resp = json.dumps({"confidence": 0.85, "missing_tools": []})
        thesis_json = json.dumps(
            {
                "entry_zone": [170.0, 175.0],
                "target_price": 200.0,
                "stop_loss": 160.0,
                "catalyst": "AI revenue growth",
                "catalyst_date": "Q2 2026",
                "conviction": 8,
                "summary": "Strong momentum thesis.",
            }
        )
        synth_resp = "[ANALYSIS: AAPL]\nConviction: 8/10\nRisk-checked response"

        llm = _mock_llm_registry(
            {
                Role.RESEARCHER: intent_resp,
                Role.ANALYST: analysis_resp,
                Role.STRATEGIST: [thesis_json, synth_resp],
            }
        )

        portfolio = PortfolioConfig(
            holdings=[Holding(ticker="MSFT", shares=100, avg_cost=300.0)],
        )
        engine = ConversationEngine(llm, DataRegistry(), portfolio_config=portfolio)

        with (
            patch("qracer.conversation.handlers.invoke_tools") as mock_invoke,
            patch("qracer.conversation.handlers.pipeline.risk_check") as mock_risk,
        ):
            mock_invoke.return_value = [_ok_result("price_event")]
            mock_risk.return_value = _ok_result(
                "risk_check", {"allocation_pct": 5.0, "limits_breached": []}
            )
            response = await engine.query("Analyze AAPL")

        mock_risk.assert_called_once()
        call_args = mock_risk.call_args
        assert call_args[0][0] == "AAPL"  # ticker
        assert call_args[0][1].ticker == "AAPL"  # TradeThesis
        assert call_args[0][1].conviction == 8

        # risk_check result should be appended
        risk_results = [r for r in response.analysis.results if r.tool == "risk_check"]
        assert len(risk_results) == 1

    async def test_risk_check_skipped_without_holdings(self) -> None:
        """Step 8 is skipped when portfolio has no holdings."""
        intent_resp = json.dumps({"intent": "event_analysis", "tickers": ["AAPL"]})
        analysis_resp = json.dumps({"confidence": 0.85, "missing_tools": []})
        thesis_json = json.dumps(
            {
                "entry_zone": [170.0, 175.0],
                "target_price": 200.0,
                "stop_loss": 160.0,
                "catalyst": "AI revenue growth",
                "catalyst_date": None,
                "conviction": 7,
                "summary": "Thesis.",
            }
        )
        synth_resp = "Response"

        llm = _mock_llm_registry(
            {
                Role.RESEARCHER: intent_resp,
                Role.ANALYST: analysis_resp,
                Role.STRATEGIST: [thesis_json, synth_resp],
            }
        )

        # No holdings → risk check should not run
        engine = ConversationEngine(llm, DataRegistry())

        with (
            patch("qracer.conversation.handlers.invoke_tools") as mock_invoke,
            patch("qracer.conversation.handlers.pipeline.risk_check") as mock_risk,
        ):
            mock_invoke.return_value = [_ok_result("price_event")]
            await engine.query("Analyze AAPL")

        mock_risk.assert_not_called()

    async def test_risk_check_skipped_without_thesis(self) -> None:
        """Step 8 is skipped when trade thesis generation fails."""
        intent_resp = json.dumps({"intent": "event_analysis", "tickers": ["AAPL"]})
        analysis_resp = json.dumps({"confidence": 0.85, "missing_tools": []})
        # Invalid JSON → thesis fails
        synth_resp = "Plain text, not JSON"

        llm = _mock_llm_registry(
            {
                Role.RESEARCHER: intent_resp,
                Role.ANALYST: analysis_resp,
                Role.STRATEGIST: [synth_resp, synth_resp],
            }
        )

        portfolio = PortfolioConfig(
            holdings=[Holding(ticker="MSFT", shares=100, avg_cost=300.0)],
        )
        engine = ConversationEngine(llm, DataRegistry(), portfolio_config=portfolio)

        with (
            patch("qracer.conversation.handlers.invoke_tools") as mock_invoke,
            patch("qracer.conversation.handlers.pipeline.risk_check") as mock_risk,
        ):
            mock_invoke.return_value = [_ok_result("price_event")]
            await engine.query("Analyze AAPL")

        mock_risk.assert_not_called()

    async def test_custom_thresholds(self) -> None:
        """Engine should accept custom max_iterations and confidence_threshold."""
        engine = ConversationEngine(
            _mock_llm_registry(
                {
                    Role.RESEARCHER: json.dumps({"intent": "macro_query", "tickers": []}),
                    Role.ANALYST: json.dumps({"confidence": 0.5, "missing_tools": []}),
                    Role.STRATEGIST: "response",
                }
            ),
            DataRegistry(),
            max_iterations=1,
            confidence_threshold=0.9,
        )

        with patch("qracer.conversation.handlers.invoke_tools") as mock_invoke:
            mock_invoke.return_value = []
            response = await engine.query("test")

        # With threshold 0.9 and confidence 0.5, it should have low confidence.
        assert response.analysis.confidence < 0.9


# ---------------------------------------------------------------------------
# ComparisonSynthesizer
# ---------------------------------------------------------------------------


class TestComparisonSynthesizer:
    async def test_synthesize_calls_strategist(self) -> None:
        llm = _mock_llm_registry(
            {
                Role.STRATEGIST: (
                    "[COMPARISON: AAPL, MSFT]\n"
                    "| Metric | AAPL | MSFT |\n"
                    "|---|---|---|\n"
                    "| Price | 180 | 350 |\n\n"
                    "VERDICT\nMSFT is stronger."
                ),
            }
        )
        synth = ComparisonSynthesizer(llm)
        intent = Intent(
            IntentType.COMPARISON,
            tickers=["AAPL", "MSFT"],
            raw_query="Compare AAPL and MSFT",
        )
        per_ticker = {
            "AAPL": [_ok_result("price_event"), _ok_result("fundamentals")],
            "MSFT": [_ok_result("price_event"), _ok_result("fundamentals")],
        }

        text = await synth.synthesize(intent, per_ticker)
        assert "COMPARISON" in text
        assert "AAPL" in text
        assert "MSFT" in text

    async def test_fallback_on_llm_failure(self) -> None:
        provider = AsyncMock()
        provider.complete.side_effect = RuntimeError("LLM down")
        llm = LLMRegistry()
        llm.register("mock", provider, [Role.STRATEGIST])

        synth = ComparisonSynthesizer(llm)
        intent = Intent(IntentType.COMPARISON, tickers=["AAPL", "MSFT"], raw_query="AAPL vs MSFT")
        per_ticker = {
            "AAPL": [_ok_result("price_event")],
            "MSFT": [_ok_result("price_event"), _failed_result("fundamentals")],
        }

        text = await synth.synthesize(intent, per_ticker)
        assert "COMPARISON" in text
        assert "LLM error" in text


# ---------------------------------------------------------------------------
# ConversationEngine — comparison path
# ---------------------------------------------------------------------------


class TestConversationEngineComparison:
    async def test_comparison_pipeline(self) -> None:
        intent_resp = json.dumps({"intent": "comparison", "tickers": ["AAPL", "MSFT"]})
        comparison_resp = (
            "[COMPARISON: AAPL, MSFT]\n"
            "| Metric | AAPL | MSFT |\n"
            "|---|---|---|\n"
            "| Price | 180 | 350 |\n\n"
            "VERDICT\nMSFT is stronger."
        )

        llm = _mock_llm_registry({Role.RESEARCHER: intent_resp, Role.STRATEGIST: comparison_resp})
        engine = ConversationEngine(llm, DataRegistry())

        with patch("qracer.conversation.handlers.invoke_tools") as mock_invoke:
            mock_invoke.return_value = [_ok_result("price_event"), _ok_result("fundamentals")]
            response = await engine.query("Compare AAPL and MSFT")

        assert isinstance(response, EngineResponse)
        assert response.intent.intent_type == IntentType.COMPARISON
        assert "COMPARISON" in response.text
        assert mock_invoke.call_count == 2  # once per ticker

    async def test_single_ticker_comparison_falls_through(self) -> None:
        intent_resp = json.dumps({"intent": "comparison", "tickers": ["AAPL"]})
        analysis_resp = json.dumps({"confidence": 0.85, "missing_tools": []})
        synth_resp = "[ANALYSIS: AAPL]\nStandard response"

        llm = _mock_llm_registry(
            {
                Role.RESEARCHER: intent_resp,
                Role.ANALYST: analysis_resp,
                Role.STRATEGIST: synth_resp,
            }
        )
        engine = ConversationEngine(llm, DataRegistry())

        with patch("qracer.conversation.handlers.invoke_tools") as mock_invoke:
            mock_invoke.return_value = [_ok_result("price_event")]
            response = await engine.query("Compare AAPL")

        assert response.intent.intent_type == IntentType.COMPARISON
        assert "ANALYSIS" in response.text


# ---------------------------------------------------------------------------
# Session logging integration
# ---------------------------------------------------------------------------


class TestSessionLogging:
    async def test_query_logs_user_and_assistant_turns(self, tmp_path) -> None:
        """Engine should log user input and assistant response to SessionLogger."""
        from qracer.memory.session_logger import SessionLogger

        log_path = tmp_path / "test.jsonl"
        session_logger = SessionLogger(log_path)

        intent_resp = json.dumps({"intent": "macro_query", "tickers": []})
        analysis_resp = json.dumps({"confidence": 0.85, "missing_tools": []})

        llm = _mock_llm_registry(
            {
                Role.RESEARCHER: intent_resp,
                Role.ANALYST: analysis_resp,
                Role.STRATEGIST: "Response text",
            }
        )
        engine = ConversationEngine(llm, DataRegistry(), session_logger=session_logger)

        with patch("qracer.conversation.handlers.invoke_tools") as mock_invoke:
            mock_invoke.return_value = []
            await engine.query("What is inflation?")

        turns = session_logger.read_all()
        assert len(turns) >= 2
        assert turns[0].role == "user"
        assert turns[0].content == "What is inflation?"
        assert turns[-1].role == "assistant"

    async def test_turn_counter_increments(self, tmp_path) -> None:
        """Turn counter should increment across multiple queries."""
        from qracer.memory.session_logger import SessionLogger

        log_path = tmp_path / "test.jsonl"
        session_logger = SessionLogger(log_path)

        intent_resp = json.dumps({"intent": "macro_query", "tickers": []})
        analysis_resp = json.dumps({"confidence": 0.85, "missing_tools": []})

        llm = _mock_llm_registry(
            {
                Role.RESEARCHER: [intent_resp, intent_resp],
                Role.ANALYST: [analysis_resp, analysis_resp],
                Role.STRATEGIST: ["Response 1", "Response 2"],
            }
        )
        engine = ConversationEngine(llm, DataRegistry(), session_logger=session_logger)

        with patch("qracer.conversation.handlers.invoke_tools") as mock_invoke:
            mock_invoke.return_value = []
            await engine.query("Query 1")
            await engine.query("Query 2")

        turns = session_logger.read_all()
        turn_numbers = [t.turn for t in turns]
        assert turn_numbers == [1, 2, 3, 4]  # user1, assistant1, user2, assistant2

    async def test_no_logging_without_session_logger(self) -> None:
        """Engine without session_logger should work normally without errors."""
        intent_resp = json.dumps({"intent": "macro_query", "tickers": []})
        analysis_resp = json.dumps({"confidence": 0.85, "missing_tools": []})

        llm = _mock_llm_registry(
            {
                Role.RESEARCHER: intent_resp,
                Role.ANALYST: analysis_resp,
                Role.STRATEGIST: "Response",
            }
        )
        engine = ConversationEngine(llm, DataRegistry())  # no session_logger

        with patch("qracer.conversation.handlers.invoke_tools") as mock_invoke:
            mock_invoke.return_value = []
            response = await engine.query("Test query")

        assert response.text == "Response"


# ---------------------------------------------------------------------------
# i18n tests
# ---------------------------------------------------------------------------


class TestResponseSynthesizerI18n:
    async def test_korean_instruction_in_system_prompt(self) -> None:
        """Korean language should append instruction to system prompt."""
        from qracer.llm.providers import CompletionResponse

        provider = AsyncMock()
        provider.complete.return_value = CompletionResponse(
            content="[분석: AAPL]\n확신도: 8/10\n한국어 응답",
            model="mock",
            input_tokens=10,
            output_tokens=5,
            cost=0.0,
        )
        llm = LLMRegistry()
        llm.register("mock", provider, [Role.STRATEGIST])

        synth = ResponseSynthesizer(llm, language="ko")
        intent = Intent(IntentType.EVENT_ANALYSIS, tickers=["AAPL"], raw_query="test")
        from qracer.conversation.analysis_loop import AnalysisResult

        analysis = AnalysisResult(results=[_ok_result("price_event")], confidence=0.8, iterations=1)

        await synth.synthesize(intent, analysis)

        # Verify the system prompt contains the Korean instruction
        call_args = provider.complete.call_args[0][0]
        system_msg = call_args.messages[0].content
        assert "Korean" in system_msg
        assert "ENTIRE response" in system_msg

    async def test_english_no_language_instruction(self) -> None:
        """English (default) should NOT append any language instruction."""
        from qracer.llm.providers import CompletionResponse

        provider = AsyncMock()
        provider.complete.return_value = CompletionResponse(
            content="[ANALYSIS: AAPL]\nConviction: 8/10",
            model="mock",
            input_tokens=10,
            output_tokens=5,
            cost=0.0,
        )
        llm = LLMRegistry()
        llm.register("mock", provider, [Role.STRATEGIST])

        synth = ResponseSynthesizer(llm, language="en")
        intent = Intent(IntentType.EVENT_ANALYSIS, tickers=["AAPL"], raw_query="test")
        from qracer.conversation.analysis_loop import AnalysisResult

        analysis = AnalysisResult(results=[_ok_result("price_event")], confidence=0.8, iterations=1)

        await synth.synthesize(intent, analysis)

        call_args = provider.complete.call_args[0][0]
        system_msg = call_args.messages[0].content
        assert "ENTIRE response" not in system_msg


class TestComparisonSynthesizerI18n:
    async def test_japanese_instruction_in_system_prompt(self) -> None:
        """Japanese language should append instruction to comparison system prompt."""
        from qracer.llm.providers import CompletionResponse

        provider = AsyncMock()
        provider.complete.return_value = CompletionResponse(
            content="[比較: AAPL, MSFT]\n日本語の応答",
            model="mock",
            input_tokens=10,
            output_tokens=5,
            cost=0.0,
        )
        llm = LLMRegistry()
        llm.register("mock", provider, [Role.STRATEGIST])

        synth = ComparisonSynthesizer(llm, language="ja")
        intent = Intent(IntentType.COMPARISON, tickers=["AAPL", "MSFT"], raw_query="test")
        per_ticker = {
            "AAPL": [_ok_result("price_event")],
            "MSFT": [_ok_result("price_event")],
        }

        await synth.synthesize(intent, per_ticker)

        call_args = provider.complete.call_args[0][0]
        system_msg = call_args.messages[0].content
        assert "Japanese" in system_msg
        assert "ENTIRE response" in system_msg


class TestConversationEngineI18n:
    async def test_language_passed_to_quickpath(self) -> None:
        """Engine with language='ko' should produce Korean quickpath output."""
        intent_resp = json.dumps({"intent": "price_check", "tickers": ["AAPL"]})

        llm = _mock_llm_registry({Role.RESEARCHER: intent_resp})
        engine = ConversationEngine(llm, DataRegistry(), language="ko")

        with patch("qracer.conversation.handlers.invoke_tools") as mock_invoke:
            failed = _failed_result("price_event")
            mock_invoke.return_value = [failed]
            response = await engine.query("AAPL 가격")

        assert "가격 정보 없음" in response.text

    async def test_default_language_is_english(self) -> None:
        """Engine without language arg should default to English."""
        intent_resp = json.dumps({"intent": "price_check", "tickers": ["AAPL"]})

        llm = _mock_llm_registry({Role.RESEARCHER: intent_resp})
        engine = ConversationEngine(llm, DataRegistry())

        with patch("qracer.conversation.handlers.invoke_tools") as mock_invoke:
            failed = _failed_result("price_event")
            mock_invoke.return_value = [failed]
            response = await engine.query("AAPL price")

        assert "unavailable" in response.text


# ---------------------------------------------------------------------------
# Cross-session memory: compact_and_save + Tier 3 auto-indexing
# ---------------------------------------------------------------------------


class TestCompactionPersistence:
    async def test_maybe_compact_saves_to_disk_and_indexes(self, tmp_path) -> None:
        """When summaries_dir and memory_searcher are set, compaction should
        write a Markdown summary to disk AND auto-index it into Tier 3."""
        from qracer.memory.memory_searcher import MemorySearcher
        from qracer.memory.session_compactor import CompactionResult
        from qracer.memory.session_logger import SessionLogger, TurnRecord

        session_logger = SessionLogger(tmp_path / "sessions" / "abc123.jsonl")
        for i in range(1, 4):
            session_logger.append(TurnRecord(turn=i, role="user", content=f"query {i}"))
            session_logger.append(TurnRecord(turn=i, role="assistant", content=f"answer {i}"))

        summaries_dir = tmp_path / "summaries"
        searcher = MemorySearcher()

        llm = _mock_llm_registry({Role.RESEARCHER: "", Role.ANALYST: "", Role.STRATEGIST: ""})
        engine = ConversationEngine(
            llm,
            DataRegistry(),
            session_logger=session_logger,
            memory_searcher=searcher,
            summaries_dir=summaries_dir,
        )

        # Stub the compactor so we don't depend on reporter LLM wiring.
        engine._compactor.needs_compaction = lambda _: True  # type: ignore[union-attr]

        async def fake_compact_and_save(sl, out_dir):  # type: ignore[no-untyped-def]
            out_dir.mkdir(parents=True, exist_ok=True)
            md_path = out_dir / (sl.path.stem + ".md")
            md_path.write_text("# Session Summary\n\n- Discussed AAPL earnings", encoding="utf-8")
            return CompactionResult(
                summary="# Session Summary\n\n- Discussed AAPL earnings",
                turn_count=6,
                input_tokens=100,
                output_tokens=20,
                cost=0.0,
            )

        engine._compactor.compact_and_save = fake_compact_and_save  # type: ignore[union-attr,method-assign]
        engine._compactor.compact = AsyncMock()  # type: ignore[union-attr,method-assign]

        await engine._maybe_compact()

        # Tier 2: file on disk.
        assert (summaries_dir / "abc123.md").exists()
        # Tier 3: the auto-indexed row is directly observable on the index
        # connection without relying on the FTS extension (which may be
        # unavailable in sandboxed environments).
        row = searcher.connection.execute(
            "SELECT summary FROM session_index WHERE session_id = 'abc123'"
        ).fetchone()
        assert row is not None
        assert "AAPL earnings" in row[0]
        # Verified we took the save-branch, not the in-memory branch.
        engine._compactor.compact.assert_not_called()  # type: ignore[union-attr]

        searcher.close()

    async def test_maybe_compact_falls_back_to_in_memory_without_dir(self, tmp_path) -> None:
        """Without summaries_dir, compaction should call compact() (no disk)."""
        from qracer.memory.session_compactor import CompactionResult
        from qracer.memory.session_logger import SessionLogger, TurnRecord

        session_logger = SessionLogger(tmp_path / "sessions" / "abc123.jsonl")
        session_logger.append(TurnRecord(turn=1, role="user", content="hi"))

        llm = _mock_llm_registry({Role.RESEARCHER: "", Role.ANALYST: "", Role.STRATEGIST: ""})
        engine = ConversationEngine(llm, DataRegistry(), session_logger=session_logger)
        assert engine._compactor is not None

        engine._compactor.needs_compaction = lambda _: True  # type: ignore[method-assign]
        engine._compactor.compact = AsyncMock(  # type: ignore[method-assign]
            return_value=CompactionResult(
                summary="x", turn_count=1, input_tokens=1, output_tokens=1, cost=0.0
            )
        )
        engine._compactor.compact_and_save = AsyncMock()  # type: ignore[method-assign]

        await engine._maybe_compact()

        engine._compactor.compact.assert_awaited_once()
        engine._compactor.compact_and_save.assert_not_called()


# ---------------------------------------------------------------------------
# Structured fact persistence (FactStore integration)
# ---------------------------------------------------------------------------


class TestFactExtraction:
    async def test_thesis_persisted_after_query(self) -> None:
        """When the standard handler produces a TradeThesis, it should be
        persisted to the fact store automatically."""
        from qracer.memory.fact_store import FactStore
        from qracer.models.base import TradeThesis

        intent_resp = json.dumps({"intent": "event_analysis", "tickers": ["AAPL"]})
        llm = _mock_llm_registry({Role.RESEARCHER: intent_resp, Role.STRATEGIST: "Response"})

        fact_store = FactStore()  # in-memory
        engine = ConversationEngine(llm, DataRegistry(), fact_store=fact_store)

        thesis = TradeThesis(
            ticker="AAPL",
            entry_zone=(170.0, 175.0),
            target_price=200.0,
            stop_loss=160.0,
            risk_reward_ratio=2.4,
            catalyst="AI revenue",
            catalyst_date="Q2 2026",
            conviction=8,
            summary="Long AAPL on AI",
        )

        # Patch handlers to return an analysis with a trade thesis.
        analysis = AnalysisResult(
            results=[_ok_result("price_event")],
            confidence=0.9,
            iterations=1,
            trade_thesis=thesis,
        )
        with patch.object(engine._standard_handler, "handle", new=AsyncMock()) as mock_handle:
            from qracer.conversation.handlers import HandlerResult

            mock_handle.return_value = HandlerResult(text="Response", analysis=analysis)
            await engine.query("Analyze AAPL")

        open_theses = fact_store.get_open_theses(["AAPL"])
        assert len(open_theses) == 1
        assert open_theses[0].ticker == "AAPL"
        assert open_theses[0].conviction == 8
        assert open_theses[0].target_price == 200.0

        fact_store.close()

    async def test_supersession_on_second_analysis(self) -> None:
        """A second thesis for the same ticker should supersede the first."""
        from qracer.memory.fact_store import FactStore
        from qracer.models.base import TradeThesis

        fact_store = FactStore()
        intent_resp = json.dumps({"intent": "event_analysis", "tickers": ["AAPL"]})
        llm = _mock_llm_registry({Role.RESEARCHER: intent_resp, Role.STRATEGIST: "R"})
        engine = ConversationEngine(llm, DataRegistry(), fact_store=fact_store)

        for target in (200.0, 220.0):
            thesis = TradeThesis(
                ticker="AAPL",
                entry_zone=(170.0, 175.0),
                target_price=target,
                stop_loss=160.0,
                risk_reward_ratio=2.0,
                catalyst="AI",
                catalyst_date=None,
                conviction=8,
                summary="thesis",
            )
            analysis = AnalysisResult(results=[], confidence=0.9, iterations=1, trade_thesis=thesis)
            with patch.object(engine._standard_handler, "handle", new=AsyncMock()) as mh:
                from qracer.conversation.handlers import HandlerResult

                mh.return_value = HandlerResult(text="R", analysis=analysis)
                await engine.query("AAPL")

        open_theses = fact_store.get_open_theses(["AAPL"])
        assert len(open_theses) == 1
        assert open_theses[0].target_price == 220.0

        fact_store.close()

    async def test_no_crash_without_fact_store(self) -> None:
        """Engine without a fact_store should work normally."""
        intent_resp = json.dumps({"intent": "price_check", "tickers": ["AAPL"]})
        llm = _mock_llm_registry({Role.RESEARCHER: intent_resp})
        engine = ConversationEngine(llm, DataRegistry())

        with patch("qracer.conversation.handlers.invoke_tools") as mock_invoke:
            mock_invoke.return_value = [_ok_result("price_event")]
            response = await engine.query("AAPL price")

        assert response.text  # Should produce a response without crashing


# ---------------------------------------------------------------------------
# Persistent ConversationContext across sessions
# ---------------------------------------------------------------------------


class TestPersistentContext:
    async def test_context_file_written_after_query(self, tmp_path) -> None:
        """Engine should write context.json after processing a query."""
        from qracer.memory.session_logger import SessionLogger

        context_path = tmp_path / "context.json"
        session_logger = SessionLogger(tmp_path / "session.jsonl")

        intent_resp = json.dumps({"intent": "event_analysis", "tickers": ["AAPL"]})
        llm = _mock_llm_registry({Role.RESEARCHER: intent_resp, Role.STRATEGIST: "Response"})
        engine = ConversationEngine(
            llm, DataRegistry(), session_logger=session_logger, context_path=context_path
        )

        with patch("qracer.conversation.handlers.invoke_tools") as mock_invoke:
            mock_invoke.return_value = [_ok_result("price_event")]
            await engine.query("Analyze AAPL")

        assert context_path.exists()
        data = json.loads(context_path.read_text())
        assert "AAPL" in data["topic_stack"]
        assert data["current_topic"] == "AAPL"

    async def test_context_resumed_in_new_engine(self, tmp_path) -> None:
        """A fresh engine pointed at the same context_path should surface
        prior-session topics via the topic_stack."""
        from qracer.memory.session_logger import SessionLogger

        context_path = tmp_path / "context.json"

        # First session: discuss AAPL.
        session1 = SessionLogger(tmp_path / "session1.jsonl")
        llm1 = _mock_llm_registry(
            {
                Role.RESEARCHER: json.dumps({"intent": "event_analysis", "tickers": ["AAPL"]}),
                Role.STRATEGIST: "R",
            }
        )
        engine1 = ConversationEngine(
            llm1, DataRegistry(), session_logger=session1, context_path=context_path
        )
        with patch("qracer.conversation.handlers.invoke_tools") as mock_invoke:
            mock_invoke.return_value = [_ok_result("price_event")]
            await engine1.query("Analyze AAPL")

        # Second session: no log, brand-new engine picks up the persisted stack.
        session2 = SessionLogger(tmp_path / "session2.jsonl")
        llm2 = _mock_llm_registry(
            {
                Role.RESEARCHER: json.dumps({"intent": "macro_query", "tickers": []}),
                Role.STRATEGIST: "R",
            }
        )
        engine2 = ConversationEngine(
            llm2, DataRegistry(), session_logger=session2, context_path=context_path
        )

        assert "AAPL" in engine2._context.topic_stack
        assert engine2._context.current_topic == "AAPL"

    async def test_stale_context_is_decayed_on_load(self, tmp_path) -> None:
        """A context file older than 7 days should come back empty."""
        from qracer.conversation.context_store import save_context
        from qracer.memory.session_logger import SessionLogger

        context_path = tmp_path / "context.json"
        stale_ctx = ConversationContext(
            current_topic="AAPL",
            topic_stack=["AAPL", "TSLA"],
            last_activity=datetime.now() - timedelta(days=30),
        )
        save_context(stale_ctx, context_path)

        llm = _mock_llm_registry(
            {Role.RESEARCHER: json.dumps({"intent": "macro_query", "tickers": []})}
        )
        engine = ConversationEngine(
            llm,
            DataRegistry(),
            session_logger=SessionLogger(tmp_path / "s.jsonl"),
            context_path=context_path,
        )

        assert engine._context.topic_stack == []
        assert engine._context.current_topic is None

    async def test_open_theses_merged_into_topic_stack(self, tmp_path) -> None:
        """Open FactStore theses should surface in the topic_stack on load."""
        from qracer.memory.fact_store import FactStore
        from qracer.memory.session_logger import SessionLogger
        from qracer.models.base import TradeThesis

        fact_store = FactStore()
        fact_store.save_thesis(
            TradeThesis(
                ticker="NVDA",
                entry_zone=(400.0, 420.0),
                target_price=500.0,
                stop_loss=380.0,
                risk_reward_ratio=4.0,
                catalyst="AI demand",
                catalyst_date=None,
                conviction=9,
                summary="Long NVDA",
            ),
            session_id="prior",
        )

        llm = _mock_llm_registry(
            {Role.RESEARCHER: json.dumps({"intent": "macro_query", "tickers": []})}
        )
        engine = ConversationEngine(
            llm,
            DataRegistry(),
            session_logger=SessionLogger(tmp_path / "s.jsonl"),
            context_path=tmp_path / "context.json",
            fact_store=fact_store,
        )

        assert "NVDA" in engine._context.topic_stack
        fact_store.close()

    async def test_no_context_path_is_noop(self) -> None:
        """Engine without a context_path should not crash on query."""
        llm = _mock_llm_registry(
            {
                Role.RESEARCHER: json.dumps({"intent": "macro_query", "tickers": []}),
                Role.STRATEGIST: "R",
            }
        )
        engine = ConversationEngine(llm, DataRegistry())  # context_path defaults to None

        with patch("qracer.conversation.handlers.invoke_tools") as mock_invoke:
            mock_invoke.return_value = []
            response = await engine.query("Macro question")

        assert response.text  # no crash, no context written
