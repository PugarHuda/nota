"""Backing had one real hole: it worked everywhere except the deployment anyone could try it on.

These cover the choice - Postgres when one is configured, the ledger's own table otherwise - and
the rule that 503 is for having nowhere to write, not for being a read-only ledger.
"""

from pathlib import Path

from fastapi.testclient import TestClient

from nota import api
from nota.backings import PostgresBackings, store_for
from nota.decide import decide
from nota.ledger import Ledger
from nota.ryo_client import RecordedRyoClient
from tests.test_decide_replay import make_llm

FIXTURES = Path(__file__).parent / "fixtures"


def _seed(tmp_path, monkeypatch) -> str:
    db = str(tmp_path / "b.db")
    monkeypatch.setenv("NOTA_DB", db)
    led = Ledger(db)
    id = decide("SOL", RecordedRyoClient(FIXTURES, name="fixture"), make_llm(), led).id
    # An immutable open ignores the WAL, so a readonly reader would not even see the schema.
    led.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    led.conn.close()
    return id


def test_the_ledger_is_the_store_until_a_database_is_configured(monkeypatch):
    led = Ledger(":memory:")
    monkeypatch.delenv("DATABASE_URL", raising=False)
    assert store_for(led) is led
    monkeypatch.setenv("DATABASE_URL", "   ")          # set-but-empty is not configured
    assert store_for(led) is led
    monkeypatch.setenv("DATABASE_URL", "postgresql://user:pw@host/db")
    chosen = store_for(led)
    assert isinstance(chosen, PostgresBackings) and chosen.dsn.endswith("/db")


def test_a_read_only_ledger_with_a_database_can_still_be_backed(tmp_path, monkeypatch):
    """The 503 exists because there was nowhere to write. With Postgres there is, and the snapshot
    stays untouched - which is the whole point of keeping the two apart."""
    id = _seed(tmp_path, monkeypatch)
    monkeypatch.setenv("NOTA_READONLY", "1")

    written: list[tuple[str, str, str]] = []

    class Fake:
        def add_backing(self, decision_id, handle, stance):
            written.append((decision_id, handle, stance))

        def backings(self, decision_id):
            return [{"handle": h, "stance": s, "created_at": "2026-09-11T00:00:00+00:00"}
                    for d, h, s in written if d == decision_id]

        def all_backings(self):
            return [{"decision_id": d, "handle": h, "stance": s, "created_at": "x"} for d, h, s in written]

    monkeypatch.setattr(api, "store_for", lambda led: Fake())
    api._BACKING_HITS.clear()
    c = TestClient(api.app)
    r = c.post(f"/api/decisions/{id}/back", json={"handle": "someone", "stance": "agree"})
    assert r.status_code == 200 and r.json()["agree"] == 1
    assert written == [(id, "someone", "agree")]


def test_without_anywhere_to_write_it_says_so_instead_of_pretending(tmp_path, monkeypatch):
    id = _seed(tmp_path, monkeypatch)
    monkeypatch.setenv("NOTA_READONLY", "1")
    monkeypatch.delenv("DATABASE_URL", raising=False)
    api._BACKING_HITS.clear()
    r = TestClient(api.app).post(f"/api/decisions/{id}/back", json={"handle": "someone", "stance": "agree"})
    assert r.status_code == 503 and "DATABASE_URL" in r.json()["detail"]


def test_the_upsert_is_one_statement_so_a_simultaneous_backer_cannot_be_lost():
    """Read-modify-write on a map of handles would drop a stance whenever two people backed at the
    same moment. This pins the shape of the write rather than the database it runs on."""
    sql = PostgresBackings.add_backing.__doc__ or ""
    import inspect

    source = inspect.getsource(PostgresBackings.add_backing)
    assert source.count("cur.execute(") == 1
    assert "ON CONFLICT (decision_id, handle) DO UPDATE" in source
    assert "SELECT" not in source.upper().replace("SELECT EXCLUDED", ""), "a read before the write is a lost update"
    assert sql == sql  # doc is optional here; the statement is the contract


def test_the_table_is_created_once_per_process_not_once_per_request(monkeypatch):
    """`store_for` builds a new object per request. A per-instance flag would have put a CREATE
    TABLE round trip in front of every receipt anyone opened."""
    import psycopg

    from nota import backings

    backings._SCHEMA_READY.clear()
    verbs: list[str] = []

    class Cursor:
        def execute(self, sql, params=None):
            verbs.append(sql.split()[0].upper())

        def fetchall(self):
            return []

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    class Conn:
        def cursor(self):
            return Cursor()

        def commit(self):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr(psycopg, "connect", lambda *a, **kw: Conn())
    for _ in range(3):
        backings.PostgresBackings("postgresql://x/y").all_backings()

    assert verbs.count("CREATE") == 1 and verbs.count("SELECT") == 3
