import json

import pytest

from arena.envelope import Envelope, RyoToolError, parse_mcp, parse_rest

SAMPLE = {
    "schema_version": "1",
    "tool": "analyze_token",
    "status": "partial",
    "data_mode": "live",
    "as_of": "2026-09-04T03:00:00Z",
    "request": {"symbol": "SOL"},
    "data": {"market": {"price_usd": 150.5, "volume_24h_usd": None}, "technicals": {"rsi_14": 55.2}},
    "summary": {"headline": "SOL steady", "key_points": ["RSI neutral"]},
    "availability": {"market": "ok", "profile": "unavailable"},
    "warnings": ["token profile unavailable"],
}


def test_parse_rest_unwraps_result_and_keeps_fields():
    env = parse_rest({"result": SAMPLE}, trace_id="t-1")
    assert env.tool == "analyze_token"
    assert env.status == "partial"
    assert env.trace_id == "t-1"
    assert env.summary.headline == "SOL steady"
    assert env.warnings == ["token profile unavailable"]


def test_get_returns_none_for_missing_or_null_never_zero():
    env = parse_rest(SAMPLE)
    assert env.get("market.price_usd") == 150.5
    assert env.get("market.volume_24h_usd") is None
    assert env.get("market.nothing.here") is None
    assert env.get("technicals.rsi_14") == 55.2


def test_parse_mcp_reads_text_block():
    body = {"jsonrpc": "2.0", "id": 3, "result": {"content": [{"type": "text", "text": json.dumps(SAMPLE)}], "isError": False}}
    env = parse_mcp(body)
    assert isinstance(env, Envelope)
    assert env.request == {"symbol": "SOL"}


def test_parse_mcp_is_error_raises():
    body = {"jsonrpc": "2.0", "id": 3, "result": {"content": [{"type": "text", "text": "unknown symbol"}], "isError": True}}
    with pytest.raises(RyoToolError, match="unknown symbol"):
        parse_mcp(body)


def test_parse_mcp_protocol_error_raises():
    with pytest.raises(RyoToolError, match="-32600"):
        parse_mcp({"jsonrpc": "2.0", "id": 1, "error": {"code": -32600, "message": "bad"}})
