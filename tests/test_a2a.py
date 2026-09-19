"""A2A 1.0: the Agent Card describes the real skills, and SendMessage runs one through `invoke`."""

import httpx
import respx
from fastapi.testclient import TestClient
from httpx import Response

from nota import a2a, api
from nota.skills import SKILLS
from nota.skills.price_check import ExchangePrices

H = {"A2A-Version": "1.0"}


def _send(parts, id_=1):
    return {"jsonrpc": "2.0", "id": id_, "method": "SendMessage",
            "params": {"message": {"messageId": "m1", "role": "ROLE_USER", "parts": parts}}}


def test_agent_card_has_the_required_fields_and_one_skill_per_nota_skill():
    card = TestClient(api.app).get("/.well-known/agent-card.json").json()
    # required by AgentCard in specification/a2a.proto
    for k in ("name", "description", "supportedInterfaces", "version", "capabilities", "defaultInputModes",
              "defaultOutputModes", "skills"):
        assert card[k], k
    iface = card["supportedInterfaces"][0]
    assert iface == {"url": "http://testserver/a2a", "protocolBinding": "JSONRPC", "protocolVersion": "1.0"}
    assert card["capabilities"]["streaming"] is False and card["capabilities"]["pushNotifications"] is False
    assert [s["id"] for s in card["skills"]] == list(SKILLS)
    for s in card["skills"]:
        assert s["name"] and s["description"] and s["tags"] and s["examples"]   # AgentSkill required fields


def _prices():
    respx.get("https://data-api.binance.vision/api/v3/ticker/price").mock(return_value=Response(200, json={"symbol": "SOLUSDT", "price": "151.0"}))
    respx.get("https://coins.llama.fi/prices/current/coingecko:solana").mock(return_value=Response(502))
    respx.get("https://api.coingecko.com/api/v3/simple/price").mock(return_value=Response(200, json={"solana": {"usd": 150.0, "last_updated_at": 1788678000}}))
    respx.get("https://api.coinbase.com/v2/prices/SOL-USD/spot").mock(return_value=Response(200, json={"data": {"amount": "151.0", "base": "SOL", "currency": "USD"}}))
    respx.get("https://api.alternative.me/fng/").mock(return_value=Response(200, json={"data": [{"value": "73", "value_classification": "Greed", "timestamp": "1788652800"}]}))
    respx.get("https://api.kraken.com/0/public/Ticker").mock(return_value=Response(200, json={"error": [], "result": {"SOLUSD": {"c": ["152.0", "1"]}}}))


@respx.mock
def test_send_message_runs_the_skill_and_returns_a_completed_task_with_the_envelope():
    _prices()
    deps = {"exchanges": ExchangePrices(http=httpx.Client())}
    out = a2a.handle(_send([{"data": {"skill": "price_crosscheck", "args": {"symbol": "sol"}}}]), deps)
    task = out["result"]["task"]
    assert task["status"]["state"] == "TASK_STATE_COMPLETED" and task["id"] and task["contextId"]
    parts = task["artifacts"][0]["parts"]
    env = parts[0]["data"]
    assert env["tool"] == "price_crosscheck" and env["data"]["median_usd"] == 151.0 and env["request"]["symbol"] == "SOL"
    assert parts[1]["text"] and "kind" not in parts[0]            # 1.0 parts carry no `kind`
    # the same call as text: `<skill> <SYMBOL>`
    again = a2a.handle(_send([{"text": "price_crosscheck SOL"}]), deps)["result"]["task"]
    assert again["artifacts"][0]["parts"][0]["data"]["data"]["median_usd"] == 151.0


def test_bad_calls_are_a2a_errors_not_tasks():
    bad_args = a2a.handle(_send([{"data": {"skill": "price_crosscheck", "args": {"symbol": "../x"}}}]), {})
    assert bad_args["error"]["code"] == -32602
    assert bad_args["error"]["data"][0]["@type"].endswith("google.rpc.BadRequest")
    unknown = a2a.handle(_send([{"data": {"skill": "nope", "args": {}}}]), {})
    assert unknown["error"]["code"] == -32602 and "nope" in unknown["error"]["message"]
    missing = a2a.handle(_send([{"data": {"skill": "price_crosscheck", "args": {}}}]), {})
    assert missing["error"]["code"] == -32602 and "symbol" in missing["error"]["message"]
    assert a2a.handle(_send([{"url": "https://x/y.pdf"}]), {})["error"]["code"] == -32005
    assert a2a.handle({"jsonrpc": "2.0", "id": 3, "method": "GetTask", "params": {"id": "t"}})["error"]["code"] == -32001
    assert a2a.handle({"jsonrpc": "2.0", "id": 4, "method": "SendStreamingMessage"})["error"]["code"] == -32004
    assert a2a.handle({"jsonrpc": "2.0", "id": 5, "method": "message/send"})["error"]["code"] == -32601
    assert a2a.handle({"jsonrpc": "2.0", "method": "SendMessage"}) is None     # a notification gets no reply


def test_the_endpoint_checks_version_origin_and_json(tmp_path, monkeypatch):
    monkeypatch.setenv("NOTA_DB", str(tmp_path / "t.db"))
    c = TestClient(api.app)
    ok = c.post("/a2a", json=_send([{"data": {"skill": "verdict_track_record", "args": {}}}]), headers=H)
    assert ok.status_code == 200 and ok.json()["result"]["task"]["status"]["state"] == "TASK_STATE_COMPLETED"
    # no header means 0.3 (spec 3.6.2), which this agent does not speak
    assert c.post("/a2a", json=_send([{"text": "price_crosscheck SOL"}])).json()["error"]["code"] == -32009
    assert c.post("/a2a?A2A-Version=1.0", json={"jsonrpc": "2.0", "id": 1, "method": "CancelTask"}).json()["error"]["code"] == -32004
    assert c.post("/a2a", content=b"{nope", headers=H).json()["error"]["code"] == -32700
    assert c.post("/a2a", json=_send([]), headers={**H, "Origin": "https://evil.example"}).status_code == 403
