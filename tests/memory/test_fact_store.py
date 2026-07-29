"""Tests for FactStore — structured fact persistence."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime, timedelta

import pytest

from qracer.memory.fact_models import ThesisStatus
from qracer.memory.fact_store import FactStore, _parse_catalyst_date
from qracer.models.base import TradeThesis


def _make_thesis(
    ticker: str = "AAPL",
    entry_zone: tuple[float, float] = (170.0, 175.0),
    target_price: float = 200.0,
    stop_loss: float = 160.0,
    conviction: int = 8,
    catalyst: str = "Q2 earnings beat",
    catalyst_date: str | None = None,
) -> TradeThesis:
    return TradeThesis(
        ticker=ticker,
        entry_zone=entry_zone,
        target_price=target_price,
        stop_loss=stop_loss,
        risk_reward_ratio=(target_price - 172.5) / (172.5 - stop_loss),
        catalyst=catalyst,
        catalyst_date=catalyst_date,
        conviction=conviction,
        summary=f"Long {ticker} thesis",
    )


@pytest.fixture
def fact_store() -> Iterator[FactStore]:
    store = FactStore()  # in-memory DuckDB
    yield store
    store.close()


# ---------------------------------------------------------------------------
# Thesis CRUD
# ---------------------------------------------------------------------------


class TestThesisCRUD:
    def test_save_and_get_open_thesis(self, fact_store: FactStore) -> None:
        thesis = _make_thesis()
        thesis_id = fact_store.save_thesis(thesis, session_id="sess_001")

        assert thesis_id >= 1
        open_theses = fact_store.get_open_theses(["AAPL"])
        assert len(open_theses) == 1
        t = open_theses[0]
        assert t.ticker == "AAPL"
        assert t.entry_zone_low == 170.0
        assert t.entry_zone_high == 175.0
        assert t.target_price == 200.0
        assert t.stop_loss == 160.0
        assert t.conviction == 8
        assert t.status == ThesisStatus.OPEN
        assert t.session_id == "sess_001"

    def test_supersession(self, fact_store: FactStore) -> None:
        """Saving a new thesis for the same ticker supersedes the old one."""
        old_id = fact_store.save_thesis(_make_thesis(), session_id="sess_001")
        new_id = fact_store.save_thesis(
            _make_thesis(target_price=220.0, conviction=9),
            session_id="sess_002",
        )

        # Only the new thesis is open.
        open_theses = fact_store.get_open_theses(["AAPL"])
        assert len(open_theses) == 1
        assert open_theses[0].id == new_id
        assert open_theses[0].target_price == 220.0

        # Old thesis is superseded.
        history = fact_store.get_thesis_history("AAPL")
        old = [t for t in history if t.id == old_id][0]
        assert old.status == ThesisStatus.SUPERSEDED
        assert old.superseded_by == new_id

    def test_get_open_theses_filters_by_ticker(self, fact_store: FactStore) -> None:
        fact_store.save_thesis(_make_thesis("AAPL"), session_id="s1")
        fact_store.save_thesis(_make_thesis("MSFT"), session_id="s1")
        fact_store.save_thesis(_make_thesis("TSLA"), session_id="s1")

        result = fact_store.get_open_theses(["AAPL", "TSLA"])
        tickers = {t.ticker for t in result}
        assert tickers == {"AAPL", "TSLA"}

    def test_get_open_theses_all(self, fact_store: FactStore) -> None:
        """get_open_theses() with no filter returns all open theses."""
        fact_store.save_thesis(_make_thesis("AAPL"), session_id="s1")
        fact_store.save_thesis(_make_thesis("MSFT"), session_id="s1")

        result = fact_store.get_open_theses()
        assert len(result) == 2

    def test_get_open_theses_excludes_closed(self, fact_store: FactStore) -> None:
        tid = fact_store.save_thesis(_make_thesis(), session_id="s1")
        fact_store.update_thesis_status(tid, ThesisStatus.CLOSED)

        assert fact_store.get_open_theses(["AAPL"]) == []

    def test_update_thesis_status(self, fact_store: FactStore) -> None:
        tid = fact_store.save_thesis(_make_thesis(), session_id="s1")
        fact_store.update_thesis_status(tid, ThesisStatus.INVALIDATED)

        history = fact_store.get_thesis_history("AAPL")
        assert history[0].status == ThesisStatus.INVALIDATED

    def test_get_thesis_history_ordered_desc(self, fact_store: FactStore) -> None:
        fact_store.save_thesis(_make_thesis(conviction=5), session_id="s1")
        fact_store.save_thesis(_make_thesis(conviction=9), session_id="s2")

        history = fact_store.get_thesis_history("AAPL")
        assert len(history) == 2
        # Most recent first (conviction 9 was inserted second).
        assert history[0].conviction == 9
        assert history[1].conviction == 5

    def test_different_tickers_not_superseded(self, fact_store: FactStore) -> None:
        """Theses for different tickers are independent."""
        fact_store.save_thesis(_make_thesis("AAPL"), session_id="s1")
        fact_store.save_thesis(_make_thesis("MSFT"), session_id="s1")

        aapl = fact_store.get_open_theses(["AAPL"])
        msft = fact_store.get_open_theses(["MSFT"])
        assert len(aapl) == 1
        assert len(msft) == 1


# ---------------------------------------------------------------------------
# Catalyst date parsing
# ---------------------------------------------------------------------------


class TestCatalystDateParsing:
    def test_iso_date(self) -> None:
        assert _parse_catalyst_date("2026-05-01") == datetime(2026, 5, 1)

    def test_quarter_notation(self) -> None:
        assert _parse_catalyst_date("Q2 2026") == datetime(2026, 4, 1)
        assert _parse_catalyst_date("Q1 2026") == datetime(2026, 1, 1)
        assert _parse_catalyst_date("Q3 2026") == datetime(2026, 7, 1)
        assert _parse_catalyst_date("Q4 2026") == datetime(2026, 10, 1)

    def test_year_month(self) -> None:
        assert _parse_catalyst_date("2026-05") == datetime(2026, 5, 1)

    def test_none_input(self) -> None:
        assert _parse_catalyst_date(None) is None

    def test_unparseable(self) -> None:
        assert _parse_catalyst_date("sometime next year") is None

    def test_get_upcoming_catalysts(self, fact_store: FactStore) -> None:
        tomorrow = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d")
        far_future = (datetime.now() + timedelta(days=365)).strftime("%Y-%m-%d")

        fact_store.save_thesis(_make_thesis("NEAR", catalyst_date=tomorrow), session_id="s1")
        fact_store.save_thesis(_make_thesis("FAR", catalyst_date=far_future), session_id="s1")
        fact_store.save_thesis(_make_thesis("NONE", catalyst_date=None), session_id="s1")

        upcoming = fact_store.get_upcoming_catalysts(days_ahead=14)
        assert len(upcoming) == 1
        assert upcoming[0].ticker == "NEAR"


# ---------------------------------------------------------------------------
# Finding CRUD
# ---------------------------------------------------------------------------


class TestFindingCRUD:
    def test_save_and_get_finding(self, fact_store: FactStore) -> None:
        fid = fact_store.save_finding(
            entity="AAPL",
            statement="iPhone sales up 12% YoY",
            confidence=0.8,
            source_tool="fundamentals",
            session_id="sess_001",
        )
        assert fid >= 1

        findings = fact_store.get_findings("AAPL")
        assert len(findings) == 1
        f = findings[0]
        assert f.entity == "AAPL"
        assert f.statement == "iPhone sales up 12% YoY"
        assert f.confidence == 0.8
        assert f.source_tool == "fundamentals"
        assert f.session_id == "sess_001"
        assert f.event_date is None

    def test_save_finding_clamps_confidence(self, fact_store: FactStore) -> None:
        fact_store.save_finding(
            entity="AAPL",
            statement="noisy",
            confidence=2.5,
            source_tool="news",
            session_id="s1",
        )
        fact_store.save_finding(
            entity="AAPL",
            statement="also noisy",
            confidence=-0.3,
            source_tool="news",
            session_id="s1",
        )
        confidences = sorted(f.confidence for f in fact_store.get_findings("AAPL"))
        assert confidences == [0.0, 1.0]

    def test_get_findings_filters_by_entity(self, fact_store: FactStore) -> None:
        fact_store.save_finding(
            entity="AAPL",
            statement="a1",
            confidence=0.5,
            source_tool="news",
            session_id="s1",
        )
        fact_store.save_finding(
            entity="MSFT",
            statement="m1",
            confidence=0.5,
            source_tool="news",
            session_id="s1",
        )
        aapl = fact_store.get_findings("AAPL")
        msft = fact_store.get_findings("MSFT")
        tsla = fact_store.get_findings("TSLA")
        assert [f.statement for f in aapl] == ["a1"]
        assert [f.statement for f in msft] == ["m1"]
        assert tsla == []

    def test_get_findings_ordered_desc(self, fact_store: FactStore) -> None:
        for label in ("first", "second", "third"):
            fact_store.save_finding(
                entity="AAPL",
                statement=label,
                confidence=0.5,
                source_tool="news",
                session_id="s1",
            )
        findings = fact_store.get_findings("AAPL")
        assert [f.statement for f in findings] == ["third", "second", "first"]

    def test_get_findings_respects_limit(self, fact_store: FactStore) -> None:
        for i in range(5):
            fact_store.save_finding(
                entity="AAPL",
                statement=f"n{i}",
                confidence=0.5,
                source_tool="news",
                session_id="s1",
            )
        assert len(fact_store.get_findings("AAPL", limit=2)) == 2

    def test_save_finding_preserves_event_date(self, fact_store: FactStore) -> None:
        fact_store.save_finding(
            entity="AAPL",
            statement="earnings announced",
            confidence=0.7,
            source_tool="news",
            session_id="s1",
            event_date="2026-05-01",
        )
        [finding] = fact_store.get_findings("AAPL")
        assert finding.event_date == "2026-05-01"


# ---------------------------------------------------------------------------
# Context manager
# ---------------------------------------------------------------------------


class TestContextManager:
    def test_context_manager_protocol(self) -> None:
        with FactStore() as store:
            store.save_thesis(_make_thesis(), session_id="s1")
            assert len(store.get_open_theses()) == 1
        # Connection closed after __exit__, no assertion needed — just no crash.
