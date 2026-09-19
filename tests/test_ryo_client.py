import json

import httpx
import pytest
import respx

from nota.ryo_client import RecordedRyoClient, RyoClient, RyoError, fixture_name, record

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
    assert fixture_name("deep_analysis", {"symbol": "sol", "include_perp": True}) == "SOL"
    assert fixture_name("market_overview", {}) == "default"
    assert fixture_name("market_overview", None) == "default"
    assert fixture_name("scan_market", {"chain": "bsc", "top_n": 5}) == "bsc-any"
    assert fixture_name("scan_market", {"theme": "news"}) == "any-news" and fixture_name("scan_market", {"top_n": 3}) == "default"
    assert fixture_name("compare_tokens", {"symbols": "sol avax", "intent": "swing"}) == "SOL-AVAX"
    assert len(fixture_name("scan_market", {"weird": 1})) == 12


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



def test_authenticated_call_without_a_key_says_which_key_is_missing(monkeypatch):
    """Otherwise httpx raises LocalProtocolError on the empty bearer, which names nothing."""
    monkeypatch.delenv("RYO_MCP_KEY", raising=False)
    client = RyoClient(BASE, sleep=lambda _s: None)
    assert client.key == ""
    with pytest.raises(RyoError, match="RYO_MCP_KEY is not set"):
        client.call("analyze_token", {"symbol": "SOL"})


# -- pacing, retries, handshake: driven by responses RYO really sent ------------------------------
from email.utils import format_datetime
from datetime import datetime, timedelta, timezone
from pathlib import Path

from nota.ryo_client import MAX_WAIT_S, check_args

FIX = Path(__file__).parent / "fixtures"
REAL_ENV = json.loads((FIX / "analyze_token" / "SOL.json").read_text(encoding="utf-8"))
CATALOG = json.loads((FIX / "ryo_catalog.json").read_text(encoding="utf-8"))  # GET /tools, captured live


def captured(name):
    """A response RYO sent when we tripped its fan-out bucket on purpose (see the file's `captured`)."""
    c = json.loads((FIX / "ryo_errors" / name).read_text(encoding="utf-8"))
    return httpx.Response(c["status"], json=c["body"], headers=c["headers"])


def mock_client(handler, **kw):
    sleeps: list[float] = []
    kw.setdefault("sleep", sleeps.append)
    c = RyoClient(BASE, "ryo_mcp_test", http=httpx.Client(transport=httpx.MockTransport(handler)), **kw)
    return c, sleeps


def mcp_handler(tool_replies, seen):
    """initialize and notifications/initialized answer as RYO does; tools/call pops the next reply."""
    def handler(request):
        body = json.loads(request.content)
        seen.append(body["method"])
        if body["method"] == "initialize":
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"], "result": {
                "protocolVersion": "2024-11-05", "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": "ryo-chan", "version": "1.0.0"}}})
        if body["method"] == "notifications/initialized":
            return httpx.Response(202)
        return tool_replies.pop(0)
    return handler


def ok_mcp(env=REAL_ENV):
    return httpx.Response(200, json={"jsonrpc": "2.0", "id": 9, "result": {
        "content": [{"type": "text", "text": json.dumps(env)}], "isError": False}}, headers={"x-trace-id": "t-ok"})


def test_mcp_rate_limit_is_200_is_error_and_is_retried_once_after_retry_after():
    seen: list[str] = []
    c, sleeps = mock_client(mcp_handler([captured("mcp_429_fanout.json"), ok_mcp()], seen), transport="mcp")
    env = c.call("analyze_token", {"symbol": "SOL"})
    assert env.tool == "analyze_token" and env.status == REAL_ENV["status"] and env.trace_id == "t-ok"
    assert sleeps == [60.0] and seen.count("tools/call") == 2


def test_mcp_rate_limit_that_never_clears_raises_rate_limited_with_its_trace_id():
    seen: list[str] = []
    c, _ = mock_client(mcp_handler([captured("mcp_429_fanout.json")] * 3, seen), transport="mcp", max_retries=2)
    with pytest.raises(RyoError) as ei:
        c.call("market_overview", {})
    assert ei.value.status_code == 429 and ei.value.code == "RATE_LIMITED"
    assert ei.value.trace_id == "5e4d7696a929431baeefc5884707afeb"


def test_initialize_precedes_the_first_tools_call_exactly_once():
    seen: list[str] = []
    c, _ = mock_client(mcp_handler([ok_mcp(), ok_mcp()], seen), transport="mcp")
    c.call("analyze_token", {"symbol": "SOL"})
    c.call("analyze_token", {"symbol": "SOL"})
    assert seen == ["initialize", "notifications/initialized", "tools/call", "tools/call"]
    assert c.server_info == {"name": "ryo-chan", "version": "1.0.0"} and c.protocol_version == "2024-11-05"


def test_sse_reply_is_parsed_from_its_last_data_line():
    seen: list[str] = []
    sse = "event: message\ndata: " + json.dumps({"jsonrpc": "2.0", "id": 3, "result": {
        "content": [{"type": "text", "text": json.dumps(REAL_ENV)}], "isError": False}}) + "\n\n"
    c, _ = mock_client(mcp_handler([httpx.Response(200, text=sse, headers={"content-type": "text/event-stream"})], seen), transport="mcp")
    assert c.call("analyze_token", {"symbol": "SOL"}).as_of == REAL_ENV["as_of"]


def test_requests_accept_json_and_event_stream():
    got = []
    c, _ = mock_client(lambda r: got.append(r) or httpx.Response(200, json={"result": REAL_ENV}))
    c.call("analyze_token", {"symbol": "SOL"})
    assert got[0].headers["accept"] == "application/json, text/event-stream"


def test_rest_fanout_refusal_is_retried_and_its_reset_is_kept():
    replies = [captured("rest_429_fanout.json"), httpx.Response(200, json={"result": REAL_ENV})]
    c, sleeps = mock_client(lambda r: replies.pop(0))
    assert c.call("analyze_token", {"symbol": "SOL"}).tool == "analyze_token"
    assert sleeps == [60.0] and c.last_rate_limit["fanout_reset_at"] == "1789831058"
    assert c.last_rate_limit["X-RateLimit-Limit"] == "60"


def test_pacer_holds_the_seventh_call_in_a_minute():
    now = [0.0]
    events: list[str] = []

    def sleep(s):
        events.append(f"sleep {s:g}")
        now[0] += s

    c, _ = mock_client(lambda r: events.append("call") or httpx.Response(200, json={"result": REAL_ENV}),
                       sleep=sleep, clock=lambda: now[0])
    for _ in range(8):
        c.call("analyze_token", {"symbol": "SOL"})
    assert events == ["call"] * 6 + ["sleep 60"] + ["call"] * 2


def test_metadata_reads_do_not_spend_the_fanout_bucket():
    now = [0.0]
    c, sleeps = mock_client(lambda r: httpx.Response(200, json=CATALOG), clock=lambda: now[0])
    for _ in range(10):
        c.tools()
    assert sleeps == []


def test_retry_after_is_capped_and_http_dates_parse():
    assert RyoClient._backoff(0, "3600") <= MAX_WAIT_S == 60.0
    soon = format_datetime(datetime.now(timezone.utc) + timedelta(seconds=30), usegmt=True)
    assert 25 <= RyoClient._backoff(0, soon) <= 30
    past = format_datetime(datetime.now(timezone.utc) - timedelta(seconds=30), usegmt=True)
    assert RyoClient._backoff(0, past) == 0.0
    assert RyoClient._backoff(0, "next tuesday") <= 1.5  # unparseable: ordinary backoff


def test_a_timeout_is_retried_once_only_with_the_tool_s_own_timeout():
    seen = []

    def handler(request):
        seen.append(request.extensions["timeout"]["read"])
        raise httpx.ReadTimeout("slow", request=request)

    c, sleeps = mock_client(handler)
    with pytest.raises(RyoError) as ei:
        c.call("deep_analysis", {"symbol": "SOL"})
    assert ei.value.code == "TIMEOUT" and seen == [120.0, 120.0] and len(sleeps) == 1
    seen.clear()
    with pytest.raises(RyoError):
        c.call("analyze_token", {"symbol": "SOL"})
    assert seen == [30.0, 30.0]


def test_network_errors_after_the_last_retry_are_ryo_errors_not_httpx_ones():
    c, _ = mock_client(lambda r: (_ for _ in ()).throw(httpx.ConnectError("refused", request=r)), max_retries=1)
    with pytest.raises(RyoError) as ei:
        c.call("analyze_token", {"symbol": "SOL"})
    assert ei.value.code == "NETWORK"


def test_check_args_against_the_live_catalog():
    from nota.evidence import SECTIONS, ryo_args

    for key, args in ryo_args("SOL").items():
        assert check_args(SECTIONS[key], args, CATALOG) == []
    assert check_args("compare_tokens", {"symbols": "SOL, BTC", "intent": "weekly"}, CATALOG) == [
        "compare_tokens: intent='weekly' is not one of ['swing', 'hold', 'spot']"]
    assert check_args("analyze_token", {"sym": "SOL"}, CATALOG) == [
        "analyze_token: missing required argument symbol", "analyze_token: unknown argument sym"]
    assert check_args("check_safety", {}, CATALOG) == ["check_safety: not in the catalog"]
