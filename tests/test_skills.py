import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest
import respx

from nota.envelope import Envelope
from nota.ryo_client import RecordedRyoClient, RyoError
from nota.skills import definitions, invoke
from nota.skills.contract import SourceUnavailable, status_from
from nota.skills.narrative import narrative_convergence, score_text
from nota.skills.news import news_verify
from nota.skills.sources import Tavily, TelegramPublic, parse_telegram_preview

HTML = Path(__file__).parent / "fixtures" / "html"
FIXTURES = Path(__file__).parent / "fixtures"
NOW = datetime.now(timezone.utc)


def page(name):
    t1 = (NOW - timedelta(hours=2)).isoformat(timespec="seconds")
    t2 = (NOW - timedelta(hours=30)).isoformat(timespec="seconds")
    return (HTML / f"tg_{name}.html").read_text(encoding="utf-8").replace("__T1__", t1).replace("__T2__", t2)


# --- contract ---------------------------------------------------------------------------
def test_status_from():
    assert status_from({"a": "ok", "b": "ok"}) == "ok"
    assert status_from({"a": "ok", "b": "unavailable"}) == "partial"
    assert status_from({"a": "unavailable", "b": "unavailable"}) == "unavailable"
    assert status_from({"search": "unavailable", "market": "ok"}, primary=["search"]) == "unavailable"
    assert status_from({}) == "unavailable"


def test_registry_definitions_and_arg_validation():
    names = {d.name for d in definitions()}
    assert names == {"narrative_convergence", "news_verify", "price_crosscheck", "technicals_crosscheck", "positioning_check", "move_base_rate", "verdict_track_record"}
    assert all(not d.requires_guard and d.read_only for d in definitions())
    with pytest.raises(ValueError, match="missing required"):
        invoke("news_verify", {})
    with pytest.raises(ValueError, match="unknown args"):
        invoke("news_verify", {"claim": "x", "bogus": 1})
    with pytest.raises(KeyError):
        invoke("nope", {})


# --- telegram parsing -------------------------------------------------------------------
def test_parse_telegram_preview_extracts_messages():
    msgs = parse_telegram_preview(page("alpha"), "tg:alpha")
    assert [m.id for m in msgs] == ["alpha/101", "alpha/102", "alpha/90"]
    assert msgs[0].url == "https://t.me/alpha/101" and msgs[0].views == "12K"
    assert "$SOL breakout" in msgs[0].text and "@alpha" in msgs[0].text
    assert msgs[1].text.startswith("$1,000 in gold")  # entities unescaped


def test_parse_private_channel_is_unavailable():
    with pytest.raises(SourceUnavailable, match="no public preview"):
        parse_telegram_preview(page("private"), "tg:private")


# --- lexicon ----------------------------------------------------------------------------
def test_score_text_tokens_sentiment_conviction_urgency():
    toks, sent, conv, urg = score_text("BREAKING: $SOL breakout confirmed, buying more now! Bullish on solana.", None)
    assert toks == ["SOL"] and sent["SOL"] is not None and sent["SOL"] > 0.5 and urg > 0 and conv > 0
    toks, sent, _, _ = score_text("$1,000 in gold vs $BTC over 10 years. No opinion.", None)
    assert toks == ["BTC"] and sent["BTC"] is None  # no sentiment words -> None, not 0
    toks, sent, _, _ = score_text("$ETH resistance rejection, bearish. $USDT fine", {"ETH"})
    assert toks == ["ETH"] and sent["ETH"] is not None and sent["ETH"] < -0.3


# --- narrative_convergence --------------------------------------------------------------
@respx.mock
def test_narrative_convergence_detects_convergence_and_reports_failures():
    respx.get(url__regex=r"https://syndication\.twitter\.com/.*").mock(return_value=httpx.Response(502))
    respx.get("https://t.me/s/alpha").mock(return_value=httpx.Response(200, text=page("alpha")))
    respx.get("https://t.me/s/beta").mock(return_value=httpx.Response(200, text=page("beta")))
    respx.get("https://t.me/s/private").mock(return_value=httpx.Response(200, text=page("private")))
    env = narrative_convergence(["tg:alpha", "tg:beta", "tg:private", "x:someone"], hours=24,
                                telegram=TelegramPublic(httpx.Client()), tavily=Tavily(api_key=""))
    assert isinstance(env, Envelope) and env.tool == "narrative_convergence" and env.status == "partial"
    assert env.availability == {"tg:alpha": "available", "tg:beta": "available", "tg:private": "unavailable", "x:someone": "unavailable"}
    assert any("TAVILY_API_KEY" in w for w in env.warnings) and any("no public preview" in w for w in env.warnings)
    tokens = {t["symbol"]: t for t in env.data["tokens"]}
    sol = tokens["SOL"]
    assert sol["voice_count"] == 2 and sol["converging"] is True and sol["direction"] == "bullish"
    assert sol["mentions"] == 2  # the 2020 'dump' post is outside the window
    assert tokens["BTC"]["sentiment_mean"] > 0 and tokens["BTC"]["voice_count"] == 1  # alpha's BTC post is 30h old
    assert "SOL bullish (2 voices)" in env.summary.headline
    assert env.data["method"]["sentiment"].startswith("vader")
    assert env.data["tokens"][0]["symbol"] in ("SOL", "BTC") and env.data["tokens"][0]["converging"]


@respx.mock
def test_narrative_all_voices_down_is_unavailable():
    respx.get("https://t.me/s/alpha").mock(return_value=httpx.Response(500))
    env = narrative_convergence(["tg:alpha"], telegram=TelegramPublic(httpx.Client()), tavily=Tavily(api_key=""))
    assert env.status == "unavailable" and env.data["tokens"] == [] and "HTTP 500" in env.warnings[0]


@respx.mock
def test_x_voice_via_tavily_marks_missing_times():
    respx.get(url__regex=r"https://syndication\.twitter\.com/.*").mock(return_value=httpx.Response(502))
    respx.post("https://api.tavily.com/search").mock(return_value=httpx.Response(200, json={"results": [
        {"title": "Trader on X", "url": "https://x.com/trader/status/1", "content": "$AVAX breakout, long here", "score": 0.8}]}))
    env = narrative_convergence(["x:trader"], telegram=TelegramPublic(httpx.Client()), tavily=Tavily(api_key="tvly-test", http=httpx.Client()))
    assert env.status == "ok" and env.data["tokens"][0]["symbol"] == "AVAX" and env.data["tokens"][0]["first_seen"] is None
    assert any("publication times unavailable" in w for w in env.warnings)


# --- news_verify ------------------------------------------------------------------------
TAVILY = {"results": [
    {"title": "A", "url": "https://www.coindesk.com/a", "content": "SOL ETF filed", "score": 0.9},
    {"title": "B", "url": "https://theblock.co/b", "content": "...", "score": 0.7},
    {"title": "C", "url": "https://coindesk.com/c", "content": "...", "score": 0.6},
    {"title": "D", "url": "https://randomblog.example/d", "content": "...", "score": 0.2},
]}


@respx.mock
def test_news_verify_corroboration_and_market_context():
    route = respx.post("https://api.tavily.com/search").mock(return_value=httpx.Response(200, json=TAVILY))
    env = news_verify("SOL ETF filed", symbol="sol", rss=False, tavily=Tavily(api_key="tvly-test", http=httpx.Client()),
                      ryo=RecordedRyoClient(FIXTURES))
    assert route.calls[0].request.headers["Authorization"] == "Bearer tvly-test"
    assert env.status == "ok" and env.data["verdict"] == "weak"  # coindesk + theblock = 2 relevant domains
    assert env.data["distinct_domains"] == 2 and env.data["top_score"] == 0.9
    assert env.data["market_context"]["symbol"] == "SOL" and env.data["market_context"]["headline"] == json.loads(
        (FIXTURES / "analyze_token" / "SOL.json").read_text(encoding="utf-8"))["summary"]["headline"]
    assert env.availability == {"search": "available", "market": "available"}
    assert "Claim weak" in env.summary.headline


@respx.mock
def test_news_verify_search_down_is_unavailable_but_market_still_attached():
    respx.post("https://api.tavily.com/search").mock(return_value=httpx.Response(503))
    env = news_verify("anything", symbol="SOL", rss=False, tavily=Tavily(api_key="tvly-test", http=httpx.Client()), ryo=RecordedRyoClient(FIXTURES))
    assert env.status == "unavailable" and env.data["verdict"] is None and env.availability["market"] == "available"


def test_news_verify_without_key_and_without_ryo():
    env = news_verify("x", symbol="SOL", tavily=Tavily(api_key=""), ryo=None, rss=False)
    assert env.status == "unavailable"
    assert any("TAVILY_API_KEY" in w for w in env.warnings) and any("no RYO source" in w for w in env.warnings)


def test_an_unavailable_envelope_does_not_claim_its_data_is_live():
    """With every source down there is no data to describe; `live` would be a claim about nothing."""
    from nota.skills.contract import make_envelope

    env = make_envelope("t", {}, {}, {"a": "unavailable", "b": "unavailable"}, ["a: down", "b: down"], "h")
    assert env.status == "unavailable" and env.data_mode == "unknown"
    ok = make_envelope("t", {}, {"x": 1}, {"a": "ok"}, [], "h")
    assert ok.status == "ok" and ok.data_mode == "live"


def test_a_source_fetch_is_cached_for_five_minutes_and_a_failure_is_not():
    import httpx

    from nota.skills import sources

    hits: list[str] = []
    page = ('<section><div class="tgme_widget_message_wrap"><div data-post="chan/1">'
            '<div class="tgme_widget_message_text">hi</div></div></section>')
    status = {"code": 500}

    def handler(request: httpx.Request) -> httpx.Response:
        hits.append(str(request.url))
        return httpx.Response(status["code"], text=page)

    tg = sources.TelegramPublic(http=httpx.Client(transport=httpx.MockTransport(handler)))
    for _ in range(2):
        try:
            tg.fetch("chan")
        except sources.SourceUnavailable:
            pass
    assert len(hits) == 2                       # a failure is never kept: the second call tried again
    status["code"] = 200
    assert tg.fetch("chan")[0].id == "chan/1" and tg.fetch("@chan")[0].id == "chan/1"
    assert len(hits) == 3                       # the repeat came from the cache
    sources._CACHE["https://t.me/s/chan"] = (0.0, [])   # older than the TTL
    tg.fetch("chan")
    assert len(hits) == 4


def test_news_verify_does_not_echo_the_claim_in_data():
    from nota.skills.news import news_verify
    from nota.skills.sources import TavilyResult

    class Search:
        name = "stub"

        def search(self, *a, **kw):
            return [TavilyResult(url="https://a.com/x", title="t", score=0.9)]

    env = news_verify("SOL ETF approved", tavily=Search(), rss=False)
    assert env.request["claim"] == "SOL ETF approved" and "claim" not in env.data


# --- news_verify: RSS survives a missing search backend; stance ------------------------------
def _rss(items_by_domain):
    """Three outlets' feeds served from memory; each item is (title, description)."""
    from email.utils import format_datetime

    when = format_datetime(NOW - timedelta(hours=3))

    def handler(request: httpx.Request) -> httpx.Response:
        items = items_by_domain[request.url.host]
        body = "".join(f"<item><title>{t}</title><link>https://{request.url.host}/{i}</link><description>{d}</description>"
                       f"<pubDate>{when}</pubDate></item>" for i, (t, d) in enumerate(items))
        return httpx.Response(200, text=f"<rss><channel>{body}</channel></rss>")

    from nota.skills.sources import RssNews

    return RssNews(http=httpx.Client(transport=httpx.MockTransport(handler)),
                   feeds={d: f"https://{d}/rss" for d in items_by_domain})


class _NoSearch:
    name = "tavily"

    def search(self, *a, **kw):
        raise SourceUnavailable("tavily: TAVILY_API_KEY not set")


def test_headlines_still_count_when_the_search_backend_is_missing():
    rss = _rss({d: [("Solana ETF approved by the SEC", "The regulator approved the first Solana ETF")]
                for d in ("coindesk.com", "cointelegraph.com", "decrypt.co")})
    env = news_verify("Solana ETF approved by SEC", rss=rss, tavily=_NoSearch())
    assert env.data["verdict"] == "corroborated" and env.data["supports"] == 3 and env.data["contradicts"] == 0
    assert env.status == "partial" and env.availability == {"headlines": "available", "search": "unavailable"}
    assert env.data["method"]["search"] is None and any("TAVILY_API_KEY" in w for w in env.warnings)


def test_headlines_about_the_asset_are_not_corroboration_of_a_claim_about_it():
    # the same week's real wire headlines: all about Bitcoin's price, none says it crashed to zero
    rss = _rss({"coindesk.com": [("Bitcoin price rises above $117,000 as ETF inflows return", "BTC price today")],
                "cointelegraph.com": [("Bitcoin hits record high this week", "Crypto market news")],
                "decrypt.co": [("Bitcoin price climbs as traders eye Fed cut", "Bitcoin this week")]})
    env = news_verify("Bitcoin price crashed to zero this week", rss=rss, tavily=_NoSearch())
    assert env.data["verdict"] in ("unverified", "disputed") and env.data["supports"] == 0


def test_a_headline_leaning_the_other_way_is_counted_against_the_claim():
    rss = _rss({"coindesk.com": [("Bitcoin crashed below $90,000 as liquidations mount", "")],
                "cointelegraph.com": [("Bitcoin crashed to a six-month low", "")],
                "decrypt.co": [("Bitcoin soared after it crashed overnight, record rebound", "")]})
    env = news_verify("Bitcoin crashed to zero", rss=rss, tavily=_NoSearch())
    stances = {s["domain"]: s["stance"] for s in env.data["sources"]}
    assert stances == {"coindesk.com": "supports", "cointelegraph.com": "supports", "decrypt.co": "contradicts"}
    assert env.data["verdict"] == "weak" and env.data["contradicts"] == 1
    rss = _rss({"coindesk.com": [("Bitcoin crashed overnight", "")], "decrypt.co": [("Bitcoin soared, crashed shorts wiped out, record rally", "")]})
    assert news_verify("Bitcoin crashed", rss=rss, tavily=_NoSearch()).data["verdict"] == "disputed"


# --- narrative lexicon: whole words, known tickers, per-clause sentiment ----------------------
def test_urgency_and_conviction_match_whole_words_only():
    assert score_text("I know nothing about $SOL", None)[3] == 0
    assert score_text("uneasy about $ETH", None)[2] == 0
    assert score_text("buy $SOL now", None)[3] > 0


def test_chart_words_carry_no_stance():
    toks, sent, _, _ = score_text("The top story on $BTC", None)
    assert toks == ["BTC"] and not sent["BTC"]


def test_one_post_can_lean_opposite_ways_on_two_tokens():
    toks, sent, _, _ = score_text("BTC pumping while ETH dumping", None)
    assert toks == ["BTC", "ETH"] and sent["BTC"] > 0 > sent["ETH"]


def test_only_known_symbols_count_as_tokens():
    toks, _, _, _ = score_text("CEO says $HODL, NFT and $WIF are the plays", None)
    assert toks == ["WIF"]
    assert score_text("$HODL mooning", {"HODL"})[0] == ["HODL"]  # a tracked token is always looked for


def test_a_topic_query_with_only_an_asset_matches_on_the_asset_and_its_name():
    rss = _rss({"coindesk.com": [("Solana speeds up blocks by 17%", "")], "decrypt.co": [("Fed holds rates", "")]})
    env = news_verify("SOL crypto news this week", rss=rss, tavily=_NoSearch())
    assert [s["domain"] for s in env.data["sources"]] == ["coindesk.com"]
