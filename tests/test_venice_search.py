import json

import pytest
import respx
from httpx import Response

from arena.skills.contract import SourceUnavailable
from arena.skills.narrative import score_text
from arena.skills.news import news_verify
from arena.skills.sources import Tavily, VeniceSearch, search_backend

VENICE = "https://api.venice.ai/api/v1/chat/completions"


def _venice_reply(cites):
    return Response(200, json={"choices": [{"message": {"content": ""}, "finish_reason": "length"}],
                               "venice_parameters": {"web_search_citations": cites}})


@respx.mock
def test_venice_search_maps_citations_and_asks_for_one_token():
    route = respx.post(VENICE).mock(return_value=_venice_reply([
        {"title": "A", "url": "https://www.coindesk.com/a", "content": "x", "date": ""},
        {"title": "B", "url": "https://theblock.co/b", "content": "y", "date": "2026-09-01"},
        {"title": "no url", "content": "z"},
    ]))
    res = VeniceSearch(api_key="k").search("sol etf", max_results=5)
    assert [r.url for r in res] == ["https://www.coindesk.com/a", "https://theblock.co/b"]
    assert res[0].score is None and res[0].published_date is None and res[1].published_date == "2026-09-01"
    body = json.loads(route.calls[0].request.content)
    assert body["max_completion_tokens"] == 1 and body["venice_parameters"]["enable_web_search"] == "on"


def test_venice_search_unconfigured_or_domain_filter_is_unavailable(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("VENICE_API_KEY", raising=False)
    with pytest.raises(SourceUnavailable):
        VeniceSearch(api_key="").search("q")
    with pytest.raises(SourceUnavailable, match="domain"):
        VeniceSearch(api_key="k").search("q", include_domains=["x.com"])


def test_search_backend_prefers_tavily_then_venice(monkeypatch):
    monkeypatch.setenv("TAVILY_API_KEY", "t")
    assert isinstance(search_backend(), Tavily)
    monkeypatch.delenv("TAVILY_API_KEY")
    monkeypatch.setenv("OPENAI_API_KEY", "v")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://api.venice.ai/api/v1")
    assert isinstance(search_backend(), VeniceSearch)
    monkeypatch.setenv("OPENAI_BASE_URL", "https://openrouter.ai/api/v1")
    assert isinstance(search_backend(), Tavily) and not search_backend().configured


@respx.mock
def test_news_verify_on_venice_warns_about_missing_dates():
    respx.post(VENICE).mock(return_value=_venice_reply([
        {"title": "A", "url": "https://coindesk.com/a", "content": "x"},
        {"title": "B", "url": "https://theblock.co/b", "content": "y"},
        {"title": "C", "url": "https://www.theblock.co/c", "content": "z"},
    ]))
    env = news_verify("SOL ETF approved", tavily=VeniceSearch(api_key="k"), rss=False)
    assert env.status == "ok" and env.data["method"]["search"] == "venice_web_search"
    assert env.data["distinct_domains"] == 2 and env.data["verdict"] == "weak" and env.data["top_score"] is None
    assert any("not time-bound" in w for w in env.warnings)


def test_lexicon_v2_scores_news_wire_headlines():
    _, sent, _, _ = score_text("JUST IN: Bitcoin hits new record high above $120,000", None)
    assert sent is not None and sent > 0.3
    _, sent, _, _ = score_text("Ethereum plunges 8% as ETF outflows continue", None)
    assert sent is not None and sent < 0  # 'etf' counts bullish, two bear verbs outweigh it
    _, sent, _, _ = score_text("$1,000 in gold vs $BTC over 10 years. No opinion.", None)
    assert sent is None
