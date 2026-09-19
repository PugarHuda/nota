"""Read-only HTTP API over the ledger plus the diff-first dashboard (Track 2).

Nothing here writes to the ledger. Every endpoint is a view over receipts the CLI already
stored, so the dashboard can never show a number that has no receipt behind it.
"""

from __future__ import annotations

import csv
import hashlib
import hmac
import html
import io
import json
import logging
import math
import os
from urllib.parse import urlparse
import re
import secrets
import sqlite3
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Literal

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse, Response
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from nota import paths
from nota.a2a import (PROTOCOL_VERSION as A2A_VERSION, VERSION_NOT_SUPPORTED as A2A_UNSUPPORTED, A2AError,
                      agent_card, call_from as a2a_call_from, handle as a2a_handle)
from nota.backings import PostgresBackings, store_for
from nota.calibration import (MEANINGFUL_N, due, reliability, role_scores, role_weights, scored_independent, skill_vs_base,
                              skill_vs_ryo, source_scores)
from nota.card import render_card
from nota.evidence import SECTIONS, EvidencePack, first_present
from nota.ledger import Ledger, now_iso
from nota.receipt import Receipt, render_markdown
from nota.replay import replay
from nota.risk import PracticeTrade
from nota.ryo_client import RyoClient, RyoError
from nota.mcp_server import (HEADER_MISMATCH, PROMPTS, SUPPORTED_PROTOCOLS, UNSUPPORTED_VERSION, VERSION_META,
                             handle as mcp_handle)
from nota.skills import SKILLS, definitions as skill_definitions, invoke as skill_invoke, live_deps
from nota.skills.contract import OK

load_dotenv()
app = FastAPI(title="Nota", description="Read-only view over decision receipts. No orders, no wallets.")
STATIC = Path(__file__).parent / "static"
KEY_PATHS = set(paths.PRICE_USD + paths.ATR_14 + paths.ATR_14_PCT + paths.RSI_14)


class HeadMiddleware:
    """FastAPI routes answer only the methods they name, so every GET page said 405 to HEAD, which is
    what uptime checks, link previews and video players probe with. HEAD is served as the GET it
    stands for, headers and all (content-length included), with the body dropped."""

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope["type"] != "http" or scope["method"] != "HEAD":
            await self.app(scope, receive, send)
            return
        done = False

        async def head_send(message: dict[str, Any]) -> None:
            nonlocal done
            if message["type"] == "http.response.start":
                await send(message)
            elif message["type"] == "http.response.body" and not done:
                done = True
                await send({"type": "http.response.body", "body": b"", "more_body": False})

        await self.app({**scope, "method": "GET"}, receive, head_send)


app.add_middleware(HeadMiddleware)

# Every page is served from this origin: fonts are self-hosted, scripts and styles are inline or
# local, and the only third-party URLs are links a reader clicks. So the policy can be 'self'.
# /docs and /redoc load Swagger UI from a CDN and are left without it.
CSP = ("default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
       "media-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'; object-src 'none'")
SECURITY_HEADERS = {"X-Content-Type-Options": "nosniff", "Referrer-Policy": "strict-origin-when-cross-origin",
                    "Permissions-Policy": "camera=(), microphone=(), geolocation=()"}
# Read-only JSON and text any page or agent may fetch cross-origin. /mcp is not here: it keeps its
# Origin allowlist, which the transport spec requires against DNS rebinding.
CORS_READ = ("/api/", "/r/", "/llms.txt", "/feed.", "/.well-known/", "/demo.vtt")
SKILL_INVOKE = re.compile(r"/api/skills/[^/]+/invoke")


@app.middleware("http")
async def security_headers(request: Request, call_next: Any) -> Response:
    path = request.url.path
    if request.method == "OPTIONS" and SKILL_INVOKE.fullmatch(path):
        # the preflight a browser sends before a cross-origin POST with a JSON body
        return Response(status_code=204, headers={
            "Access-Control-Allow-Origin": "*", "Access-Control-Allow-Methods": "GET, POST",
            "Access-Control-Allow-Headers": "content-type", "Access-Control-Max-Age": "86400"})
    response = await call_next(request)
    response.headers.update(SECURITY_HEADERS)
    if not path.startswith(("/docs", "/redoc")):
        response.headers["Content-Security-Policy"] = CSP
    if (request.method in ("GET", "HEAD") and path.startswith(CORS_READ)) or SKILL_INVOKE.fullmatch(path):
        response.headers["Access-Control-Allow-Origin"] = "*"
    return response


def _ledger() -> Ledger:
    # ponytail: one connection per request; sqlite objects cannot cross FastAPI's worker threads
    return Ledger(os.environ.get("NOTA_DB", "nota.db"))


def _base(request: Request) -> str:
    """The absolute origin links are built on: NOTA_PUBLIC_URL when set, else what the request came in on.
    The fallback comes from the Host header, so anything put into HTML goes through html.escape."""
    return os.environ.get("NOTA_PUBLIC_URL", "").rstrip("/") or str(request.base_url).rstrip("/")


FEED_LINKS = ['<link rel="alternate" type="application/atom+xml" title="Nota receipts and settled RYO plans" href="/feed.xml">',
              '<link rel="alternate" type="application/feed+json" title="Nota receipts and settled RYO plans" href="/feed.json">']


def _page(file: str, request: Request, path: str, extra: tuple[str, ...] | list[str] = (), status: int = 200,
          image: str = "/img/card.png") -> HTMLResponse:
    """A static page with its head completed at <!--OG-->: canonical, og:url, og:image and the feed links,
    all absolute, because link previews and crawlers resolve nothing relative in these tags."""
    base = html.escape(_base(request))
    tags = [f'<link rel="canonical" href="{base}{path}">', f'<meta property="og:url" content="{base}{path}">',
            f'<meta property="og:image" content="{base}{image}">', f'<meta name="twitter:image" content="{base}{image}">',
            '<meta name="twitter:card" content="summary_large_image">', *FEED_LINKS, *extra]
    page = (STATIC / file).read_text(encoding="utf-8")
    return HTMLResponse(page.replace("<!--OG-->", "\n".join(tags), 1), status_code=status)


def _hreflang(request: Request) -> list[str]:
    base = html.escape(_base(request))
    return [f'<link rel="alternate" hreflang="en" href="{base}/">', f'<link rel="alternate" hreflang="ja" href="{base}/ja">',
            f'<link rel="alternate" hreflang="x-default" href="{base}/">']


def _receipt(led: Ledger, id: str) -> Receipt:
    raw = led.get_decision(id)
    if raw is None:
        raise HTTPException(404, f"no decision {id}")
    return Receipt.model_validate_json(raw)


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


NOISE = re.compile(r"\.(since|fetched_at|as_of|window_hours|snippet|samples\.\d+\..*|sources\.\d+\.content)$")


def _leaves(pack: EvidencePack) -> dict[str, Any]:
    """Leaf values under `<section>.data`. A list of scalars is one leaf (sorted), so a re-ordered domain
    list is not a change; timestamps and free text that differ on every run are skipped."""
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
    """The receipt stored right before this one for the same symbol, by the ledger's own ordering."""
    ids = [d["id"] for d in led.list_decisions(limit=200, symbol=r.symbol)]
    if r.id not in ids or ids.index(r.id) + 1 >= len(ids):
        return None
    return _receipt(led, ids[ids.index(r.id) + 1])


@app.get("/api/decisions")
def list_decisions(symbol: str | None = None, limit: int = Query(50, ge=1, le=200)) -> list[dict[str, Any]]:
    led = _ledger()
    rows = led.list_decisions(limit=limit, symbol=symbol.upper() if symbol else None)
    return [_summary(led, _receipt(led, d["id"])) for d in rows]


@app.get("/api/decisions/{id}")
def get_decision(id: str) -> dict[str, Any]:
    led = _ledger()
    r = _receipt(led, id)
    prev = _previous(led, r)
    out = led.get_outcome(id)
    return {"receipt": r.model_dump(mode="json"), "previous_id": prev.id if prev else None,
            "changes": what_changed(led, r, prev), "outcome": json.loads(out) if out else None, "degraded": _degraded(r),
            "backing": _backing_counts(led, id), **_pack_views(led, r)}


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


@app.get("/api/scores")
def scores() -> dict[str, Any]:
    led = _ledger()
    s = role_scores(led)
    return {"scores": s, "weights": role_weights(s), "resolved": len(led.list_outcomes()), "unresolved": len(led.unresolved()),
            "n_independent": scored_independent(led), "meaningful_at": MEANINGFUL_N,
            "reliability": reliability(led), "sources": source_scores(led), "vs_base_rate": skill_vs_base(led),
            "vs_ryo": skill_vs_ryo(led)}


@app.get("/api/scorecard")
def scorecard(horizon: int = 24) -> dict[str, Any]:
    """RYO's own plans, locked daily and settled on OKX (nota.scorecard). A snapshot from before the
    scorecard existed has no tables yet, which is an empty scorecard, not an error."""
    from nota.scorecard import HORIZONS_H, summary

    if horizon not in HORIZONS_H:
        raise HTTPException(422, f"horizon must be one of {list(HORIZONS_H)}")
    led = _ledger()
    try:
        # The contrasts run permutation tests, so a summary is computed once per ledger state (and per
        # ten minutes, which is how fresh the `overdue` flags need to be), not once per request.
        state = led.conn.execute("SELECT (SELECT COUNT(*) FROM locks), (SELECT MAX(locked_at) FROM locks), "
                                 "(SELECT COUNT(*) FROM settlements), (SELECT MAX(settled_at) FROM settlements)").fetchone()
    except sqlite3.OperationalError:
        return summary(Ledger(":memory:"), horizon)
    key = (os.environ.get("NOTA_DB", "nota.db"), horizon, tuple(state), int(time.time() // 600))
    if key not in _scorecard_cache:
        if len(_scorecard_cache) > 16:
            _scorecard_cache.clear()
        _scorecard_cache[key] = summary(led, horizon)
    return _scorecard_cache[key]


_scorecard_cache: dict[tuple[Any, ...], dict[str, Any]] = {}


@app.get("/scorecard")
def scorecard_page(request: Request) -> HTMLResponse:
    """The page plus a schema.org Dataset, so a dataset search engine can find and cite the settled plans."""
    base = _base(request)
    try:
        first = _ledger().conn.execute("SELECT MIN(locked_at) FROM locks").fetchone()[0]
    except sqlite3.OperationalError:          # a snapshot from before the scorecard existed
        first = None
    today = datetime.now(timezone.utc).date().isoformat()
    dataset = {
        "@context": "https://schema.org", "@type": "Dataset", "name": "RYO Verdict Scorecard",
        "description": ("RYO's deep_analysis trade plans for 25 major tokens, locked once a day before the outcome "
                        "is known and settled on OKX hourly candles at 24 and 72 hours: which level was touched first, "
                        "after how many hours, and the return at the horizon."),
        "url": f"{base}/scorecard", "license": "https://creativecommons.org/licenses/by/4.0/",
        "isAccessibleForFree": True,
        "creator": {"@type": "Organization", "name": "Nota", "url": "https://github.com/PugarHuda/nota"},
        "keywords": ["crypto", "trade plans", "forecast verification", "RYO", "OKX"],
        "temporalCoverage": f"{first[:10]}/{today}" if first else today,
        "distribution": [
            {"@type": "DataDownload", "encodingFormat": "application/json", "contentUrl": f"{base}/api/scorecard?horizon=24"},
            {"@type": "DataDownload", "encodingFormat": "application/json", "contentUrl": f"{base}/api/scorecard?horizon=72"},
            {"@type": "DataDownload", "encodingFormat": "text/csv", "contentUrl": f"{base}/api/scorecard.csv"}],
    }
    # '<' escaped so no value can close the script element early
    ld = json.dumps(dataset).replace("<", "\\u003c")
    return _page("scorecard.html", request, "/scorecard", [f'<script type="application/ld+json">{ld}</script>'])


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


@app.get("/api/health")
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
    }


@app.get("/api/decisions/{id}/replay")
def replay_check(id: str) -> dict[str, Any]:
    led = _ledger()
    r = _receipt(led, id)
    try:
        res = replay(id, led, None)
    except RuntimeError as exc:
        return {"identical": None, "diff": [], "error": str(exc)}
    return {"identical": res.identical, "diff": res.diff, "error": None}


# --- Nota as an MCP server: the same seven skills, over the Streamable HTTP transport ------------
MCP_BATCH_MAX = 25
ALLOWED_ORIGIN_HOSTS = {"nota-ryo.vercel.app", "ryo-arena.vercel.app", "localhost", "127.0.0.1", "testserver"}


def _origin_ok(request: Request) -> bool:
    """The transport spec requires servers to validate Origin against DNS rebinding. A request with
    no Origin is a non-browser client (curl, an MCP host) and is allowed."""
    origin = request.headers.get("origin")
    if not origin:
        return True
    host = urlparse(origin).hostname or ""
    return host in ALLOWED_ORIGIN_HOSTS


MCP_BODY_MAX = 1_000_000


def _rpc_error(status: int, code: int, message: str, data: Any = None, headers: dict[str, str] | None = None,
               id_: Any = None) -> JSONResponse:
    """A transport-level refusal, still as JSON-RPC: an MCP client parses the body, not {detail}."""
    err: dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        err["data"] = data
    return JSONResponse({"jsonrpc": "2.0", "id": id_, "error": err}, status_code=status, headers=headers)


def _metered(m: dict[str, Any]) -> int:
    """What a message spends of the skill budget: a tools/call request costs what the same call costs
    over REST (`_skill_cost`); everything else touches nothing outside this process and is free."""
    params = m.get("params")
    if "id" not in m or m.get("method") != "tools/call" or not isinstance(params, dict):
        return 0
    return _skill_cost(params.get("name"), params.get("arguments"))


def _safe_handle(m: dict[str, Any], deps: dict[str, Any]) -> dict[str, Any] | None:
    try:
        return mcp_handle(m, deps)
    except Exception:          # one broken message must not take the batch, or the process, with it
        logging.getLogger("nota.mcp").exception("mcp message failed")
        return {"jsonrpc": "2.0", "id": m.get("id"), "error": {"code": -32603, "message": "internal error"}}


@app.post("/mcp", include_in_schema=False)
async def mcp_endpoint(request: Request) -> Response:
    if not _origin_ok(request):
        return _rpc_error(403, -32600, "origin not allowed")
    version = request.headers.get("mcp-protocol-version")
    if version is not None and version not in SUPPORTED_PROTOCOLS:
        return _rpc_error(400, UNSUPPORTED_VERSION, "Unsupported protocol version",
                          {"supported": list(SUPPORTED_PROTOCOLS), "requested": version})
    if int(request.headers.get("content-length") or 0) > MCP_BODY_MAX:
        return _rpc_error(413, -32600, f"body exceeds {MCP_BODY_MAX} bytes")
    raw = bytearray()
    async for chunk in request.stream():   # chunked bodies carry no length, so cap while reading
        raw += chunk
        if len(raw) > MCP_BODY_MAX:
            return _rpc_error(413, -32600, f"body exceeds {MCP_BODY_MAX} bytes")
    try:
        body = json.loads(raw)
    except (ValueError, RecursionError):
        return _rpc_error(400, -32700, "parse error: body is not JSON this server can read")
    batch = body if isinstance(body, list) else [body]
    if not batch or not all(isinstance(m, dict) for m in batch):
        return _rpc_error(400, -32600, "expected a JSON-RPC message or a batch")
    if len(batch) > MCP_BATCH_MAX:
        return _rpc_error(400, -32600, f"batch of {len(batch)} exceeds the limit of {MCP_BATCH_MAX}")
    if version is not None:                # 2026-07-28: the header must match the body's own claim
        for m in batch:
            meta = m.get("params", {}).get("_meta") if isinstance(m.get("params"), dict) else None
            claimed = meta.get(VERSION_META) if isinstance(meta, dict) else None
            if claimed is not None and claimed != version:
                return _rpc_error(400, HEADER_MISMATCH, f"Header mismatch: MCP-Protocol-Version {version!r} "
                                  f"does not match body {claimed!r}", id_=m.get("id"))
    # tools/call reaches third-party APIs, so it is metered exactly like the REST skill route. The
    # endpoint is public and unauthenticated by design, which makes it an amplifier without this.
    # The whole batch is checked before any of it is charged, so a refused batch costs nothing.
    calls = sum(_metered(m) for m in batch)
    if calls:
        ip = request.client.host if request.client else "unknown"
        wait = _throttle_n(f"skill:{ip}", calls, limit=60)
        if wait is not None:
            return _rpc_error(429, -32000, "rate limited", {"retry_after_s": wait},
                              headers={"Retry-After": str(wait)})
    deps: dict[str, Any] = {}
    # skills do blocking network I/O; off the event loop so one slow source does not stall the server
    replies = [r for r in await run_in_threadpool(lambda: [_safe_handle(m, deps) for m in batch]) if r is not None]
    if not replies:                       # only notifications or responses came in
        return Response(status_code=202)
    if isinstance(body, list):
        return JSONResponse(replies)
    code = replies[0].get("error", {}).get("code")
    # 2026-07-28 transport: an unknown method is 404, a version problem is 400, both with the JSON-RPC body
    status = 404 if code == -32601 else 400 if code in (UNSUPPORTED_VERSION, HEADER_MISMATCH) else 200
    return JSONResponse(replies[0], status_code=status)


@app.get("/mcp", include_in_schema=False)
@app.delete("/mcp", include_in_schema=False)
def mcp_no_stream() -> Response:
    """Stateless server: no server-initiated stream and no session to delete, which the spec
    answers with 405 rather than pretending to hold one."""
    return Response(status_code=405, headers={"Allow": "POST"})


@app.get("/.well-known/mcp/server.json", include_in_schema=False)
@app.get("/server.json", include_in_schema=False)
def mcp_server_json() -> FileResponse:
    """The official MCP registry's server.json, served from the deployment it describes so a client
    or a sub-registry can discover this server without going through GitHub."""
    path = Path(__file__).resolve().parents[1] / "server.json"
    if not path.exists():
        raise HTTPException(404, "server.json is not bundled in this checkout")
    return FileResponse(path, media_type="application/json")


@app.get("/llms.txt", include_in_schema=False)
def llms_txt(request: Request) -> PlainTextResponse:
    """The llms.txt convention: one page that tells an agent what is here and how to call it,
    instead of making it infer the API from HTML. Generated, so it cannot drift from the routes."""
    base = _base(request)
    led = _ledger()
    nl = chr(10)
    skills = nl.join(f"- `{d.name}`: {d.description}" for d in skill_definitions())
    pages = _llms_pages(base)
    receipts = nl.join(
        f"- [{d['symbol']} {d['id']}]({base}/r/{d['id']}.md) recorded {d['created_at']}"
        for d in led.list_decisions(limit=20))
    return PlainTextResponse(f"""# Nota

> A council of AI agents argues over live RYO market evidence and records each call as a decision
> receipt that replays identically from the ledger. Read-only research: no order is ever placed.

Every number in a receipt carries the dotted RYO path it came from, its `as_of` and its `data_mode`.
A value that could not be fetched stays null and is never turned into zero. Nota also recomputes
RYO's own RSI and ATR from public candles and checks its price against three exchanges, and the
judge refuses to size a trade when they disagree.

## Call the skills over MCP

POST {base}/mcp speaks the Model Context Protocol over Streamable HTTP (stateless; versions
{', '.join(SUPPORTED_PROTOCOLS)} are all accepted, and `server/discover` lists them).

- `tools/list`, `tools/call` for the seven research skills below; each is read-only and declares its
  output schema
- `resources/list`, `resources/read` for every receipt, addressed as `nota://receipt/<id>` (markdown)
  or `nota://receipt/<id>.json`, plus `ui://nota/receipt`, an MCP Apps view that renders any tool result
- `prompts/list`, `prompts/get` for {', '.join(f'`{n}`' for n in PROMPTS)}
- `completion/complete` offers the receipt ids and symbols this ledger actually holds

```
curl -s {base}/mcp -H 'content-type: application/json' \\
  -d '{{"jsonrpc":"2.0","id":1,"method":"tools/list"}}'
```

## Skills

{skills}

Each returns RYO's public envelope: `status`, `data_mode`, `as_of`, `availability` per source and
`warnings`. They are also served over plain HTTP at `{base}/api/skills/` and
`POST {base}/api/skills/<name>/invoke`.

## Receipts in this ledger

{receipts}

Each receipt is also available as `{base}/r/<id>.json` and as a card image at `{base}/r/<id>.png`,
and `{base}/api/decisions/<id>/replay` re-runs it from the stored evidence and reports whether the
result is identical.

## Scorecard

RYO's own deep_analysis trade plans for 25 majors are locked once a day, before the outcome is known,
and settled on OKX hourly candles (1 m candles inside an hour that touched both levels) at 24 and
72 hours: which of stop and first target was touched first, and the return at the horizon.

- [Scorecard]({base}/scorecard): the page, with a schema.org Dataset description
- `{base}/api/scorecard?horizon=24` and `?horizon=72`: the summary and every row as JSON
- `{base}/api/scorecard.csv`: every lock at both horizons, failures included

## Agent to agent (A2A 1.0)

The Agent Card at `{base}/.well-known/agent-card.json` lists the same skills. `POST {base}/a2a` takes
the JSON-RPC `SendMessage` method with header `A2A-Version: 1.0` and a data part `{{"skill", "args"}}`
(or text such as `price_crosscheck SOL`), and returns a completed task whose artifact is the envelope.

```
curl -s {base}/a2a -H 'content-type: application/json' -H 'A2A-Version: 1.0' \\
  -d '{{"jsonrpc":"2.0","id":1,"method":"SendMessage","params":{{"message":{{"messageId":"m1","role":"ROLE_USER","parts":[{{"data":{{"skill":"price_crosscheck","args":{{"symbol":"SOL"}}}}}}]}}}}}}'
```

## Pages and data

{pages}
""", media_type="text/plain; charset=utf-8")


# What llms.txt says about each public route. The list is filtered by the routes the app really has,
# so a route that is removed drops out of llms.txt instead of becoming a dead link.
LLMS_PAGES = {
    "/": "Overview: what Nota claims and how to check it",
    "/ja": "The overview in Japanese",
    "/app": "Dashboard: every receipt, diffed against the one before it",
    "/scorecard": "RYO Verdict Scorecard",
    "/demo": "Narrated three-minute walkthrough with a clickable transcript",
    "/demo.json": "Walkthrough chapters and transcript with the second each line was spoken",
    "/demo.vtt": "Walkthrough captions (WebVTT)",
    "/api/decisions": "Receipt summaries, newest first (JSON)",
    "/api/positions": "Open practice positions against the latest evidence price",
    "/api/scores": "Brier score per council role, against the base rate and against RYO's own call",
    "/api/backers": "Handles that backed or faded receipts, and how often they were right",
    "/api/outcomes.csv": "Every resolved decision: each role's p_up_7d, the base rate, what happened, Brier",
    "/api/scorecard.csv": "Every scorecard lock at both horizons",
    "/feed.xml": "Atom feed of receipts, their outcomes and settled RYO plans",
    "/feed.json": "The same feed as JSON Feed 1.1",
    "/api/health": "What this deployment can and cannot do right now",
    "/docs": "OpenAPI",
    "/.well-known/mcp/server.json": "This server's entry for the official MCP registry",
    "/.well-known/agent-card.json": "A2A Agent Card",
    "/sitemap.xml": "Sitemap",
}


def _llms_pages(base: str) -> str:
    have = {getattr(r, "path", None) for r in app.routes}
    return chr(10).join(f"- [{desc}]({base}{path})" for path, desc in LLMS_PAGES.items() if path in have)


@app.get("/r/{id}.md")
def receipt_markdown(id: str) -> PlainTextResponse:
    return PlainTextResponse(render_markdown(_receipt(_ledger(), id)), media_type="text/markdown; charset=utf-8")


@app.get("/r/{id}.json")
def receipt_json(id: str) -> dict[str, Any]:
    return _receipt(_ledger(), id).model_dump(mode="json")


# --- Track 3: skills exposed on RYO's own paths (/api/skills, SkillCallRequest/Response) -------------
class SkillCallRequest(BaseModel):
    name: str | None = None
    args: dict[str, Any] = Field(default_factory=dict)
    conversation_id: str | None = None


@app.get("/api/skills/")
def list_skills() -> list[dict[str, Any]]:
    return [d.model_dump() for d in skill_definitions()]


@app.get("/api/skills/{name}")
def skill_def(name: str) -> dict[str, Any]:
    for d in skill_definitions():
        if d.name == name:
            return d.model_dump()
    raise HTTPException(404, f"unknown skill {name}")


@app.post("/api/skills/{name}/invoke")
def invoke_skill(name: str, body: SkillCallRequest, request: Request) -> dict[str, Any]:
    """RYO's SkillCallResponse shape: name, status, result (our envelope), latency_ms, xp, guard_decision."""
    if body.name and body.name != name:
        raise HTTPException(422, "body.name does not match the path")
    _throttle(f"skill:{request.client.host if request.client else 'unknown'}", limit=60, cost=_skill_cost(name, body.args))
    deps = live_deps(name, body.args) if name in SKILLS else {}
    started = time.time()
    try:
        env = skill_invoke(name, body.args, **deps)
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    # SkillCastStatus enum in RYO's OpenAPI: pending | running | success | error
    return {"name": name, "status": "success" if env.status != "unavailable" else "error", "result": env.model_dump(mode="json"),
            "latency_ms": int((time.time() - started) * 1000), "xp": 0, "guard_decision": None}


# --- SocialFi: public backing of calls, scored against outcomes ------------------------------
HANDLE = re.compile(r"[A-Za-z0-9_.\-]{3,32}")  # fullmatch: `$` would also accept a trailing newline


class BackingIn(BaseModel):
    handle: str = Field(description="X / Discord / Telegram handle, 3-32 chars")
    stance: Literal["agree", "disagree"]
    token: str | None = Field(None, max_length=128, description="the edit token returned by this handle's first backing")


def _backing_counts(led: Ledger, id: str) -> dict[str, Any]:
    rows = store_for(led).backings(id)
    return {"agree": sum(r["stance"] == "agree" for r in rows), "disagree": sum(r["stance"] == "disagree" for r in rows),
            "handles": [{"handle": r["handle"], "stance": r["stance"]} for r in rows[-20:]]}


_BACKING_HITS: dict[str, list[float]] = {}
BACKING_LIMIT, BACKING_WINDOW = 30, 3600.0
HITS_MAX_KEYS = 10_000   # a flood of distinct addresses clears the table rather than growing it without bound


def _throttle_n(key: str, n: int, limit: int, now: float | None = None) -> int | None:
    """Record n hits if all n fit in the window; otherwise record nothing and return the seconds until
    the oldest hit ages out, which is when a retry can succeed.

    With DATABASE_URL the count lives in Postgres, so every serverless instance shares one budget
    instead of each granting its own. Without it, or if Postgres cannot be reached, the count is
    this process's own: a limiter that fails closed would take the whole public surface down with
    the database."""
    if n <= 0:
        return None
    dsn = os.environ.get("DATABASE_URL", "").strip()
    if dsn and now is None:           # an explicit `now` is a test's clock, which only the local count follows
        try:
            allowed, wait = PostgresBackings(dsn).hit(key, n, limit, BACKING_WINDOW)
            return None if allowed else wait
        except Exception:
            logging.getLogger("nota.api").exception("shared rate limit unreachable; counting in this process")
    now = now if now is not None else time.time()
    # hits are appended in time order, so a key whose newest hit is expired is wholly expired
    for k in [k for k, v in _BACKING_HITS.items() if now - v[-1] >= BACKING_WINDOW]:
        del _BACKING_HITS[k]
    if len(_BACKING_HITS) > HITS_MAX_KEYS:
        _BACKING_HITS.clear()
    hits = [t for t in _BACKING_HITS.get(key, []) if now - t < BACKING_WINDOW]
    if len(hits) + n > limit:
        if hits:
            _BACKING_HITS[key] = hits
        return max(1, math.ceil(hits[0] + BACKING_WINDOW - now)) if hits else int(BACKING_WINDOW)
    _BACKING_HITS[key] = hits + [now] * n
    return None


def _throttle(key: str, now: float | None = None, limit: int = BACKING_LIMIT, cost: int = 1) -> None:
    wait = _throttle_n(key, cost, limit, now)
    if wait is not None:
        raise HTTPException(429, f"too many requests from this address; limit {limit} per hour",
                            headers={"Retry-After": str(wait)})


def _skill_cost(name: Any, args: Any) -> int:
    """Units of the hourly budget one call spends: roughly the upstream fetches it makes. One
    narrative call with 20 voices is 20 fetches, so it costs 20, not the 1 a price check costs;
    the ledger-only track record fetches nothing and is free."""
    if name == "verdict_track_record":
        return 0
    if name == "narrative_convergence":
        voices = args.get("voices") if isinstance(args, dict) else None
        return len(voices) if isinstance(voices, list) and voices else 1
    return 2 if name == "news_verify" else 1


@app.post("/api/decisions/{id}/back")
def back_decision(id: str, body: BackingIn, request: Request) -> dict[str, Any]:
    """One stance per handle per receipt, latest wins. There are no accounts: the first post under a
    handle claims it and gets an edit token back once, and every later post under that handle has to
    carry the token. Only its SHA-256 is stored, so a leaked database does not leak the tokens."""
    handle = body.handle.strip(" ").lstrip("@").lower()  # "@Abc" and "abc" are one backer; a newline is refused, not trimmed
    if not HANDLE.fullmatch(handle):
        raise HTTPException(422, "handle must be 3-32 characters: letters, digits, _ . - (a leading @ is dropped)")
    _throttle(request.client.host if request.client else "unknown")
    led = _ledger()
    store = store_for(led)
    if store is led and led.readonly:
        raise HTTPException(503, "this is a read-only demo deployment over a ledger snapshot and no DATABASE_URL is set; "
                                 "backing works on a writable `nota serve`")
    _receipt(led, id)
    new_token = None
    stored = store.token_sha(handle)
    if stored is None:
        new_token = secrets.token_urlsafe(24)
        if not store.claim(handle, hashlib.sha256(new_token.encode()).hexdigest()):
            stored = store.token_sha(handle)   # someone claimed it between the read and the write
            new_token = None
    if new_token is None:
        sent = hashlib.sha256((body.token or "").encode()).hexdigest()
        if not stored or not hmac.compare_digest(sent, stored):
            raise HTTPException(409, "handle already claimed; send its edit token")
    store.add_backing(id, handle, body.stance)
    out = _backing_counts(led, id)
    if new_token:
        out["edit_token"] = new_token   # shown once; the server keeps only its hash
    return out


def backer_correct(stance: str, action: str, went_up: bool) -> bool | None:
    """A backer is right when their stance matched how the call turned out; no_trade calls are not scored."""
    if action not in ("long", "short"):
        return None
    call_right = (action == "long") == went_up
    return call_right if stance == "agree" else not call_right


@app.get("/api/backers")
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


@app.get("/r/{id}.png")
def receipt_card(id: str) -> Response:
    return Response(render_card(_receipt(_ledger(), id)), media_type="image/png", headers={"Cache-Control": "public, max-age=300"})


@app.get("/favicon.ico", include_in_schema=False)
def favicon() -> Response:
    return Response(status_code=204)


@app.get("/")
def landing(request: Request) -> HTMLResponse:
    """The reading room: what Nota claims and how to check it. The instrument itself is /app."""
    return _page("landing.html", request, "/", _hreflang(request))


@app.get("/ja")
def landing_ja(request: Request) -> HTMLResponse:
    """The same reading room in Japanese. Same script, same ids, translated prose only."""
    return _page("landing.ja.html", request, "/ja", _hreflang(request))


@app.get("/landing.js", include_in_schema=False)
def landing_script() -> FileResponse:
    # One behaviour and one appearance shared by every language of the landing, so a translation
    # cannot drift into being a different page.
    return FileResponse(STATIC / "landing.js", media_type="application/javascript")


@app.get("/landing.css", include_in_schema=False)
def landing_style() -> FileResponse:
    return FileResponse(STATIC / "landing.css", media_type="text/css")


FONTS = {p.name for p in (STATIC / "fonts").glob("*.woff2")}  # served by name from this set only


@app.get("/fonts/{name}", include_in_schema=False)
def font(name: str) -> FileResponse:
    """The five Latin subsets the pages set type in (SIL OFL), self-hosted so a page never waits on
    or fails over a third-party font request."""
    if name not in FONTS:
        raise HTTPException(404, "no such font")
    return FileResponse(STATIC / "fonts" / name, media_type="font/woff2",
                        headers={"Cache-Control": "public, max-age=31536000, immutable"})


@app.get("/app")
def index(request: Request) -> HTMLResponse:
    # index.html has no og:title of its own: /r/<id> serves the same file with the receipt's
    return _page("index.html", request, "/app", [
        '<meta property="og:title" content="Nota receipts dashboard">',
        '<meta property="og:description" content="Every decision receipt, diffed against the one before it, '
        'with the evidence path behind each number and a replay check.">',
        '<meta property="og:type" content="website">'], image="/img/dashboard.png")


@app.get("/img/{name}.png", include_in_schema=False)
def landing_image(name: str) -> FileResponse:
    path = (STATIC / "img" / f"{name}.png").resolve()
    if path.parent != (STATIC / "img").resolve() or not path.exists():
        raise HTTPException(404, "no such image")
    return FileResponse(path, media_type="image/png")


@app.get("/demo")
def demo_page(request: Request) -> HTMLResponse:
    """The walkthrough with its transcript, so the video is watchable and readable at one URL. og:video must
    be absolute for a link preview to play it inline; 1280x720 is what the recorder renders."""
    base = html.escape(_base(request))
    return _page("demo.html", request, "/demo", [
        f'<meta property="og:video" content="{base}/demo.mp4">', f'<meta property="og:video:secure_url" content="{base}/demo.mp4">',
        '<meta property="og:video:type" content="video/mp4">', '<meta property="og:video:width" content="1280">',
        '<meta property="og:video:height" content="720">'], image="/img/demo-poster.png")


def _vtt_time(t: float) -> str:
    ms = int(round(t * 1000))
    return f"{ms // 3_600_000:02d}:{ms // 60_000 % 60:02d}:{ms // 1000 % 60:02d}.{ms % 1000:03d}"


@app.get("/demo.vtt", include_in_schema=False)
def demo_captions() -> Response:
    """WebVTT captions from the same chapters the transcript shows: a cue runs from its line's start to
    the next line's, so the caption stays up through the pause, and the last one to the end of the video.
    Lines are not broken by hand: a player wraps to its own width, and a fixed break at 80 characters
    left one-word orphan lines on a 650 px video."""
    path = STATIC / "demo.json"
    if not path.exists():
        raise HTTPException(404, "demo chapters not bundled in this checkout")
    data = json.loads(path.read_text(encoding="utf-8"))
    ch = data["chapters"]
    cues = ["WEBVTT", ""]
    for i, c in enumerate(ch):
        end = ch[i + 1]["start"] if i + 1 < len(ch) else data["seconds"]
        cues += [c["id"], f"{_vtt_time(c['start'])} --> {_vtt_time(end)}", html.escape(c["text"], quote=False), ""]
    return Response("\n".join(cues), media_type="text/vtt; charset=utf-8")


@app.get("/demo.mp4", include_in_schema=False)
def demo_video() -> FileResponse:
    """The submission walkthrough, served from the app itself so the demo URL needs no third party."""
    path = STATIC / "demo.mp4"
    if not path.exists():
        raise HTTPException(404, "demo video not bundled in this checkout")
    return FileResponse(path, media_type="video/mp4")


@app.get("/demo.json", include_in_schema=False)
def demo_chapters() -> FileResponse:
    """Chapters and transcript, written by the recorder from the seconds each line was really spoken."""
    path = STATIC / "demo.json"
    if not path.exists():
        raise HTTPException(404, "demo chapters not bundled in this checkout")
    return FileResponse(path, media_type="application/json")


# --- Discovery and open data: robots, sitemap, feeds, CSV --------------------------------------------
@app.get("/robots.txt", include_in_schema=False)
def robots(request: Request) -> PlainTextResponse:
    return PlainTextResponse(f"User-agent: *\nAllow: /\n\nSitemap: {_base(request)}/sitemap.xml\n")


SITEMAP_PAGES = ("/", "/ja", "/app", "/scorecard", "/demo")


@app.get("/sitemap.xml", include_in_schema=False)
def sitemap(request: Request) -> Response:
    """Every page plus every receipt permalink. The two landings name each other as language alternates."""
    base = _base(request)
    sm, xh = "http://www.sitemaps.org/schemas/sitemap/0.9", "http://www.w3.org/1999/xhtml"
    ET.register_namespace("", sm)
    ET.register_namespace("xhtml", xh)
    root = ET.Element(f"{{{sm}}}urlset")

    def add(path: str, lastmod: str | None = None) -> ET.Element:
        u = ET.SubElement(root, f"{{{sm}}}url")
        ET.SubElement(u, f"{{{sm}}}loc").text = base + path
        if lastmod:
            ET.SubElement(u, f"{{{sm}}}lastmod").text = lastmod
        return u

    for path in SITEMAP_PAGES:
        u = add(path)
        if path in ("/", "/ja"):
            for lang, href in (("en", "/"), ("ja", "/ja"), ("x-default", "/")):
                ET.SubElement(u, f"{{{xh}}}link", rel="alternate", hreflang=lang, href=base + href)
    for d in _ledger().list_decisions(limit=200):
        add(f"/r/{d['id']}", d["created_at"])
    return Response(ET.tostring(root, encoding="unicode", xml_declaration=True), media_type="application/xml")


FEED_TITLE = "Nota: decision receipts and settled RYO plans"


def _settled_locks(led: Ledger) -> list[dict[str, Any]]:
    """Every settled scorecard plan at both horizons, newest first; empty for a ledger without the tables."""
    from nota.scorecard import HORIZONS_H, summary

    try:
        rows = [{**r, "horizon_h": h} for h in HORIZONS_H for r in summary(led, h, stats=False)["settled"]]
    except sqlite3.OperationalError:
        return []
    return sorted(rows, key=lambda r: r["settles_at"], reverse=True)


def _feed_items(request: Request) -> list[dict[str, Any]]:
    """The newest 50 receipts and every settled plan, as neutral items both feed formats are written from."""
    base, led = _base(request), _ledger()
    items = []
    for d in led.list_decisions(limit=50):
        r = _receipt(led, d["id"])
        text = f"Council: {r.verdict.action}, p_up_7d {r.verdict.p_up_7d:.2f}."
        down = sorted(k for k, v in r.availability.items() if v not in OK)
        text += f" Degraded sections: {', '.join(down)}." if down else " Every evidence section answered."
        updated = r.created_at
        out = led.get_outcome(r.id)
        if out:
            o = json.loads(out)
            updated = o.get("resolved_at") or updated
            text += (f" Resolved: {'up' if o['went_up'] else 'down'} {o['return_pct']:+.2f}% after 7 days,"
                     f" judge Brier {o['brier'].get('judge')}.")
        items.append({"id": f"urn:nota:receipt:{r.id}", "url": f"{base}/r/{r.id}", "title": r.headline,
                      "summary": text, "published": r.created_at, "updated": updated, "tags": [r.symbol, "receipt"]})
    for s in _settled_locks(led):
        ret = s.get("return_at_horizon_pct")
        items.append({"id": f"urn:nota:lock:{s['id']}:{s['horizon_h']}", "url": f"{base}/scorecard?h={s['horizon_h']}",
                      "title": f"{s['symbol']}: RYO {s['verdict'] or 'no verdict'} plan ({s['side']}) settled {s['result']} at {s['horizon_h']}h",
                      "summary": (f"Locked {s['locked_at']}, confluence {s['confluence_state']}. First touch: {s['result']}"
                                  + (f" after {s['hours_to_touch']} h" if s.get("hours_to_touch") is not None else "")
                                  + (f"; return at the horizon {ret:+.2f}%." if isinstance(ret, (int, float)) else ".")
                                  + " Settled on OKX hourly candles."),
                      "published": s["settles_at"], "updated": s["settles_at"], "tags": [s["symbol"], "scorecard"]})
    return items


@app.get("/feed.xml", include_in_schema=False)
def feed_atom(request: Request) -> Response:
    """Atom 1.0 (RFC 4287): subscribe once and see each receipt, its outcome, and each settled RYO plan."""
    a = "http://www.w3.org/2005/Atom"
    ET.register_namespace("", a)
    base, items = _base(request), _feed_items(request)
    root = ET.Element(f"{{{a}}}feed")
    ET.SubElement(root, f"{{{a}}}id").text = "urn:nota:feed"
    ET.SubElement(root, f"{{{a}}}title").text = FEED_TITLE
    ET.SubElement(root, f"{{{a}}}updated").text = max((i["updated"] for i in items), default=now_iso())
    ET.SubElement(ET.SubElement(root, f"{{{a}}}author"), f"{{{a}}}name").text = "Nota"
    ET.SubElement(root, f"{{{a}}}link", rel="self", href=f"{base}/feed.xml")
    ET.SubElement(root, f"{{{a}}}link", rel="alternate", href=f"{base}/")
    for i in items:
        e = ET.SubElement(root, f"{{{a}}}entry")
        ET.SubElement(e, f"{{{a}}}id").text = i["id"]
        ET.SubElement(e, f"{{{a}}}title").text = i["title"]
        ET.SubElement(e, f"{{{a}}}link", rel="alternate", href=i["url"])
        ET.SubElement(e, f"{{{a}}}published").text = i["published"]
        ET.SubElement(e, f"{{{a}}}updated").text = i["updated"]
        ET.SubElement(e, f"{{{a}}}summary").text = i["summary"]
        for t in i["tags"]:
            ET.SubElement(e, f"{{{a}}}category", term=t)
    return Response(ET.tostring(root, encoding="unicode", xml_declaration=True), media_type="application/atom+xml")


@app.get("/feed.json", include_in_schema=False)
def feed_json(request: Request) -> JSONResponse:
    """The same items as JSON Feed 1.1."""
    base = _base(request)
    return JSONResponse({
        "version": "https://jsonfeed.org/version/1.1", "title": FEED_TITLE, "home_page_url": f"{base}/",
        "feed_url": f"{base}/feed.json", "language": "en", "authors": [{"name": "Nota", "url": "https://github.com/PugarHuda/nota"}],
        "description": "Each council decision as a replayable receipt, its 7-day outcome once resolved, and each of RYO's "
                       "own trade plans once settled on OKX candles.",
        "items": [{"id": i["id"], "url": i["url"], "title": i["title"], "content_text": i["summary"], "summary": i["summary"],
                   "date_published": i["published"], "date_modified": i["updated"], "tags": i["tags"]}
                  for i in _feed_items(request)]}, media_type="application/feed+json")


def _csv(header: list[str], rows: list[list[Any]], name: str) -> Response:
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(header)
    w.writerows(["" if v is None else v for v in r] for r in rows)
    return Response(buf.getvalue(), media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": f'inline; filename="{name}"'})


@app.get("/api/scorecard.csv")
def scorecard_csv() -> Response:
    """Every lock at both horizons, one row each, failures included: the scorecard's raw table. Levels are
    RYO's stop and first target re-applied to OKX's price at lock time, the bracket the settlement used."""
    from nota.scorecard import HORIZONS_H, bracket

    led = _ledger()
    try:
        locks = [json.loads(j) for _, j in led.list_locks()]
    except sqlite3.OperationalError:
        locks = []
    rows = []
    for r in locks:
        b = bracket(r) if r["status"] == "locked" else {}
        for h in HORIZONS_H:
            raw = led.get_settlement(r["id"], h)
            s = json.loads(raw) if raw else {}
            source = ("okx:1H+1m" if s.get("resolved_by") == "1m" else "okx:1H") if s.get("status") == "settled" else None
            rows.append([r["id"], r["symbol"], r["locked_at"], h, r["status"], r.get("verdict"), r.get("confluence_state"),
                         b.get("side"), b.get("entry"), b.get("stop"), b.get("target"),
                         s.get("status") or ("open" if r["status"] == "locked" else None), s.get("result"),
                         s.get("hours_to_touch"), s.get("return_at_horizon_pct"), source])
    return _csv(["lock_id", "symbol", "locked_at", "horizon_h", "lock_status", "verdict", "confluence_state", "side",
                 "entry", "stop", "target", "settlement_status", "result", "hours_to_touch", "return_at_horizon_pct",
                 "settlement_source"], rows, "nota-scorecard.csv")


@app.get("/api/outcomes.csv")
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


# --- Nota as an A2A agent: the same skills, discoverable from an Agent Card --------------------------
@app.get("/.well-known/agent-card.json", include_in_schema=False)
def a2a_card(request: Request) -> JSONResponse:
    return JSONResponse(agent_card(_base(request)), headers={"Cache-Control": "public, max-age=3600"})


@app.post("/a2a", include_in_schema=False)
async def a2a_endpoint(request: Request) -> Response:
    """A2A 1.0 JSON-RPC binding. Same Origin allowlist, body cap and skill budget as /mcp, since
    SendMessage reaches the same third-party sources."""
    if not _origin_ok(request):
        return _rpc_error(403, -32600, "origin not allowed")
    if int(request.headers.get("content-length") or 0) > MCP_BODY_MAX:
        return _rpc_error(413, -32600, f"body exceeds {MCP_BODY_MAX} bytes")
    raw = bytearray()
    async for chunk in request.stream():
        raw += chunk
        if len(raw) > MCP_BODY_MAX:
            return _rpc_error(413, -32600, f"body exceeds {MCP_BODY_MAX} bytes")
    try:
        body = json.loads(raw)
    except (ValueError, RecursionError):
        return _rpc_error(200, -32700, "Invalid JSON payload")
    if not isinstance(body, dict):
        return _rpc_error(200, -32600, "Request payload validation error")
    # spec 3.6: the version travels as a header or a query parameter; an empty one means 0.3
    version = request.headers.get("a2a-version") or request.query_params.get("A2A-Version") or "0.3"
    if version != A2A_VERSION:
        return JSONResponse({"jsonrpc": "2.0", "id": body.get("id"),
                             "error": A2AError(A2A_UNSUPPORTED, f"A2A version {version} is not supported; send "
                                               f"A2A-Version: {A2A_VERSION}").body()})
    cost = 0
    if body.get("method") == "SendMessage" and "id" in body:
        try:   # charged what the same skill call costs over REST and MCP; a malformed one is refused for free
            cost = _skill_cost(*a2a_call_from(body["params"]["message"]))
        except (A2AError, KeyError, TypeError):
            cost = 0
    if cost:
        ip = request.client.host if request.client else "unknown"
        wait = _throttle_n(f"skill:{ip}", cost, limit=60)
        if wait is not None:
            return _rpc_error(429, -32000, "rate limited", {"retry_after_s": wait}, headers={"Retry-After": str(wait)},
                              id_=body.get("id"))

    def run() -> dict[str, Any] | None:
        try:
            return a2a_handle(body)
        except Exception:
            logging.getLogger("nota.a2a").exception("a2a request failed")
            return {"jsonrpc": "2.0", "id": body.get("id"), "error": {"code": -32603, "message": "Internal error"}}

    reply = await run_in_threadpool(run)
    if reply is None:
        return Response(status_code=202)
    return JSONResponse(reply, headers={"A2A-Version": A2A_VERSION})


@app.get("/r/{id}")
def permalink(id: str, request: Request) -> HTMLResponse:
    """Same page as /app, with Open Graph / X card tags for this receipt so a shared link previews as a card."""
    raw = _ledger().get_decision(id)
    if raw is None:
        # the page still loads and says there is no such receipt, but the status says it too, so a
        # crawler or a link checker does not index a made-up id as a real receipt
        page = (STATIC / "index.html").read_text(encoding="utf-8")
        return HTMLResponse(page.replace("<!--OG-->", '<meta name="robots" content="noindex">', 1), status_code=404)
    r = Receipt.model_validate_json(raw)
    desc = html.escape(f"{r.verdict.action} (p_up_7d {r.verdict.p_up_7d:.2f}). {r.verdict.rationale}"[:200])
    return _page("index.html", request, f"/r/{r.id}", [
        f'<meta property="og:title" content="{html.escape(r.headline)}">',
        f'<meta property="og:description" content="{desc}">',
        '<meta property="og:type" content="article">',
        f'<meta name="twitter:title" content="{html.escape(r.headline)}">',
    ], image=f"/r/{r.id}.png")
