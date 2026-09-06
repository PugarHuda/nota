import httpx
import respx
from fastapi.testclient import TestClient
from httpx import Response

from arena import api
from tests.test_api import _seed


def test_skill_catalog_follows_ryo_shape(tmp_path, monkeypatch):
    _seed(tmp_path, monkeypatch)
    c = TestClient(api.app)
    defs = c.get("/api/skills/").json()
    assert {d["name"] for d in defs} == {"narrative_convergence", "news_verify", "price_crosscheck", "technicals_crosscheck"}
    assert all(set(d) >= {"name", "description", "args", "requires_guard", "xp"} and d["requires_guard"] is False for d in defs)
    one = c.get("/api/skills/price_crosscheck").json()
    assert one["args"][0] == {"name": "symbol", "type": "string", "required": True, "description": "Token symbol, e.g. SOL", "enum": None, "items": None}
    assert c.get("/api/skills/nope").status_code == 404


@respx.mock
def test_invoke_returns_skill_call_response(tmp_path, monkeypatch):
    _seed(tmp_path, monkeypatch)
    api._BACKING_HITS.clear()
    respx.get("https://api.alternative.me/fng/").mock(return_value=Response(200, json={"data": [{"value": "50", "value_classification": "Neutral", "timestamp": "1788652800"}]}))
    respx.get("https://api.coingecko.com/api/v3/simple/price").mock(return_value=Response(200, json={"solana": {"usd": 100.0, "last_updated_at": 1788678000}}))
    respx.get("https://api.coinbase.com/v2/prices/SOL-USD/spot").mock(return_value=Response(200, json={"data": {"amount": "101"}}))
    respx.get("https://api.kraken.com/0/public/Ticker").mock(return_value=Response(200, json={"error": [], "result": {"SOLUSD": {"c": ["102", "1"]}}}))
    c = TestClient(api.app)
    res = c.post("/api/skills/price_crosscheck/invoke", json={"name": "price_crosscheck", "args": {"symbol": "sol", "reference_price": 150}})
    assert res.status_code == 200
    body = res.json()
    assert set(body) == {"name", "status", "result", "latency_ms", "xp", "guard_decision"} and body["status"] == "success" and body["guard_decision"] is None
    assert body["result"]["tool"] == "price_crosscheck" and body["result"]["data"]["median_usd"] == 101.0 and body["result"]["data"]["reference"]["deviation_pct"] > 40
    assert c.post("/api/skills/price_crosscheck/invoke", json={"args": {}}).status_code == 422  # missing required arg
    assert c.post("/api/skills/price_crosscheck/invoke", json={"args": {"symbol": "SOL", "bogus": 1}}).status_code == 422
    assert c.post("/api/skills/nope/invoke", json={"args": {}}).status_code == 404
    assert c.post("/api/skills/price_crosscheck/invoke", json={"name": "other", "args": {"symbol": "SOL"}}).status_code == 422


def test_positions_report_stopped_target_open(tmp_path, monkeypatch):
    from arena.api import positions

    first, second = _seed(tmp_path, monkeypatch)  # second: long from 162 (perturbed fixture), stop 150, target 180
    rows = positions()
    assert rows[0]["status"] == "open" and rows[0]["pnl_usd"] == 0.0
    import json

    from arena.ledger import Ledger

    led = Ledger(str(tmp_path / "t.db"))
    raw = json.loads(led.get_pack(second.pack_hash))
    raw["sections"]["deep_analysis"]["envelope"]["data"]["market"]["price_usd"] = 140.0  # below the 150 stop
    from arena.council import run_council
    from arena.evidence import EvidencePack
    from arena.receipt import build_receipt
    from arena.risk import size_trade
    from tests.test_decide_replay import make_llm

    pack = EvidencePack.model_validate(raw)
    led.save_pack(pack.pack_hash(), pack.symbol, pack.source, pack.model_dump_json())
    council = run_council(pack, make_llm(action="no_trade", p=0.5), led)
    third = build_receipt(pack, council, size_trade(council.verdict, pack))
    led.save_decision(third.id, third.pack_hash, third.symbol, third.model, third.model_dump_json())
    led.conn.execute("UPDATE decisions SET created_at='2030-01-01T00:00:00+00:00' WHERE id=?", (third.id,))  # strictly newest
    rows = positions()
    assert rows[0]["decision_id"] == second.id and rows[0]["status"] == "stopped" and rows[0]["latest_price"] == 140.0 and rows[0]["pnl_usd"] < 0
