"""The SocialFi layer past backing: one reputation board for humans and agents, the reasoning feed,
the public watchlist and a profile per handle. Offline, against a seeded ledger."""

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from nota import api, calibration
from nota.calibration import Outcome
from nota.ledger import Ledger
from tests.test_api import _seed


@pytest.fixture(autouse=True)
def _fresh_budget(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    api._BACKING_HITS.clear()
    yield
    api._BACKING_HITS.clear()


def _resolve(tmp_path, id, up=True):
    Ledger(str(tmp_path / "t.db")).save_outcome(id, Outcome(
        decision_id=id, symbol="SOL", resolved_at="x", decided_as_of=None, horizon_reached=True, price_then=150.0,
        price_now=170.0 if up else 130.0, return_pct=13.3 if up else -13.3, went_up=up,
        brier={"macro": 0.09, "technician": 0.09, "narrative": 0.09, "judge": 0.09}).model_dump_json())


def test_humans_and_agents_share_one_board_and_too_few_calls_are_never_ranked(tmp_path, monkeypatch):
    first, second = _seed(tmp_path, monkeypatch)          # second: long; first: no_trade
    c = TestClient(api.app)
    c.post(f"/api/decisions/{second.id}/back", json={"handle": "alice", "stance": "agree"})
    c.post(f"/api/decisions/{second.id}/back", json={"handle": "bob", "stance": "disagree"})
    c.post(f"/api/decisions/{first.id}/back", json={"handle": "carol", "stance": "agree"})   # no_trade: never counted
    _resolve(tmp_path, second.id, up=True)
    board = c.get("/api/reputation").json()
    rows = {(r["kind"], r["name"]): r for r in board["rows"]}
    assert {("agent", "macro"), ("agent", "technician"), ("agent", "narrative"), ("agent", "judge"),
            ("human", "alice"), ("human", "bob")} <= set(rows)
    assert ("human", "carol") not in rows
    assert rows[("human", "alice")] == {"kind": "human", "name": "alice", "n": 1, "hits": 1, "hit_rate": 1.0, "n_independent": 1,
                                        "enough_to_read": False, "brier_mean": None, "rank": None}
    assert rows[("human", "bob")]["hits"] == 0
    assert rows[("agent", "judge")]["n"] == 1 and rows[("agent", "judge")]["brier_mean"] == 0.09
    assert all(r["rank"] is None for r in board["rows"]) and board["meaningful_at"] == calibration.MEANINGFUL_N


def test_a_long_independent_record_is_ranked_and_a_burst_of_calls_in_one_week_is_not(tmp_path, monkeypatch):
    """Twenty calls on SOL two minutes apart are one look at SOL's next week; twenty a week apart are twenty."""
    _, second = _seed(tmp_path, monkeypatch)
    t0 = datetime(2026, 1, 1, tzinfo=timezone.utc)

    def pairs(step):
        return [({"went_up": True, "brier": {"judge": 0.1}},
                 second.model_copy(update={"id": f"d{i}", "created_at": (t0 + step * i).isoformat()})) for i in range(20)]

    monkeypatch.setattr(calibration, "_scored_outcomes", lambda led: pairs(timedelta(days=8)))
    backs = [{"decision_id": f"d{i}", "handle": "alice", "stance": "agree" if i % 4 else "disagree"} for i in range(20)]
    rows = {r["name"]: r for r in calibration.reputation(Ledger(":memory:"), backs)["rows"]}
    assert rows["alice"]["enough_to_read"] and rows["alice"]["hit_rate"] == 0.75 and rows["alice"]["rank"] is not None
    assert rows["judge"]["hit_rate"] == 1.0 and rows["judge"]["rank"] < rows["alice"]["rank"]   # higher rate ranks first

    monkeypatch.setattr(calibration, "_scored_outcomes", lambda led: pairs(timedelta(minutes=2)))
    rows = {r["name"]: r for r in calibration.reputation(Ledger(":memory:"), backs)["rows"]}
    assert rows["alice"]["n"] == 20 and rows["alice"]["n_independent"] == 1
    assert not rows["alice"]["enough_to_read"] and rows["alice"]["rank"] is None


def test_the_watchlist_claims_handles_like_backing_and_aggregates(tmp_path, monkeypatch):
    _, second = _seed(tmp_path, monkeypatch)
    c = TestClient(api.app)
    first = c.post("/api/watchlist", json={"handle": "@Alice", "symbol": " sol "})
    assert first.status_code == 200 and first.json()["symbols"] == ["SOL"] and first.json()["edit_token"]
    token = first.json()["edit_token"]
    r = c.post("/api/watchlist", json={"handle": "alice", "symbol": "ETH"})          # no token: someone else
    assert r.status_code == 409 and r.json()["detail"] == "handle already claimed; send its edit token"
    assert c.post("/api/watchlist", json={"handle": "alice", "symbol": "ETH", "token": "guess"}).status_code == 409
    ok = c.post("/api/watchlist", json={"handle": "alice", "symbol": "eth", "token": token}).json()
    assert ok == {"handle": "alice", "symbols": ["SOL", "ETH"]}                      # token only on the claiming write
    # the same token is the one backing uses: one handle, one claim
    assert c.post(f"/api/decisions/{second.id}/back", json={"handle": "alice", "stance": "agree", "token": token}).status_code == 200
    c.post("/api/watchlist", json={"handle": "bob", "symbol": "SOL"})
    for bad in ("SOL/USDT", "", "<script>", "X" * 16):
        assert c.post("/api/watchlist", json={"handle": "bob", "symbol": bad}).status_code == 422, bad
    assert c.post("/api/watchlist", json={"handle": "x", "symbol": "SOL"}).status_code == 422
    agg = c.get("/api/watchlist").json()
    assert agg == {"symbols": [{"symbol": "SOL", "watchers": 2, "handles": ["alice", "bob"]},
                               {"symbol": "ETH", "watchers": 1, "handles": ["alice"]}], "handles": 2}
    gone = c.post("/api/watchlist", json={"handle": "alice", "symbol": "SOL", "watch": False, "token": token}).json()
    assert gone["symbols"] == ["ETH"]
    assert c.post("/api/watchlist", json={"handle": "alice", "symbol": "SOL", "watch": False, "token": token}).status_code == 200


def test_a_watchlist_is_capped(tmp_path, monkeypatch):
    _seed(tmp_path, monkeypatch)
    monkeypatch.setattr(api, "WATCH_MAX", 2)
    monkeypatch.setattr(api, "BACKING_LIMIT", 100)
    c = TestClient(api.app)
    token = c.post("/api/watchlist", json={"handle": "alice", "symbol": "SOL"}).json()["edit_token"]
    c.post("/api/watchlist", json={"handle": "alice", "symbol": "ETH", "token": token})
    r = c.post("/api/watchlist", json={"handle": "alice", "symbol": "BTC", "token": token})
    assert r.status_code == 422 and "at most 2" in r.json()["detail"]
    assert c.post("/api/watchlist", json={"handle": "alice", "symbol": "SOL", "token": token}).status_code == 200  # already on it


def test_watchlist_writes_share_the_backing_throttle_and_the_read_only_rule(tmp_path, monkeypatch):
    import sqlite3
    import time

    _seed(tmp_path, monkeypatch)
    c = TestClient(api.app)
    api._BACKING_HITS["testclient"] = [time.time()] * api.BACKING_LIMIT
    r = c.post("/api/watchlist", json={"handle": "alice", "symbol": "SOL"})
    assert r.status_code == 429 and int(r.headers["retry-after"]) > 0
    api._BACKING_HITS.clear()

    sqlite3.connect(str(tmp_path / "t.db")).execute(f"VACUUM INTO '{(tmp_path / 'snap.db').as_posix()}'")
    monkeypatch.setenv("NOTA_DB", str(tmp_path / "snap.db"))
    monkeypatch.setenv("NOTA_READONLY", "1")
    r = c.post("/api/watchlist", json={"handle": "alice", "symbol": "SOL"})
    assert r.status_code == 503 and "read-only" in r.json()["detail"]
    assert c.get("/api/watchlist").json() == {"symbols": [], "handles": 0}        # reads still answer


def test_a_snapshot_from_before_the_watchlist_reads_as_empty(tmp_path):
    db = tmp_path / "old.db"
    led = Ledger(str(db))
    led.conn.execute("DROP TABLE watchlist")
    led.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")   # an immutable open ignores the WAL
    led.conn.close()
    old = Ledger(str(db), readonly=True)
    assert "watchlist" not in {r[0] for r in old.conn.execute("SELECT name FROM sqlite_master")}
    assert old.watches() == []


def test_the_feed_names_who_dissented_and_carries_share_links(tmp_path, monkeypatch):
    first, second = _seed(tmp_path, monkeypatch)
    c = TestClient(api.app)
    c.post(f"/api/decisions/{second.id}/back", json={"handle": "alice", "stance": "agree"})
    feed = c.get("/api/feed").json()
    assert [f["id"] for f in feed] == [second.id, first.id]
    top = feed[0]
    assert top["backing"] == {"agree": 1, "disagree": 0} and top["url"] == f"http://testserver/r/{second.id}"
    assert top["card"] == f"http://testserver/r/{second.id}.png" and top["outcome"] is None
    assert {a["role"] for a in top["agents"]} == {o.role for o in second.opinions}
    aligned = api.ALIGNED[top["action"]]
    assert [d["role"] for d in top["dissent"]] == [o.role for o in second.opinions if o.stance != aligned]
    assert top["split"] == (len({o.stance for o in second.opinions}) > 1)
    assert all(len(a["thesis"]) <= 180 for a in top["agents"])
    assert [d["role"] for d in feed[1]["dissent"]] == [o.role for o in first.opinions]   # bullish agents, no_trade verdict
    assert api._line("First claim. Second claim.") == "First claim." and api._line("a" * 300).endswith("…")


def test_a_profile_shows_backings_record_and_watchlist(tmp_path, monkeypatch):
    first, second = _seed(tmp_path, monkeypatch)
    c = TestClient(api.app)
    token = c.post(f"/api/decisions/{second.id}/back", json={"handle": "alice", "stance": "agree"}).json()["edit_token"]
    c.post(f"/api/decisions/{first.id}/back", json={"handle": "alice", "stance": "disagree", "token": token})
    c.post("/api/watchlist", json={"handle": "alice", "symbol": "SOL", "token": token})
    _resolve(tmp_path, second.id, up=False)
    u = c.get("/api/users/@Alice").json()
    assert u["handle"] == "alice" and u["claimed"] and u["watchlist"] == ["SOL"]
    by = {b["decision_id"]: b for b in u["backings"]}
    assert by[second.id]["correct"] is False and by[second.id]["resolved"]
    assert by[first.id]["correct"] is None and not by[first.id]["resolved"]
    assert u["reputation"]["n"] == 1 and u["reputation"]["hits"] == 0 and not u["reputation"]["enough_to_read"]
    nobody = c.get("/api/users/nobody").json()
    assert nobody == {"handle": "nobody", "claimed": False, "backings": [], "reputation": None,
                      "meaningful_at": calibration.MEANINGFUL_N, "watchlist": []}
    assert c.get("/api/users/a").status_code == 422


def test_the_pages_are_served_linked_and_escape_what_people_type(tmp_path, monkeypatch):
    _seed(tmp_path, monkeypatch)
    c = TestClient(api.app)
    feed = c.get("/feed")
    assert feed.status_code == 200 and '<link rel="canonical" href="http://testserver/feed">' in feed.text
    prof = c.get("/u/Alice")
    assert prof.status_code == 200 and 'href="http://testserver/u/alice"' in prof.text and 'content="noindex"' in prof.text
    for hostile in ("%3Cscript%3E", "a%22b%3Ec", "ab"):
        assert c.get(f"/u/{hostile}").status_code == 422, hostile                    # never echoed into the page
    for page in ("feed.html", "profile.html"):
        src = (api.STATIC / page).read_text(encoding="utf-8")
        assert "const esc = s =>" in src and "<!--OG-->" in src
    assert "/feed" in c.get("/sitemap.xml").text
    llms = c.get("/llms.txt").text
    assert "/feed)" in llms and "/api/reputation)" in llms and "/api/watchlist)" in llms
    for page in ("landing.html", "landing.ja.html", "judges.html", "scorecard.html", "index.html"):
        assert 'href="/feed"' in (api.STATIC / page).read_text(encoding="utf-8"), page
