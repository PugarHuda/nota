"""Runs on demand: the council on live RYO evidence, the failure drill, and the multi-KOL agent.
Each is budgeted per address (and the paid ones across all visitors) before it spends anything."""

from __future__ import annotations

import json
import logging
import os
import time
from typing import Annotated, Any

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field, StringConstraints

from nota import kol
from nota.api.common import _ledger, _skill_cost, _throttle
from nota.ledger import Ledger
from nota.receipt import render_markdown
from nota.ryo_client import RyoClient, RyoError

router = APIRouter()


# ponytail: a whole council run is ~4 model calls (~0.004 USD on the hosted model); these two hourly
# budgets cap what a visitor, and all visitors together, can spend. Raise them if judges queue up.
COUNCIL_PER_IP, COUNCIL_ALL = 3, 20


@router.post("/api/council/{symbol}")
def council_live(symbol: str, request: Request) -> dict[str, Any]:
    """Convene the council on live RYO evidence now and return the receipt. It is not stored: the hosted
    ledger is the read-only snapshot the daily cycle commits, so this run lives in a throwaway ledger
    (roles are weighted 1.0, not by their record) and the response says so."""
    from nota.cli import _extras  # ponytail: the CLI already wires every cross-check; reuse it
    from nota.decide import decide
    from nota.skills.contract import clean_symbol

    try:
        sym = clean_symbol(symbol)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    kind = os.environ.get("NOTA_LLM") or ("openai" if os.environ.get("OPENAI_API_KEY") else "anthropic")
    if not os.environ.get("RYO_MCP_KEY"):
        raise HTTPException(503, "this deployment has no RYO builder key, so it cannot gather live evidence")
    if not (os.environ.get("OPENAI_API_KEY") if kind == "openai" else os.environ.get("ANTHROPIC_API_KEY")):
        raise HTTPException(503, "this deployment has no LLM key, so the council cannot run; every stored receipt still replays")
    _throttle(f"council:{request.client.host if request.client else 'unknown'}", limit=COUNCIL_PER_IP)
    _throttle("council:all", limit=COUNCIL_ALL)
    if kind == "openai":
        from nota.llm import OpenAICompatLLM as Model
    else:
        from nota.llm import AnthropicLLM as Model
    src = RyoClient()
    started = time.time()
    try:
        r = decide(sym, src, Model(), Ledger(":memory:"), extras=_extras(src, "", False, ledger=_ledger()))
    except RyoError as exc:
        raise HTTPException(502, f"RYO answered with an error ({exc}); nothing was fabricated in its place") from exc
    return {"stored": False, "note": "live run, not added to the ledger; roles weighted 1.0",
            "seconds": round(time.time() - started, 1), "receipt": r.model_dump(mode="json"), "markdown": render_markdown(r)}


class DrillIn(BaseModel):
    scenario: str = Field(description="ryo_down | ryo_401 | deep_analysis_missing | rate_limited | exchange_down | llm_down | partial")


DRILL_PER_IP = 60   # ponytail: no network and no model, only CPU (two offline pipeline runs per call)


@router.post("/api/drill")
def drill(body: DrillIn, request: Request) -> dict[str, Any]:
    """Run the real pipeline offline on RYO's recorded answers with one fault injected, beside a healthy run of
    the same recording. Nothing is stored: both receipts are labelled source "drill" and live in a throwaway ledger."""
    from nota.drill import SCENARIOS, compare

    if body.scenario not in SCENARIOS:
        raise HTTPException(422, f"unknown scenario {body.scenario!r}; one of {', '.join(SCENARIOS)}")
    _throttle(f"drill:{request.client.host if request.client else 'unknown'}", limit=DRILL_PER_IP)
    return compare(body.scenario)


# --- Multi-KOL narrative agent (Track 1 spotlight) ---------------------------------------------------
# ponytail: all visitors together may spend this many RYO deep_analysis reads an hour on KOL sizing
KOL_RYO_ALL = 30


class KolIn(BaseModel):
    voices: list[Annotated[str, StringConstraints(strip_whitespace=True, min_length=3, max_length=100)]] = Field(
        min_length=1, max_length=20, description="x:handle, tg:channel or bs:handle.bsky.social")
    rules: kol.KolRules = Field(default_factory=kol.KolRules)
    limits: kol.KolLimits = Field(default_factory=kol.KolLimits)


@router.post("/api/kol/run")
def kol_run(body: KolIn, request: Request) -> dict[str, Any]:
    """Read the voices now, apply the rules, size any signal on RYO's ATR. Stored in the ledger, except on
    the read-only hosted snapshot, where the run is returned unstored and the response says so."""
    ip = request.client.host if request.client else "unknown"
    # the voices are the skill's own fetches; each RYO read for sizing is one more
    slots = body.limits.max_open_positions if os.environ.get("RYO_MCP_KEY") else 0
    _throttle(f"skill:{ip}", limit=60, cost=_skill_cost("narrative_convergence", {"voices": body.voices}) + slots)
    _throttle("kol:ryo", limit=KOL_RYO_ALL, cost=slots)
    led = _ledger()
    try:
        r, packs = kol.run(body.voices, body.rules, body.limits, RyoClient() if slots else None, kol.held_symbols(led))
    except Exception as exc:  # a source crashing is a failed run, never a traceback
        logging.getLogger("nota.kol").exception("kol run failed")
        raise HTTPException(502, "source failure during the KOL run; nothing was fabricated in its place") from exc
    note = None if slots else "no RYO builder key on this deployment, so a signal cannot be sized and no trade is made"
    if led.readonly:
        return {"stored": False, "note": "read-only demo: this run is not added to the ledger", "ryo_note": note,
                "run": r.model_dump(mode="json")}
    kol.save(led, r, packs)
    return {"stored": True, "note": f"stored; GET /api/kol/{r.id} replays it", "ryo_note": note, "run": r.model_dump(mode="json")}


@router.get("/api/kol")
def kol_list(limit: int = Query(50, ge=1, le=200)) -> list[dict[str, Any]]:
    """Stored KOL runs, newest first: what fired and what was traded."""
    out = []
    for id, created_at, raw in _ledger().list_kol(limit):
        r = json.loads(raw)
        out.append({"id": id, "created_at": created_at, "headline": r["headline"], "voices": r["voices"],
                    "signals": {d["token"]: d["signal"] for d in r["decisions"] if d["signal"] != "none"},
                    "trades": [d["token"] for d in r["decisions"] if (d.get("trade") or {}).get("kind") == "trade"]})
    return out


@router.get("/api/kol/{id}")
def kol_get(id: str) -> dict[str, Any]:
    """One stored run, with its decisions re-derived from the stored envelope and evidence."""
    led = _ledger()
    raw = led.get_kol(id)
    if raw is None:
        raise HTTPException(404, f"no KOL run {id}")
    try:
        again = kol.replay(id, led)
    except KeyError as exc:  # its evidence pack is missing: say so rather than claim a result
        again = {"id": id, "identical": None, "diff": [str(exc).strip("'\"")]}
    return {"run": json.loads(raw), "replay": again}
