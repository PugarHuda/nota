"""Risk sizing: a pure function from (verdict, evidence, limits) to a practice trade or a block.

No LLM here. Entry, stop, target and size are one decision derived from ATR(14). If price or
ATR is missing from the evidence the trade is blocked; we never assume a number.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

from nota import paths
from nota.council import Verdict
from nota.evidence import PRIMARY, EvidencePack, first_present


class RiskLimits(BaseModel):
    account_usd: float = 10_000.0
    risk_per_trade_pct: float = 1.0
    max_position_pct: float = 20.0
    atr_stop_mult: float = 2.0
    atr_target_mult: float = 3.0
    min_edge: float = Field(default=0.55, description="Minimum probability of being right before a trade is allowed")


class PracticeTrade(BaseModel):
    kind: Literal["trade"] = "trade"
    symbol: str
    side: Literal["long", "short"]
    entry_price: float
    stop_price: float
    target_price: float
    size_units: float
    size_usd: float
    risk_usd: float
    atr: float
    edge: float
    source_paths: dict[str, str]
    vs_ryo_plan: dict[str, Any] | None = None


class Blocked(BaseModel):
    kind: Literal["blocked"] = "blocked"
    reason: str


def atr_usd(pack: EvidencePack, price: float) -> tuple[str | None, float | None]:
    """ATR in price units. RYO publishes it absolutely (`trade_plan.atr_14_usd`) and as a percentage
    of price (`technical_analysis.atr_14_pct`); prefer the absolute figure, and when only the
    percentage is there, convert it and say so in the path. Using 4.17 as if it were dollars is
    right by luck near $100 and hundreds of times wrong on BTC."""
    path, atr = first_present(pack, paths.ATR_14)
    if atr is not None:
        return path, atr
    pct_path, pct = first_present(pack, paths.ATR_14_PCT)
    if pct is None:
        return None, None
    return f"{pct_path} x price / 100", price * pct / 100.0


def compare_to_ryo_plan(pack: EvidencePack, entry: float, stop: float, target: float) -> dict[str, Any] | None:
    """RYO publishes its own preview plan from the same ATR. Sizing here is independent, so the two
    can be held against each other: agreement is corroboration, and a gap is worth seeing before
    anyone acts. Returns None when RYO published no plan for this token."""
    plan = pack.get(paths.RYO_PLAN)
    if not isinstance(plan, dict):
        return None
    ryo_stop, ryo_targets = plan.get("stop"), plan.get("targets") or []
    ryo_target = ryo_targets[0] if ryo_targets else plan.get("target")
    out: dict[str, Any] = {
        "path": paths.RYO_PLAN,
        "method": plan.get("method"),
        "ryo_atr_multiplier": plan.get("atr_multiplier"),
        "nota_atr_multiplier": None,
        "ryo_entry": plan.get("entry"), "ryo_stop": ryo_stop, "ryo_target": ryo_target,
        "stop_diff_pct": None, "target_diff_pct": None, "agrees_on_direction": None,
    }
    if isinstance(ryo_stop, (int, float)) and not isinstance(ryo_stop, bool) and entry:
        out["stop_diff_pct"] = round((stop - ryo_stop) / entry * 100, 3)
    if isinstance(ryo_target, (int, float)) and not isinstance(ryo_target, bool) and entry:
        out["target_diff_pct"] = round((target - ryo_target) / entry * 100, 3)
        # RYO's plan is a long setup when its first target sits above its entry
        ryo_long = ryo_target > (plan.get("entry") or entry)
        out["agrees_on_direction"] = ryo_long == (target > entry)
    return out


def size_trade(verdict: Verdict, pack: EvidencePack, limits: RiskLimits | None = None) -> PracticeTrade | Blocked:
    limits = limits or RiskLimits()
    if not pack.primary_ok:
        return Blocked(reason="primary evidence (deep_analysis) unavailable; no trade without it")
    primary = pack.sections[PRIMARY].envelope
    if primary is not None and primary.data_mode == "simulated":
        return Blocked(reason="primary evidence is simulated data (data_mode=simulated); no practice trade on simulated prices")
    if verdict.action == "no_trade":
        return Blocked(reason="judge decided no_trade")
    edge = verdict.p_up_7d if verdict.action == "long" else 1.0 - verdict.p_up_7d
    if edge < limits.min_edge:
        return Blocked(reason=f"edge {edge:.2f} below minimum {limits.min_edge:.2f}")
    price_path, price = first_present(pack, paths.PRICE_USD)
    atr_path, atr = atr_usd(pack, price) if price is not None else (None, None)
    if price is None or atr is None or price <= 0 or atr <= 0:
        return Blocked(reason="price or ATR(14) unavailable in evidence; refusing to assume a value")

    sign = 1.0 if verdict.action == "long" else -1.0
    stop_distance = atr * limits.atr_stop_mult
    stop_price = price - sign * stop_distance
    target_price = price + sign * atr * limits.atr_target_mult
    if stop_price <= 0 or target_price <= 0:
        # ATR that large next to the price means the levels fall through zero. A receipt must not
        # print a negative stop; say the instrument is too volatile for this rule instead.
        return Blocked(reason=f"ATR({limits.atr_stop_mult:g}x/{limits.atr_target_mult:g}x) of {atr:g} "
                              f"puts the stop or target at or below zero for a price of {price:g}; "
                              "the fixed-multiple rule does not apply here")
    risk_usd = limits.account_usd * limits.risk_per_trade_pct / 100.0
    size_units = risk_usd / stop_distance
    max_usd = limits.account_usd * limits.max_position_pct / 100.0
    size_usd = size_units * price
    if size_usd > max_usd:
        size_usd = max_usd
        size_units = size_usd / price
        risk_usd = size_units * stop_distance
    return PracticeTrade(
        symbol=pack.symbol,
        side=verdict.action,
        entry_price=price,
        stop_price=stop_price,
        target_price=target_price,
        size_units=round(size_units, 6),
        size_usd=round(size_usd, 2),
        risk_usd=round(risk_usd, 2),
        atr=atr,
        edge=round(edge, 4),
        source_paths={"price": price_path or "", "atr": atr_path or ""},
        vs_ryo_plan=(lambda c: c and {**c, "nota_atr_multiplier": limits.atr_stop_mult})(
            compare_to_ryo_plan(pack, price, stop_price, target_price)),
    )
