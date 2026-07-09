"""FactStore — structured fact persistence for cross-session memory.

Manages trade theses (Phase 1), findings, and session digests in DuckDB.
Follows the same connection pattern as ``storage/repositories.py``.

The store uses its own DuckDB file (``fact_store.duckdb``), separate from
``memory_index.duckdb``, so the existing :class:`MemorySearcher` is
completely unaffected.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta
from pathlib import Path

import duckdb

from qracer.memory.fact_models import Finding, PersistedThesis, ThesisStatus
from qracer.models.base import TradeThesis

logger = logging.getLogger(__name__)

_SCHEMA_SQL = """\
CREATE SEQUENCE IF NOT EXISTS thesis_id_seq START 1;

CREATE TABLE IF NOT EXISTS theses (
    id                INTEGER PRIMARY KEY DEFAULT nextval('thesis_id_seq'),
    ticker            VARCHAR NOT NULL,
    entry_zone_low    DOUBLE NOT NULL,
    entry_zone_high   DOUBLE NOT NULL,
    target_price      DOUBLE NOT NULL,
    stop_loss         DOUBLE NOT NULL,
    risk_reward_ratio DOUBLE NOT NULL,
    catalyst          VARCHAR NOT NULL,
    catalyst_date     VARCHAR,
    conviction        INTEGER NOT NULL,
    summary           VARCHAR NOT NULL,
    status            VARCHAR NOT NULL DEFAULT 'open',
    session_id        VARCHAR NOT NULL,
    created_at        TIMESTAMP NOT NULL,
    updated_at        TIMESTAMP NOT NULL,
    superseded_by     INTEGER
);

CREATE SEQUENCE IF NOT EXISTS finding_id_seq START 1;

CREATE TABLE IF NOT EXISTS findings (
    id          INTEGER PRIMARY KEY DEFAULT nextval('finding_id_seq'),
    entity      VARCHAR NOT NULL,
    statement   VARCHAR NOT NULL,
    confidence  DOUBLE NOT NULL,
    source_tool VARCHAR NOT NULL,
    session_id  VARCHAR NOT NULL,
    event_date  VARCHAR,
    created_at  TIMESTAMP NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_findings_entity ON findings(entity);
"""


def _parse_catalyst_date(raw: str | None) -> datetime | None:
    """Best-effort parse of catalyst_date strings into a datetime.

    Handles ISO dates (``2026-05-01``), quarter notation (``Q2 2026``),
    and year-month (``2026-05``).  Returns ``None`` for unparseable values.
    """
    if not raw:
        return None
    raw = raw.strip()
    # ISO date: 2026-05-01
    try:
        return datetime.fromisoformat(raw)
    except ValueError:
        pass
    # Quarter: Q1/Q2/Q3/Q4 2026
    m = re.match(r"Q([1-4])\s*(\d{4})", raw, re.IGNORECASE)
    if m:
        quarter, year = int(m.group(1)), int(m.group(2))
        month = (quarter - 1) * 3 + 1
        return datetime(year, month, 1)
    # Year-month: 2026-05
    m = re.match(r"(\d{4})-(\d{2})$", raw)
    if m:
        return datetime(int(m.group(1)), int(m.group(2)), 1)
    return None


def _row_to_thesis(row: tuple) -> PersistedThesis:
    """Convert a DuckDB row tuple to a PersistedThesis."""
    return PersistedThesis(
        id=row[0],
        ticker=row[1],
        entry_zone_low=row[2],
        entry_zone_high=row[3],
        target_price=row[4],
        stop_loss=row[5],
        risk_reward_ratio=row[6],
        catalyst=row[7],
        catalyst_date=row[8],
        conviction=row[9],
        summary=row[10],
        status=ThesisStatus(row[11]),
        session_id=row[12],
        created_at=row[13],
        updated_at=row[14],
        superseded_by=row[15],
    )


_SELECT_COLUMNS = (
    "id, ticker, entry_zone_low, entry_zone_high, target_price, stop_loss, "
    "risk_reward_ratio, catalyst, catalyst_date, conviction, summary, "
    "status, session_id, created_at, updated_at, superseded_by"
)


class FactStore:
    """Structured fact storage for cross-session memory.

    Usage::

        store = FactStore(Path("~/.qracer/fact_store.duckdb"))
        thesis_id = store.save_thesis(trade_thesis, session_id="abc123")
        open_theses = store.get_open_theses(["AAPL"])
        store.close()
    """

    def __init__(self, path: str | Path | None = None) -> None:
        db_path = str(path) if path else ":memory:"
        self._conn = duckdb.connect(db_path)
        self._conn.execute(_SCHEMA_SQL)

    @property
    def connection(self) -> duckdb.DuckDBPyConnection:
        return self._conn

    # ------------------------------------------------------------------
    # Thesis CRUD
    # ------------------------------------------------------------------

    def save_thesis(self, thesis: TradeThesis, session_id: str) -> int:
        """Persist a TradeThesis with automatic supersession handling.

        If an open thesis exists for the same ticker, it is marked as
        ``superseded`` and linked to the new thesis via ``superseded_by``.

        Returns the new thesis id.
        """
        now = datetime.now()

        # Insert the new thesis first to get its id.
        self._conn.execute(
            """
            INSERT INTO theses (
                ticker, entry_zone_low, entry_zone_high, target_price,
                stop_loss, risk_reward_ratio, catalyst, catalyst_date,
                conviction, summary, status, session_id,
                created_at, updated_at, superseded_by
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                thesis.ticker,
                thesis.entry_zone[0],
                thesis.entry_zone[1],
                thesis.target_price,
                thesis.stop_loss,
                thesis.risk_reward_ratio,
                thesis.catalyst,
                thesis.catalyst_date,
                thesis.conviction,
                thesis.summary,
                ThesisStatus.OPEN.value,
                session_id,
                now,
                now,
                None,  # superseded_by
            ],
        )

        new_id: int = self._conn.execute("SELECT currval('thesis_id_seq')").fetchone()[0]  # type: ignore[index]

        # Supersede any prior open theses on the same ticker.
        self._conn.execute(
            """
            UPDATE theses
            SET status = ?, superseded_by = ?, updated_at = ?
            WHERE ticker = ? AND status = ? AND id != ?
            """,
            [
                ThesisStatus.SUPERSEDED.value,
                new_id,
                now,
                thesis.ticker,
                ThesisStatus.OPEN.value,
                new_id,
            ],
        )

        return new_id

    def get_open_theses(self, tickers: list[str] | None = None) -> list[PersistedThesis]:
        """Get all open theses, optionally filtered by ticker list."""
        if tickers:
            placeholders = ", ".join("?" for _ in tickers)
            rows = self._conn.execute(
                f"SELECT {_SELECT_COLUMNS} FROM theses "
                f"WHERE status = 'open' AND ticker IN ({placeholders}) "
                "ORDER BY created_at DESC",
                tickers,
            ).fetchall()
        else:
            rows = self._conn.execute(
                f"SELECT {_SELECT_COLUMNS} FROM theses "
                "WHERE status = 'open' ORDER BY created_at DESC",
            ).fetchall()
        return [_row_to_thesis(r) for r in rows]

    def get_upcoming_catalysts(self, days_ahead: int = 14) -> list[PersistedThesis]:
        """Get open theses with catalyst_date within *days_ahead* of today.

        Best-effort date parsing: ISO dates, quarter notation (``Q2 2026``),
        and year-month (``2026-05``) are supported.  Unparseable dates are
        excluded.
        """
        all_open = self.get_open_theses()
        cutoff = datetime.now() + timedelta(days=days_ahead)
        now = datetime.now()
        result: list[PersistedThesis] = []
        for t in all_open:
            dt = _parse_catalyst_date(t.catalyst_date)
            if dt is not None and now <= dt <= cutoff:
                result.append(t)
        return result

    def get_thesis_history(self, ticker: str, limit: int = 10) -> list[PersistedThesis]:
        """Get all theses for a ticker (all statuses), most recent first."""
        rows = self._conn.execute(
            f"SELECT {_SELECT_COLUMNS} FROM theses "
            "WHERE ticker = ? ORDER BY created_at DESC LIMIT ?",
            [ticker, limit],
        ).fetchall()
        return [_row_to_thesis(r) for r in rows]

    def update_thesis_status(
        self,
        thesis_id: int,
        status: ThesisStatus,
        *,
        superseded_by: int | None = None,
    ) -> None:
        """Update a thesis status (close, invalidate, supersede)."""
        self._conn.execute(
            "UPDATE theses SET status = ?, superseded_by = ?, updated_at = ? WHERE id = ?",
            [status.value, superseded_by, datetime.now(), thesis_id],
        )

    # ------------------------------------------------------------------
    # Finding CRUD
    # ------------------------------------------------------------------

    def save_finding(
        self,
        *,
        entity: str,
        statement: str,
        confidence: float,
        source_tool: str,
        session_id: str,
        event_date: str | None = None,
    ) -> int:
        """Persist a Finding and return its new id.

        Confidence is clamped to the ``[0.0, 1.0]`` range; upstream extractors
        may emit out-of-range values when parsing noisy inputs.
        """
        clamped_confidence = max(0.0, min(1.0, float(confidence)))
        self._conn.execute(
            """
            INSERT INTO findings (
                entity, statement, confidence, source_tool,
                session_id, event_date, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            [
                entity,
                statement,
                clamped_confidence,
                source_tool,
                session_id,
                event_date,
                datetime.now(),
            ],
        )
        new_id: int = self._conn.execute("SELECT currval('finding_id_seq')").fetchone()[0]  # type: ignore[index]
        return new_id

    def get_findings(self, entity: str, limit: int = 20) -> list[Finding]:
        """Return findings for *entity* ordered most-recent first."""
        rows = self._conn.execute(
            """
            SELECT id, entity, statement, confidence, source_tool,
                   session_id, event_date, created_at
            FROM findings
            WHERE entity = ?
            ORDER BY created_at DESC
            LIMIT ?
            """,
            [entity, limit],
        ).fetchall()
        return [
            Finding(
                id=row[0],
                entity=row[1],
                statement=row[2],
                confidence=row[3],
                source_tool=row[4],
                session_id=row[5],
                event_date=row[6],
                created_at=row[7],
            )
            for row in rows
        ]

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> FactStore:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()
