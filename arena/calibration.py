"""Calibration: resolve past decisions against a fresh price read and score each agent.

Brier score = (p_up_7d - outcome)^2, outcome 1 if price rose. Lower is better; 0.25 is a coin
flip. Weights feed back into the judge prompt so agents that are often wrong lose influence.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

from pydantic import BaseModel

from arena import paths
from arena.council import ROLES
from arena.evidence import EvidencePack, Section, first_present
from arena.ledger import Ledger, now_iso
from arena.receipt import Receipt
from arena.risk import PracticeTrade
from arena.ryo_client import RyoError, RyoSource

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
    return_pct: float
    went_up: bool
    brier: dict[str, float]
    trade_result_usd: float | None = None


def _price_then(receipt: Receipt, ledger: Ledger) -> float | None:
    if isinstance(receipt.trade, PracticeTrade):
        return receipt.trade.entry_price
    pack_json = ledger.get_pack(receipt.pack_hash)
    if pack_json is None:
        return None
    _, price = first_present(EvidencePack.model_validate_json(pack_json), paths.PRICE_USD)
    return price


def _price_now(symbol: str, source: RyoSource) -> tuple[float | None, str | None]:
    try:
        env = source.call("analyze_token", {"symbol": symbol})
    except RyoError as exc:
        raise CannotResolve(f"analyze_token failed: {exc.code}") from exc
    probe = EvidencePack(symbol=symbol, created_at=now_iso(), source=source.name,
                         sections={"analyze_token": Section(tool="analyze_token", status=env.status, envelope=env)})
    _, price = first_present(probe, [p for p in paths.PRICE_USD if p.startswith("analyze_token.")])
    return price, env.as_of


def resolve(decision_id: str, ledger: Ledger, source: RyoSource) -> Outcome:
    raw = ledger.get_decision(decision_id)
    if raw is None:
        raise KeyError(f"no decision {decision_id}")
    receipt = Receipt.model_validate_json(raw)
    price_then = _price_then(receipt, ledger)
    price_now, as_of_now = _price_now(receipt.symbol, source)
    if price_then is None or price_now is None:
        raise CannotResolve("price unavailable at decision or resolution time; refusing to score with a guess")
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
        decision_id=decision_id, symbol=receipt.symbol, resolved_at=now_iso(), decided_as_of=decided_as_of,
        horizon_reached=horizon, price_then=price_then, price_now=price_now,
        return_pct=round((price_now / price_then - 1.0) * 100.0, 4), went_up=went_up, brier=brier, trade_result_usd=trade_result,
    )
    ledger.save_outcome(decision_id, out.model_dump_json())
    return out


def role_scores(ledger: Ledger) -> dict[str, dict[str, float]]:
    sums: dict[str, list[float]] = {}
    for raw in ledger.list_outcomes():
        for role, b in json.loads(raw)["brier"].items():
            sums.setdefault(role, []).append(b)
    return {r: {"n": float(len(v)), "brier_mean": round(sum(v) / len(v), 4)} for r, v in sums.items()}


def role_weights(scores: dict[str, dict[str, float]]) -> dict[str, float]:
    """1/(brier+0.05), normalised so the best role has weight 1.0; unscored roles get 1.0; floor 0.2."""
    raw = {r: 1.0 / (scores[r]["brier_mean"] + 0.05) for r in ROLES if r in scores}
    if not raw:
        return {r: 1.0 for r in ROLES}
    top = max(raw.values())
    return {r: (round(max(raw[r] / top, 0.2), 3) if r in raw else 1.0) for r in ROLES}
