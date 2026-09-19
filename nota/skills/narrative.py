"""Skill `narrative_convergence`: do trusted voices converge on the same token narrative?

Voices are `tg:<channel>` (Telegram public channel preview, free, timestamps included),
`bs:<handle>` (Bluesky's public AppView, dated) or `x:<handle>` (X's public syndication endpoint,
Tavily restricted to x.com as fallback; best effort, missing timestamps reported as such). Sentiment, conviction and urgency come from a transparent lexicon,
not a model, so the same messages always score the same. The method is named in the output.
"""

from __future__ import annotations

import re
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any

from pydantic import BaseModel

from nota.envelope import Envelope
from nota.skills.contract import SkillArg, SkillDefinition, SourceUnavailable, make_envelope
from vaderSentiment.vaderSentiment import SentimentIntensityAnalyzer

from nota.skills.sources import BlueskyPublic, Message, Tavily, TelegramPublic, XPublic

DEFINITION = SkillDefinition(
    name="narrative_convergence",
    description="Monitor up to 20 user-selected voices (Telegram public channels, Bluesky and X handles) and report which tokens "
    "they mention, lexicon-scored sentiment per token, conviction and urgency, and whether the voices converge: at least two "
    "voices carry a non-zero net sentiment on the token and every one of them has the same sign.",
    args=[
        SkillArg(name="voices", type="array", description="Up to 20 ids like tg:WatcherGuru, bs:handle.bsky.social or x:handle", items={"type": "string"}),
        SkillArg(name="tokens", type="array", required=False, description="Symbols to track; default = every cashtag, ticker or name of a known major found", items={"type": "string"}),
        SkillArg(name="hours", type="integer", required=False, description="Look-back window in hours (default 24)"),
    ],
)

METHOD = "vader_3.3.2+crypto_lexicon_v3"  # VADER (MIT) handles negation/intensity; crypto terms added at +/-2.0
# v3 dropped words that are chart vocabulary or plain English more often than a stance ("top story",
# "short term", "support for", "exit", "higher fees") and added the past tenses news wires use.
BULL = {"bullish", "buy", "buying", "breakout", "moon", "pump", "pumping", "pumped", "accumulate", "accumulating", "undervalued",
        "ath", "rally", "rallied", "surge", "surged", "surging", "bottom", "bounce", "reversal", "adoption", "approved", "approves",
        "etf", "hits", "record", "highs", "soars", "soared", "soaring", "jumps", "jumped", "gains", "gained", "rises", "climbs",
        "climbed", "inflows", "recovers", "recovered", "rebounds", "rebounded"}
BEAR = {"bearish", "sell", "selling", "dump", "dumping", "dumped", "crash", "crashed", "crashes", "crashing", "rug", "scam",
        "overvalued", "rejection", "rejects", "rejected", "liquidated", "liquidation", "hack", "hacked", "exploit", "exploited",
        "delist", "lawsuit", "ban", "falls", "drops", "dropped", "plunges", "plunged", "plunging", "plummets", "plummeted", "slides",
        "tumbles", "tumbled", "loses", "outflows", "declines", "sinks", "lows", "selloff", "slumps", "slumped"}
CONVICTION = ("all in", "conviction", "guaranteed", "100%", "massive", "huge", "generational", "no doubt", "easy", "obvious")
URGENCY = ("now", "today", "breaking", "urgent", "asap", "immediately", "just in", "right now", "last chance", "alert")


def _phrases(words: tuple[str, ...]) -> list[re.Pattern[str]]:
    # whole words only: "now" must not fire inside "know", nor "easy" inside "uneasy"
    return [re.compile(rf"(?<![a-z0-9]){re.escape(w)}(?![a-z0-9])") for w in words]


_CONVICTION = _phrases(CONVICTION)
_URGENCY = _phrases(URGENCY)
NAMES = {"bitcoin": "BTC", "ethereum": "ETH", "ether": "ETH", "solana": "SOL", "bnb": "BNB", "xrp": "XRP", "dogecoin": "DOGE",
         "cardano": "ADA", "avalanche": "AVAX", "chainlink": "LINK", "polygon": "POL", "toncoin": "TON", "sui": "SUI"}
CASHTAG = re.compile(r"\$([A-Za-z]{2,10})\b")
TICKER = re.compile(r"\b[A-Z0-9]{2,10}\b")
WORD = re.compile(r"[a-z0-9%]+")
STOP_TAGS = {"USD", "USDT", "USDC", "US"}
# One clause per stance: "BTC pumping while ETH dumping" says two opposite things about two tokens.
CLAUSE = re.compile(r"[.!?\n;]+|\b(?:while|but|whereas|although|though)\b", re.I)

_VADER = SentimentIntensityAnalyzer()
_VADER.lexicon.update({w: 2.0 for w in BULL})
_VADER.lexicon.update({w: -2.0 for w in BEAR})
_VADER.lexicon.pop("no", None)  # "no opinion" must stay null, not negative
# chart vocabulary carries no stance on its own; VADER scores some of it ("top", "support") as English praise
NEUTRAL = ("top", "short", "long", "support", "resistance", "exit", "higher", "lower")
for _w in NEUTRAL:
    _VADER.lexicon.pop(_w, None)


def known_symbols() -> set[str]:
    """Symbols a bare uppercase word or a cashtag may stand for: the scorecard universe and the tokens
    price_crosscheck maps to CoinGecko. "$HODL" or "CEO" in a post is not a token call."""
    from nota.scorecard import UNIVERSE
    from nota.skills.price_check import COINGECKO_IDS

    return (set(UNIVERSE) | set(COINGECKO_IDS)) - STOP_TAGS


def sentiment(text: str) -> float | None:
    """VADER compound with the crypto lexicon; None when no lexicon word is present: silence is not neutrality."""
    return round(_VADER.polarity_scores(text)["compound"], 3) if any(w in _VADER.lexicon for w in WORD.findall(text.lower())) else None


class Scored(BaseModel):
    voice: str
    id: str
    url: str
    at: str | None
    text: str
    tokens: list[str]
    sentiment: dict[str, float | None]  # per token, from the clauses naming it; None = no sentiment words
    conviction: float
    urgency: float


def _tokens(text: str, tracked: set[str] | None, known: set[str]) -> set[str]:
    low = text.lower()
    allowed = known | (tracked or set())
    tokens = {t.upper() for t in CASHTAG.findall(text)} | set(TICKER.findall(text))
    tokens = {t for t in tokens if t in allowed}
    for name, sym in NAMES.items():
        if re.search(rf"\b{name}\b", low):
            tokens.add(sym)
    if tracked:
        for sym in tracked:
            if re.search(rf"\b{re.escape(sym.lower())}\b", low):
                tokens.add(sym)
        tokens &= tracked
    return tokens


def score_text(text: str, tracked: set[str] | None) -> tuple[list[str], dict[str, float | None], float, float]:
    """Tokens, sentiment per token, conviction, urgency. A token's sentiment reads only the clauses that
    name it, so one post can be bullish on one token and bearish on another."""
    known = known_symbols()
    tokens = _tokens(text, tracked, known)
    clauses = [c for c in CLAUSE.split(text) if c and c.strip()]
    per = {t: sentiment(" ".join(c for c in clauses if t in _tokens(c, tracked, known))) for t in tokens}
    low = text.lower()
    conviction = min(1.0, round(0.25 * text.count("!") + 0.3 * sum(bool(p.search(low)) for p in _CONVICTION)
                                + 0.2 * (sum(w.isupper() and len(w) > 3 for w in text.split()) >= 3), 3))
    urgency = min(1.0, round(0.4 * sum(bool(p.search(low)) for p in _URGENCY), 3))
    return sorted(tokens), per, conviction, urgency


def _within(msg: Message, since: datetime) -> bool:
    if msg.at is None:
        return True  # unknown time: keep, and the voice carries a warning
    try:
        return datetime.fromisoformat(msg.at.replace("Z", "+00:00")) >= since
    except ValueError:
        return True


def narrative_convergence(
    voices: list[str], tokens: list[str] | None = None, hours: int = 24,
    telegram: TelegramPublic | None = None, tavily: Tavily | None = None, x: XPublic | None = None,
    bluesky: BlueskyPublic | None = None,
) -> Envelope:
    voices = [v.strip() for v in voices if v.strip()][:20]
    tracked = {t.upper() for t in tokens} if tokens else None
    hours = max(1, min(int(hours), 24 * 14))
    since = datetime.now(timezone.utc) - timedelta(hours=hours)
    telegram = telegram or TelegramPublic()
    tavily = tavily or Tavily()
    x = x or XPublic()
    bluesky = bluesky or BlueskyPublic()

    availability: dict[str, str] = {}
    warnings: list[str] = []
    voice_rows: list[dict[str, Any]] = []
    scored: list[Scored] = []

    for voice in voices:
        kind, _, ident = voice.partition(":")
        try:
            if kind == "tg":
                msgs = telegram.fetch(ident)
            elif kind == "bs":
                msgs = bluesky.fetch(ident)
            elif kind == "x":
                try:
                    msgs = x.fetch(ident)
                    warnings.append(f"{voice}: read through an unofficial X syndication endpoint; treat as best effort")
                except SourceUnavailable as exc:
                    if not tavily.configured:
                        raise SourceUnavailable(f"{exc}; no TAVILY_API_KEY to fall back on") from exc
                    warnings.append(f"{exc}; fell back to Tavily search")
                    res = tavily.search(f"from:{ident} OR \"@{ident}\"", max_results=10,
                                        time_range="day" if hours <= 24 else "week", include_domains=["x.com", "twitter.com"])
                    msgs = [Message(voice=voice, id=r.url, url=r.url, at=r.published_date, text=f"{r.title} {r.content}".strip()) for r in res]
                if msgs and all(m.at is None for m in msgs):
                    warnings.append(f"{voice}: publication times unavailable; window filter not applied")
            else:
                raise SourceUnavailable(f"{voice}: unknown voice kind {kind!r} (use tg:, bs: or x:)")
        except SourceUnavailable as exc:
            availability[voice] = "unavailable"
            warnings.append(str(exc))
            voice_rows.append({"id": voice, "status": "unavailable", "messages": 0, "error": str(exc)})
            continue
        kept = [m for m in msgs if _within(m, since)]
        availability[voice] = "available"
        voice_rows.append({"id": voice, "status": "available", "messages": len(kept), "fetched": len(msgs)})
        for m in kept:
            toks, sent, conv, urg = score_text(m.text, tracked)
            if toks:
                scored.append(Scored(voice=m.voice, id=m.id, url=m.url, at=m.at, text=m.text[:280], tokens=toks,
                                     sentiment=sent, conviction=conv, urgency=urg))

    ok_voices = [v for v, s in availability.items() if s == "available"]
    by_token: dict[str, list[Scored]] = defaultdict(list)
    for s in scored:
        for t in s.tokens:
            by_token[t].append(s)

    token_rows: list[dict[str, Any]] = []
    for sym, items in by_token.items():
        per_voice: dict[str, list[float]] = defaultdict(list)
        for it in items:
            if it.sentiment.get(sym) is not None:
                per_voice[it.voice].append(it.sentiment[sym])
        voice_signs = {v: (1 if sum(xs) > 0 else -1 if sum(xs) < 0 else 0) for v, xs in per_voice.items()}
        signed = [s for s in voice_signs.values() if s != 0]
        sentiments = [it.sentiment[sym] for it in items if it.sentiment.get(sym) is not None]
        direction = None
        if signed:
            direction = "bullish" if sum(signed) > 0 else "bearish" if sum(signed) < 0 else "mixed"
        converging = len(signed) >= 2 and len(set(signed)) == 1  # every signed voice agrees, and there are two
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
            "samples": [{"voice": it.voice, "at": it.at, "url": it.url, "text": it.text[:200], "sentiment": it.sentiment.get(sym)}
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
