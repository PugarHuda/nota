"""Candidate dotted paths into RYO envelopes for the few numbers the risk engine needs.

ponytail: the live `data` schema is only visible with a builder key, so we look through a
short list of plausible paths and use the first one that exists. When the real catalog is
recorded, trim each list to the one true path. Missing on every candidate means None,
never 0.
"""

PRICE_USD = [
    "deep_analysis.data.market.price_usd",
    "deep_analysis.data.market_context.price_usd",
    "deep_analysis.data.price_usd",
    "analyze_token.data.market.price_usd",
    "analyze_token.data.price_usd",
]

ATR_14 = [
    "deep_analysis.data.technicals.atr_14",
    "deep_analysis.data.technicals.atr14",
    "deep_analysis.data.technicals.atr.value",
    "analyze_token.data.technicals.atr_14",
    "analyze_token.data.technicals.atr14",
]

RSI_14 = [
    "deep_analysis.data.technicals.rsi_14",
    "deep_analysis.data.technicals.rsi14",
    "analyze_token.data.technicals.rsi_14",
    "analyze_token.data.technicals.rsi14",
]
