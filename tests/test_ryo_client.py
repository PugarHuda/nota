import json

import httpx
import pytest
import respx

from arena.ryo_client import RecordedRyoClient, RyoClient, RyoError, fixture_name, record

BASE = "https://ryo.test/api/mcp"
ENVELOPE = {
    "tool": "analyze_token", "status": "ok", "data_mode": "live", "as_of": "2026-09-04T00:00:00Z",
    "request": {"symbol": "SOL"}, "data": {"market": {"price_usd": 150.0}},
    "summary": {"headline": "h", "key_points": []}, "availability": {}, "warnings": [],
}


def make_client(**kw):
    sleeps: list[float] = []
    client = RyoClient(BASE, "ryo_mcp_test", sleep=sleeps.append, **kw)
    return client, sleeps


@respx.mock
def test_call_sends_bearer_and_parses_envelope():
    route = respx.post(f"{BASE}/tools/analyze_token/call").mock(
        return_value=httpx.Response(200, json={"result": ENVELOPE}, headers={"x-trace-id": "abc", "X-RateLimit-Remaining": "41"})
    )
    client, _ = make_client()
    env = client.call("analyze_token", {"symbol": "SOL"})
    assert route.calls[0].request.headers["Authorization"] == "Bearer ryo_mcp_test"
    assert json.loads(route.calls[0].request.content) == {"symbol": "SOL"}
    assert env.get("market.price_usd") == 150.0
    assert env.trace_id == "abc"
    assert client.last_rate_limit["X-RateLimit-Remaining"] == "41"


@respx.mock
def test_retries_429_using_retry_after_then_succeeds():
    respx.post(f"{BASE}/tools/analyze_token/call").mock(side_effect=[
        httpx.Response(429, json={"code": "RATE_LIMITED", "message": "slow down", "trace_id": "t"}, headers={"Retry-After": "2"}),
        httpx.Response(200, json={"result": ENVELOPE}),
    ])
    client, sleeps = make_client()
    env = client.call("analyze_token", {"symbol": "SOL"})
    assert env.status == "ok"
    assert sleeps == [2.0]


@respx.mock
def test_400_is_not_retried_and_raises_error_envelope():
    route = respx.post(f"{BASE}/tools/analyze_token/call").mock(
        return_value=httpx.Response(400, json={"code": "INVALID_ARGUMENT", "message": "bad symbol", "trace_id": "tr-9"})
    )
    client, sleeps = make_client()
    with pytest.raises(RyoError) as ei:
        client.call("analyze_token", {"symbol": "???"})
    assert ei.value.code == "INVALID_ARGUMENT" and ei.value.trace_id == "tr-9" and ei.value.status_code == 400
    assert route.call_count == 1 and sleeps == []


@respx.mock
def test_503_exhausts_retries():
    route = respx.post(f"{BASE}/tools/deep_analysis/call").mock(return_value=httpx.Response(503, json={"code": "UNAVAILABLE", "message": "down"}))
    client, sleeps = make_client(max_retries=2)
    with pytest.raises(RyoError) as ei:
        client.call("deep_analysis", {"symbol": "SOL"})
    assert ei.value.status_code == 503 and route.call_count == 3 and len(sleeps) == 2


@respx.mock
def test_health_is_unauthenticated():
    route = respx.get(f"{BASE}/health").mock(return_value=httpx.Response(200, json={"status": "ok", "tools": 6}))
    client, _ = make_client()
    assert client.health()["tools"] == 6
    assert "Authorization" not in route.calls[0].request.headers


def test_fixture_name():
    assert fixture_name("analyze_token", {"symbol": "sol"}) == "SOL"
    assert fixture_name("market_overview", {}) == "default"
    assert fixture_name("market_overview", None) == "default"
    assert len(fixture_name("scan_market", {"chain": "bsc", "top_n": 5})) == 12


def test_recorded_client_reads_fixture_and_raises_when_missing(tmp_path):
    (tmp_path / "analyze_token").mkdir()
    (tmp_path / "analyze_token" / "SOL.json").write_text(json.dumps(ENVELOPE))
    src = RecordedRyoClient(tmp_path)
    assert src.call("analyze_token", {"symbol": "SOL"}).as_of == "2026-09-04T00:00:00Z"
    with pytest.raises(RyoError) as ei:
        src.call("analyze_token", {"symbol": "BTC"})
    assert ei.value.code == "NO_FIXTURE"


@respx.mock
def test_record_writes_fixture(tmp_path):
    respx.post(f"{BASE}/tools/market_overview/call").mock(return_value=httpx.Response(200, json={"result": {**ENVELOPE, "tool": "market_overview", "request": {}}}))
    client, _ = make_client()
    path = record(client, "market_overview", {}, tmp_path)
    assert path == tmp_path / "market_overview" / "default.json"
    assert RecordedRyoClient(tmp_path).call("market_overview", {}).tool == "market_overview"
