"""SocialFi: public backing of calls under claimed handles, watchlists, one reputation board for
humans and agents, the reasoning feed and per-handle profiles."""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import secrets
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field

from nota.api.common import _base, _handle, _ledger, _receipt, _throttle
from nota.backings import store_for
from nota.calibration import MEANINGFUL_N, backer_correct, reputation
from nota.ledger import Ledger
from nota.receipt import Receipt

router = APIRouter()


# --- SocialFi: public backing of calls, scored against outcomes ------------------------------


class BackingIn(BaseModel):
    handle: str = Field(description="X / Discord / Telegram handle, 3-32 chars")
    stance: Literal["agree", "disagree"]
    token: str | None = Field(None, max_length=128, description="the edit token returned by this handle's first backing")


def _backing_counts(led: Ledger, id: str) -> dict[str, Any]:
    rows = store_for(led).backings(id)
    return {"agree": sum(r["stance"] == "agree" for r in rows), "disagree": sum(r["stance"] == "disagree" for r in rows),
            "handles": [{"handle": r["handle"], "stance": r["stance"]} for r in rows[-20:]]}


def _writable(request: Request) -> tuple[Ledger, Any]:
    """The ledger and the store a public write goes to, after the address's hourly budget is charged."""
    _throttle(request.client.host if request.client else "unknown")
    led = _ledger()
    store = store_for(led)
    if store is led and led.readonly:
        raise HTTPException(503, "this is a read-only demo deployment over a ledger snapshot and no DATABASE_URL is set; "
                                 "backing works on a writable `nota serve`")
    return led, store


def _authorise(store: Any, handle: str, token: str | None) -> str | None:
    """There are no accounts: the first write under a handle claims it and gets an edit token back once,
    and every later write under that handle has to carry the token. Only its SHA-256 is stored, so a
    leaked database does not leak the tokens. Returns the new token when this call claimed the handle."""
    new_token = None
    stored = store.token_sha(handle)
    if stored is None:
        new_token = secrets.token_urlsafe(24)
        if not store.claim(handle, hashlib.sha256(new_token.encode()).hexdigest()):
            stored = store.token_sha(handle)   # someone claimed it between the read and the write
            new_token = None
    if new_token is None:
        sent = hashlib.sha256((token or "").encode()).hexdigest()
        if not stored or not hmac.compare_digest(sent, stored):
            raise HTTPException(409, "handle already claimed; send its edit token")
    return new_token


@router.post("/api/decisions/{id}/back")
def back_decision(id: str, body: BackingIn, request: Request) -> dict[str, Any]:
    """One stance per handle per receipt, latest wins, under a claimed handle (`_authorise`)."""
    handle = _handle(body.handle)
    led, store = _writable(request)
    _receipt(led, id)
    new_token = _authorise(store, handle, body.token)
    store.add_backing(id, handle, body.stance)
    out = _backing_counts(led, id)
    if new_token:
        out["edit_token"] = new_token   # shown once; the server keeps only its hash
    return out


# --- SocialFi: a public watchlist, one reputation board for humans and agents, a reasoning feed ------
WATCH_MAX = 50   # symbols per handle: a watchlist, not a mirror of every listing


class WatchIn(BaseModel):
    handle: str = Field(description="X / Discord / Telegram handle, 3-32 chars")
    symbol: str = Field(max_length=32)
    watch: bool = Field(True, description="false removes the symbol")
    token: str | None = Field(None, max_length=128, description="the edit token returned by this handle's first write")


@router.post("/api/watchlist")
def watch_symbol(body: WatchIn, request: Request) -> dict[str, Any]:
    """Add a symbol to (or, with watch=false, remove it from) a handle's public watchlist. Same handle
    claim, edit token, throttle and read-only rule as backing."""
    from nota.skills.contract import clean_symbol

    handle = _handle(body.handle)
    try:
        sym = clean_symbol(body.symbol)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    _, store = _writable(request)
    new_token = _authorise(store, handle, body.token)
    mine = [w["symbol"] for w in store.watches(handle)]
    if body.watch and sym not in mine and len(mine) >= WATCH_MAX:
        raise HTTPException(422, f"a watchlist holds at most {WATCH_MAX} symbols")
    store.watch(handle, sym, body.watch)
    out: dict[str, Any] = {"handle": handle, "symbols": [w["symbol"] for w in store.watches(handle)]}
    if new_token:
        out["edit_token"] = new_token
    return out


@router.get("/api/watchlist")
def watchlist(limit: int = Query(50, ge=1, le=200)) -> dict[str, Any]:
    """Which symbols the most handles watch, each with up to 20 of those handles."""
    # ponytail: counted in Python over every row; a GROUP BY when the table outgrows one response
    rows = store_for(_ledger()).watches()
    by: dict[str, list[str]] = {}
    for w in rows:
        by.setdefault(w["symbol"], []).append(w["handle"])
    symbols = sorted(({"symbol": k, "watchers": len(v), "handles": v[:20]} for k, v in by.items()),
                     key=lambda r: (-r["watchers"], r["symbol"]))
    return {"symbols": symbols[:limit], "handles": len({w["handle"] for w in rows})}


@router.get("/api/reputation")
def reputation_board() -> dict[str, Any]:
    """Backer handles and the council's agents ranked on one scale (calibration.reputation)."""
    led = _ledger()
    return reputation(led, store_for(led).all_backings())


def _line(text: str, cap: int = 180) -> str:
    """The first sentence, so a card shows the claim and the receipt holds the argument."""
    first = re.split(r"(?<=[.!?])\s", text.strip(), maxsplit=1)[0]
    return first if len(first) <= cap else first[:cap - 1].rstrip() + "…"


ALIGNED = {"long": "bullish", "short": "bearish", "no_trade": "neutral"}


@router.get("/api/feed")
def reasoning_feed(request: Request, limit: int = Query(20, ge=1, le=50)) -> list[dict[str, Any]]:
    """The newest receipts as public reasoning: each agent's stance and one-line thesis, who dissented
    from the verdict and why, and the backing counts. The /feed page is drawn from this."""
    base, led = _base(request), _ledger()
    backs: dict[str, list[str]] = {}
    for b in store_for(led).all_backings():   # once, not once per card: on Postgres each read is a connection
        backs.setdefault(b["decision_id"], []).append(b["stance"])
    out = []
    for d in led.list_decisions(limit=limit):
        r = _receipt(led, d["id"])
        raw = led.get_outcome(r.id)
        o = json.loads(raw) if raw else None
        agents = [{"role": op.role, "stance": op.stance, "p_up_7d": op.p_up_7d, "confidence": op.confidence,
                   "thesis": _line(op.thesis)} for op in r.opinions]
        st = backs.get(r.id, [])
        out.append({"id": r.id, "symbol": r.symbol, "created_at": r.created_at, "headline": r.headline,
                    "action": r.verdict.action, "p_up_7d": r.verdict.p_up_7d, "rationale": _line(r.verdict.rationale, 240),
                    "agents": agents, "split": len({a["stance"] for a in agents}) > 1,
                    # an agent dissents when its stance is not the one the verdict acts on
                    "dissent": [a for a in agents if a["stance"] != ALIGNED[r.verdict.action]],
                    "judge_disagreed_with": r.verdict.disagreed_with,
                    "backing": {"agree": st.count("agree"), "disagree": st.count("disagree")},
                    "outcome": {"went_up": o["went_up"], "return_pct": o.get("return_pct")} if o else None,
                    "url": f"{base}/r/{r.id}", "card": f"{base}/r/{r.id}.png"})
    return out


@router.get("/api/users/{handle}")
def user_profile(handle: str) -> dict[str, Any]:
    """One handle's public record: its backings (each with how it scored), its row on the reputation
    board, and its watchlist. A handle nobody has used answers with empty lists, not 404."""
    handle = _handle(handle)
    led = _ledger()
    store = store_for(led)
    every = store.all_backings()   # ponytail: every backing, since the reputation row needs them all anyway
    mine = []
    for b in sorted((b for b in every if b["handle"] == handle), key=lambda b: b["created_at"], reverse=True):
        raw, out = led.get_decision(b["decision_id"]), led.get_outcome(b["decision_id"])
        r = Receipt.model_validate_json(raw) if raw else None
        o = json.loads(out) if out else None
        mine.append({"decision_id": b["decision_id"], "stance": b["stance"], "created_at": b["created_at"],
                     "symbol": r.symbol if r else None, "action": r.verdict.action if r else None,
                     "headline": r.headline if r else None, "resolved": o is not None,
                     "correct": backer_correct(b["stance"], r.verdict.action, bool(o["went_up"])) if r and o else None})
    row = next((x for x in reputation(led, every)["rows"] if x["kind"] == "human" and x["name"] == handle), None)
    return {"handle": handle, "claimed": store.token_sha(handle) is not None, "backings": mine, "reputation": row,
            "meaningful_at": MEANINGFUL_N, "watchlist": [w["symbol"] for w in store.watches(handle)]}


@router.get("/api/backers")
def backers() -> list[dict[str, Any]]:
    led = _ledger()
    outcomes = {o["decision_id"]: o for o in (json.loads(raw) for raw in led.list_outcomes())}
    actions: dict[str, str] = {}
    table: dict[str, dict[str, Any]] = {}
    for b in store_for(led).all_backings():
        row = table.setdefault(b["handle"], {"handle": b["handle"], "backed": 0, "scored": 0, "correct": 0})
        row["backed"] += 1
        o = outcomes.get(b["decision_id"])
        if o is None:
            continue
        if b["decision_id"] not in actions:
            actions[b["decision_id"]] = _receipt(led, b["decision_id"]).verdict.action
        verdict = backer_correct(b["stance"], actions[b["decision_id"]], bool(o["went_up"]))
        if verdict is None:
            continue
        row["scored"] += 1
        row["correct"] += int(verdict)
    out = list(table.values())
    for row in out:
        row["accuracy"] = round(row["correct"] / row["scored"], 3) if row["scored"] else None
    out.sort(key=lambda r: (-(r["accuracy"] if r["accuracy"] is not None else -1), -r["scored"], -r["backed"]))
    return out
