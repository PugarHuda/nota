"""Dotted paths into RYO envelopes for the few numbers the risk engine needs.

Recorded live on 2026-09-07 with a builder key (`nota record SOL`, fixtures/recorded), so these
are RYO's real field names, not guesses. Each list is still a list because a field can be absent
from one section and present in another; missing on every candidate means None, never 0.

RYO reports volatility two ways: `technical_analysis.atr_14_pct` (percent of price) and
`trade_plan.atr_14_usd` (absolute). Sizing needs the absolute one - see `nota.risk.atr_usd`.
"""

PRICE_USD = [
    "deep_analysis.data.market.price_usd",
    "analyze_token.data.market.price_usd",
]

ATR_14 = [
    "deep_analysis.data.trade_plan.atr_14_usd",
]

# Percent of price. Only usable after multiplying by the price; never as a price itself.
ATR_14_PCT = [
    "deep_analysis.data.technical_analysis.atr_14_pct",
    "analyze_token.data.technical_analysis.atr_14_pct",
]

RSI_14 = [
    "deep_analysis.data.technical_analysis.rsi_14",
    "analyze_token.data.technical_analysis.rsi_14",
]

# RYO ships its own ATR-based preview plan. Nota sizes independently and then compares, because a
# provider's plan is evidence about the provider, not an instruction.
RYO_PLAN = "deep_analysis.data.trade_plan"
