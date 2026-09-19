"""Skill `news_verify`: is a story corroborated, and what does the market evidence say?

Corroboration is counted, not judged: how many distinct domains report it, how relevant the search
backend or the headline match scores them, and how many lean the opposite way (the claim's and the
headline's lexicon sentiment have opposite signs). When a RYO source is supplied the skill attaches the token's own
`analyze_token` read so a reader sees claim and price evidence side by side.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import urlparse

from nota.envelope import Envelope
from nota.ryo_client import RyoError, RyoSource
from nota.skills.contract import OK, SkillArg, SkillDefinition, SourceUnavailable, make_envelope
from nota.skills.narrative import METHOD, sentiment
from nota.skills.sources import RssNews, Tavily, TavilyResult, VeniceSearch, search_backend

DEFINITION = SkillDefinition(
    name="news_verify",
    description="Verify a crypto news claim: count independent domains reporting it (four outlets' RSS headlines plus Tavily "
    "or Venice web search), count those whose headline leans the opposite way, and attach the token's RYO analyze_token "
    "evidence so the story and the market read sit side by side.",
    args=[
        SkillArg(name="claim", type="string", description="The story to verify, in one sentence"),
        SkillArg(name="symbol", type="string", required=False, description="Token symbol to attach market evidence for"),
        SkillArg(name="max_results", type="integer", required=False, description="Search results to inspect (default 6, max 20)"),
    ],
)

CORROBORATED_DOMAINS = 3
MIN_SCORE = 0.5
SIGN_MIN = 0.05  # VADER's own neutral band: |compound| under 0.05 has no stance


def _sign(x: float | None) -> int:
    return 0 if x is None or abs(x) < SIGN_MIN else (1 if x > 0 else -1)


def _domain(url: str) -> str:
    host = urlparse(url).netloc.lower()
    return host[4:] if host.startswith("www.") else host


def news_verify(claim: str, symbol: str | None = None, max_results: int = 6,
                tavily: Tavily | VeniceSearch | None = None, ryo: RyoSource | None = None,
                rss: RssNews | None | bool = None) -> Envelope:
    """`rss=False` disables the headline pass (tests); `None` uses the four default feeds."""
    search = tavily or search_backend()
    rss = rss if rss is not None else RssNews()
    max_results = max(1, min(int(max_results), 20))
    availability: dict[str, str] = {}
    warnings: list[str] = []
    # the claim is already in the envelope's `request`; echoing it in `data` too doubled what a caller
    # could make this public endpoint repeat back in its answer
    data: dict[str, Any] = {"sources": [], "distinct_domains": None, "top_score": None, "verdict": None,
                            "method": {"search": None, "headlines": rss.name if rss else None, "stance": METHOD}}

    results: list[TavilyResult] = []
    # dated headlines first (free, deterministic), then the search backend for breadth
    if rss:
        try:
            results += rss.search(claim, max_results=max_results, time_range="week")
            availability["headlines"] = "available" if not rss.failed else "partial"
            warnings += [f"rss: {f}" for f in rss.failed]
        except SourceUnavailable as exc:
            availability["headlines"] = "unavailable"
            warnings.append(str(exc))
    try:
        seen = {r.url for r in results}
        results += [r for r in search.search(claim, max_results=max_results, topic="news", time_range="week") if r.url not in seen]
        data["method"]["search"] = getattr(search, "name", "tavily")
        availability["search"] = "available"
        if not getattr(search, "has_dates", True):
            warnings.append("search backend returns no dates or relevance scores: its hits are not time-bound and each counts as relevant")
    except SourceUnavailable as exc:
        # a missing search key costs the search pass only; the headlines already read still count
        availability["search"] = "unavailable"
        warnings.append(str(exc))

    if any(availability.get(k) in OK | {"partial"} for k in ("headlines", "search")):
        # A headline on the same topic that leans the other way ("crashed" vs "hits record") reports the
        # opposite story; counting it as corroboration would confirm a claim by its own refutation.
        claim_sign = _sign(sentiment(claim))
        sources = [{"title": r.title, "url": r.url, "domain": _domain(r.url), "score": r.score,
                    "published_date": r.published_date, "snippet": r.content[:300],
                    "stance": "contradicts" if claim_sign and _sign(sentiment(r.title)) == -claim_sign else "supports"}
                   for r in results]
        relevant = [s for s in sources if s["score"] is None or s["score"] >= MIN_SCORE]
        domains = sorted({s["domain"] for s in relevant if s["stance"] == "supports"})
        against = sorted({s["domain"] for s in relevant if s["stance"] == "contradicts"})
        scores = [s["score"] for s in sources if s["score"] is not None]
        verdict = ("disputed" if against and len(against) >= len(domains) else
                   "corroborated" if len(domains) >= CORROBORATED_DOMAINS else "weak" if domains else "unverified")
        data.update(sources=sources, distinct_domains=len(domains), domains=domains, contradicting_domains=against,
                    supports=len(domains), contradicts=len(against), claim_sentiment=sentiment(claim),
                    top_score=max(scores) if scores else None, verdict=verdict,
                    thresholds={"corroborated_domains": CORROBORATED_DOMAINS, "min_score": MIN_SCORE,
                                "sentiment_sign": SIGN_MIN})

    if symbol:
        symbol = symbol.upper()
        if ryo is None:
            availability["market"] = "unavailable"
            warnings.append("market context not attached: no RYO source configured")
        else:
            try:
                env = ryo.call("analyze_token", {"symbol": symbol})
                data["market_context"] = {"symbol": symbol, "status": env.status, "as_of": env.as_of, "data_mode": env.data_mode,
                                          "headline": env.summary.headline, "key_points": env.summary.key_points,
                                          "performance": env.get("performance"), "verdict": env.get("verdict"),
                                          "warnings": env.warnings}
                availability["market"] = {"ok": "available"}.get(env.status, env.status)
            except RyoError as exc:
                availability["market"] = "unavailable"
                warnings.append(f"analyze_token failed: {exc.code}: {exc.message}")

    v = data["verdict"]
    if v is None:
        headline = "Claim could not be checked: headlines and search unavailable"
    else:
        headline = f"Claim {v}: {data['supports']} independent domain(s) report it"
        if data["contradicts"]:
            headline += f", {data['contradicts']} report the opposite"
        if data.get("market_context"):
            headline += f"; {symbol} market: {data['market_context']['headline']}"
    return make_envelope("news_verify", {"claim": claim, "symbol": symbol, "max_results": max_results}, data, availability,
                         warnings, headline, key_points=[f"{s['domain']}: {s['title']}" for s in data["sources"][:5]],
                         primary=["search", "headlines"] if "headlines" in availability else ["search"])
