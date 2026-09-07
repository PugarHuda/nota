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
    assert names == {"narrative_convergence", "news_verify", "price_crosscheck", "technicals_crosscheck"}
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
    assert toks == ["SOL"] and sent is not None and sent > 0.5 and urg > 0 and conv > 0
    toks, sent, _, _ = score_text("$1,000 in gold vs $BTC over 10 years. No opinion.", None)
    assert toks == ["BTC"] and sent is None  # no sentiment words -> None, not 0
    toks, sent, _, _ = score_text("$ETH resistance rejection, bearish. $USDT fine", {"ETH"})
    assert toks == ["ETH"] and sent is not None and sent < -0.3


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
    assert env.availability == {"tg:alpha": "ok", "tg:beta": "ok", "tg:private": "unavailable", "x:someone": "unavailable"}
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
                      ryo=RecordedRyoClient(FIXTURES, name="fixture"))
    assert route.calls[0].request.headers["Authorization"] == "Bearer tvly-test"
    assert env.status == "ok" and env.data["verdict"] == "weak"  # coindesk + theblock = 2 relevant domains
    assert env.data["distinct_domains"] == 2 and env.data["top_score"] == 0.9
    assert env.data["market_context"]["symbol"] == "SOL" and env.data["market_context"]["headline"] == "SOL steady"
    assert env.availability == {"search": "ok", "market": "ok"}
    assert "Claim weak" in env.summary.headline


@respx.mock
def test_news_verify_search_down_is_unavailable_but_market_still_attached():
    respx.post("https://api.tavily.com/search").mock(return_value=httpx.Response(503))
    env = news_verify("anything", symbol="SOL", rss=False, tavily=Tavily(api_key="tvly-test", http=httpx.Client()), ryo=RecordedRyoClient(FIXTURES, name="fixture"))
    assert env.status == "unavailable" and env.data["verdict"] is None and env.availability["market"] == "ok"


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
