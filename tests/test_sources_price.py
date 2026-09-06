import json
from pathlib import Path

import httpx
import pytest
import respx
from httpx import Response

from nota.calibration import resolve
from nota.decide import decide
from nota.ledger import Ledger
from nota.ryo_client import RecordedRyoClient, RyoError
from nota.skills.contract import SourceUnavailable
from nota.skills.narrative import narrative_convergence, score_text
from nota.skills.news import news_verify
from nota.skills.price_check import ExchangePrices, price_crosscheck
from nota.skills.sources import NitterPublic, RssNews, Tavily, parse_nitter_timeline
from tests.test_decide_replay import make_llm

FIXTURES = Path(__file__).parent / "fixtures"

RSS = """<?xml version="1.0"?><rss version="2.0"><channel><title>t</title>
<item><title>SEC approves spot Solana ETF</title><link>https://www.coindesk.com/a</link><description>&lt;p&gt;Approval for SOL fund&lt;/p&gt;</description><pubDate>Fri, 05 Sep 2026 10:00:00 +0000</pubDate></item>
<item><title>Ethereum gas fees fall</title><link>https://www.coindesk.com/b</link><description>x</description><pubDate>Fri, 05 Sep 2026 09:00:00 +0000</pubDate></item>
<item><title>Solana ETF filing from 2024</title><link>https://www.coindesk.com/old</link><description>old</description><pubDate>Mon, 01 Jan 2024 09:00:00 +0000</pubDate></item>
</channel></rss>"""


@respx.mock
def test_rss_scores_keywords_and_filters_dates():
    respx.get("https://www.coindesk.com/arc/outboundfeeds/rss/").mock(return_value=Response(200, content=RSS.encode()))
    respx.get("https://cointelegraph.com/rss").mock(return_value=Response(503))
    rss = RssNews(feeds={"coindesk.com": "https://www.coindesk.com/arc/outboundfeeds/rss/", "cointelegraph.com": "https://cointelegraph.com/rss"})
    hits = rss.search("SEC approves spot Solana ETF", time_range="week")
    assert [h.url for h in hits] == ["https://www.coindesk.com/a"] and hits[0].score == 1.0
    assert hits[0].published_date == "2026-09-05T10:00:00+00:00" and hits[0].content == "Approval for SOL fund"
    assert rss.failed == ["cointelegraph.com: rss cointelegraph.com: HTTP 503"]
    with pytest.raises(SourceUnavailable):
        rss.search("the of", time_range="week")


@respx.mock
def test_rss_rejects_entity_expansion():
    bomb = '<?xml version="1.0"?><!DOCTYPE a [<!ENTITY x "xxxx"><!ENTITY y "&x;&x;&x;&x;">]><rss><channel><item><title>&y;</title><link>https://d/1</link></item></channel></rss>'
    respx.get("https://d/feed").mock(return_value=Response(200, content=bomb.encode()))
    rss = RssNews(feeds={"d": "https://d/feed"})
    with pytest.raises(SourceUnavailable, match="every feed failed"):
        rss.search("xxxx")


@respx.mock
def test_news_verify_combines_rss_and_backend(monkeypatch):
    respx.get("https://www.coindesk.com/arc/outboundfeeds/rss/").mock(return_value=Response(200, content=RSS.encode()))
    respx.post("https://api.tavily.com/search").mock(return_value=Response(200, json={"results": [
        {"title": "B", "url": "https://theblock.co/b", "content": "y", "score": 0.9},
        {"title": "dup", "url": "https://www.coindesk.com/a", "content": "y", "score": 0.9}]}))
    rss = RssNews(feeds={"coindesk.com": "https://www.coindesk.com/arc/outboundfeeds/rss/"})
    env = news_verify("SEC approves spot Solana ETF", tavily=Tavily(api_key="t", http=httpx.Client()), rss=rss)
    assert env.availability == {"headlines": "ok", "search": "ok"} and env.status == "ok"
    assert env.data["method"] == {"search": "tavily", "headlines": "rss_headlines"}
    assert [s["domain"] for s in env.data["sources"]] == ["coindesk.com", "theblock.co"]  # de-duplicated by url
    assert env.data["sources"][0]["published_date"] == "2026-09-05T10:00:00+00:00" and env.data["verdict"] == "weak"


NITTER = """<div class="timeline-item "><a class="tweet-link" href="/WatcherGuru/status/1#m"></a><div class="tweet-body">
<span class="tweet-date"><a href="/WatcherGuru/status/1" title="Sep 6, 2026 · 7:00 AM UTC">Sep 6</a></span>
<div class="tweet-content media-body" dir="auto">JUST IN: $SOL soars 8% as spot ETF approved</div></div></div></div>
<div class="timeline-item "><a class="tweet-link" href="/WatcherGuru/status/2#m"></a><div class="tweet-body">
<div class="tweet-content media-body" dir="auto">Not bullish on $ETH here, outflows continue</div></div></div></div>"""


@respx.mock
def test_nitter_parse_and_router_redirect():
    respx.get("https://twiiit.com/WatcherGuru").mock(return_value=Response(302, headers={"location": "https://nitter.example/WatcherGuru"}))
    respx.get("https://nitter.example/WatcherGuru").mock(return_value=Response(200, text=NITTER))
    msgs = NitterPublic(http=httpx.Client(follow_redirects=True)).fetch("@WatcherGuru")
    assert [m.url for m in msgs] == ["https://x.com/WatcherGuru/status/1", "https://x.com/WatcherGuru/status/2"]
    assert msgs[0].at == "2026-09-06T07:00:00+00:00" and msgs[1].at is None
    with pytest.raises(SourceUnavailable):
        parse_nitter_timeline("<html>rate limited</html>", "x:a", "https://n")


@respx.mock
def test_x_voice_uses_mirror_then_tavily_fallback(monkeypatch):
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    respx.get("https://twiiit.com/WatcherGuru").mock(return_value=Response(200, text=NITTER))
    respx.get("https://twiiit.com/dead").mock(return_value=Response(502))
    env = narrative_convergence(["x:WatcherGuru", "x:dead"], hours=24 * 7, nitter=NitterPublic(http=httpx.Client(follow_redirects=True)), tavily=Tavily(api_key=""))
    assert env.availability == {"x:WatcherGuru": "ok", "x:dead": "unavailable"}
    sol = next(t for t in env.data["tokens"] if t["symbol"] == "SOL")
    assert sol["sentiment_mean"] > 0
    assert any("unofficial Nitter mirror" in w for w in env.warnings) and any("x:dead" in w and "TAVILY" in w for w in env.warnings)


def test_vader_handles_negation_and_null():
    _, pos, _, _ = score_text("$SOL looks very bullish!!", None)
    _, neg, _, _ = score_text("Not bullish on $ETH here", None)
    _, none, _, _ = score_text("$BTC 21 million cap", None)
    assert pos > 0.5 and neg < 0 and none is None


def _prices(cg=150.0, cb=151.0, kr=None):
    respx.get("https://api.alternative.me/fng/").mock(return_value=Response(200, json={"data": [{"value": "73", "value_classification": "Greed", "timestamp": "1788652800"}]}))
    respx.get("https://api.coingecko.com/api/v3/simple/price").mock(return_value=Response(200, json={"solana": {"usd": cg, "last_updated_at": 1788678000}}))
    respx.get("https://api.coinbase.com/v2/prices/SOL-USD/spot").mock(return_value=Response(200, json={"data": {"amount": str(cb), "base": "SOL", "currency": "USD"}}))
    respx.get("https://api.kraken.com/0/public/Ticker").mock(return_value=Response(200, json={"error": ["EQuery:Unknown asset pair"]} if kr is None else {"error": [], "result": {"SOLUSD": {"c": [str(kr), "1"]}}}))


@respx.mock
def test_price_crosscheck_reports_sources_median_and_deviation():
    _prices(kr=152.0)
    env = price_crosscheck("sol", reference_price=160.0, reference_path="deep_analysis.data.market.price_usd", exchanges=ExchangePrices(http=httpx.Client()))
    assert env.status == "ok" and env.data["median_usd"] == 151.0 and env.data["spread_pct"] == pytest.approx(1.325, abs=0.001)
    assert env.data["sources"][0]["as_of"] == "2026-09-06T07:00:00+00:00"
    assert env.data["reference"]["deviation_pct"] == pytest.approx(5.96, abs=0.01) and any("deviates" in w for w in env.warnings)


@respx.mock
def test_price_crosscheck_partial_and_unavailable():
    _prices()  # kraken unknown pair
    env = price_crosscheck("SOL", exchanges=ExchangePrices(http=httpx.Client()))
    assert env.status == "partial" and env.availability["kraken"] == "unavailable" and env.data["sources_ok"] == 2
    respx.get("https://api.coingecko.com/api/v3/simple/price").mock(return_value=Response(429))
    respx.get("https://api.coinbase.com/v2/prices/SOL-USD/spot").mock(side_effect=httpx.ConnectError("x"))
    env = price_crosscheck("SOL", exchanges=ExchangePrices(http=httpx.Client()))
    assert env.status == "unavailable" and env.data["median_usd"] is None and env.data["spread_pct"] is None


@respx.mock
def test_resolve_falls_back_to_exchange_median_when_ryo_has_no_price():
    _prices(kr=152.0)
    led = Ledger(":memory:")
    r = decide("SOL", RecordedRyoClient(FIXTURES, name="fixture"), make_llm(), led)

    class Dead:
        name = "dead"

        def call(self, tool, args=None):
            raise RyoError(503, "UPSTREAM", "down")

    out = resolve(r.id, led, Dead())
    assert out.price_now == 151.0 and out.price_now_source == "exchange_median:3_sources" and out.price_then == 150.0
