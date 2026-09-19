"""Skill `verdict_track_record`: what became of RYO's own plans for this token?

RYO's `deep_analysis` gives a verdict and a bracket every time and keeps no record of either. The
scorecard (nota.scorecard) locks them daily and settles them on OKX's hourly candles; this skill
reads that record back in RYO's envelope shape, so an agent about to act on "SOL constructive,
CONFIRMED" can first ask how RYO's plans for SOL, and under that confluence state, have fared.

Counts only, with their denominators. A token with nothing settled says so; it is never a 0% rate.
"""

from __future__ import annotations

import os
from typing import Any

from nota.envelope import Envelope
from nota.ledger import Ledger
from nota.skills.contract import SkillArg, SkillDefinition, clean_symbol, make_envelope

DEFINITION = SkillDefinition(
    name="verdict_track_record",
    description="How RYO's own deep_analysis plans have fared: locked daily for 25 majors and settled on OKX hourly candles "
    "(stop or +1R target first, 24 h or 72 h). Returns counts by verdict and by confluence state, plans whose verdict leans "
    "against their own direction, and the latest locked verdict. Omit the symbol for all tokens.",
    args=[
        SkillArg(name="symbol", type="string", required=False, description="Token symbol, e.g. SOL; omit for every token"),
        SkillArg(name="horizon_hours", type="integer", required=False, enum=["24", "72"], description="24 (default) or 72"),
    ],
)


def _tally(rows: list[dict[str, Any]], key: str) -> dict[str, dict[str, int]]:
    out: dict[str, dict[str, int]] = {}
    for r in rows:
        t = out.setdefault(str(r.get(key)), {"target": 0, "stop": 0, "neither": 0, "ambiguous": 0})
        t[r["result"]] += 1
    return out


def verdict_track_record(symbol: str | None = None, horizon_hours: int = 24, ledger: Ledger | None = None) -> Envelope:
    from nota.scorecard import HORIZONS_H, UNIVERSE, summary

    if horizon_hours not in HORIZONS_H:
        raise ValueError(f"horizon_hours must be one of {list(HORIZONS_H)}")
    sym = clean_symbol(symbol) if symbol else None
    h = horizon_hours
    warnings: list[str] = []
    try:
        s = summary(ledger or Ledger(os.environ.get("NOTA_DB", "nota.db")), h)
        availability = {"ledger": "ok"}
    except Exception as exc:  # an old snapshot without scorecard tables, or no ledger at all
        s = {"open": [], "settled": [], "lock_days": 0}
        availability = {"ledger": "unavailable"}
        warnings.append(f"ledger: {type(exc).__name__}: {exc}")
    pick = (lambda r: r["symbol"] == sym) if sym else (lambda r: True)
    settled = [r for r in s["settled"] if pick(r)]
    open_ = [r for r in s["open"] if pick(r)]
    latest = max(open_ + settled, key=lambda r: r["locked_at"], default=None)
    decided = [r for r in settled if r["result"] in ("target", "stop")]
    data = {"symbol": sym, "horizon_hours": h, "lock_days": s["lock_days"], "settled": len(settled), "open": len(open_),
            "target_first": {"hits": sum(r["result"] == "target" for r in decided), "of_decided": len(decided)},
            "by_verdict": _tally(settled, "verdict"), "by_confluence_state": _tally(settled, "confluence_state"),
            "verdict_against_plan": sum(r["verdict_contradicts_side"] for r in open_ + settled),
            "latest": None if latest is None else {k: latest.get(k) for k in ("symbol", "locked_at", "verdict", "confluence_state",
                                                                             "confluence_score", "side", "trace_id")},
            "method": "deep_analysis locked daily, bracket re-anchored to OKX, first touch on OKX 1H candles (nota.scorecard)"}
    if sym and sym not in UNIVERSE:  # nothing is ever locked for it, so "none settled yet" would imply there will be
        availability["ledger"] = "unavailable"
        warnings.append(f"{sym} is not in the scorecard universe (25 majors): {', '.join(UNIVERSE)}")
    elif availability["ledger"] == "ok" and not settled:
        availability["ledger"] = "partial"
        warnings.append(f"nothing settled at {h} h{' for ' + sym if sym else ''} yet; no rate is given")
    who = sym or "all tokens"
    t = data["target_first"]
    headline = (f"{who}: {t['hits']} of {t['of_decided']} decided RYO plans reached +1R before the stop within {h} h"
                if t["of_decided"] else f"{who}: {len(open_)} RYO plans locked, none decided at {h} h yet")
    return make_envelope("verdict_track_record", {"symbol": sym, "horizon_hours": h}, data, availability, warnings, headline,
                         primary=["ledger"])
