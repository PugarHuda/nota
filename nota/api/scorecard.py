"""The RYO Verdict Scorecard over HTTP: the summary, each lock as stored with its proof, the CSV,
and the page with its schema.org Dataset. The scoring itself lives in nota.scorecard."""

from __future__ import annotations

import json
import os
import sqlite3
import time
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, Response

from nota.api.common import _base, _csv, _ledger, _ots, _page
from nota.ledger import Ledger
from nota.stamp import view as stamp_view

router = APIRouter()


@router.get("/api/scorecard")
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
    out = _scorecard_cache[key]
    for r in out["open"] + out["settled"]:  # proofs upgrade on their own clock, so they are read fresh, not cached
        r["stamp"] = stamp_view(led.get_stamp(r["id"]), f"/api/scorecard/locks/{r['id']}.ots")
    return out


@router.get("/api/scorecard/locks/{id}.json")
def lock_json(id: str) -> Response:
    """The lock row exactly as stored: its SHA-256 is the digest its .ots proof anchors."""
    raw = _ledger().get_lock(id)
    if raw is None:
        raise HTTPException(404, f"no lock {id}")
    return Response(raw, media_type="application/json")


@router.get("/api/scorecard/locks/{id}.ots")
def lock_ots(id: str) -> Response:
    return _ots(_ledger().get_stamp(id), f"lock-{id}.json")


_scorecard_cache: dict[tuple[Any, ...], dict[str, Any]] = {}


@router.get("/scorecard")
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
        "url": f"{base}/scorecard",   # no licence is declared: the owner has not chosen one
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


@router.get("/api/scorecard.csv")
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
