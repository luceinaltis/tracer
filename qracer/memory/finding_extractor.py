"""Zero-LLM-cost extraction of Finding drafts from ToolResult data.

Called from :meth:`ConversationEngine._persist_facts` for each successful
tool result.  Parses structured ``ToolResult.data`` dicts — no LLM calls,
no expensive post-processing.

Extractors are registered per ``ToolResult.tool`` name.  Tools without a
registered extractor yield no findings (graceful degradation).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from qracer.models import ToolResult


@dataclass
class FindingDraft:
    """A Finding value ready to be persisted (no db id yet)."""

    entity: str
    statement: str
    confidence: float
    source_tool: str
    event_date: str | None = None


# Sentiment-string → confidence weight.  Directional sentiment (positive /
# negative) carries stronger signal than neutral / unlabeled news.
_SENTIMENT_CONFIDENCE: dict[str, float] = {
    "positive": 0.7,
    "negative": 0.7,
    "neutral": 0.4,
}

_DEFAULT_NEWS_CONFIDENCE = 0.5
_DEFAULT_FUNDAMENTALS_CONFIDENCE = 0.9
_MAX_NEWS_FINDINGS = 3


def _extract_thesis(data: dict[str, Any]) -> list[FindingDraft]:
    thesis = data.get("thesis") or {}
    ticker = thesis.get("ticker")
    catalyst = thesis.get("catalyst")
    conviction = thesis.get("conviction")
    if not ticker or not catalyst or conviction is None:
        return []
    statement = (
        f"{ticker}: {catalyst} — target ${thesis.get('target_price')}, "
        f"stop ${thesis.get('stop_loss')}, R/R {thesis.get('risk_reward_ratio')}x, "
        f"conviction {conviction}/10"
    )
    return [
        FindingDraft(
            entity=ticker,
            statement=statement,
            confidence=max(0.0, min(1.0, float(conviction) / 10.0)),
            source_tool="trade_thesis",
            event_date=thesis.get("catalyst_date"),
        )
    ]


def _extract_news(data: dict[str, Any]) -> list[FindingDraft]:
    ticker = data.get("ticker")
    articles = data.get("articles") or []
    if not ticker or not articles:
        return []
    drafts: list[FindingDraft] = []
    for art in articles[:_MAX_NEWS_FINDINGS]:
        title = art.get("title")
        if not title:
            continue
        raw_sentiment = art.get("sentiment")
        sentiment = (raw_sentiment or "").strip().lower()
        confidence = _SENTIMENT_CONFIDENCE.get(sentiment, _DEFAULT_NEWS_CONFIDENCE)
        source = art.get("source") or "unknown"
        label = sentiment or "news"
        drafts.append(
            FindingDraft(
                entity=ticker,
                statement=f"[{label}] {title} ({source})",
                confidence=confidence,
                source_tool="news",
                event_date=art.get("published_at"),
            )
        )
    return drafts


def _format_ratio(value: float) -> str:
    return f"{value:.2f}"


def _format_money(value: float) -> str:
    return f"${value:,.0f}"


def _format_percent(value: float) -> str:
    return f"{value:.2%}"


def _extract_fundamentals(data: dict[str, Any]) -> list[FindingDraft]:
    ticker = data.get("ticker")
    if not ticker:
        return []
    parts: list[str] = []
    if (pe := data.get("pe_ratio")) is not None:
        parts.append(f"P/E {_format_ratio(float(pe))}")
    if (mc := data.get("market_cap")) is not None:
        parts.append(f"market cap {_format_money(float(mc))}")
    if (rev := data.get("revenue")) is not None:
        parts.append(f"revenue {_format_money(float(rev))}")
    if (eps := data.get("earnings")) is not None:
        parts.append(f"earnings {_format_money(float(eps))}")
    if (dy := data.get("dividend_yield")) is not None:
        parts.append(f"dividend yield {_format_percent(float(dy))}")
    if not parts:
        return []
    return [
        FindingDraft(
            entity=ticker,
            statement=f"{ticker} fundamentals: " + ", ".join(parts),
            confidence=_DEFAULT_FUNDAMENTALS_CONFIDENCE,
            source_tool="fundamentals",
        )
    ]


_EXTRACTORS: dict[str, Callable[[dict[str, Any]], list[FindingDraft]]] = {
    "trade_thesis": _extract_thesis,
    "news": _extract_news,
    "fundamentals": _extract_fundamentals,
}


def extract_findings(tool_result: ToolResult) -> list[FindingDraft]:
    """Return zero-or-more FindingDrafts for a single ToolResult.

    Failed results, unknown tools, and extractor exceptions all yield an
    empty list so a bad payload never breaks the persistence pipeline.
    """
    if not tool_result.success:
        return []
    extractor = _EXTRACTORS.get(tool_result.tool)
    if extractor is None:
        return []
    try:
        return extractor(tool_result.data or {})
    except Exception:  # pragma: no cover - defensive guard
        return []
