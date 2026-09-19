"""SQLite ledger: evidence packs, LLM cache, decisions, outcomes.

Everything is keyed by content hash so re-running a step after a crash or restart is a
no-op rather than a duplicate. ponytail: SQLite in WAL mode; move to Postgres if more than
one writer process ever exists.
"""

from __future__ import annotations

import os
import sqlite3
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS evidence (
  pack_hash TEXT PRIMARY KEY, symbol TEXT NOT NULL, source TEXT NOT NULL,
  created_at TEXT NOT NULL, pack_json TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS llm_cache (
  cache_key TEXT PRIMARY KEY, pack_hash TEXT NOT NULL, role TEXT NOT NULL,
  prompt_version TEXT NOT NULL, model TEXT NOT NULL, output_json TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS decisions (
  id TEXT PRIMARY KEY, pack_hash TEXT NOT NULL, symbol TEXT NOT NULL, model TEXT NOT NULL,
  created_at TEXT NOT NULL, receipt_json TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS outcomes (
  decision_id TEXT PRIMARY KEY, resolved_at TEXT NOT NULL, outcome_json TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS decisions_symbol ON decisions(symbol, created_at);
CREATE TABLE IF NOT EXISTS backings (
  decision_id TEXT NOT NULL, handle TEXT NOT NULL, stance TEXT NOT NULL, created_at TEXT NOT NULL,
  PRIMARY KEY (decision_id, handle));
CREATE TABLE IF NOT EXISTS locks (
  id TEXT PRIMARY KEY, symbol TEXT NOT NULL, locked_at TEXT NOT NULL, status TEXT NOT NULL, row_json TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS locks_locked_at ON locks(locked_at);
CREATE TABLE IF NOT EXISTS settlements (
  lock_id TEXT NOT NULL, horizon_h INTEGER NOT NULL, settled_at TEXT NOT NULL, result_json TEXT NOT NULL,
  PRIMARY KEY (lock_id, horizon_h));
CREATE TABLE IF NOT EXISTS handle_claims (
  handle TEXT PRIMARY KEY, token_sha256 TEXT NOT NULL, created_at TEXT NOT NULL);
"""


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Ledger:
    def __init__(self, path: str = "nota.db", readonly: bool | None = None):
        """`readonly` (or env NOTA_READONLY=1) opens a shipped snapshot immutably, e.g. on a serverless host
        whose filesystem cannot be written; every write method then raises instead of pretending."""
        self.readonly = bool(readonly if readonly is not None else os.environ.get("NOTA_READONLY") == "1") and path != ":memory:"
        if self.readonly:
            self.conn = sqlite3.connect(f"file:{Path(path).as_posix()}?mode=ro&immutable=1", uri=True, isolation_level=None)
        else:
            self.conn = sqlite3.connect(path, isolation_level=None)  # autocommit
        self.conn.row_factory = sqlite3.Row
        if not self.readonly:
            if path != ":memory:":
                self.conn.execute("PRAGMA journal_mode=WAL")
            self.conn.executescript(SCHEMA)

    # evidence ------------------------------------------------------------------------
    def save_pack(self, pack_hash: str, symbol: str, source: str, pack_json: str) -> None:
        self.conn.execute(
            "INSERT OR IGNORE INTO evidence VALUES (?,?,?,?,?)", (pack_hash, symbol, source, now_iso(), pack_json)
        )

    def get_pack(self, pack_hash: str) -> str | None:
        row = self.conn.execute("SELECT pack_json FROM evidence WHERE pack_hash=?", (pack_hash,)).fetchone()
        return row["pack_json"] if row else None

    # llm cache ------------------------------------------------------------------------
    def get_cached(self, cache_key: str) -> str | None:
        row = self.conn.execute("SELECT output_json FROM llm_cache WHERE cache_key=?", (cache_key,)).fetchone()
        return row["output_json"] if row else None

    def put_cached(self, cache_key: str, pack_hash: str, role: str, prompt_version: str, model: str, output_json: str) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO llm_cache VALUES (?,?,?,?,?,?,?)",
            (cache_key, pack_hash, role, prompt_version, model, output_json, now_iso()),
        )

    # decisions ------------------------------------------------------------------------
    def save_decision(self, id: str, pack_hash: str, symbol: str, model: str, receipt_json: str) -> None:
        self.conn.execute("INSERT OR REPLACE INTO decisions VALUES (?,?,?,?,?,?)", (id, pack_hash, symbol, model, now_iso(), receipt_json))

    def get_decision(self, id: str) -> str | None:
        row = self.conn.execute("SELECT receipt_json FROM decisions WHERE id=?", (id,)).fetchone()
        return row["receipt_json"] if row else None

    def list_decisions(self, limit: int = 50, symbol: str | None = None) -> list[dict[str, Any]]:
        sql = "SELECT id, pack_hash, symbol, model, created_at FROM decisions"
        params: tuple[Any, ...] = ()
        if symbol:
            sql += " WHERE symbol=?"
            params = (symbol,)
        sql += " ORDER BY created_at DESC LIMIT ?"
        limit = max(1, min(int(limit), 200))  # SQLite reads LIMIT -1 as "no limit"
        return [dict(r) for r in self.conn.execute(sql, params + (limit,)).fetchall()]

    # outcomes -------------------------------------------------------------------------
    def save_outcome(self, decision_id: str, outcome_json: str) -> None:
        self.conn.execute("INSERT OR REPLACE INTO outcomes VALUES (?,?,?)", (decision_id, now_iso(), outcome_json))

    def get_outcome(self, decision_id: str) -> str | None:
        row = self.conn.execute("SELECT outcome_json FROM outcomes WHERE decision_id=?", (decision_id,)).fetchone()
        return row["outcome_json"] if row else None

    def list_outcomes(self) -> list[str]:
        return [r["outcome_json"] for r in self.conn.execute("SELECT outcome_json FROM outcomes").fetchall()]

    # backings (SocialFi) ------------------------------------------------------------
    def add_backing(self, decision_id: str, handle: str, stance: str) -> None:
        """One stance per handle per decision; backing again replaces the earlier stance."""
        self.conn.execute("INSERT OR REPLACE INTO backings VALUES (?,?,?,?)", (decision_id, handle, stance, now_iso()))

    def backings(self, decision_id: str) -> list[dict[str, Any]]:
        rows = self.conn.execute("SELECT handle, stance, created_at FROM backings WHERE decision_id=? ORDER BY created_at", (decision_id,))
        return [dict(r) for r in rows.fetchall()]

    def all_backings(self) -> list[dict[str, Any]]:
        return [dict(r) for r in self.conn.execute("SELECT decision_id, handle, stance, created_at FROM backings").fetchall()]

    def claim(self, handle: str, token_sha256: str) -> bool:
        """True when this call took the handle, False when it was already held (nota.backings)."""
        return self.conn.execute("INSERT OR IGNORE INTO handle_claims VALUES (?,?,?)",
                                 (handle, token_sha256, now_iso())).rowcount == 1

    def token_sha(self, handle: str) -> str | None:
        row = self.conn.execute("SELECT token_sha256 FROM handle_claims WHERE handle=?", (handle,)).fetchone()
        return row["token_sha256"] if row else None

    def unresolved(self) -> list[str]:
        rows = self.conn.execute(
            "SELECT d.id FROM decisions d LEFT JOIN outcomes o ON o.decision_id=d.id WHERE o.decision_id IS NULL ORDER BY d.created_at"
        ).fetchall()
        return [r["id"] for r in rows]

    # scorecard: RYO's own plans, locked daily and settled later (nota.scorecard) -----------
    def save_lock(self, id: str, symbol: str, locked_at: str, status: str, row_json: str) -> None:
        self.conn.execute("INSERT OR IGNORE INTO locks VALUES (?,?,?,?,?)", (id, symbol, locked_at, status, row_json))

    def replace_lock(self, id: str, symbol: str, locked_at: str, status: str, row_json: str) -> None:
        """A failed or aborted lock keeps one row per symbol-day: a retry overwrites it instead of adding one."""
        self.conn.execute("INSERT OR REPLACE INTO locks VALUES (?,?,?,?,?)", (id, symbol, locked_at, status, row_json))

    def delete_lock(self, id: str) -> None:
        self.conn.execute("DELETE FROM locks WHERE id=?", (id,))

    def list_locks(self, day: str | None = None) -> list[tuple[str, str]]:
        """All locks, or one UTC day's (`YYYY-MM-DD`), oldest first."""
        sql, params = "SELECT id, row_json FROM locks", ()
        if day:
            nxt = (date.fromisoformat(day) + timedelta(days=1)).isoformat()
            sql, params = sql + " WHERE locked_at >= ? AND locked_at < ?", (day, nxt)
        return [(r["id"], r["row_json"]) for r in self.conn.execute(sql + " ORDER BY locked_at", params).fetchall()]

    def unsettled_locks(self) -> list[tuple[str, str]]:
        rows = self.conn.execute(
            "SELECT id, row_json FROM locks l WHERE status='locked' AND "
            "(SELECT COUNT(*) FROM settlements s WHERE s.lock_id=l.id) < 2 ORDER BY locked_at").fetchall()  # 2 = len(scorecard.HORIZONS_H)
        return [(r["id"], r["row_json"]) for r in rows]

    def save_settlement(self, lock_id: str, horizon_h: int, result_json: str) -> None:
        self.conn.execute("INSERT OR REPLACE INTO settlements VALUES (?,?,?,?)", (lock_id, horizon_h, now_iso(), result_json))

    def get_settlement(self, lock_id: str, horizon_h: int) -> str | None:
        row = self.conn.execute("SELECT result_json FROM settlements WHERE lock_id=? AND horizon_h=?", (lock_id, horizon_h)).fetchone()
        return row["result_json"] if row else None
