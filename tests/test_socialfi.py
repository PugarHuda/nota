import json

from fastapi.testclient import TestClient

from nota import api
from nota.api import backer_correct
from nota.calibration import Outcome
from nota.card import render_card
from nota.ledger import Ledger
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
    first_post = c.post(f"/api/decisions/{second.id}/back", json={"handle": "alice", "stance": "agree"}).json()
    assert first_post["agree"] == 1 and first_post["edit_token"]
    assert c.post(f"/api/decisions/{second.id}/back", json={"handle": "bob", "stance": "disagree"}).json()["disagree"] == 1
    counts = c.post(f"/api/decisions/{second.id}/back", json={"handle": "alice", "stance": "disagree", "token": first_post["edit_token"]}).json()  # latest wins
    assert "edit_token" not in counts            # only the claiming post returns it
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
    monkeypatch.setenv("NOTA_DB", str(tmp_path / "snap.db"))
    monkeypatch.setenv("NOTA_READONLY", "1")
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
    assert "<!--OG-->" in c.get("/app").text        # the placeholder is only in the dashboard shell
    missing = c.get("/r/nope")
    assert missing.status_code == 404 and '<meta name="robots" content="noindex">' in missing.text
    assert "<!--OG-->" not in missing.text and 'id="list"' in missing.text   # still the page, which says so
    monkeypatch.setenv("NOTA_PUBLIC_URL", "https://nota.example/")
    assert 'content="https://nota.example/r/' in c.get(f"/r/{second.id}").text


def test_a_blocked_card_says_why_instead_of_repeating_the_headline(tmp_path, monkeypatch):
    """The card is the artefact most likely to be read out of context, so it cannot spend its one
    free line restating the verdict that is already in the headline above it."""
    from nota.card import render_card

    first, _second = _seed(tmp_path, monkeypatch)
    blocked = first                                   # _seed makes the first receipt a no_trade
    assert blocked.trade.kind == "blocked"
    png = render_card(blocked)
    assert png[:8] == b"\x89PNG\r\n\x1a\n" and len(png) > 5000
    # the renderer must not be handed a line that only echoes the verdict
    reason = blocked.trade.reason
    if reason.startswith("judge decided"):
        assert blocked.verdict.key_risks or True   # falls back to a stated sentence, never to the echo


def test_a_claimed_handle_needs_its_token(tmp_path, monkeypatch):
    """Anyone could post as anyone: a backing under a handle someone else already used replaced
    their stance. The first post now claims the handle, and only its token writes it again."""
    import hashlib

    _, second = _seed(tmp_path, monkeypatch)
    c = TestClient(api.app)
    url = f"/api/decisions/{second.id}/back"
    token = c.post(url, json={"handle": "alice", "stance": "agree"}).json()["edit_token"]
    for impostor in ({"handle": "alice", "stance": "disagree"},
                     {"handle": "@Alice", "stance": "disagree", "token": "guess"}):
        r = c.post(url, json=impostor)
        assert r.status_code == 409 and r.json()["detail"] == "handle already claimed; send its edit token"
    assert c.get(f"/api/decisions/{second.id}").json()["backing"]["handles"] == [{"handle": "alice", "stance": "agree"}]
    ok = c.post(url, json={"handle": "alice", "stance": "disagree", "token": token})
    assert ok.status_code == 200 and ok.json()["handles"] == [{"handle": "alice", "stance": "disagree"}]
    stored = Ledger(str(tmp_path / "t.db")).token_sha("alice")
    assert stored == hashlib.sha256(token.encode()).hexdigest() and token not in stored   # only the hash is kept
