import json
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
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
from nota.skills.sources import RssNews, Tavily, XPublic
from tests.test_decide_replay import make_llm

FIXTURES = Path(__file__).parent / "fixtures"

# Dated relative to today: fixed September dates aged out of the "week" filter and failed the suite.
_NEW = datetime.now(timezone.utc).replace(microsecond=0) - timedelta(days=1)
RSS = f"""<?xml version="1.0"?><rss version="2.0"><channel><title>t</title>
<item><title>SEC approves spot Solana ETF</title><link>https://www.coindesk.com/a</link><description>&lt;p&gt;Approval for SOL fund&lt;/p&gt;</description><pubDate>{format_datetime(_NEW)}</pubDate></item>
<item><title>Ethereum gas fees fall</title><link>https://www.coindesk.com/b</link><description>x</description><pubDate>{format_datetime(_NEW - timedelta(hours=1))}</pubDate></item>
<item><title>Solana ETF filing from 2024</title><link>https://www.coindesk.com/old</link><description>old</description><pubDate>Mon, 01 Jan 2024 09:00:00 +0000</pubDate></item>
</channel></rss>"""


@respx.mock
def test_rss_scores_keywords_and_filters_dates():
    respx.get("https://www.coindesk.com/arc/outboundfeeds/rss/").mock(return_value=Response(200, content=RSS.encode()))
    respx.get("https://cointelegraph.com/rss").mock(return_value=Response(503))
    rss = RssNews(feeds={"coindesk.com": "https://www.coindesk.com/arc/outboundfeeds/rss/", "cointelegraph.com": "https://cointelegraph.com/rss"})
    hits = rss.search("SEC approves spot Solana ETF", time_range="week")
    assert [h.url for h in hits] == ["https://www.coindesk.com/a"] and hits[0].score == 1.0
    assert hits[0].published_date == _NEW.isoformat() and hits[0].content == "Approval for SOL fund"
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
    assert env.availability == {"headlines": "available", "search": "available"} and env.status == "ok"
    assert env.data["method"] == {"search": "tavily", "headlines": "rss_headlines", "stance": "vader_3.3.2+crypto_lexicon_v3"}
    assert [s["domain"] for s in env.data["sources"]] == ["coindesk.com", "theblock.co"]  # de-duplicated by url
    assert env.data["sources"][0]["published_date"] == _NEW.isoformat() and env.data["verdict"] == "weak"


NITTER = """<div class="timeline-item "><a class="tweet-link" href="/WatcherGuru/status/1#m"></a><div class="tweet-body">
<span class="tweet-date"><a href="/WatcherGuru/status/1" title="Sep 6, 2026 · 7:00 AM UTC">Sep 6</a></span>
<div class="tweet-content media-body" dir="auto">JUST IN: $SOL soars 8% as spot ETF approved</div></div></div></div>
<div class="timeline-item "><a class="tweet-link" href="/WatcherGuru/status/2#m"></a><div class="tweet-body">
<div class="tweet-content media-body" dir="auto">Not bullish on $ETH here, outflows continue</div></div></div></div>"""


@respx.mock
def test_x_voice_reads_syndication_and_falls_back_when_a_handle_cannot_be_read(monkeypatch):
    """The X reader has its own tests; this one is about the skill's behaviour around it."""
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    from pathlib import Path as _P
    payload = (_P(__file__).parent / "fixtures" / "x" / "ryodigital.html").read_text(encoding="utf-8")
    base = "https://syndication.twitter.com/srv/timeline-profile/screen-name"
    respx.get(f"{base}/ryodigital").mock(return_value=Response(200, text=payload))
    respx.get(f"{base}/dead").mock(return_value=Response(429))
    env = narrative_convergence(["x:ryodigital", "x:dead"], hours=24 * 400, x=XPublic(),
                                tavily=Tavily(api_key=""))
    assert env.availability == {"x:ryodigital": "available", "x:dead": "unavailable"}
    assert any("syndication" in w for w in env.warnings)
    assert any("x:dead" in w for w in env.warnings)


def test_vader_handles_negation_and_null():
    _, pos, _, _ = score_text("$SOL looks very bullish!!", None)
    _, neg, _, _ = score_text("Not bullish on $ETH here", None)
    _, none, _, _ = score_text("$BTC 21 million cap", None)
    assert pos["SOL"] > 0.5 and neg["ETH"] < 0 and none["BTC"] is None


def _prices(cg=150.0, cb=151.0, kr=None, bn=None, ll=None):
    respx.get("https://data-api.binance.vision/api/v3/ticker/price").mock(return_value=Response(200, json={"symbol": "SOLUSDT", "price": str(bn)}) if bn else Response(451))
    respx.get("https://coins.llama.fi/prices/current/coingecko:solana").mock(return_value=Response(200, json={"coins": {"coingecko:solana": {"price": ll, "symbol": "SOL", "timestamp": 1788678000, "confidence": 0.99}}}) if ll else Response(502))
    respx.get("https://api.alternative.me/fng/").mock(return_value=Response(200, json={"data": [{"value": "73", "value_classification": "Greed", "timestamp": "1788652800"}]}))
    respx.get("https://api.coingecko.com/api/v3/simple/price").mock(return_value=Response(200, json={"solana": {"usd": cg, "last_updated_at": 1788678000}}))
    respx.get("https://api.coinbase.com/v2/prices/SOL-USD/spot").mock(return_value=Response(200, json={"data": {"amount": str(cb), "base": "SOL", "currency": "USD"}}))
    respx.get("https://api.kraken.com/0/public/Ticker").mock(return_value=Response(200, json={"error": ["EQuery:Unknown asset pair"]} if kr is None else {"error": [], "result": {"SOLUSD": {"c": [str(kr), "1"]}}}))


@respx.mock
def test_price_crosscheck_reports_sources_median_and_deviation():
    _prices(kr=152.0, bn=151.0, ll=151.0)
    env = price_crosscheck("sol", reference_price=160.0, reference_path="deep_analysis.data.market.price_usd", exchanges=ExchangePrices(http=httpx.Client()))
    assert env.status == "ok" and env.data["median_usd"] == 151.0 and env.data["spread_pct"] == pytest.approx(1.325, abs=0.001)
    assert env.data["sources"][0]["as_of"] == "2026-09-06T07:00:00+00:00" and env.data["coingecko_id"] == "solana"
    assert set(env.availability.values()) == {"available"} and "binance: USDT used as USD proxy" in env.warnings
    assert env.data["reference"]["deviation_pct"] == pytest.approx(5.96, abs=0.01) and any("deviates" in w for w in env.warnings)


@respx.mock
def test_price_crosscheck_partial_and_unavailable():
    _prices()  # kraken unknown pair, binance geo-blocked, defillama down
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


@respx.mock
def test_resolve_needs_no_ryo_source_at_all():
    """Scoring a decision is keyless, like verifying one: with no source the price is the exchange
    median by construction, so a ledger keeps calibrating after a builder key expires."""
    _prices(kr=152.0)
    led = Ledger(":memory:")
    r = decide("SOL", RecordedRyoClient(FIXTURES, name="fixture"), make_llm(), led)
    out = resolve(r.id, led, None)
    assert out.price_now == 151.0 and out.price_now_source == "exchange_median:3_sources"


@respx.mock
def test_resolve_prices_then_from_the_exchange_median_when_ryo_answered_nothing(tmp_path):
    """The SOL call of 2026-09-10 was made while every RYO tool answered 401; its only price is the
    exchange median recorded beside that evidence. It sat unscorable in the daily cycle until this."""
    import shutil

    _prices(kr=152.0)
    db = tmp_path / "demo.db"
    shutil.copy(Path(__file__).parent.parent / "data" / "demo.db", db)
    out = resolve("2ae531ec2c9b", Ledger(str(db)), None)
    assert out.price_then == 101.0 and out.price_now == 151.0 and out.went_up and out.horizon_reached


@pytest.fixture
def slept(monkeypatch):
    from nota.skills import price_check

    out: list[float] = []
    monkeypatch.setattr(price_check.time, "sleep", out.append)
    return out


@respx.mock
def test_a_symbol_search_that_lands_on_another_coin_is_an_outlier(slept):
    """AI: CoinGecko's first 'AI' hit is not the exchanges' AI (0.2854 vs 0.0201). It used to pass with a 1317% spread."""
    respx.get("https://api.coingecko.com/api/v3/search").mock(return_value=Response(200, json={"coins": [
        {"id": "unranked-ai", "symbol": "AI", "market_cap_rank": None},
        {"id": "artificial-inu-3", "symbol": "AI", "market_cap_rank": 149},
        {"id": "ai-something-else", "symbol": "AIX", "market_cap_rank": 3},
    ]}))
    respx.get("https://api.coingecko.com/api/v3/simple/price").mock(return_value=Response(200, json={"artificial-inu-3": {"usd": 0.2854}}))
    respx.get("https://coins.llama.fi/prices/current/coingecko:artificial-inu-3").mock(return_value=Response(200, json={
        "coins": {"coingecko:artificial-inu-3": {"price": 0.2854, "timestamp": 1788678000, "confidence": 0.99}}}))
    respx.get("https://api.coinbase.com/v2/prices/AI-USD/spot").mock(return_value=Response(200, json={"data": {"amount": "0.0201"}}))
    respx.get("https://api.kraken.com/0/public/Ticker").mock(return_value=Response(200, json={"error": [], "result": {"AIUSD": {"c": ["0.0201", "1"]}}}))
    respx.get("https://data-api.binance.vision/api/v3/ticker/price").mock(return_value=Response(200, json={"price": "0.0202"}))
    respx.get("https://api.alternative.me/fng/").mock(return_value=Response(503))
    env = price_crosscheck("AI", exchanges=ExchangePrices(http=httpx.Client()))
    assert env.data["coingecko_id"] == "artificial-inu-3"  # ranked by market cap, unranked last
    assert env.availability["coingecko"] == "outlier" and env.availability["defillama"] == "outlier"  # same id, same wrong coin
    assert env.data["median_usd"] == 0.0201 and env.data["sources_ok"] == 3 and env.status == "partial"
    assert any("coingecko 0.2854" in w and "0.0201" in w and "outlier" in w for w in env.warnings)
    assert "coingecko id artificial-inu-3 may be a different asset" in env.warnings
    assert env.availability["fear_greed"] == "unavailable" and slept == []


@respx.mock
def test_coingecko_429_is_retried_once_after_retry_after(slept, monkeypatch):
    monkeypatch.setenv("COINGECKO_API_KEY", "demo-key")
    route = respx.get("https://api.coingecko.com/api/v3/simple/price").mock(side_effect=[
        Response(429, headers={"Retry-After": "60"}), Response(200, json={"solana": {"usd": 150.0}})])
    assert ExchangePrices(http=httpx.Client()).coingecko("SOL") == (150.0, None)
    assert route.call_count == 2 and slept == [10.0]  # capped
    assert route.calls[0].request.headers["x-cg-demo-api-key"] == "demo-key"
    route.side_effect = [Response(429, headers={"Retry-After": "2"}), Response(429)]
    with pytest.raises(SourceUnavailable, match="HTTP 429"):
        ExchangePrices(http=httpx.Client()).coingecko("SOL")
    assert route.call_count == 4 and slept == [10.0, 2.0]  # one retry, not a loop


def test_status_ignores_optional_sections_and_counts_outliers_as_partial():
    from nota.skills.contract import status_from

    assert status_from({"a": "ok", "b": "available", "c": "ok", "fear_greed": "unavailable"}, primary=["a", "b", "c"]) == "ok"
    assert status_from({"a": "available", "b": "outlier"}) == "partial"
    assert status_from({"a": "error", "b": "unavailable"}) == "unavailable"


@respx.mock
def test_every_skill_speaks_ryos_availability_vocabulary(monkeypatch, slept):
    """RYO's envelopes say 'available'; ours said 'ok'. Every section value, success or failure, is RYO's word."""
    from nota.skills import SKILLS

    for key in ("TAVILY_API_KEY", "VENICE_API_KEY", "RYO_MCP_KEY"):
        monkeypatch.delenv(key, raising=False)
    respx.route().mock(return_value=Response(503))
    allowed = {"available", "partial", "unavailable", "error", "outlier"}
    args = {"narrative_convergence": {"voices": ["tg:nobody"]}, "news_verify": {"claim": "SOL ETF approved"},
            "verdict_track_record": {}}
    for name, (_, fn) in SKILLS.items():
        env = fn(**args.get(name, {"symbol": "SOL"}))
        assert env.availability and set(env.availability.values()) <= allowed, (name, env.availability)
