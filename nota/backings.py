"""Where a backing lives when the ledger itself cannot be written.

The hosted deployment reads its ledger from a snapshot on a filesystem it cannot write to, so
backing answered 503 there: a feature that worked everywhere except the one place anyone could try
it. A backing is four columns and one rule - one stance per handle per receipt, the latest wins -
which is a single upsert. When `DATABASE_URL` is set it goes to Postgres and the ledger stays the
read-only thing it is; with no `DATABASE_URL` the ledger's own table is used exactly as before, so
nothing local needs a database to run or to test.

The upsert matters more than the store. Reading a map of handles, changing one and writing it back
would lose a vote whenever two people backed at the same moment, and a public record that silently
drops someone's stance is worse than no public record.
"""

from __future__ import annotations

import math
import os
from typing import Any, Protocol

SCHEMA = """
CREATE TABLE IF NOT EXISTS backings (
  decision_id text NOT NULL,
  handle      text NOT NULL,
  stance      text NOT NULL,
  created_at  timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (decision_id, handle)
);
CREATE TABLE IF NOT EXISTS handle_claims (
  handle       text PRIMARY KEY,
  token_sha256 text NOT NULL,
  created_at   timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS hits (
  key text NOT NULL,
  at  timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS hits_key_at ON hits (key, at)
"""


class BackingStore(Protocol):
    """What the API needs, and all it needs. `Ledger` already satisfies it."""

    def add_backing(self, decision_id: str, handle: str, stance: str) -> None: ...
    def backings(self, decision_id: str) -> list[dict[str, Any]]: ...
    def all_backings(self) -> list[dict[str, Any]]: ...
    def claim(self, handle: str, token_sha256: str) -> bool: ...
    def token_sha(self, handle: str) -> str | None: ...


# Which DSNs this process has already created the table on. It belongs to the process, not to an
# instance: `store_for` builds a new object per request, so an instance flag would have meant a
# CREATE TABLE round trip on every receipt anyone opened.
_SCHEMA_READY: set[str] = set()


class PostgresBackings:
    """One connection per call. A serverless function is not a long-lived process, and Neon's
    pooled URL is built for exactly this; holding a socket open between requests would only give
    the next invocation a dead one."""

    def __init__(self, dsn: str) -> None:
        self.dsn = dsn

    def _connect(self):
        import psycopg
        from psycopg.rows import dict_row

        conn = psycopg.connect(self.dsn, row_factory=dict_row, connect_timeout=10)
        if self.dsn not in _SCHEMA_READY:
            with conn.cursor() as cur:
                cur.execute(SCHEMA)
            conn.commit()
            _SCHEMA_READY.add(self.dsn)
        return conn

    @staticmethod
    def _rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        # SQLite hands back ISO strings; the API and its tests expect the same shape from either.
        return [{**r, "created_at": r["created_at"].isoformat()} for r in rows]

    def add_backing(self, decision_id: str, handle: str, stance: str) -> None:
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                "INSERT INTO backings (decision_id, handle, stance) VALUES (%s, %s, %s) "
                "ON CONFLICT (decision_id, handle) DO UPDATE SET stance = EXCLUDED.stance, created_at = now()",
                (decision_id, handle, stance),
            )

    def backings(self, decision_id: str) -> list[dict[str, Any]]:
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute("SELECT handle, stance, created_at FROM backings WHERE decision_id = %s ORDER BY created_at",
                        (decision_id,))
            return self._rows(cur.fetchall())

    def all_backings(self) -> list[dict[str, Any]]:
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute("SELECT decision_id, handle, stance, created_at FROM backings")
            return self._rows(cur.fetchall())

    # handle claims: the first writer of a handle gets a token, and only that token writes it again
    def claim(self, handle: str, token_sha256: str) -> bool:
        """True when this call took the handle; False when someone already holds it. One statement,
        so two first posts at the same moment cannot both win."""
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute("INSERT INTO handle_claims (handle, token_sha256) VALUES (%s, %s) ON CONFLICT (handle) DO NOTHING",
                        (handle, token_sha256))
            return cur.rowcount == 1

    def token_sha(self, handle: str) -> str | None:
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute("SELECT token_sha256 FROM handle_claims WHERE handle = %s", (handle,))
            row = cur.fetchone()
            return row["token_sha256"] if row else None

    # the rate limit, shared by every serverless instance instead of counted once per instance
    def hit(self, key: str, cost: int, limit: int, window_s: float) -> tuple[bool, int]:
        """Charge `cost` hits to `key` if they fit in the window. Returns (allowed, retry_after_s).

        The advisory lock serialises callers on one key, so two instances cannot both read 59 and
        both insert. ponytail: expired rows of every key go on each call; the table only ever holds
        one window of traffic, so that delete stays small."""
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (key,))
            cur.execute("DELETE FROM hits WHERE at < now() - make_interval(secs => %s)", (window_s,))
            cur.execute("SELECT count(*) AS n, extract(epoch FROM min(at) + make_interval(secs => %s) - now()) AS wait "
                        "FROM hits WHERE key = %s", (window_s, key))
            row = cur.fetchone()
            if row["n"] + cost > limit:
                return False, max(1, math.ceil(row["wait"] if row["wait"] is not None else window_s))
            cur.execute("INSERT INTO hits (key) SELECT %s FROM generate_series(1, %s)", (key, cost))
            return True, 0


def store_for(ledger: BackingStore) -> BackingStore:
    """Postgres when one is configured, otherwise the ledger's own table."""
    dsn = os.environ.get("DATABASE_URL", "").strip()
    return PostgresBackings(dsn) if dsn else ledger
