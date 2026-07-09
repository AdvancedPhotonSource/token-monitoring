"""SQLite storage for request usage rows and API-key labels.

Single-writer (the proxy) + many-reader (the dashboard). WAL mode so
readers never block writers. Contention is trivial at expected volumes.
"""
from __future__ import annotations

import sqlite3
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator, Optional


_SCHEMA = """
CREATE TABLE IF NOT EXISTS requests (
    id                    INTEGER PRIMARY KEY,
    ts_utc                TEXT    NOT NULL,
    user_hash             TEXT    NOT NULL,
    model                 TEXT    NOT NULL,
    input_tokens          INTEGER,
    output_tokens         INTEGER,
    cache_read_tokens     INTEGER,
    cache_creation_tokens INTEGER,
    latency_ms            INTEGER,
    status_code           INTEGER NOT NULL,
    endpoint              TEXT    NOT NULL,
    streamed              INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_requests_ts       ON requests(ts_utc);
CREATE INDEX IF NOT EXISTS ix_requests_user_day ON requests(user_hash, substr(ts_utc, 1, 10));
CREATE INDEX IF NOT EXISTS ix_requests_model    ON requests(model);

CREATE TABLE IF NOT EXISTS key_labels (
    user_hash    TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    updated_at   TEXT NOT NULL
);
"""


@dataclass(frozen=True)
class UsageRow:
    ts_utc: str
    user_hash: str
    model: str
    input_tokens: Optional[int]
    output_tokens: Optional[int]
    cache_read_tokens: Optional[int]
    cache_creation_tokens: Optional[int]
    latency_ms: Optional[int]
    status_code: int
    endpoint: str
    streamed: bool


class UsageStore:
    """Thread-safe SQLite wrapper. One connection guarded by a lock;
    plenty for the expected write rate (single-digit inserts/sec at
    peak). Dashboard reads use short-lived cursors and don't block
    each other in WAL mode."""

    def __init__(self, db_path: Path) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(
            str(self.db_path), check_same_thread=False, timeout=10.0,
        )
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            # WAL so dashboard reads don't lock out proxy writes.
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
            self._conn.executescript(_SCHEMA)
            self._conn.commit()

    # ----- writes -------------------------------------------------------

    def insert_request(self, row: UsageRow) -> int:
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO requests "
                "  (ts_utc, user_hash, model, input_tokens, output_tokens, "
                "   cache_read_tokens, cache_creation_tokens, latency_ms, "
                "   status_code, endpoint, streamed) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    row.ts_utc, row.user_hash, row.model,
                    row.input_tokens, row.output_tokens,
                    row.cache_read_tokens, row.cache_creation_tokens,
                    row.latency_ms, row.status_code,
                    row.endpoint, 1 if row.streamed else 0,
                ),
            )
            self._conn.commit()
            return int(cur.lastrowid or 0)

    def upsert_label(self, user_hash: str, display_name: str) -> None:
        now = _now_iso()
        with self._lock:
            self._conn.execute(
                "INSERT INTO key_labels (user_hash, display_name, updated_at) "
                "VALUES (?, ?, ?) "
                "ON CONFLICT(user_hash) DO UPDATE SET "
                "  display_name = excluded.display_name, "
                "  updated_at   = excluded.updated_at",
                (user_hash, display_name, now),
            )
            self._conn.commit()

    def get_label(self, user_hash: str) -> Optional[str]:
        with self._lock:
            row = self._conn.execute(
                "SELECT display_name FROM key_labels WHERE user_hash = ?",
                (user_hash,),
            ).fetchone()
        return row["display_name"] if row else None

    def all_labels(self) -> dict[str, str]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT user_hash, display_name FROM key_labels"
            ).fetchall()
        return {r["user_hash"]: r["display_name"] for r in rows}

    # ----- queries ------------------------------------------------------

    def tokens_per_day(self, days: int = 30) -> list[dict]:
        """Sum of input+output tokens per UTC day for the last `days`."""
        since = _iso_days_ago(days)
        with self._lock:
            rows = self._conn.execute(
                "SELECT substr(ts_utc, 1, 10) AS day, "
                "       COALESCE(SUM(input_tokens), 0)  AS input_tokens, "
                "       COALESCE(SUM(output_tokens), 0) AS output_tokens, "
                "       COALESCE(SUM(cache_read_tokens), 0)     AS cache_read_tokens, "
                "       COALESCE(SUM(cache_creation_tokens), 0) AS cache_creation_tokens, "
                "       COUNT(*) AS n "
                "  FROM requests WHERE ts_utc >= ? "
                "GROUP BY day ORDER BY day",
                (since,),
            ).fetchall()
        return [dict(r) for r in rows]

    def top_users(self, n: int = 10, days: int = 30) -> list[dict]:
        since = _iso_days_ago(days)
        with self._lock:
            rows = self._conn.execute(
                "SELECT user_hash, "
                "       COALESCE(SUM(input_tokens), 0)  AS input_tokens, "
                "       COALESCE(SUM(output_tokens), 0) AS output_tokens, "
                "       COUNT(*) AS n "
                "  FROM requests WHERE ts_utc >= ? "
                "GROUP BY user_hash "
                "ORDER BY (COALESCE(SUM(input_tokens),0) + COALESCE(SUM(output_tokens),0)) DESC "
                "LIMIT ?",
                (since, n),
            ).fetchall()
        return [dict(r) for r in rows]

    def top_models(self, n: int = 10, days: int = 30) -> list[dict]:
        since = _iso_days_ago(days)
        with self._lock:
            rows = self._conn.execute(
                "SELECT model, "
                "       COALESCE(SUM(input_tokens), 0)  AS input_tokens, "
                "       COALESCE(SUM(output_tokens), 0) AS output_tokens, "
                "       COUNT(*) AS n "
                "  FROM requests WHERE ts_utc >= ? "
                "GROUP BY model "
                "ORDER BY (COALESCE(SUM(input_tokens),0) + COALESCE(SUM(output_tokens),0)) DESC "
                "LIMIT ?",
                (since, n),
            ).fetchall()
        return [dict(r) for r in rows]

    def current_month_totals(self) -> dict:
        since = _iso_month_start()
        with self._lock:
            row = self._conn.execute(
                "SELECT COALESCE(SUM(input_tokens), 0)          AS input_tokens, "
                "       COALESCE(SUM(output_tokens), 0)         AS output_tokens, "
                "       COALESCE(SUM(cache_read_tokens), 0)     AS cache_read_tokens, "
                "       COALESCE(SUM(cache_creation_tokens), 0) AS cache_creation_tokens, "
                "       COUNT(*) AS n "
                "  FROM requests WHERE ts_utc >= ?",
                (since,),
            ).fetchone()
        d = dict(row) if row else {"input_tokens": 0, "output_tokens": 0,
                                    "cache_read_tokens": 0, "cache_creation_tokens": 0, "n": 0}
        d["total_tokens"] = int(d.get("input_tokens", 0) or 0) + int(d.get("output_tokens", 0) or 0)
        d["since"] = since
        return d

    def list_requests(
        self,
        *,
        user_hash: Optional[str] = None,
        model: Optional[str] = None,
        since: Optional[str] = None,
        until: Optional[str] = None,
        limit: int = 250,
        offset: int = 0,
    ) -> list[dict]:
        clauses: list[str] = []
        params: list = []
        if user_hash:
            clauses.append("user_hash = ?")
            params.append(user_hash)
        if model:
            clauses.append("model = ?")
            params.append(model)
        if since:
            clauses.append("ts_utc >= ?")
            params.append(since)
        if until:
            clauses.append("ts_utc < ?")
            params.append(until)
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        sql = (
            "SELECT id, ts_utc, user_hash, model, input_tokens, output_tokens, "
            "       cache_read_tokens, cache_creation_tokens, latency_ms, "
            "       status_code, endpoint, streamed "
            f"  FROM requests {where} "
            "ORDER BY id DESC LIMIT ? OFFSET ?"
        )
        params.extend([int(limit), int(offset)])
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]

    def count_requests(
        self,
        *,
        user_hash: Optional[str] = None,
        model: Optional[str] = None,
        since: Optional[str] = None,
        until: Optional[str] = None,
    ) -> int:
        clauses: list[str] = []
        params: list = []
        if user_hash:
            clauses.append("user_hash = ?")
            params.append(user_hash)
        if model:
            clauses.append("model = ?")
            params.append(model)
        if since:
            clauses.append("ts_utc >= ?")
            params.append(since)
        if until:
            clauses.append("ts_utc < ?")
            params.append(until)
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        sql = f"SELECT COUNT(*) AS n FROM requests {where}"
        with self._lock:
            row = self._conn.execute(sql, params).fetchone()
        return int(row["n"]) if row else 0

    def user_summary(self, user_hash: str, days: int = 30) -> dict:
        since = _iso_days_ago(days)
        with self._lock:
            row = self._conn.execute(
                "SELECT COALESCE(SUM(input_tokens), 0)  AS input_tokens, "
                "       COALESCE(SUM(output_tokens), 0) AS output_tokens, "
                "       COALESCE(SUM(cache_read_tokens), 0)     AS cache_read_tokens, "
                "       COALESCE(SUM(cache_creation_tokens), 0) AS cache_creation_tokens, "
                "       COUNT(*) AS n "
                "  FROM requests WHERE user_hash = ? AND ts_utc >= ?",
                (user_hash, since),
            ).fetchone()
            per_model = self._conn.execute(
                "SELECT model, "
                "       COALESCE(SUM(input_tokens), 0)  AS input_tokens, "
                "       COALESCE(SUM(output_tokens), 0) AS output_tokens, "
                "       COUNT(*) AS n "
                "  FROM requests WHERE user_hash = ? AND ts_utc >= ? "
                "GROUP BY model "
                "ORDER BY (COALESCE(SUM(input_tokens),0) + COALESCE(SUM(output_tokens),0)) DESC",
                (user_hash, since),
            ).fetchall()
        totals = dict(row) if row else {}
        totals["since"] = since
        return {"totals": totals, "per_model": [dict(r) for r in per_model]}

    def close(self) -> None:
        with self._lock:
            self._conn.close()


# =========================================================================
# Module-level store; wired up by init_db() at app startup
# =========================================================================


_store: Optional[UsageStore] = None


def store() -> Optional[UsageStore]:
    return _store


def init_db(db_path: Path) -> UsageStore:
    global _store
    _store = UsageStore(db_path)
    return _store


def shutdown_db() -> None:
    global _store
    if _store is not None:
        _store.close()
    _store = None


# =========================================================================
# Small time helpers — kept here so tests can monkeypatch if needed
# =========================================================================


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _iso_days_ago(days: int) -> str:
    """UTC ISO8601 for midnight `days` days ago (inclusive of that day)."""
    now = datetime.now(timezone.utc)
    return (now.replace(hour=0, minute=0, second=0, microsecond=0)
            - _timedelta_days(days)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _iso_month_start() -> str:
    now = datetime.now(timezone.utc)
    return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)\
              .strftime("%Y-%m-%dT%H:%M:%SZ")


def _timedelta_days(days: int):
    from datetime import timedelta
    return timedelta(days=days)


# =========================================================================
# Test helper
# =========================================================================


from contextlib import contextmanager


@contextmanager
def override_store(new_store: UsageStore) -> Iterator[None]:
    global _store
    prev = _store
    _store = new_store
    try:
        yield
    finally:
        _store = prev
