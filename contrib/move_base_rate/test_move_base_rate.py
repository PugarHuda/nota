"""Offline: OKX is an httpx.MockTransport. Run with `pytest -q contrib`."""

import json

import httpx
import pytest

from move_base_rate.move_base_rate import SKILL_DEFINITION, base_rate, hit, invoke, wilder_atr_pct

DAY = 86_400_000
ENVELOPE = {"schema_version", "tool", "status", "data_mode", "as_of", "request", "data", "summary", "availability", "warnings"}


def candles(n=250, amp=0.02):
    """Close alternates 100 / 102; every high is 1% above and every low 1% below the day's close."""
    out = []
    for i in range(n):
        c = 100.0 * (1 + amp * (i % 2))
        out.append({"ts": 1_700_000_000_000 + i * DAY, "high": c * 1.01, "low": c * 0.99, "close": c})
    return out


def okx(daily=None, status=200, body=None) -> httpx.Client:
    """OKX history-candles, newest first, 100 per page, paged by `after`."""
    rows = [[str(c["ts"]), "0", str(c["high"]), str(c["low"]), str(c["close"]), "0", "0", "0", "1"] for c in reversed(daily or [])]

    def handler(req: httpx.Request) -> httpx.Response:
        assert req.url.path == "/api/v5/market/history-candles" and req.url.params["bar"] == "1Dutc"
        if status != 200 or body is not None:
            return httpx.Response(status, json=body or {})
        after = req.url.params.get("after")
        start = next(i for i, r in enumerate(rows) if r[0] == after) + 1 if after else 0
        return httpx.Response(200, json={"code": "0", "data": rows[start:start + 100]})
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_definition_has_ryos_skill_definition_shape():
    assert {"name", "description", "args", "requires_guard", "xp"} <= set(SKILL_DEFINITION)
    assert SKILL_DEFINITION["requires_guard"] is False and SKILL_DEFINITION["xp"] >= 0
    for a in SKILL_DEFINITION["args"]:
        assert {"name", "type", "description"} <= set(a) and a["type"] in ("string", "number", "integer", "boolean", "object", "array")
    json.dumps(SKILL_DEFINITION)


def test_hit_touch_and_close_up_and_down():
    d = candles(5)  # closes 100, 102, 100, 102, 100
    assert hit(d, 0, 2.0, 1, "up", "close") is False and hit(d, 0, 1.9, 1, "up", "close") is True
    assert hit(d, 0, 3.0, 1, "up", "touch") is True and hit(d, 1, 3.1, 2, "down", "touch") is False


def test_base_rate_conditions_on_todays_tercile():
    out = base_rate(candles(120), k=0.0, h=1, direction="up", event="close")
    # today closed at 102 (low tercile) and from 102 the next close is always 100: 0, not half
    assert out["p"] == 0.0 and out["tercile"] == "low" and out["n_days"] >= 30
    assert base_rate(candles(40), 1, 3, "up", "touch")["p"] is None  # too little history is said, not guessed
    assert wilder_atr_pct(candles(20))[:14] == [None] * 14


def test_invoke_returns_skill_call_response_and_scales_k_to_ryos_atr():
    d = candles()
    ryo_atr = round(wilder_atr_pct(d)[-1] * 0.9, 4)
    out = invoke({"symbol": " sol ", "k": 1, "horizon_days": 3, "atr_14_pct": ryo_atr}, http=okx(d))
    assert {"name", "status", "result", "latency_ms", "xp", "guard_decision"} <= set(out) and out["status"] == "success"
    env = out["result"]
    assert set(env) == ENVELOPE and env["status"] == "ok" and env["data_mode"] == "live"
    assert env["data"]["atr_scale"]["ryo_over_okx"] == 0.9 and env["data"]["k_in_okx_atr"] == 0.9
    assert env["data"]["history"][0] < env["data"]["history"][1] and env["data"]["n_days"] > 0  # all three pages read
    assert "days touch +1 ATR above within 3d" in env["summary"]["headline"]
    json.dumps(out)


@pytest.mark.parametrize("client, why", [(okx(status=503), "HTTP 503"), (okx(body={"code": "51001", "msg": "Instrument ID does not exist"}), "does not exist"),
                                         (okx([]), "no daily candles")])
def test_okx_failing_is_an_error_with_p_null_not_zero(client, why):
    out = invoke({"symbol": "NOPE"}, http=client)
    env = out["result"]
    assert out["status"] == "error" and env["status"] == "unavailable" and env["data_mode"] == "unknown"
    assert env["data"]["p"] is None and env["availability"] == {"okx_daily": "unavailable"} and any(why in w for w in env["warnings"])


def test_short_history_and_as_of_before_every_candle_are_partial():
    env = invoke({"symbol": "NEW"}, http=okx(candles(40)))["result"]
    assert env["status"] == "partial" and env["data"]["p"] is None and any("need 60" in w for w in env["warnings"])
    env = invoke({"symbol": "SOL", "as_of": "2020-01-01"}, http=okx(candles()))["result"]
    assert env["status"] == "partial" and env["data"]["p"] is None and any("2020-01-01" in w for w in env["warnings"])


@pytest.mark.parametrize("args", [{}, None, {"symbol": "../evil"}, {"symbol": "SOL", "x": 1}, {"symbol": "SOL", "k": "1"},
                                  {"symbol": "SOL", "k": -1}, {"symbol": "SOL", "horizon_days": 30}, {"symbol": "SOL", "horizon_days": 2.5},
                                  {"symbol": "SOL", "direction": "sideways"}, {"symbol": "SOL", "event": "cross"},
                                  {"symbol": "SOL", "atr_14_pct": 0}, {"symbol": "SOL", "as_of": "yesterday"},
                                  {"symbol": "SOL", "as_of": "2026-02-30"}, {"symbol": "SOL", "k": 0}])
def test_bad_args_are_refused_before_any_request(args):
    with pytest.raises(ValueError):
        invoke(args, http=httpx.Client(transport=httpx.MockTransport(lambda r: pytest.fail(f"requested {r.url}"))))


def test_router_serves_ryos_two_skill_paths(monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from move_base_rate import fastapi_router

    monkeypatch.setattr(fastapi_router, "invoke", lambda args: invoke(args, http=okx(candles())))
    app = FastAPI()
    app.include_router(fastapi_router.router)
    c = TestClient(app)
    assert c.get("/api/skills/move_base_rate").json()["name"] == "move_base_rate"
    r = c.post("/api/skills/move_base_rate/invoke", json={"name": "move_base_rate", "args": {"symbol": "SOL", "k": 0, "event": "close"}, "conversation_id": None})
    assert r.status_code == 200 and r.json()["result"]["tool"] == "move_base_rate"
    assert c.post("/api/skills/move_base_rate/invoke", json={"args": {"symbol": "SOL", "k": 0}}).status_code == 422
