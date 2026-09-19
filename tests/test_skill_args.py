"""Every public entry point refuses malformed skill arguments before any source is called.

REST, MCP and the CLI all route through `nota.skills.invoke`, so one check there covers all three;
these tests hit each surface anyway, because the point is what a caller sees: 422, -32602, never 500.
respx.mock blocks the network, so an argument that slipped through would fail loudly, not fetch.
"""

import pytest
import respx
from fastapi.testclient import TestClient

from nota import api
from nota.skills import SKILLS, invoke
from nota.skills.base_rate import move_base_rate
from nota.skills.contract import clean_symbol

client = TestClient(api.app)
VALID = {"symbol": "SOL", "claim": "SOL ETF approved", "voices": ["tg:WatcherGuru"]}
WRONG = {"string": 5, "integer": True, "number": "abc", "array": "SOL", "object": "x", "boolean": "yes"}
CASES = [(name, a.name, WRONG[a.type]) for name, (d, _) in SKILLS.items() for a in d.args]


def _base(name: str) -> dict:
    return {a.name: VALID[a.name] for a in SKILLS[name][0].args if a.required}


def _rest(name: str, args: dict):
    return client.post(f"/api/skills/{name}/invoke", json={"args": args})


def _mcp(name: str, args: dict) -> dict:
    return client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                     "params": {"name": name, "arguments": args}}).json()


@respx.mock
@pytest.mark.parametrize("name,arg,bad", CASES, ids=[f"{n}.{a}" for n, a, _ in CASES])
def test_wrong_type_is_refused_on_every_surface(name, arg, bad):
    args = {**_base(name), arg: bad}
    with pytest.raises(ValueError, match=f"arg {arg} must be"):
        invoke(name, args)
    assert _rest(name, args).status_code == 422
    err = _mcp(name, args)["error"]
    assert err["code"] == -32602 and "Error:" not in err["message"] and "str" not in err["message"].split()


@respx.mock
@pytest.mark.parametrize("name,args", [
    ("move_base_rate", {"symbol": "SOL", "direction": "sideways"}),
    ("move_base_rate", {"symbol": "SOL", "event": "closee"}),
    ("verdict_track_record", {"horizon_hours": 48}),
    ("price_crosscheck", {"symbol": "../x"}),
    ("price_crosscheck", {"symbol": "BTC?x"}),
    ("price_crosscheck", {"symbol": ["SOL"]}),
    ("price_crosscheck", {"symbol": "   "}),
    ("narrative_convergence", {"voices": ["tg:a"], "tokens": ["SOL/../x"]}),
    ("narrative_convergence", {"voices": [f"tg:v{i}" for i in range(21)]}),
    ("news_verify", {"claim": "x" * 501}),
    ("move_base_rate", {"symbol": "SOL", "k": 0, "event": "touch"}),
    ("price_crosscheck", ["SOL"]),
])
def test_bad_values_are_422_never_500(name, args):
    assert _rest(name, args).status_code == 422
    assert _mcp(name, args)["error"]["code"] == -32602


def test_non_finite_numbers_are_refused():
    # Python's JSON parser accepts the non-standard NaN / Infinity tokens, so a raw body can carry them
    res = client.post("/api/skills/price_crosscheck/invoke", content=b'{"args": {"symbol": "SOL", "reference_price": Infinity}}',
                      headers={"content-type": "application/json"})
    assert res.status_code == 422
    with pytest.raises(ValueError, match="reference_price must be number"):
        invoke("price_crosscheck", {"symbol": "SOL", "reference_price": float("nan")})


def test_clean_symbol_normalises_and_refuses():
    assert clean_symbol(" sol ") == "SOL" and clean_symbol("1000pepe") == "1000PEPE"
    for bad in ("", "../x", "BTC?x", "A" * 16, None, 5):
        with pytest.raises(ValueError, match="1-15 letters/digits"):
            clean_symbol(bad)


def test_valid_args_are_normalised_before_the_skill_sees_them(monkeypatch):
    seen = {}
    definition, _ = SKILLS["price_crosscheck"]
    monkeypatch.setitem(SKILLS, "price_crosscheck", (definition, lambda **kw: seen.update(kw)))
    invoke("price_crosscheck", {"symbol": " sol ", "reference_price": 1, "reference_path": None})
    assert seen == {"symbol": "SOL", "reference_price": 1}  # null on an optional arg means the default


def test_base_rate_headline_follows_event_and_direction(monkeypatch):
    import nota.skills.base_rate as br

    days = [{"ts": 1_700_000_000_000 + i * 86_400_000, "high": 101.0 + i % 5, "low": 99.0 - i % 3, "close": 100.0 + i % 4}
            for i in range(200)]
    monkeypatch.setattr(br, "okx_daily", lambda *a, **k: days)
    assert "touch -1 ATR below within 3d" in move_base_rate("SOL", k=1, direction="down").summary.headline
    assert "close +1.5 ATR above after 2d" in move_base_rate("SOL", k=1.5, horizon_days=2, event="close").summary.headline
    assert "close higher after 3d" in move_base_rate("SOL", k=0, event="close").summary.headline
