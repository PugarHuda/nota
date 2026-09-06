"""Read-only HTTP API over the ledger plus the diff-first dashboard (Track 2).

Nothing here writes to the ledger. Every endpoint is a view over receipts the CLI already
stored, so the dashboard can never show a number that has no receipt behind it.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse

from arena import paths
from arena.calibration import role_scores, role_weights
from arena.evidence import EvidencePack, first_present
from arena.ledger import Ledger
from arena.receipt import Receipt
from arena.risk import PracticeTrade

load_dotenv()
app = FastAPI(title="RYO Arena", description="Read-only view over decision receipts. No orders, no wallets.")
STATIC = Path(__file__).parent / "static"
KEY_PATHS = set(paths.PRICE_USD + paths.ATR_14 + paths.RSI_14)


def _ledger() -> Ledger:
    # ponytail: one connection per request; sqlite objects cannot cross FastAPI's worker threads
    return Ledger(os.environ.get("ARENA_DB", "arena.db"))


def _receipt(led: Ledger, id: str) -> Receipt:
    raw = led.get_decision(id)
    if raw is None:
        raise HTTPException(404, f"no decision {id}")
    return Receipt.model_validate_json(raw)


def _degraded(r: Receipt) -> bool:
    return any(s != "ok" for s in r.availability.values())


def _summary(led: Ledger, r: Receipt) -> dict[str, Any]:
    return {
        "id": r.id, "symbol": r.symbol, "created_at": r.created_at, "headline": r.headline, "source": r.source,
        "model": r.model, "action": r.verdict.action, "p_up_7d": r.verdict.p_up_7d, "rationale": r.verdict.rationale,
        "trade_kind": r.trade.kind, "degraded": _degraded(r), "resolved": led.get_outcome(r.id) is not None,
    }


def _leaves(pack: EvidencePack) -> dict[str, Any]:
    out: dict[str, Any] = {}

    def walk(prefix: str, node: Any) -> None:
        if isinstance(node, dict):
            for k, v in node.items():
                walk(f"{prefix}.{k}", v)
        elif isinstance(node, list):
            for i, v in enumerate(node):
                walk(f"{prefix}.{i}", v)
        else:
            out[prefix] = node

    for key, sec in pack.sections.items():
        if sec.envelope is not None:
            walk(f"{key}.data", sec.envelope.data)
    return out


def what_changed(led: Ledger, cur: Receipt, prev: Receipt | None) -> list[dict[str, Any]]:
    """Diff against the previous receipt for the same symbol, ranked by impact.

    Impact: verdict / trade / availability flips outrank everything; then the numbers the risk
    engine reads (price, ATR, RSI) by absolute % move; then every other evidence leaf by % move.
    ponytail: a heuristic rank, not a model; good enough to put the important row first.
    """
    if prev is None:
        return []
    changes: list[dict[str, Any]] = []

    def add(path: str, before: Any, after: Any, impact: float, why: str) -> None:
        changes.append({"path": path, "before": before, "after": after, "impact": round(impact, 3), "why": why})

    if cur.verdict.action != prev.verdict.action:
        add("verdict.action", prev.verdict.action, cur.verdict.action, 1000, "council verdict flipped")
    if cur.trade.kind != prev.trade.kind:
        add("trade.kind", prev.trade.kind, cur.trade.kind, 900, "trade became " + ("possible" if cur.trade.kind == "trade" else "blocked"))
    for k in sorted(set(cur.availability) | set(prev.availability)):
        a, b = prev.availability.get(k), cur.availability.get(k)
        if a != b:
            add(f"availability.{k}", a, b, 800, "evidence section availability changed")
    if cur.model != prev.model:
        add("model", prev.model, cur.model, 300, "a different model produced this receipt")
    dp = abs(cur.verdict.p_up_7d - prev.verdict.p_up_7d)
    if dp >= 0.05:
        add("verdict.p_up_7d", prev.verdict.p_up_7d, cur.verdict.p_up_7d, 500 + dp * 100, "judge probability moved")
    for o_prev in prev.opinions:
        o_cur = next((o for o in cur.opinions if o.role == o_prev.role), None)
        if o_cur and o_cur.stance != o_prev.stance:
            add(f"opinions.{o_prev.role}.stance", o_prev.stance, o_cur.stance, 400, f"{o_prev.role} changed stance")

    pa, pb = led.get_pack(prev.pack_hash), led.get_pack(cur.pack_hash)
    if pa and pb:
        la, lb = _leaves(EvidencePack.model_validate_json(pa)), _leaves(EvidencePack.model_validate_json(pb))
        for path in sorted(set(la) | set(lb)):
            a, b = la.get(path), lb.get(path)
            if a == b:
                continue
            key = path in KEY_PATHS
            numeric = all(isinstance(x, (int, float)) and not isinstance(x, bool) for x in (a, b))
            if numeric and a != 0:
                pct = (b - a) / abs(a) * 100
                add(path, a, b, abs(pct) * (10 if key else 1), f"{pct:+.1f}%" + (" (feeds risk sizing)" if key else ""))
            elif a is None or b is None:
                add(path, a, b, 50 if key else 5, "value became unavailable" if b is None else "value became available")
            else:
                add(path, a, b, 20 if key else 2, "changed")
    changes.sort(key=lambda c: -c["impact"])
    return changes


def _previous(led: Ledger, r: Receipt) -> Receipt | None:
    for d in led.list_decisions(limit=200, symbol=r.symbol):
        if d["created_at"] < r.created_at or (d["created_at"] == r.created_at and d["id"] < r.id):
            return _receipt(led, d["id"])
    return None


@app.get("/api/decisions")
def list_decisions(symbol: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
    led = _ledger()
    rows = led.list_decisions(limit=min(limit, 200), symbol=symbol.upper() if symbol else None)
    return [_summary(led, _receipt(led, d["id"])) for d in rows]


@app.get("/api/decisions/{id}")
def get_decision(id: str) -> dict[str, Any]:
    led = _ledger()
    r = _receipt(led, id)
    prev = _previous(led, r)
    out = led.get_outcome(id)
    return {"receipt": r.model_dump(mode="json"), "previous_id": prev.id if prev else None,
            "changes": what_changed(led, r, prev), "outcome": json.loads(out) if out else None, "degraded": _degraded(r)}


@app.get("/api/positions")
def positions() -> list[dict[str, Any]]:
    """Open practice positions (latest unresolved trade per symbol) against the latest evidence price."""
    led = _ledger()
    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    latest: dict[str, tuple[float | None, str | None]] = {}
    for d in led.list_decisions(limit=200):
        r = _receipt(led, d["id"])
        if r.symbol not in latest:  # newest receipt per symbol carries the freshest evidence
            pack_json = led.get_pack(r.pack_hash)
            price = first_present(EvidencePack.model_validate_json(pack_json), paths.PRICE_USD)[1] if pack_json else None
            latest[r.symbol] = (price, r.provenance.get("deep_analysis", {}).get("as_of"))
        if r.symbol in seen or not isinstance(r.trade, PracticeTrade) or led.get_outcome(r.id):
            continue
        seen.add(r.symbol)
        t = r.trade
        price, as_of = latest[r.symbol]
        sign = 1.0 if t.side == "long" else -1.0
        # +100 = at target distance of one stop, -100 = at stop; null when the latest price is unavailable
        move_pct = None if price is None else round(sign * (price / t.entry_price - 1) * 100, 3)
        progress = None if price is None else round(sign * (price - t.entry_price) / abs(t.entry_price - t.stop_price) * 100, 1)
        out.append({"decision_id": r.id, "symbol": r.symbol, "side": t.side, "entry": t.entry_price, "stop": t.stop_price,
                    "target": t.target_price, "size_usd": t.size_usd, "opened_at": r.created_at, "latest_price": price,
                    "latest_as_of": as_of, "move_pct": move_pct, "stop_progress_pct": progress})
    return out


@app.get("/api/scores")
def scores() -> dict[str, Any]:
    led = _ledger()
    s = role_scores(led)
    return {"scores": s, "weights": role_weights(s), "resolved": len(led.list_outcomes()), "unresolved": len(led.unresolved())}


@app.get("/")
@app.get("/r/{id}")
def index(id: str | None = None) -> FileResponse:
    return FileResponse(STATIC / "index.html")
