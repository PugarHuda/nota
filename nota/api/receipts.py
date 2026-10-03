"""Decisions and receipts: summaries, the impact-ranked diff against the previous receipt, replay,
practice positions, role scores, health, and the /r/<id> files (markdown, stored JSON, proof, card)."""

from __future__ import annotations

import json
import math
import os
import re
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import PlainTextResponse, Response

from nota import paths
from nota.api.common import _csv, _ledger, _ots, _receipt
from nota.api.social import _backing_counts
from nota.backings import store_for
from nota.calibration import (MEANINGFUL_N, due, reliability, role_scores, role_weights, scored_independent,
                              skill_vs_base, skill_vs_ryo, source_scores)
from nota.card import render_card
from nota.evidence import SECTIONS, EvidencePack, first_present
from nota.ledger import Ledger
from nota.receipt import Receipt, render_markdown
from nota.replay import replay
from nota.risk import PracticeTrade
from nota.ryo_client import RyoClient, RyoError
from nota.skills.contract import OK
from nota.stamp import view as stamp_view

router = APIRouter()
ATTESTATIONS = Path(__file__).resolve().parents[2] / "data" / "attestations.json"


KEY_PATHS = set(paths.PRICE_USD + paths.ATR_14 + paths.ATR_14_PCT + paths.RSI_14)


def _degraded(r: Receipt) -> bool:
    return any(s not in OK for s in r.availability.values())


def _failed_sections(r: Receipt) -> int:
    """RYO sections that came back error or unavailable: the landing's failure panel shows the receipt
    where the most of RYO went dark, not one where a single cross-check was partial."""
    return sum(1 for k in SECTIONS if r.availability.get(k) in ("error", "unavailable"))


def _summary(led: Ledger, r: Receipt) -> dict[str, Any]:
    return {
        # pack_hash travels with the summary so a caller can draw or cite the evidence identity
        # without a second request - the landing page draws its marks from it.
        "id": r.id, "symbol": r.symbol, "created_at": r.created_at, "headline": r.headline, "source": r.source,
        "pack_hash": r.pack_hash,
        "model": r.model, "action": r.verdict.action, "p_up_7d": r.verdict.p_up_7d, "rationale": r.verdict.rationale,
        "trade_kind": r.trade.kind, "degraded": _degraded(r), "resolved": led.get_outcome(r.id) is not None,
        "availability": r.availability, "failed_sections": _failed_sections(r),
    }


NOISE = re.compile(r"\.(since|fetched_at|as_of|window_hours|snippet|samples\.[^.]+\..*|sources\.[^.]+\.content)$")


# leaves that are themselves a percentage, a change or a points gap: diffed in points, not in % of a %
IN_POINTS = re.compile(r"(_pct|pct_|percent|performance|_points|\.changes?\.|change_|\.h\d+$|_bps|bps_|sentiment|delta|rsi)")
# an evidence number never outranks a model change (300) or a stance, verdict, trade or availability flip:
# a move off a value near zero is unbounded in percent (-0.003 -> -0.354 bps read as -11700%)
LEAF_CAP = 299


def _leaves(pack: EvidencePack) -> dict[str, Any]:
    """Leaf values under `<section>.data`. A list of scalars is one leaf (sorted), so a re-ordered domain
    list is not a change; a list of records is keyed by symbol/token/id/name when each row has a unique
    one; timestamps and free text that differ on every run are skipped."""
    out: dict[str, Any] = {}

    def walk(prefix: str, node: Any) -> None:
        if NOISE.search(prefix):
            return
        if isinstance(node, dict):
            for k, v in node.items():
                walk(f"{prefix}.{k}", v)
        elif isinstance(node, list):
            if all(not isinstance(v, (dict, list)) for v in node):
                out[prefix] = sorted(node, key=lambda v: (v is None, str(v)))
            else:
                # ranked lists (top movers, compared tokens) reshuffle between runs: key rows by their
                # identity so gainers.4 today is diffed against the same token, not whoever was 5th yesterday
                ident = next((f for f in ("symbol", "token", "id", "name")
                              if all(isinstance(v, dict) and isinstance(v.get(f), str) for v in node)), None)
                keys = [v[ident] for v in node] if ident else []
                if not ident or len(set(keys)) != len(keys):
                    keys = [str(i) for i in range(len(node))]
                for k, v in zip(keys, node):
                    walk(f"{prefix}.{k}", v)
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
        packs = (EvidencePack.model_validate_json(pa), EvidencePack.model_validate_json(pb))
        la, lb = _leaves(packs[0]), _leaves(packs[1])
        # a section present in only one receipt is one availability row above, not one row per leaf
        both = {k for k, s in packs[0].sections.items() if s.envelope} & {k for k, s in packs[1].sections.items() if s.envelope}
        for path in sorted(set(la) | set(lb)):
            if path.split(".", 1)[0] not in both:
                continue
            a, b = la.get(path), lb.get(path)
            if a == b:
                continue
            key = path in KEY_PATHS
            numeric = all(isinstance(x, (int, float)) and not isinstance(x, bool) for x in (a, b))
            if numeric and IN_POINTS.search(path):
                # already a percentage or a points gap: 0.004 -> -0.394 is a 0.4-point move, not -9950%
                add(path, a, b, min(LEAF_CAP, abs(b - a) * (10 if key else 1)), f"{b - a:+.2f} pts" + (" (feeds risk sizing)" if key else ""))
            elif numeric and a != 0:
                pct = (b - a) / abs(a) * 100
                add(path, a, b, min(LEAF_CAP, abs(pct) * (10 if key else 1)), f"{pct:+.1f}%" + (" (feeds risk sizing)" if key else ""))
            elif a is None or b is None:
                add(path, a, b, 50 if key else 5, "value became unavailable" if b is None else "value became available")
            else:
                add(path, a, b, 20 if key else 2, "changed")
    changes.sort(key=lambda c: -c["impact"])
    return changes


def _previous(led: Ledger, r: Receipt) -> Receipt | None:
    """The receipt stored right before this one for the same symbol, by the ledger's own ordering."""
    ids = [d["id"] for d in led.list_decisions(limit=200, symbol=r.symbol)]
    if r.id not in ids or ids.index(r.id) + 1 >= len(ids):
        return None
    return _receipt(led, ids[ids.index(r.id) + 1])


@router.get("/api/decisions")
def list_decisions(symbol: str | None = None, limit: int = Query(50, ge=1, le=200)) -> list[dict[str, Any]]:
    led = _ledger()
    rows = led.list_decisions(limit=limit, symbol=symbol.upper() if symbol else None)
    return [_summary(led, _receipt(led, d["id"])) for d in rows]


@router.get("/api/decisions/{id}")
def get_decision(id: str) -> dict[str, Any]:
    led = _ledger()
    r = _receipt(led, id)
    prev = _previous(led, r)
    out = led.get_outcome(id)
    return {"receipt": r.model_dump(mode="json"), "previous_id": prev.id if prev else None,
            "changes": what_changed(led, r, prev), "outcome": json.loads(out) if out else None, "degraded": _degraded(r),
            "backing": _backing_counts(led, id), "stamp": stamp_view(led.get_stamp(id), f"/r/{id}.ots"), **_pack_views(led, r)}


def _finite(*vs: Any) -> bool:
    return all(isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) for v in vs)


def _pack_views(led: Ledger, r: Receipt) -> dict[str, Any]:
    """Two reads of the stored evidence the receipt itself does not carry.

    `requests`: the exact arguments each section was called with, so the dashboard can offer a real
    positioning_check input instead of an invented example. `audit`: Nota's recomputed ATR, RSI and
    price beside RYO's, with the warning thresholds the skills applied, or null when this receipt
    lacks any of them. The landing's audit table is filled from it, never typed by hand."""
    raw = led.get_pack(r.pack_hash)
    if not raw:
        return {"requests": {}, "audit": None}
    pack = EvidencePack.model_validate_json(raw)
    envs = {k: s.envelope for k, s in pack.sections.items() if s.envelope}
    audit = None
    tech, price = envs.get("technicals_check"), envs.get("price_check")
    if tech and price and "deep_analysis" in envs:
        t, p = tech.data, price.data
        tref, pref = t.get("reference") or {}, p.get("reference") or {}
        rows = {"atr": (t.get("atr_14"), tref.get("atr_14"), (t.get("thresholds") or {}).get("atr_warn_pct")),
                "rsi": (t.get("rsi_14"), tref.get("rsi_14"), (t.get("thresholds") or {}).get("rsi_warn_points")),
                "price": (p.get("median_usd"), pref.get("price_usd"), (p.get("thresholds") or {}).get("deviation_warn_pct"))}
        if all(_finite(*v) for v in rows.values()):
            audit = {k: {"nota": n, "ryo": y, "warn": w} for k, (n, y, w) in rows.items()}
    return {"requests": {k: e.request for k, e in envs.items()}, "audit": audit}


@router.get("/api/positions")
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
            pack = EvidencePack.model_validate_json(pack_json) if pack_json else None
            med = pack.get("price_check.data.median_usd") if pack else None
            if isinstance(med, (int, float)) and not isinstance(med, bool):  # independent exchange read beats an older RYO read
                latest[r.symbol] = (float(med), f"{pack.get('price_check.data.fetched_at')} (exchange median)")
            else:
                price = first_present(pack, paths.PRICE_USD)[1] if pack else None
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
        if price is None:
            status = "price_unavailable"
        elif sign * (price - t.stop_price) <= 0:
            status = "stopped"  # latest price is at or beyond the stop: this practice position would be closed at a loss
        elif sign * (price - t.target_price) >= 0:
            status = "target"
        else:
            status = "open"
        out.append({"decision_id": r.id, "symbol": r.symbol, "side": t.side, "entry": t.entry_price, "stop": t.stop_price,
                    "target": t.target_price, "size_usd": t.size_usd, "opened_at": r.created_at, "latest_price": price,
                    "latest_as_of": as_of, "move_pct": move_pct, "stop_progress_pct": progress, "status": status,
                    "pnl_usd": None if price is None else round(sign * (price - t.entry_price) * t.size_units, 2)})
    return out


@router.get("/api/scores")
def scores() -> dict[str, Any]:
    led = _ledger()
    s = role_scores(led)
    return {"scores": s, "weights": role_weights(s), "resolved": len(led.list_outcomes()), "unresolved": len(led.unresolved()),
            "n_independent": scored_independent(led), "meaningful_at": MEANINGFUL_N,
            "reliability": reliability(led), "sources": source_scores(led), "vs_base_rate": skill_vs_base(led),
            "vs_ryo": skill_vs_ryo(led)}


_HEALTH_TTL = 60.0
_health_cache: tuple[float, dict[str, Any]] | None = None   # (monotonic time, RYO probe); per process


def _ryo_probe() -> dict[str, Any]:
    """RYO's /health, at most once a minute per process: the dashboard asks on every receipt it
    opens, and each ask used to be a live call to RYO. A timeout gets one more try, since a cold
    upstream often answers the second time; anything else is reported as the error it was."""
    global _health_cache
    now = time.monotonic()
    if _health_cache and now - _health_cache[0] < _HEALTH_TTL:
        return _health_cache[1]
    client = RyoClient(timeout=5.0, max_retries=0)  # a health probe must not hold the page hostage
    value: dict[str, Any]
    for _ in range(2):
        try:
            value = client.health()
            break
        except RyoError as exc:
            value = {"error": f"{type(exc).__name__}: {str(exc)[:200]}"}
            if exc.code != "TIMEOUT":
                break
        except Exception as exc:  # the dashboard must load even when RYO is down
            value = {"error": f"{type(exc).__name__}: {str(exc)[:200]}"}
            break
    value = {**value, "checked_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    _health_cache = (now, value)
    return value


LEDGER_STALE_H = 36   # the cycle runs daily; a day and a half without a lock or a receipt means it stopped


def _freshness(led: Ledger) -> dict[str, Any]:
    def newest(sql: str) -> str | None:
        try:
            return led.conn.execute(sql).fetchone()[0]
        except sqlite3.OperationalError:   # a snapshot from before the scorecard has no such table
            return None

    out = {"last_lock_at": newest("SELECT MAX(locked_at) FROM locks"),
           "last_decision_at": newest("SELECT MAX(created_at) FROM decisions"),
           "last_settled_at": newest("SELECT MAX(settled_at) FROM settlements")}
    stamps = [datetime.fromisoformat(v) for v in (out["last_lock_at"], out["last_decision_at"]) if v]
    latest = max((t if t.tzinfo else t.replace(tzinfo=timezone.utc) for t in stamps), default=None)
    out["stale"] = latest is None or datetime.now(timezone.utc) - latest > timedelta(hours=LEDGER_STALE_H)
    return out


def _attestation() -> dict[str, Any] | None:
    """The newest Sigstore attestation of the ledger snapshot, as the ledger cycle recorded it, so anyone can
    check what this deployment serves with `gh attestation verify data/demo.db --repo PugarHuda/nota`."""
    try:
        rows = json.loads(ATTESTATIONS.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return rows[-1] if isinstance(rows, list) and rows else None


@router.get("/api/health")
def health() -> dict[str, Any]:
    """What this deployment can and cannot do right now. No secrets, only whether they are set."""
    led = _ledger()
    ryo = _ryo_probe()
    # the provider actually configured: NOTA_LLM when set, else whichever key is present
    kind = os.environ.get("NOTA_LLM") or ("openai" if os.environ.get("OPENAI_API_KEY") else "anthropic")
    store = store_for(led)
    return {
        "ryo": ryo, "ryo_key_set": bool(os.environ.get("RYO_MCP_KEY")), "readonly": led.readonly,
        "checked_at": ryo["checked_at"],
        # where a backing would be written: Postgres, the ledger itself, or nowhere (a read-only
        # snapshot with no DATABASE_URL), so the dashboard can say which instead of guessing
        "backing": "postgres" if store is not led else "none" if led.readonly else "ledger",
        # `key_set` matters more than the name: the hosted demo has no LLM at all, and saying
        # "anthropic" there would imply a model that is not reachable.
        "llm": {"kind": kind, "model": os.environ.get("NOTA_MODEL"),
                "key_set": bool(os.environ.get("OPENAI_API_KEY") if kind == "openai"
                                else os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"))},
        "ledger": {"decisions": led.conn.execute("SELECT COUNT(*) FROM decisions").fetchone()[0],
                   "resolved": len(led.list_outcomes()), "unresolved": len(led.unresolved()), "due": len(due(led)),
                   **_freshness(led)},
        "notify": {"telegram": bool(os.environ.get("TELEGRAM_BOT_TOKEN") and os.environ.get("TELEGRAM_CHAT_ID")),
                   "discord": bool(os.environ.get("DISCORD_WEBHOOK_URL"))},
        "attestation": _attestation(),
    }


@router.get("/api/decisions/{id}/replay")
def replay_check(id: str) -> dict[str, Any]:
    led = _ledger()
    _receipt(led, id)  # 404 for an unknown id before replay is asked
    try:
        res = replay(id, led, None)
    except RuntimeError as exc:
        return {"identical": None, "diff": [], "error": str(exc)}
    return {"identical": res.identical, "diff": res.diff, "error": None}


@router.get("/r/{id}.md")
def receipt_markdown(id: str) -> PlainTextResponse:
    return PlainTextResponse(render_markdown(_receipt(_ledger(), id)), media_type="text/markdown; charset=utf-8")


@router.get("/r/{id}.json")
def receipt_json(id: str) -> Response:
    """The receipt exactly as stored, byte for byte, so its SHA-256 is the digest /r/<id>.ots anchors."""
    raw = _ledger().get_decision(id)
    if raw is None:
        raise HTTPException(404, f"no decision {id}")
    return Response(raw, media_type="application/json")


@router.get("/r/{id}.ots")
def receipt_ots(id: str) -> Response:
    return _ots(_ledger().get_stamp(id), f"{id}.json")


@router.get("/r/{id}.png")
def receipt_card(id: str) -> Response:
    return Response(render_card(_receipt(_ledger(), id)), media_type="image/png", headers={"Cache-Control": "public, max-age=300"})


@router.get("/api/outcomes.csv")
def outcomes_csv() -> Response:
    """Every resolved decision: what each role said, the base rate it had to beat, what happened, and Brier."""
    from nota.council import ROLES

    led = _ledger()
    roles = [*ROLES, "judge"]
    rows = []
    for raw in led.list_outcomes():
        o = json.loads(raw)
        rec = led.get_decision(o["decision_id"])
        r = Receipt.model_validate_json(rec) if rec else None
        said = {op.role: op.p_up_7d for op in r.opinions} if r else {}
        if r:
            said["judge"] = r.verdict.p_up_7d
        rows.append([o["decision_id"], o["symbol"], r.created_at if r else None, r.verdict.action if r else None,
                     *[said.get(k) for k in roles], o.get("base_rate_p"), o["went_up"], o.get("return_pct"),
                     o.get("resolved_at"), o.get("price_now_source"), *[o["brier"].get(k) for k in roles]])
    rows.sort(key=lambda x: x[2] or "")
    return _csv(["decision_id", "symbol", "created_at", "action", *[f"p_up_7d_{k}" for k in roles], "base_rate_p",
                 "went_up", "return_pct", "resolved_at", "price_now_source", *[f"brier_{k}" for k in roles]],
                rows, "nota-outcomes.csv")
