"""Tests for Finding extraction from ToolResult data."""

from __future__ import annotations

from qracer.memory.finding_extractor import FindingDraft, extract_findings
from qracer.models import ToolResult


def _ok(tool: str, data: dict) -> ToolResult:
    return ToolResult(tool=tool, success=True, data=data, source="test")


def _fail(tool: str) -> ToolResult:
    return ToolResult(tool=tool, success=False, data={}, source="test", error="boom")


class TestFailureAndUnknown:
    def test_failed_tool_result_yields_nothing(self) -> None:
        assert extract_findings(_fail("news")) == []

    def test_unknown_tool_yields_nothing(self) -> None:
        assert extract_findings(_ok("price_event", {"ticker": "AAPL"})) == []


class TestTradeThesisExtraction:
    def _thesis(self, **overrides) -> dict:
        thesis = {
            "ticker": "AAPL",
            "entry_zone": [170.0, 175.0],
            "target_price": 200.0,
            "stop_loss": 160.0,
            "risk_reward_ratio": 2.2,
            "catalyst": "Q2 earnings beat",
            "catalyst_date": "2026-05-01",
            "conviction": 8,
            "summary": "Long AAPL.",
        }
        thesis.update(overrides)
        return {"thesis": thesis}

    def test_extracts_basic_thesis(self) -> None:
        drafts = extract_findings(_ok("trade_thesis", self._thesis()))
        assert len(drafts) == 1
        draft = drafts[0]
        assert isinstance(draft, FindingDraft)
        assert draft.entity == "AAPL"
        assert draft.source_tool == "trade_thesis"
        assert draft.event_date == "2026-05-01"
        assert "Q2 earnings beat" in draft.statement
        assert "conviction 8/10" in draft.statement
        assert draft.confidence == 0.8

    def test_missing_catalyst_skips(self) -> None:
        drafts = extract_findings(_ok("trade_thesis", self._thesis(catalyst="")))
        assert drafts == []

    def test_conviction_clamped_to_unit_interval(self) -> None:
        drafts = extract_findings(_ok("trade_thesis", self._thesis(conviction=15)))
        assert drafts[0].confidence == 1.0


class TestNewsExtraction:
    def _news(self, articles: list[dict]) -> dict:
        return {"ticker": "AAPL", "count": len(articles), "articles": articles}

    def test_extracts_article_findings_with_sentiment_confidence(self) -> None:
        data = self._news(
            [
                {
                    "title": "Earnings beat expectations",
                    "source": "Reuters",
                    "published_at": "2026-05-01T10:00:00",
                    "sentiment": "positive",
                    "summary": "",
                    "url": "https://example.com/1",
                },
                {
                    "title": "Supply chain warning",
                    "source": "Bloomberg",
                    "published_at": "2026-05-02T10:00:00",
                    "sentiment": "negative",
                    "summary": "",
                    "url": "https://example.com/2",
                },
                {
                    "title": "Analyst day recap",
                    "source": "WSJ",
                    "published_at": "2026-05-03T10:00:00",
                    "sentiment": "neutral",
                    "summary": "",
                    "url": "https://example.com/3",
                },
            ]
        )
        drafts = extract_findings(_ok("news", data))
        assert len(drafts) == 3
        assert all(d.entity == "AAPL" and d.source_tool == "news" for d in drafts)
        assert drafts[0].confidence == 0.7  # positive
        assert drafts[1].confidence == 0.7  # negative
        assert drafts[2].confidence == 0.4  # neutral
        assert "Earnings beat" in drafts[0].statement
        assert "(Reuters)" in drafts[0].statement
        assert drafts[0].event_date == "2026-05-01T10:00:00"

    def test_caps_at_three_articles(self) -> None:
        articles = [
            {
                "title": f"t{i}",
                "source": "src",
                "published_at": None,
                "sentiment": None,
                "summary": "",
                "url": "",
            }
            for i in range(10)
        ]
        drafts = extract_findings(_ok("news", self._news(articles)))
        assert len(drafts) == 3

    def test_unlabeled_sentiment_uses_default_confidence(self) -> None:
        data = self._news(
            [
                {
                    "title": "title",
                    "source": "src",
                    "published_at": None,
                    "sentiment": None,
                    "summary": "",
                    "url": "",
                }
            ]
        )
        drafts = extract_findings(_ok("news", data))
        assert drafts[0].confidence == 0.5
        assert "[news]" in drafts[0].statement

    def test_empty_articles_yields_nothing(self) -> None:
        assert extract_findings(_ok("news", self._news([]))) == []

    def test_missing_title_is_skipped(self) -> None:
        data = self._news(
            [
                {"title": None, "source": "src", "sentiment": "positive"},
                {"title": "real", "source": "src", "sentiment": "positive"},
            ]
        )
        drafts = extract_findings(_ok("news", data))
        assert len(drafts) == 1
        assert "real" in drafts[0].statement


class TestFundamentalsExtraction:
    def test_extracts_summary_finding(self) -> None:
        data = {
            "ticker": "AAPL",
            "pe_ratio": 29.5,
            "market_cap": 3_000_000_000_000,
            "revenue": 400_000_000_000,
            "earnings": 100_000_000_000,
            "dividend_yield": 0.005,
        }
        drafts = extract_findings(_ok("fundamentals", data))
        assert len(drafts) == 1
        draft = drafts[0]
        assert draft.entity == "AAPL"
        assert draft.source_tool == "fundamentals"
        assert draft.confidence == 0.9
        assert "P/E 29.50" in draft.statement
        assert "dividend yield 0.50%" in draft.statement

    def test_partial_fundamentals_still_produces_finding(self) -> None:
        data = {"ticker": "AAPL", "pe_ratio": 29.5}
        drafts = extract_findings(_ok("fundamentals", data))
        assert len(drafts) == 1
        assert "P/E 29.50" in drafts[0].statement
        assert "market cap" not in drafts[0].statement

    def test_empty_fundamentals_yields_nothing(self) -> None:
        drafts = extract_findings(_ok("fundamentals", {"ticker": "AAPL"}))
        assert drafts == []

    def test_missing_ticker_yields_nothing(self) -> None:
        drafts = extract_findings(_ok("fundamentals", {"pe_ratio": 10}))
        assert drafts == []
