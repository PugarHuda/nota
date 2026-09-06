import json

from fastapi.testclient import TestClient

from arena import api
from arena.api import backer_correct
from arena.calibration import Outcome
from arena.card import render_card
from arena.ledger import Ledger
from tests.test_api import _seed


def test_backer_correct_matrix():
    assert backer_correct("agree", "long", True) and not backer_correct("agree", "long", False)
    assert backer_correct("disagree", "short", True) and not backer_correct("disagree", "short", False)
    assert backer_correct("agree", "no_trade", True) is None


def test_backing_flow_and_leaderboard(tmp_path, monkeypatch):
    first, second = _seed(tmp_path, monkeypatch)  # second: long, first: no_trade
    c = TestClient(api.app)
    assert c.post(f"/api/decisions/{second.id}/back", json={"handle": "x", "stance": "agree"}).status_code == 422
    assert c.post(f"/api/decisions/{second.id}/back", json={"handle": "alice", "stance": "maybe"}).status_code == 422
    assert c.post("/api/decisions/nope/back", json={"handle": "alice", "stance": "agree"}).status_code == 404
    assert c.post(f"/api/decisions/{second.id}/back", json={"handle": "alice", "stance": "agree"}).json()["agree"] == 1
    assert c.post(f"/api/decisions/{second.id}/back", json={"handle": "bob", "stance": "disagree"}).json()["disagree"] == 1
    counts = c.post(f"/api/decisions/{second.id}/back", json={"handle": "alice", "stance": "disagree"}).json()  # latest wins
    assert counts == {"agree": 0, "disagree": 2, "handles": [{"handle": "alice", "stance": "disagree"}, {"handle": "bob", "stance": "disagree"}]}
    c.post(f"/api/decisions/{first.id}/back", json={"handle": "carol", "stance": "agree"})  # no_trade: never scored
    assert c.get(f"/api/decisions/{second.id}").json()["backing"]["disagree"] == 2

    led = Ledger(str(tmp_path / "t.db"))
    led.save_outcome(second.id, Outcome(decision_id=second.id, symbol="SOL", resolved_at="x", decided_as_of=None, horizon_reached=True,
                                        price_then=150.0, price_now=170.0, return_pct=13.3, went_up=True, brier={}).model_dump_json())
    board = c.get("/api/backers").json()
    assert {(b["handle"], b["backed"], b["scored"], b["correct"], b["accuracy"]) for b in board} == {
        ("carol", 1, 0, 0, None), ("alice", 1, 1, 0, 0.0), ("bob", 1, 1, 0, 0.0)}
    assert board[-1]["handle"] == "carol"  # unscored backers sort last
    c.post(f"/api/decisions/{second.id}/back", json={"handle": "dave", "stance": "agree"})
    assert c.get("/api/backers").json()[0] == {"handle": "dave", "backed": 1, "scored": 1, "correct": 1, "accuracy": 1.0}


def test_readonly_snapshot_serves_reads_and_refuses_backing(tmp_path, monkeypatch):
    import sqlite3

    first, second = _seed(tmp_path, monkeypatch)
    sqlite3.connect(str(tmp_path / "t.db")).execute(f"VACUUM INTO '{(tmp_path / 'snap.db').as_posix()}'")
    monkeypatch.setenv("ARENA_DB", str(tmp_path / "snap.db"))
    monkeypatch.setenv("ARENA_READONLY", "1")
    c = TestClient(api.app)
    assert c.get("/api/health").json()["readonly"] is True
    assert [r["id"] for r in c.get("/api/decisions").json()] == [second.id, first.id]
    assert c.get(f"/api/decisions/{second.id}/replay").json()["identical"] is True
    res = c.post(f"/api/decisions/{second.id}/back", json={"handle": "alice", "stance": "agree"})
    assert res.status_code == 503 and "read-only" in res.json()["detail"]


def test_backing_throttle_per_ip():
    from fastapi import HTTPException
    import pytest

    api._BACKING_HITS.clear()
    for i in range(api.BACKING_LIMIT):
        api._throttle("1.2.3.4", now=1000.0 + i)
    with pytest.raises(HTTPException) as exc:
        api._throttle("1.2.3.4", now=1000.0 + api.BACKING_LIMIT)
    assert exc.value.status_code == 429
    api._throttle("5.6.7.8", now=1000.0)  # other addresses unaffected
    api._throttle("1.2.3.4", now=1000.0 + api.BACKING_WINDOW + 1)  # window expired
    api._BACKING_HITS.clear()


def test_card_png_and_open_graph_tags(tmp_path, monkeypatch):
    first, second = _seed(tmp_path, monkeypatch)
    c = TestClient(api.app)
    png = c.get(f"/r/{second.id}.png")
    assert png.status_code == 200 and png.headers["content-type"] == "image/png" and png.content[:8] == b"\x89PNG\r\n\x1a\n"
    assert len(render_card(second)) > 5000
    page = c.get(f"/r/{second.id}").text
    assert f'<meta property="og:image" content="http://testserver/r/{second.id}.png">' in page
    assert 'name="twitter:card" content="summary_large_image"' in page and "<!--OG-->" not in page
    assert "<!--OG-->" in c.get("/").text and "<!--OG-->" in c.get("/r/nope").text
    monkeypatch.setenv("ARENA_PUBLIC_URL", "https://arena.example/")
    assert 'content="https://arena.example/r/' in c.get(f"/r/{second.id}").text
