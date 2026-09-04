"""Risk sizing: a pure function from (verdict, evidence, limits) to a practice trade or a block.

No LLM here. Entry, stop, target and size are one decision derived from ATR(14). If price or
ATR is missing from the evidence the trade is blocked; we never assume a number.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from arena import paths
from arena.council import Verdict
from arena.evidence import EvidencePack, first_present


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


class Blocked(BaseModel):
    kind: Literal["blocked"] = "blocked"
    reason: str


def size_trade(verdict: Verdict, pack: EvidencePack, limits: RiskLimits | None = None) -> PracticeTrade | Blocked:
    limits = limits or RiskLimits()
    if not pack.primary_ok:
        return Blocked(reason="primary evidence (deep_analysis) unavailable; no trade without it")
    if verdict.action == "no_trade":
        return Blocked(reason="judge decided no_trade")
    edge = verdict.p_up_7d if verdict.action == "long" else 1.0 - verdict.p_up_7d
    if edge < limits.min_edge:
        return Blocked(reason=f"edge {edge:.2f} below minimum {limits.min_edge:.2f}")
    price_path, price = first_present(pack, paths.PRICE_USD)
    atr_path, atr = first_present(pack, paths.ATR_14)
    if price is None or atr is None or price <= 0 or atr <= 0:
        return Blocked(reason="price or ATR(14) unavailable in evidence; refusing to assume a value")

    sign = 1.0 if verdict.action == "long" else -1.0
    stop_distance = atr * limits.atr_stop_mult
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
        stop_price=price - sign * stop_distance,
        target_price=price + sign * atr * limits.atr_target_mult,
        size_units=round(size_units, 6),
        size_usd=round(size_usd, 2),
        risk_usd=round(risk_usd, 2),
        atr=atr,
        edge=round(edge, 4),
        source_paths={"price": price_path or "", "atr": atr_path or ""},
    )
