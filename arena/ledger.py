"""SQLite ledger: evidence packs, LLM cache, decisions, outcomes.

Everything is keyed by content hash so re-running a step after a crash or restart is a
no-op rather than a duplicate. ponytail: SQLite in WAL mode; move to Postgres if more than
one writer process ever exists.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
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
"""


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Ledger:
    def __init__(self, path: str = "arena.db"):
        self.conn = sqlite3.connect(path, isolation_level=None)  # autocommit
        self.conn.row_factory = sqlite3.Row
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
        return [dict(r) for r in self.conn.execute(sql, params + (limit,)).fetchall()]

    # outcomes -------------------------------------------------------------------------
    def save_outcome(self, decision_id: str, outcome_json: str) -> None:
        self.conn.execute("INSERT OR REPLACE INTO outcomes VALUES (?,?,?)", (decision_id, now_iso(), outcome_json))

    def get_outcome(self, decision_id: str) -> str | None:
        row = self.conn.execute("SELECT outcome_json FROM outcomes WHERE decision_id=?", (decision_id,)).fetchone()
        return row["outcome_json"] if row else None

    def list_outcomes(self) -> list[str]:
        return [r["outcome_json"] for r in self.conn.execute("SELECT outcome_json FROM outcomes").fetchall()]

    def unresolved(self) -> list[str]:
        rows = self.conn.execute(
            "SELECT d.id FROM decisions d LEFT JOIN outcomes o ON o.decision_id=d.id WHERE o.decision_id IS NULL ORDER BY d.created_at"
        ).fetchall()
        return [r["id"] for r in rows]
