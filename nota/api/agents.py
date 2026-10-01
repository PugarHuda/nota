"""Everything an agent or a crawler reads: the skills over REST, Nota as an MCP server and as an A2A
agent, llms.txt, server.json, robots, the sitemap and the Atom / JSON feeds."""

from __future__ import annotations

import json
import logging
import sqlite3
import time
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from fastapi import APIRouter, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, Response
from fastapi.routing import iter_route_contexts
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from nota.a2a import (PROTOCOL_VERSION as A2A_VERSION, VERSION_NOT_SUPPORTED as A2A_UNSUPPORTED, A2AError,
                      agent_card, call_from as a2a_call_from, handle as a2a_handle)
from nota.api.common import _base, _ledger, _receipt, _skill_cost, _throttle, _throttle_n
from nota.ledger import Ledger, now_iso
from nota.mcp_server import (HEADER_MISMATCH, PROMPTS, SUPPORTED_PROTOCOLS, UNSUPPORTED_VERSION, VERSION_META,
                             handle as mcp_handle)
from nota.skills import SKILLS, definitions as skill_definitions, invoke as skill_invoke, live_deps
from nota.skills.contract import OK

router = APIRouter()


# --- Nota as an MCP server: the same nine skills, over the Streamable HTTP transport ------------
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


@router.post("/mcp", include_in_schema=False)
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


@router.get("/mcp", include_in_schema=False)
@router.delete("/mcp", include_in_schema=False)
def mcp_no_stream() -> Response:
    """Stateless server: no server-initiated stream and no session to delete, which the spec
    answers with 405 rather than pretending to hold one."""
    return Response(status_code=405, headers={"Allow": "POST"})


@router.get("/.well-known/mcp/server.json", include_in_schema=False)
@router.get("/server.json", include_in_schema=False)
def mcp_server_json() -> FileResponse:
    """The official MCP registry's server.json, served from the deployment it describes so a client
    or a sub-registry can discover this server without going through GitHub."""
    path = Path(__file__).resolve().parents[2] / "server.json"
    if not path.exists():
        raise HTTPException(404, "server.json is not bundled in this checkout")
    return FileResponse(path, media_type="application/json")


@router.get("/llms.txt", include_in_schema=False)
def llms_txt(request: Request) -> PlainTextResponse:
    """The llms.txt convention: one page that tells an agent what is here and how to call it,
    instead of making it infer the API from HTML. Generated, so it cannot drift from the routes."""
    base = _base(request)
    led = _ledger()
    nl = chr(10)
    skills = nl.join(f"- `{d.name}`: {d.description}" for d in skill_definitions())
    pages = _llms_pages(request.app, base)
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

- `tools/list`, `tools/call` for the nine research skills below; each is read-only and declares its
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

Each receipt is also available as `{base}/r/<id>.json` (the bytes as stored) and as a card image at
`{base}/r/<id>.png`, and `{base}/api/decisions/<id>/replay` re-runs it from the stored evidence and
reports whether the result is identical. `{base}/r/<id>.ots` is its OpenTimestamps proof: the SHA-256
of `/r/<id>.json` anchored in Bitcoin (`ots verify`), once the ledger cycle has stamped it.

## Scorecard

RYO's own deep_analysis trade plans for 25 majors are locked once a day, before the outcome is known,
and settled on OKX hourly candles (1 m candles inside an hour that touched both levels) at 24 and
72 hours: which of stop and first target was touched first, and the return at the horizon.

- [Scorecard]({base}/scorecard): the page, with a schema.org Dataset description
- `{base}/api/scorecard?horizon=24` and `?horizon=72`: the summary and every row as JSON
- `{base}/api/scorecard.csv`: every lock at both horizons, failures included
- `{base}/api/scorecard/locks/<id>.json` and `.ots`: one lock as stored, and its OpenTimestamps proof

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
    "/feed": "Public reasoning feed: each agent's stance and thesis, who dissented, backing counts, share links",
    "/scorecard": "RYO Verdict Scorecard",
    "/judges": "One screen per hackathon track: what to open and which rubric line it answers",
    "/drill": "Failure drill: break RYO, the exchanges or the model on purpose and watch the real pipeline refuse to trade",
    "/kol": "Multi-KOL narrative agent: up to 20 voices, transparent signal rules, a practice trade sized on RYO's ATR",
    "/api/kol": "Stored multi-KOL agent runs: signals, rule trails and practice trades (JSON)",
    "/demo": "Narrated three-and-a-half-minute walkthrough with a clickable transcript",
    "/demo.json": "Walkthrough chapters and transcript with the second each line was spoken",
    "/demo.vtt": "Walkthrough captions (WebVTT)",
    "/api/decisions": "Receipt summaries, newest first (JSON)",
    "/api/positions": "Open practice positions against the latest evidence price",
    "/api/scores": "Brier score per council role, against the base rate and against RYO's own call",
    "/api/backers": "Handles that backed or faded receipts, and how often they were right",
    "/api/reputation": "Backers and council agents ranked on one scale (hit rate), unranked until 20 calls from 20 independent weeks",
    "/api/feed": "The reasoning feed as JSON: stances, one-line theses, dissent and backing per receipt",
    "/api/watchlist": "The public watchlist: symbols ranked by how many handles watch them",
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


def _llms_pages(app: FastAPI, base: str) -> str:
    have = {c.path for c in iter_route_contexts(app.routes)}   # routes inside the included routers too
    return chr(10).join(f"- [{desc}]({base}{path})" for path, desc in LLMS_PAGES.items() if path in have)


# --- Track 3: skills exposed on RYO's own paths (/api/skills, SkillCallRequest/Response) -------------
class SkillCallRequest(BaseModel):
    name: str | None = None
    args: dict[str, Any] = Field(default_factory=dict)
    conversation_id: str | None = None


@router.get("/api/skills/")
def list_skills() -> list[dict[str, Any]]:
    return [d.model_dump() for d in skill_definitions()]


@router.get("/api/skills/{name}")
def skill_def(name: str) -> dict[str, Any]:
    for d in skill_definitions():
        if d.name == name:
            return d.model_dump()
    raise HTTPException(404, f"unknown skill {name}")


@router.post("/api/skills/{name}/invoke")
def invoke_skill(name: str, body: SkillCallRequest, request: Request) -> dict[str, Any]:
    """RYO's SkillCallResponse shape: name, status, result (our envelope), latency_ms, xp, guard_decision."""
    if body.name and body.name != name:
        raise HTTPException(422, "body.name does not match the path")
    _throttle(f"skill:{request.client.host if request.client else 'unknown'}", limit=60, cost=_skill_cost(name, body.args))
    deps = live_deps(name, body.args) if name in SKILLS else {}
    started = time.time()
    if name not in SKILLS:
        raise HTTPException(404, f"unknown skill {name!r}; known: {', '.join(sorted(SKILLS))}")
    try:
        env = skill_invoke(name, body.args, **deps)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    except Exception as exc:   # a skill crashing is a failed source, not "no such skill", and never a traceback
        logging.getLogger("nota.skills").exception("skill %s failed", name)
        raise HTTPException(502, f"source failure while running {name}; nothing was fabricated in its place") from exc
    # SkillCastStatus enum in RYO's OpenAPI: pending | running | success | error
    return {"name": name, "status": "success" if env.status != "unavailable" else "error", "result": env.model_dump(mode="json"),
            "latency_ms": int((time.time() - started) * 1000), "xp": 0, "guard_decision": None}


# --- Discovery and open data: robots, sitemap, feeds --------------------------------------------
@router.get("/robots.txt", include_in_schema=False)
def robots(request: Request) -> PlainTextResponse:
    return PlainTextResponse(f"User-agent: *\nAllow: /\n\nSitemap: {_base(request)}/sitemap.xml\n")


SITEMAP_PAGES = ("/", "/ja", "/app", "/feed", "/scorecard", "/judges", "/kol", "/drill", "/demo")


@router.get("/sitemap.xml", include_in_schema=False)
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


@router.get("/feed.xml", include_in_schema=False)
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


@router.get("/feed.json", include_in_schema=False)
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


# --- Nota as an A2A agent: the same skills, discoverable from an Agent Card --------------------------
@router.get("/.well-known/agent-card.json", include_in_schema=False)
def a2a_card(request: Request) -> JSONResponse:
    return JSONResponse(agent_card(_base(request)), headers={"Cache-Control": "public, max-age=3600"})


@router.post("/a2a", include_in_schema=False)
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
