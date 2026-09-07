"""Read-only HTTP API over the ledger plus the diff-first dashboard (Track 2).

Nothing here writes to the ledger. Every endpoint is a view over receipts the CLI already
stored, so the dashboard can never show a number that has no receipt behind it.
"""

from __future__ import annotations

import html
import json
import os
from urllib.parse import urlparse
import re
import time
from pathlib import Path
from typing import Any, Literal

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse, Response
from pydantic import BaseModel, Field

from nota import paths
from nota.calibration import due, reliability, role_scores, role_weights
from nota.card import render_card
from nota.evidence import EvidencePack, first_present
from nota.ledger import Ledger
from nota.receipt import Receipt, render_markdown
from nota.replay import replay
from nota.risk import PracticeTrade
from nota.ryo_client import RyoClient
from nota.mcp_server import handle as mcp_handle
from nota.skills import definitions as skill_definitions, invoke as skill_invoke

load_dotenv()
app = FastAPI(title="Nota", description="Read-only view over decision receipts. No orders, no wallets.")
STATIC = Path(__file__).parent / "static"
KEY_PATHS = set(paths.PRICE_USD + paths.ATR_14 + paths.ATR_14_PCT + paths.RSI_14)


def _ledger() -> Ledger:
    # ponytail: one connection per request; sqlite objects cannot cross FastAPI's worker threads
    return Ledger(os.environ.get("NOTA_DB", "nota.db"))


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
            "changes": what_changed(led, r, prev), "outcome": json.loads(out) if out else None, "degraded": _degraded(r),
            "backing": _backing_counts(led, id)}


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
            "reliability": reliability(led)}


@app.get("/api/health")
def health() -> dict[str, Any]:
    """What this deployment can and cannot do right now. No secrets, only whether they are set."""
    led = _ledger()
    client = RyoClient(timeout=5.0, max_retries=0)  # a health probe must not hold the page hostage
    try:
        ryo: dict[str, Any] = client.health()
    except Exception as exc:  # the dashboard must load even when RYO is down
        ryo = {"error": f"{type(exc).__name__}: {str(exc)[:200]}"}
    kind = os.environ.get("NOTA_LLM", "anthropic")
    return {
        "ryo": ryo, "ryo_key_set": bool(client.key), "readonly": led.readonly,
        # `key_set` matters more than the name: the hosted demo has no LLM at all, and saying
        # "anthropic" there would imply a model that is not reachable.
        "llm": {"kind": kind, "model": os.environ.get("NOTA_MODEL"),
                "key_set": bool(os.environ.get("OPENAI_API_KEY") if kind == "openai"
                                else os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"))},
        "ledger": {"decisions": led.conn.execute("SELECT COUNT(*) FROM decisions").fetchone()[0],
                   "resolved": len(led.list_outcomes()), "unresolved": len(led.unresolved()), "due": len(due(led))},
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


# --- Nota as an MCP server: the same four skills, over the Streamable HTTP transport ------------
ALLOWED_ORIGIN_HOSTS = {"nota-ryo.vercel.app", "ryo-arena.vercel.app", "localhost", "127.0.0.1", "testserver"}


def _origin_ok(request: Request) -> bool:
    """The transport spec requires servers to validate Origin against DNS rebinding. A request with
    no Origin is a non-browser client (curl, an MCP host) and is allowed."""
    origin = request.headers.get("origin")
    if not origin:
        return True
    host = urlparse(origin).hostname or ""
    return host in ALLOWED_ORIGIN_HOSTS


@app.post("/mcp", include_in_schema=False)
async def mcp_endpoint(request: Request) -> Response:
    if not _origin_ok(request):
        raise HTTPException(403, "origin not allowed")
    try:
        body = json.loads(await request.body())
    except ValueError as exc:
        return JSONResponse({"jsonrpc": "2.0", "id": None,
                             "error": {"code": -32700, "message": f"parse error: {exc}"}}, status_code=400)
    batch = body if isinstance(body, list) else [body]
    if not batch or not all(isinstance(m, dict) for m in batch):
        return JSONResponse({"jsonrpc": "2.0", "id": None,
                             "error": {"code": -32600, "message": "expected a JSON-RPC message or a batch"}},
                            status_code=400)
    deps: dict[str, Any] = {}
    replies = [r for r in (mcp_handle(m, deps) for m in batch) if r is not None]
    if not replies:                       # only notifications or responses came in
        return Response(status_code=202)
    return JSONResponse(replies if isinstance(body, list) else replies[0])


@app.get("/mcp", include_in_schema=False)
@app.delete("/mcp", include_in_schema=False)
def mcp_no_stream() -> Response:
    """Stateless server: no server-initiated stream and no session to delete, which the spec
    answers with 405 rather than pretending to hold one."""
    return Response(status_code=405, headers={"Allow": "POST"})


@app.get("/llms.txt", include_in_schema=False)
def llms_txt(request: Request) -> PlainTextResponse:
    """The llms.txt convention: one page that tells an agent what is here and how to call it,
    instead of making it infer the API from HTML. Generated, so it cannot drift from the routes."""
    base = os.environ.get("NOTA_PUBLIC_URL", "").rstrip("/") or str(request.base_url).rstrip("/")
    led = _ledger()
    nl = chr(10)
    skills = nl.join(f"- `{d.name}`: {d.description}" for d in skill_definitions())
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
2026-07-28, 2025-06-18, 2025-03-26 and 2024-11-05 are all accepted).

- `tools/list`, `tools/call` for the four research skills below
- `resources/list`, `resources/read` for every receipt, addressed as `nota://receipt/<id>`

```
curl -s {base}/mcp -H 'content-type: application/json' \
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

## Pages

- [Overview]({base}/): what Nota claims and how to check it
- [Dashboard]({base}/app): every receipt, diffed against the one before it
- [Health]({base}/api/health): what this deployment can and cannot do right now
- [OpenAPI]({base}/docs)
""", media_type="text/plain; charset=utf-8")


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
    _throttle(f"skill:{request.client.host if request.client else 'unknown'}", limit=60)
    deps: dict[str, Any] = {}
    if name == "news_verify" and body.args.get("symbol") and os.environ.get("RYO_MCP_KEY"):
        deps["ryo"] = RyoClient()
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
HANDLE = re.compile(r"^[A-Za-z0-9_@.\-]{3,32}$")


class BackingIn(BaseModel):
    handle: str = Field(description="X / Discord / Telegram handle, 3-32 chars")
    stance: Literal["agree", "disagree"]


def _backing_counts(led: Ledger, id: str) -> dict[str, Any]:
    rows = led.backings(id)
    return {"agree": sum(r["stance"] == "agree" for r in rows), "disagree": sum(r["stance"] == "disagree" for r in rows),
            "handles": [{"handle": r["handle"], "stance": r["stance"]} for r in rows[-20:]]}


_BACKING_HITS: dict[str, list[float]] = {}
BACKING_LIMIT, BACKING_WINDOW = 30, 3600.0  # ponytail: in-process per-IP throttle; a shared store if ever run on >1 worker


def _throttle(ip: str, now: float | None = None, limit: int = BACKING_LIMIT) -> None:
    now = now if now is not None else time.time()
    hits = [t for t in _BACKING_HITS.get(ip, []) if now - t < BACKING_WINDOW]
    if len(hits) >= limit:
        raise HTTPException(429, f"too many requests from this address; limit {limit} per hour")
    hits.append(now)
    _BACKING_HITS[ip] = hits


@app.post("/api/decisions/{id}/back")
def back_decision(id: str, body: BackingIn, request: Request) -> dict[str, Any]:
    """Unauthenticated by design for the hackathon: one stance per handle per receipt, latest wins."""
    if not HANDLE.match(body.handle):
        raise HTTPException(422, "handle must be 3-32 characters: letters, digits, _ @ . -")
    _throttle(request.client.host if request.client else "unknown")
    led = _ledger()
    if led.readonly:
        raise HTTPException(503, "this is a read-only demo deployment over a ledger snapshot; backing works on a writable `nota serve`")
    _receipt(led, id)
    led.add_backing(id, body.handle, body.stance)
    return _backing_counts(led, id)


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
    for b in led.all_backings():
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
def landing() -> FileResponse:
    """The reading room: what Nota claims and how to check it. The instrument itself is /app."""
    return FileResponse(STATIC / "landing.html")


@app.get("/app")
def index() -> FileResponse:
    return FileResponse(STATIC / "index.html")


@app.get("/img/{name}.png", include_in_schema=False)
def landing_image(name: str) -> FileResponse:
    path = (STATIC / "img" / f"{name}.png").resolve()
    if path.parent != (STATIC / "img").resolve() or not path.exists():
        raise HTTPException(404, "no such image")
    return FileResponse(path, media_type="image/png")


@app.get("/demo.mp4", include_in_schema=False)
def demo_video() -> FileResponse:
    """The submission walkthrough, served from the app itself so the demo URL needs no third party."""
    path = STATIC / "demo.mp4"
    if not path.exists():
        raise HTTPException(404, "demo video not bundled in this checkout")
    return FileResponse(path, media_type="video/mp4")


@app.get("/r/{id}")
def permalink(id: str, request: Request) -> HTMLResponse:
    """Same page as `/`, with Open Graph / X card tags for this receipt so a shared link previews as a card."""
    page = (STATIC / "index.html").read_text(encoding="utf-8")
    raw = _ledger().get_decision(id)
    if raw is None:
        return HTMLResponse(page)
    r = Receipt.model_validate_json(raw)
    # the fallback comes from the Host header, so it is escaped like any other untrusted input
    base = html.escape(os.environ.get("NOTA_PUBLIC_URL", "").rstrip("/") or str(request.base_url).rstrip("/"))
    desc = html.escape(f"{r.verdict.action} (p_up_7d {r.verdict.p_up_7d:.2f}). {r.verdict.rationale}"[:200])
    tags = "\n".join([
        f'<meta property="og:title" content="{html.escape(r.headline)}">',
        f'<meta property="og:description" content="{desc}">',
        f'<meta property="og:image" content="{base}/r/{r.id}.png">',
        f'<meta property="og:url" content="{base}/r/{r.id}">',
        '<meta property="og:type" content="article">',
        '<meta name="twitter:card" content="summary_large_image">',
        f'<meta name="twitter:title" content="{html.escape(r.headline)}">',
        f'<meta name="twitter:image" content="{base}/r/{r.id}.png">',
    ])
    return HTMLResponse(page.replace("<!--OG-->", tags, 1))
