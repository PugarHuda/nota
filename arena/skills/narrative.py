"""Skill `narrative_convergence`: do trusted voices converge on the same token narrative?

Voices are `tg:<channel>` (Telegram public channel preview, free, timestamps included) or
`x:<handle>` (best effort through Tavily restricted to x.com; timestamps usually unavailable
and reported as such). Sentiment, conviction and urgency come from a transparent lexicon,
not a model, so the same messages always score the same. The method is named in the output.
"""

from __future__ import annotations

import re
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any

from pydantic import BaseModel

from arena.envelope import Envelope
from arena.skills.contract import SkillArg, SkillDefinition, SourceUnavailable, make_envelope
from arena.skills.sources import Message, Tavily, TelegramPublic

DEFINITION = SkillDefinition(
    name="narrative_convergence",
    description="Monitor up to 20 user-selected voices (Telegram public channels, X handles) and report which tokens "
    "they mention, lexicon-scored sentiment, conviction and urgency, and whether several voices converge on one narrative.",
    args=[
        SkillArg(name="voices", type="array", description="Up to 20 ids like tg:WatcherGuru or x:handle", items={"type": "string"}),
        SkillArg(name="tokens", type="array", required=False, description="Symbols to track; default = every cashtag found", items={"type": "string"}),
        SkillArg(name="hours", type="integer", required=False, description="Look-back window in hours (default 24)"),
    ],
)

METHOD = "lexicon_v2"  # v2 adds news-wire verbs (hits/soars/plunges...) so headline-style channels score
BULL = {"bullish", "buy", "buying", "long", "breakout", "moon", "pump", "pumping", "accumulate", "accumulating", "undervalued",
        "ath", "rally", "surge", "surging", "higher", "support", "bottom", "bounce", "reversal", "adoption", "approved", "etf",
        "hits", "record", "highs", "soars", "soaring", "jumps", "gains", "rises", "climbs", "inflows", "recovers", "rebounds"}
BEAR = {"bearish", "sell", "selling", "short", "dump", "dumping", "crash", "crashing", "rug", "scam", "exit", "overvalued",
        "lower", "resistance", "top", "rejection", "liquidated", "liquidation", "hack", "exploit", "delist", "lawsuit", "ban",
        "falls", "drops", "plunges", "plunging", "slides", "tumbles", "loses", "outflows", "declines", "sinks", "lows", "selloff"}
CONVICTION = {"all in", "conviction", "guaranteed", "100%", "massive", "huge", "generational", "no doubt", "easy", "obvious"}
URGENCY = {"now", "today", "breaking", "urgent", "asap", "immediately", "just in", "right now", "last chance", "alert"}
NAMES = {"bitcoin": "BTC", "ethereum": "ETH", "ether": "ETH", "solana": "SOL", "bnb": "BNB", "xrp": "XRP", "dogecoin": "DOGE",
         "cardano": "ADA", "avalanche": "AVAX", "chainlink": "LINK", "polygon": "POL", "toncoin": "TON", "sui": "SUI"}
CASHTAG = re.compile(r"\$([A-Za-z]{2,10})\b")
WORD = re.compile(r"[a-z0-9%]+")
STOP_TAGS = {"USD", "USDT", "USDC", "US"}


class Scored(BaseModel):
    voice: str
    id: str
    url: str
    at: str | None
    text: str
    tokens: list[str]
    sentiment: float | None  # None = no sentiment-bearing words found (never coerced to 0)
    conviction: float
    urgency: float


def score_text(text: str, tracked: set[str] | None) -> tuple[list[str], float | None, float, float]:
    low = text.lower()
    words = WORD.findall(low)
    tokens = {t.upper() for t in CASHTAG.findall(text) if t.upper() not in STOP_TAGS}
    for name, sym in NAMES.items():
        if re.search(rf"\b{name}\b", low):
            tokens.add(sym)
    if tracked:
        for sym in tracked:
            if re.search(rf"\b{re.escape(sym.lower())}\b", low):
                tokens.add(sym)
        tokens &= tracked
    bull = sum(w in BULL for w in words)
    bear = sum(w in BEAR for w in words)
    sentiment = None if bull + bear == 0 else round((bull - bear) / (bull + bear), 3)
    conviction = min(1.0, round(0.25 * text.count("!") + 0.3 * sum(p in low for p in CONVICTION)
                                + 0.2 * (sum(w.isupper() and len(w) > 3 for w in text.split()) >= 3), 3))
    urgency = min(1.0, round(0.4 * sum(p in low for p in URGENCY), 3))
    return sorted(tokens), sentiment, conviction, urgency


def _within(msg: Message, since: datetime) -> bool:
    if msg.at is None:
        return True  # unknown time: keep, and the voice carries a warning
    try:
        return datetime.fromisoformat(msg.at.replace("Z", "+00:00")) >= since
    except ValueError:
        return True


def narrative_convergence(
    voices: list[str], tokens: list[str] | None = None, hours: int = 24,
    telegram: TelegramPublic | None = None, tavily: Tavily | None = None,
) -> Envelope:
    voices = [v.strip() for v in voices if v.strip()][:20]
    tracked = {t.upper() for t in tokens} if tokens else None
    hours = max(1, min(int(hours), 24 * 14))
    since = datetime.now(timezone.utc) - timedelta(hours=hours)
    telegram = telegram or TelegramPublic()
    tavily = tavily or Tavily()

    availability: dict[str, str] = {}
    warnings: list[str] = []
    voice_rows: list[dict[str, Any]] = []
    scored: list[Scored] = []

    for voice in voices:
        kind, _, ident = voice.partition(":")
        try:
            if kind == "tg":
                msgs = telegram.fetch(ident)
            elif kind == "x":
                if not tavily.configured:
                    raise SourceUnavailable(f"{voice}: TAVILY_API_KEY not set; X voices need Tavily")
                res = tavily.search(f"from:{ident} OR \"@{ident}\"", max_results=10,
                                    time_range="day" if hours <= 24 else "week", include_domains=["x.com", "twitter.com"])
                msgs = [Message(voice=voice, id=r.url, url=r.url, at=r.published_date, text=f"{r.title} {r.content}".strip()) for r in res]
                if msgs and all(m.at is None for m in msgs):
                    warnings.append(f"{voice}: publication times unavailable from search; window filter not applied")
            else:
                raise SourceUnavailable(f"{voice}: unknown voice kind {kind!r} (use tg: or x:)")
        except SourceUnavailable as exc:
            availability[voice] = "unavailable"
            warnings.append(str(exc))
            voice_rows.append({"id": voice, "status": "unavailable", "messages": 0, "error": str(exc)})
            continue
        kept = [m for m in msgs if _within(m, since)]
        availability[voice] = "ok"
        voice_rows.append({"id": voice, "status": "ok", "messages": len(kept), "fetched": len(msgs)})
        for m in kept:
            toks, sent, conv, urg = score_text(m.text, tracked)
            if toks:
                scored.append(Scored(voice=m.voice, id=m.id, url=m.url, at=m.at, text=m.text[:280], tokens=toks,
                                     sentiment=sent, conviction=conv, urgency=urg))

    ok_voices = [v for v, s in availability.items() if s == "ok"]
    by_token: dict[str, list[Scored]] = defaultdict(list)
    for s in scored:
        for t in s.tokens:
            by_token[t].append(s)

    token_rows: list[dict[str, Any]] = []
    for sym, items in by_token.items():
        per_voice: dict[str, list[float]] = defaultdict(list)
        for it in items:
            if it.sentiment is not None:
                per_voice[it.voice].append(it.sentiment)
        voice_signs = {v: (1 if sum(xs) > 0 else -1 if sum(xs) < 0 else 0) for v, xs in per_voice.items()}
        signed = [s for s in voice_signs.values() if s != 0]
        sentiments = [it.sentiment for it in items if it.sentiment is not None]
        direction = None
        if signed:
            direction = "bullish" if sum(signed) > 0 else "bearish" if sum(signed) < 0 else "mixed"
        converging = len(signed) >= 2 and len(set(signed)) == 1
        times = sorted(it.at for it in items if it.at)
        token_rows.append({
            "symbol": sym,
            "voices": sorted({it.voice for it in items}),
            "voice_count": len({it.voice for it in items}),
            "mentions": len(items),
            "sentiment_mean": round(sum(sentiments) / len(sentiments), 3) if sentiments else None,
            "sentiment_samples": len(sentiments),
            "conviction_mean": round(sum(it.conviction for it in items) / len(items), 3),
            "urgency_max": max(it.urgency for it in items),
            "direction": direction,
            "converging": converging,
            "coverage": round(len({it.voice for it in items}) / len(ok_voices), 3) if ok_voices else None,
            "first_seen": times[0] if times else None,
            "last_seen": times[-1] if times else None,
            "samples": [{"voice": it.voice, "at": it.at, "url": it.url, "text": it.text[:200], "sentiment": it.sentiment}
                        for it in sorted(items, key=lambda x: x.at or "", reverse=True)[:5]],
        })
    token_rows.sort(key=lambda r: (r["converging"], r["voice_count"], r["mentions"]), reverse=True)

    conv = [r for r in token_rows if r["converging"]]
    if not ok_voices:
        headline = "No voice could be read; no narrative signal"
    elif conv:
        headline = f"{len(conv)} converging narrative(s): " + ", ".join(f"{r['symbol']} {r['direction']} ({r['voice_count']} voices)" for r in conv[:3])
    else:
        headline = f"{len(token_rows)} token(s) mentioned across {len(ok_voices)} voice(s), no convergence"
    data = {"window_hours": hours, "since": since.isoformat(timespec="seconds"), "method": {"sentiment": METHOD, "lexicon_sizes": {"bull": len(BULL), "bear": len(BEAR)}},
            "voices": voice_rows, "tokens": token_rows}
    return make_envelope("narrative_convergence", {"voices": voices, "tokens": sorted(tracked) if tracked else None, "hours": hours},
                         data, availability, warnings, headline,
                         key_points=[f"{r['symbol']}: {r['direction'] or 'no sentiment words'}, {r['voice_count']} voice(s), {r['mentions']} mention(s)" for r in token_rows[:5]])
