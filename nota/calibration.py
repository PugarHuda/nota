"""Calibration: resolve past decisions against a fresh price read and score each agent.

Brier score = (p_up_7d - outcome)^2, outcome 1 if price rose. Lower is better; 0.25 is a coin
flip. Weights feed back into the judge prompt so agents that are often wrong lose influence.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel

from nota import paths
from nota.council import ROLES
from nota.evidence import EvidencePack, Section, first_present
from nota.ledger import Ledger, now_iso
from nota.receipt import Receipt
from nota.risk import PracticeTrade
from nota.ryo_client import RyoError, RyoSource

HORIZON_DAYS = 7


class CannotResolve(Exception):
    pass


class Outcome(BaseModel):
    decision_id: str
    symbol: str
    resolved_at: str
    decided_as_of: str | None
    horizon_reached: bool
    price_then: float
    price_now: float
    price_now_source: str = "ryo"
    price_now_as_of: str | None = None
    return_pct: float
    went_up: bool
    brier: dict[str, float]
    trade_result_usd: float | None = None
    closed_reason: str | None = None  # 'stopped' | 'target' when a practice position was exited early


def _price_then(receipt: Receipt, ledger: Ledger) -> float | None:
    if isinstance(receipt.trade, PracticeTrade):
        return receipt.trade.entry_price
    pack_json = ledger.get_pack(receipt.pack_hash)
    if pack_json is None:
        return None
    _, price = first_present(EvidencePack.model_validate_json(pack_json), paths.PRICE_USD)
    return price


def _price_now(symbol: str, source: RyoSource | None) -> tuple[float | None, str | None, str]:
    """RYO's analyze_token first; when there is no source at all, or it fails or carries no price, the
    exchange median from `price_crosscheck`, labelled as such. Returns (price, as_of, source_label).

    `source=None` is the keyless path: scoring a decision needs no builder key, the same way verifying
    one needs no model key. The price it is scored against is then an independent one by construction."""
    if source is not None:
        try:
            env = source.call("analyze_token", {"symbol": symbol})
            probe = EvidencePack(symbol=symbol, created_at=now_iso(), source=source.name,
                                 sections={"analyze_token": Section(tool="analyze_token", status=env.status, envelope=env)})
            _, price = first_present(probe, [p for p in paths.PRICE_USD if p.startswith("analyze_token.")])
            if price is not None:
                return price, env.as_of, f"ryo:{source.name}"
        except RyoError:
            pass
    from nota.skills.price_check import price_crosscheck

    check = price_crosscheck(symbol)
    med = check.get("median_usd")
    if med is None:
        raise CannotResolve("no price from RYO analyze_token nor from any exchange")
    return float(med), check.as_of, f"exchange_median:{check.get('sources_ok')}_sources"


def _build_outcome(receipt: Receipt, ledger: Ledger, price_now: float, as_of_now: str | None, price_source: str,
                   closed_reason: str | None = None) -> Outcome:
    price_then = _price_then(receipt, ledger)
    if price_then is None:
        raise CannotResolve("price unavailable at decision time; refusing to score with a guess")
    went_up = price_now > price_then
    outcome_val = 1.0 if went_up else 0.0
    brier = {o.role: round((o.p_up_7d - outcome_val) ** 2, 4) for o in receipt.opinions}
    brier["judge"] = round((receipt.verdict.p_up_7d - outcome_val) ** 2, 4)
    decided_as_of = receipt.provenance.get("deep_analysis", {}).get("as_of")
    horizon = False
    if decided_as_of:
        try:
            then = datetime.fromisoformat(decided_as_of.replace("Z", "+00:00"))
            horizon = (datetime.now(timezone.utc) - then).days >= HORIZON_DAYS
        except ValueError:
            horizon = False
    trade_result = None
    if isinstance(receipt.trade, PracticeTrade):
        sign = 1.0 if receipt.trade.side == "long" else -1.0
        trade_result = round(sign * (price_now - price_then) * receipt.trade.size_units, 2)
    out = Outcome(
        decision_id=receipt.id, symbol=receipt.symbol, resolved_at=now_iso(), decided_as_of=decided_as_of,
        horizon_reached=horizon, price_then=price_then, price_now=price_now, price_now_source=price_source, price_now_as_of=as_of_now,
        return_pct=round((price_now / price_then - 1.0) * 100.0, 4), went_up=went_up, brier=brier, trade_result_usd=trade_result,
        closed_reason=closed_reason,
    )
    ledger.save_outcome(receipt.id, out.model_dump_json())
    return out


def resolve(decision_id: str, ledger: Ledger, source: RyoSource | None) -> Outcome:
    raw = ledger.get_decision(decision_id)
    if raw is None:
        raise KeyError(f"no decision {decision_id}")
    receipt = Receipt.model_validate_json(raw)
    price_now, as_of_now, price_source = _price_now(receipt.symbol, source)
    if price_now is None:
        raise CannotResolve("price unavailable at resolution time; refusing to score with a guess")
    return _build_outcome(receipt, ledger, price_now, as_of_now, price_source)


def close_position(decision_id: str, ledger: Ledger, price: float, price_source: str, reason: str) -> Outcome:
    """Practice-trade exit: the latest independent price crossed the stop or the target, so the position is
    closed now at that price and scored immediately. `closed_reason` says which level was hit; the horizon
    flag stays honest (usually False)."""
    raw = ledger.get_decision(decision_id)
    if raw is None:
        raise KeyError(f"no decision {decision_id}")
    receipt = Receipt.model_validate_json(raw)
    if not isinstance(receipt.trade, PracticeTrade):
        raise CannotResolve("no practice position on this receipt")
    return _build_outcome(receipt, ledger, price, now_iso(), price_source, closed_reason=reason)


def due(ledger: Ledger, now: datetime | None = None) -> list[str]:
    """Unresolved decisions whose seven-day horizon has passed (measured from the receipt's own created_at).
    Scoring earlier would freeze a premature outcome, because `unresolved()` never returns a scored id again."""
    now = now or datetime.now(timezone.utc)
    out = []
    for id in ledger.unresolved():
        raw = ledger.get_decision(id)
        if raw is None:
            continue
        created = datetime.fromisoformat(Receipt.model_validate_json(raw).created_at.replace("Z", "+00:00"))
        if (now - created).days >= HORIZON_DAYS:
            out.append(id)
    return out


def role_scores(ledger: Ledger) -> dict[str, dict[str, float]]:
    sums: dict[str, list[float]] = {}
    for raw in ledger.list_outcomes():
        for role, b in json.loads(raw)["brier"].items():
            sums.setdefault(role, []).append(b)
    return {r: {"n": float(len(v)), "brier_mean": round(sum(v) / len(v), 4)} for r, v in sums.items()}


def reliability(ledger: Ledger, bins: int = 5) -> dict[str, Any]:
    """Reliability (calibration) table for the judge: outcomes bucketed by stated p_up_7d.
    A well-calibrated judge has hit_rate close to mean_p in every bucket. Empty buckets stay empty.

    Only outcomes that actually reached the seven-day horizon count. A position closed early at its
    stop, or scored with `resolve --early`, answers a different question than "is the price higher in
    seven days", and mixing it in would bias the table towards whatever moves fastest. Those outcomes
    still feed `role_scores`, which is the trading feedback loop rather than a calibration claim."""
    rows: list[tuple[float, float]] = []
    excluded = 0
    for raw in ledger.list_outcomes():
        o = json.loads(raw)
        if not o.get("horizon_reached"):
            excluded += 1
            continue
        rec = ledger.get_decision(o["decision_id"])
        if rec:
            rows.append((Receipt.model_validate_json(rec).verdict.p_up_7d, 1.0 if o["went_up"] else 0.0))
    table = []
    for i in range(bins):
        lo, hi = i / bins, (i + 1) / bins
        hits = [(p, y) for p, y in rows if lo <= p < hi or (i == bins - 1 and p == 1.0)]
        table.append({"bin": f"{lo:.1f}-{hi:.1f}", "n": len(hits),
                      "mean_p": round(sum(p for p, _ in hits) / len(hits), 3) if hits else None,
                      "hit_rate": round(sum(y for _, y in hits) / len(hits), 3) if hits else None})
    brier = round(sum((p - y) ** 2 for p, y in rows) / len(rows), 4) if rows else None
    return {"n": len(rows), "judge_brier": brier, "bins": table, "excluded_before_horizon": excluded}


def role_weights(scores: dict[str, dict[str, float]]) -> dict[str, float]:
    """1/(brier+0.05), normalised so the best role has weight 1.0; unscored roles get 1.0; floor 0.2."""
    raw = {r: 1.0 / (scores[r]["brier_mean"] + 0.05) for r in ROLES if r in scores}
    if not raw:
        return {r: 1.0 for r in ROLES}
    top = max(raw.values())
    return {r: (round(max(raw[r] / top, 0.2), 3) if r in raw else 1.0) for r in ROLES}
