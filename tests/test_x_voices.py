"""x: voices: syndication first, then Tavily search restricted to the handle's own status URLs."""

from datetime import datetime, timedelta, timezone
import json

import httpx

from nota.skills.narrative import narrative_convergence
from nota.skills.sources import Tavily, TelegramPublic, XPublic


def _x_down() -> XPublic:
    return XPublic(http=httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(429, text="Rate limit exceeded"))))


def _tavily(results: list[dict], seen: list[dict] | None = None) -> Tavily:
    def handler(req: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(json.loads(req.content))
        return httpx.Response(200, json={"results": results})
    return Tavily(api_key="tvly-test", http=httpx.Client(transport=httpx.MockTransport(handler)))


NOW = datetime.now(timezone.utc)


def test_syndication_down_falls_back_to_tavily_with_warning():
    seen: list[dict] = []
    tv = _tavily([
        {"url": "https://x.com/fbtrader/status/111", "title": "fbtrader", "content": "$SOL breakout, bullish",
         "published_date": (NOW - timedelta(hours=2)).isoformat()},
        {"url": "https://x.com/somebodyelse/status/222", "title": "x", "content": "$SOL dump",
         "published_date": NOW.isoformat()},  # another author quoting the handle: not this voice
    ], seen)
    env = narrative_convergence(["x:fbtrader"], hours=24, x=_x_down(), tavily=tv, telegram=TelegramPublic(httpx.Client()))
    assert env.availability == {"x:fbtrader": "partial"} and env.status == "partial"
    row = env.data["voices"][0]
    assert row["via"] == "tavily_search" and row["coverage"] == "partial" and row["messages"] == 1
    assert any("syndication HTTP 429" in w and "x:fbtrader via Tavily search, 1 dated post(s), coverage partial" in w
               for w in env.warnings)
    assert env.data["tokens"][0]["symbol"] == "SOL" and env.data["tokens"][0]["first_seen"] is not None
    body = seen[0]
    assert body["include_domains"] == ["x.com", "twitter.com"] and body["include_published_date"] is True
    assert body["start_date"] == (NOW - timedelta(hours=24)).date().isoformat()


def test_tavily_missing_key_leaves_voice_unavailable_with_reason():
    env = narrative_convergence(["x:nokey"], x=_x_down(), tavily=Tavily(api_key=""), telegram=TelegramPublic(httpx.Client()))
    assert env.availability == {"x:nokey": "unavailable"} and env.status == "unavailable"
    row = env.data["voices"][0]
    assert row["messages"] is None and "no TAVILY_API_KEY" in row["error"]  # null, never 0


def test_undated_tavily_results_are_dropped_with_warning():
    tv = _tavily([
        {"url": "https://x.com/undated/status/1", "title": "undated", "content": "$AVAX breakout", "published_date": None},
        {"url": "https://twitter.com/undated/status/2", "title": "undated", "content": "$AVAX moon", "published_date": "not a date"},
    ])
    env = narrative_convergence(["x:undated"], x=_x_down(), tavily=tv, telegram=TelegramPublic(httpx.Client()))
    assert env.data["tokens"] == [] and env.data["voices"][0]["fetched"] == 0
    assert any("dropped 2 undated result(s)" in w for w in env.warnings)
