"""Run history in SQLite (data/history.db): idempotency, picks, weekly changes
and pick performance.

Idempotency: `runs` has UNIQUE(run_date, run_type), and the CLI skips a run whose
(New York date, run type) is already recorded -- so the many cron triggers per
window can never send the same report twice. A run is recorded only after its
email was sent successfully (so a failed send is retried by the next trigger).

Monday reference price = the latest completed session's close available at the
Monday run (normally the previous Friday's close), stored per pick as ref_price.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from .config import PROJECT_ROOT

DEFAULT_DB_PATH = PROJECT_ROOT / "data" / "history.db"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    run_date         TEXT NOT NULL,
    run_type         TEXT NOT NULL CHECK (run_type IN ('monday', 'friday')),
    generated_at_utc TEXT NOT NULL,
    data_session     TEXT NOT NULL,
    benchmark_symbol TEXT NOT NULL,
    benchmark_ref    REAL,
    universe_size    INTEGER NOT NULL,
    UNIQUE (run_date, run_type)
);
CREATE TABLE IF NOT EXISTS picks (
    run_id      INTEGER NOT NULL REFERENCES runs(id),
    list        TEXT NOT NULL CHECK (list IN ('BUY', 'SELL', 'HOLD')),
    rank        INTEGER NOT NULL,
    symbol      TEXT NOT NULL,
    ticker      TEXT NOT NULL,
    description TEXT NOT NULL,
    sector      TEXT NOT NULL,
    ref_price   REAL NOT NULL,
    score       REAL NOT NULL,
    subscores   TEXT NOT NULL,
    rationale   TEXT NOT NULL,
    PRIMARY KEY (run_id, list, symbol)
);
CREATE TABLE IF NOT EXISTS weekly_changes (
    week_ending TEXT NOT NULL,
    symbol      TEXT NOT NULL,
    kind        TEXT NOT NULL CHECK (kind IN ('stock', 'index')),
    weekly_pct  REAL NOT NULL,
    PRIMARY KEY (week_ending, symbol)
);
CREATE TABLE IF NOT EXISTS pick_results (
    friday_run_id        INTEGER NOT NULL REFERENCES runs(id),
    monday_run_id        INTEGER NOT NULL REFERENCES runs(id),
    list                 TEXT NOT NULL CHECK (list IN ('BUY', 'SELL')),
    symbol               TEXT NOT NULL,
    ticker               TEXT NOT NULL,
    ref_price            REAL NOT NULL,
    end_price            REAL,
    return_pct           REAL,
    benchmark_return_pct REAL,
    beat_benchmark       INTEGER,
    PRIMARY KEY (friday_run_id, list, symbol)
);
CREATE TABLE IF NOT EXISTS alerts (
    run_date    TEXT NOT NULL,
    run_type    TEXT NOT NULL,
    sent_at_utc TEXT NOT NULL,
    PRIMARY KEY (run_date, run_type)
);
"""
# One failure email per run type per scheduled window. Each run type has exactly
# one window per New York date, so (run_date, run_type) identifies the window.
# scripts/notify_failure.py writes the same table with the same DDL (stdlib only).
ALERTS_DDL = _SCHEMA[_SCHEMA.index("CREATE TABLE IF NOT EXISTS alerts") :]


@dataclass(frozen=True)
class PickRecord:
    list: str
    rank: int
    symbol: str
    ticker: str
    description: str
    sector: str
    ref_price: float
    score: float
    subscores: Mapping[str, float]
    rationale: str


@dataclass(frozen=True)
class PickResult:
    list: str
    symbol: str
    ticker: str
    ref_price: float
    end_price: float | None
    return_pct: float | None
    benchmark_return_pct: float | None
    beat_benchmark: bool | None


@dataclass(frozen=True)
class RunRecord:
    run_date: date
    run_type: str
    generated_at_utc: str
    data_session: date
    benchmark_symbol: str
    benchmark_ref: float | None
    universe_size: int


@dataclass(frozen=True)
class StoredRun:
    id: int
    run_date: date
    run_type: str
    data_session: date
    benchmark_ref: float | None


class History:
    def __init__(self, path: Path = DEFAULT_DB_PATH) -> None:
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.executescript(_SCHEMA)

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        try:
            with conn:  # one transaction: commit on success, roll back on error
                yield conn
        finally:
            conn.close()

    def run_exists(self, run_date: date, run_type: str) -> bool:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM runs WHERE run_date = ? AND run_type = ?",
                (run_date.isoformat(), run_type),
            ).fetchone()
        return row is not None

    def record_run(
        self,
        run: RunRecord,
        picks: Sequence[PickRecord],
        *,
        weekly: Mapping[str, tuple[str, float]] | None = None,
        week_ending: date | None = None,
        results: Sequence[PickResult] = (),
        monday_run_id: int | None = None,
    ) -> int:
        """Store a completed run atomically. `weekly` maps symbol -> (kind, pct)."""

        with self._connect() as conn:
            cur = conn.execute(
                "INSERT INTO runs (run_date, run_type, generated_at_utc, data_session, "
                "benchmark_symbol, benchmark_ref, universe_size) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    run.run_date.isoformat(),
                    run.run_type,
                    run.generated_at_utc,
                    run.data_session.isoformat(),
                    run.benchmark_symbol,
                    run.benchmark_ref,
                    run.universe_size,
                ),
            )
            run_id = int(cur.lastrowid or 0)
            conn.executemany(
                "INSERT INTO picks VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    (
                        run_id,
                        p.list,
                        p.rank,
                        p.symbol,
                        p.ticker,
                        p.description,
                        p.sector,
                        p.ref_price,
                        p.score,
                        json.dumps(dict(p.subscores), sort_keys=True),
                        p.rationale,
                    )
                    for p in picks
                ],
            )
            if weekly and week_ending is not None:
                conn.executemany(
                    "INSERT OR REPLACE INTO weekly_changes VALUES (?, ?, ?, ?)",
                    [
                        (week_ending.isoformat(), sym, kind, pct)
                        for sym, (kind, pct) in weekly.items()
                    ],
                )
            if results:
                if monday_run_id is None:
                    raise ValueError("pick results need the Monday run id")
                conn.executemany(
                    "INSERT INTO pick_results VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    [
                        (
                            run_id,
                            monday_run_id,
                            r.list,
                            r.symbol,
                            r.ticker,
                            r.ref_price,
                            r.end_price,
                            r.return_pct,
                            r.benchmark_return_pct,
                            None if r.beat_benchmark is None else int(r.beat_benchmark),
                        )
                        for r in results
                    ],
                )
        return run_id

    def monday_run_between(self, start: date, end: date) -> StoredRun | None:
        """The Monday run recorded with run_date in [start, end] (latest if several)."""

        with self._connect() as conn:
            row = conn.execute(
                "SELECT id, run_date, run_type, data_session, benchmark_ref FROM runs "
                "WHERE run_type = 'monday' AND run_date BETWEEN ? AND ? "
                "ORDER BY run_date DESC, id DESC LIMIT 1",
                (start.isoformat(), end.isoformat()),
            ).fetchone()
        if row is None:
            return None
        return StoredRun(
            int(row["id"]),
            date.fromisoformat(row["run_date"]),
            str(row["run_type"]),
            date.fromisoformat(row["data_session"]),
            row["benchmark_ref"],
        )

    def picks_for(self, run_id: int, lists: Sequence[str] = ("BUY", "SELL")) -> list[PickRecord]:
        placeholders = ", ".join("?" for _ in lists)
        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT * FROM picks WHERE run_id = ? AND list IN ({placeholders}) "
                "ORDER BY list, rank",
                (run_id, *lists),
            ).fetchall()
        return [
            PickRecord(
                r["list"],
                int(r["rank"]),
                r["symbol"],
                r["ticker"],
                r["description"],
                r["sector"],
                float(r["ref_price"]),
                float(r["score"]),
                json.loads(r["subscores"]),
                r["rationale"],
            )
            for r in rows
        ]

    def alert_already_sent(self, run_date: date, run_type: str) -> bool:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM alerts WHERE run_date = ? AND run_type = ?",
                (run_date.isoformat(), run_type),
            ).fetchone()
        return row is not None

    def record_alert(self, run_date: date, run_type: str, sent_at_utc: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO alerts VALUES (?, ?, ?)",
                (run_date.isoformat(), run_type, sent_at_utc),
            )

    def stored_weekly(self, week_ending: date, kind: str) -> dict[str, float]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT symbol, weekly_pct FROM weekly_changes WHERE week_ending = ? AND kind = ?",
                (week_ending.isoformat(), kind),
            ).fetchall()
        return {str(r["symbol"]): float(r["weekly_pct"]) for r in rows}

    def all_results(self, *, excluding_friday_on: date | None = None) -> list[PickResult]:
        """Every stored pick result (optionally excluding one Friday's run)."""

        sql = "SELECT pr.* FROM pick_results pr JOIN runs r ON r.id = pr.friday_run_id" + (
            " WHERE r.run_date <> ?" if excluding_friday_on else ""
        )
        params = (excluding_friday_on.isoformat(),) if excluding_friday_on else ()
        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [
            PickResult(
                r["list"],
                r["symbol"],
                r["ticker"],
                float(r["ref_price"]),
                r["end_price"],
                r["return_pct"],
                r["benchmark_return_pct"],
                None if r["beat_benchmark"] is None else bool(r["beat_benchmark"]),
            )
            for r in rows
        ]
