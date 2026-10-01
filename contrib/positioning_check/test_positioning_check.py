"""Offline: every venue is an httpx.MockTransport. Run with `pytest -q contrib`."""

import json

import httpx
import pytest

from positioning_check.positioning_check import SKILL_DEFINITION, invoke

RYO_SOL = {"funding_rate_bps": 0.0, "open_interest_change_24h_pct": -10.44, "long_short_ratio": None}
PEERS = [{"symbol": s, "funding_rate_bps": 0.0, "open_interest_change_24h_pct": -10.44} for s in ("ETH", "WIF", "ONDO")]
ENVELOPE = {"schema_version", "tool", "status", "data_mode", "as_of", "request", "data", "summary", "availability", "warnings"}


def venues(down: frozenset[str] = frozenset()) -> httpx.Client:
    """down: hosts that answer 503."""
    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.host in down:
            return httpx.Response(503)
        p = req.url.path
        if p.endswith("/public/funding-rate"):
            return httpx.Response(200, json={"code": "0", "data": [{"premium": "-0.00023", "fundingRate": "0.0001", "interestRate": "0.0001"}]})
        if p.endswith("/open-interest-history"):
            rows = [["t", "0", "110", "0"]] + [["t", "0", "105", "0"]] * 23 + [["t", "0", "100", "0"]]
            return httpx.Response(200, json={"code": "0", "data": rows})
        if p.endswith("/long-short-account-ratio-contract"):
            return httpx.Response(200, json={"code": "0", "data": [["t", str(r)] for r in (1.35, 1.44, 1.47, 1.2)]})
        if req.url.host == "api.hyperliquid.xyz":
            return httpx.Response(200, json=[{"universe": [{"name": "SOL"}, {"name": "BTC"}]}, [{"premium": "-0.00018"}, {"premium": "0.0002"}]])
        if req.url.host == "www.deribit.com":
            return httpx.Response(200, json={"result": {"data": [[1_790_000_000_000, 50, 52, 49, 52.0]]}})
        return httpx.Response(404)
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_definition_has_ryos_skill_definition_shape():
    assert {"name", "description", "args", "requires_guard", "xp"} <= set(SKILL_DEFINITION)
    assert SKILL_DEFINITION["requires_guard"] is False and SKILL_DEFINITION["xp"] >= 0
    for a in SKILL_DEFINITION["args"]:
        assert {"name", "type", "description"} <= set(a) and a["type"] in ("string", "number", "boolean", "object", "array")
    json.dumps(SKILL_DEFINITION)


def test_invoke_returns_skill_call_response_with_the_ryo_envelope():
    out = invoke({"symbol": " sol ", "reference_derivatives": RYO_SOL, "peer_derivatives": PEERS}, http=venues())
    assert {"name", "status", "result", "latency_ms", "xp", "guard_decision"} <= set(out) and out["status"] == "success"
    env = out["result"]
    assert set(env) == ENVELOPE and env["status"] == "ok" and env["data_mode"] == "live"
    d = env["data"]
    assert d["okx"]["premium_bps"] == -2.3 and d["okx"]["oi_change_24h_pct_coin"] == 10.0 and d["premium_consensus"] == "below_spot_2_venues"
    assert {g["field"]: g["verdict"] for g in d["gate"]} == {
        "funding_rate_bps": "not_token_specific", "open_interest_change_24h_pct": "not_token_specific", "long_short_ratio": "absent"}
    json.dumps(out)


def test_null_stays_null_and_unmeasured_is_null_not_zero():
    env = invoke({"symbol": "SOL", "reference_derivatives": RYO_SOL}, http=venues())["result"]
    ls = next(g for g in env["data"]["gate"] if g["field"] == "long_short_ratio")
    assert ls["ryo_value"] is None and ls["verdict"] == "absent"
    assert env["data"]["implied_vol"] is None and env["availability"]["deribit_dvol"] == "unavailable"  # no DVOL for SOL, nothing estimated
    assert env["status"] == "ok"  # DVOL is context, not a primary section


def test_a_down_venue_is_partial_and_its_fields_are_null():
    env = invoke({"symbol": "SOL"}, http=venues(frozenset({"api.hyperliquid.xyz"})))["result"]
    assert env["status"] == "partial" and env["availability"]["hyperliquid_premium"] == "unavailable"
    assert env["data"]["hyperliquid"]["premium_bps"] is None and any("HTTP 503" in w for w in env["warnings"])
    assert {g["verdict"] for g in env["data"]["gate"]} == {"not_provided"}


def test_every_venue_down_is_unavailable_and_an_error_with_no_fabricated_number():
    out = invoke({"symbol": "BTC"}, http=venues(frozenset({"www.okx.com", "api.hyperliquid.xyz", "www.deribit.com"})))
    env = out["result"]
    assert out["status"] == "error" and env["status"] == "unavailable" and env["data_mode"] == "unknown"
    assert all(v is None for k, v in env["data"]["okx"].items() if k not in ("venue", "inst"))
    assert env["data"]["premium_consensus"] == "unavailable" and env["data"]["implied_vol"] is None
    assert env["data"]["plain"]["en"] == "No venue reported a premium."


def test_btc_dvol_checks_the_stop_and_fetch_reference_hook_feeds_the_gate():
    out = invoke({"symbol": "BTC", "atr_stop_pct": 2.0}, http=venues(), fetch_reference=lambda s: {"funding_rate_bps": 0.0})
    d = out["result"]["data"]
    assert d["implied_vol"]["implied_7d_move_pct"] == 7.2 and d["stop_check"]["inside_noise"] is True
    assert out["result"]["availability"]["ryo_reference"] == "available" and d["gate"][0]["verdict"] == "unverified"


@pytest.mark.parametrize("args", [{}, {"symbol": "../evil"}, {"symbol": "SOL", "atr_stop_pct": "2"}, {"symbol": "SOL", "x": 1},
                                  {"symbol": "SOL", "peer_derivatives": [{}] * 26}, None])
def test_bad_args_are_refused_before_any_request(args):
    with pytest.raises(ValueError):
        invoke(args, http=httpx.Client(transport=httpx.MockTransport(lambda r: pytest.fail(f"requested {r.url}"))))


def test_router_serves_ryos_two_skill_paths(monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from positioning_check import fastapi_router

    monkeypatch.setattr(fastapi_router, "invoke", lambda args: invoke(args, http=venues()))
    app = FastAPI()
    app.include_router(fastapi_router.router)
    c = TestClient(app)
    assert c.get("/api/skills/positioning_check").json()["name"] == "positioning_check"
    r = c.post("/api/skills/positioning_check/invoke", json={"name": "positioning_check", "args": {"symbol": "SOL"}, "conversation_id": None})
    assert r.status_code == 200 and r.json()["result"]["tool"] == "positioning_check"
    assert c.post("/api/skills/positioning_check/invoke", json={"args": {"symbol": "no way"}}).status_code == 422
