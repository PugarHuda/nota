"""Nota answering MCP over Streamable HTTP, checked against the transport spec's own requirements."""

import json

from fastapi.testclient import TestClient

from nota import api
from nota.mcp_server import SUPPORTED_PROTOCOLS, input_schema, tool_list
from nota.skills import definitions

client = TestClient(api.app)


def rpc(payload, **kw):
    return client.post("/mcp", json=payload, **kw)


def test_initialize_negotiates_the_version_the_client_asked_for():
    for asked in SUPPORTED_PROTOCOLS:
        r = rpc({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                 "params": {"protocolVersion": asked, "capabilities": {}}})
        assert r.status_code == 200 and r.json()["result"]["protocolVersion"] == asked
    unknown = rpc({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                   "params": {"protocolVersion": "1999-01-01", "capabilities": {}}}).json()
    assert unknown["result"]["protocolVersion"] in SUPPORTED_PROTOCOLS  # falls back, never echoes nonsense
    assert unknown["result"]["serverInfo"]["name"] == "nota"


def test_tools_list_mirrors_the_skill_registry_with_usable_schemas():
    tools = rpc({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}).json()["result"]["tools"]
    assert [t["name"] for t in tools] == [d.name for d in definitions()]
    for t, d in zip(tools, definitions()):
        schema = t["inputSchema"]
        assert schema["type"] == "object" and schema["additionalProperties"] is False
        assert set(schema["properties"]) == {a.name for a in d.args}
        assert schema["required"] == [a.name for a in d.args if a.required]
        for a in d.args:                                  # an array argument declares its item type
            if a.type == "array":
                assert schema["properties"][a.name]["items"]["type"]


def test_tools_call_returns_the_envelope_as_text_and_structured_content():
    r = rpc({"jsonrpc": "2.0", "id": 3, "method": "tools/call",
             "params": {"name": "price_crosscheck", "arguments": {"symbol": "SOL"}}})
    result = r.json()["result"]
    body = result["structuredContent"]
    assert body["tool"] == "price_crosscheck" and body["schema_version"] == "nota-skill-1"
    assert set(body) >= {"status", "data_mode", "as_of", "availability", "warnings", "summary"}
    assert json.loads(result["content"][0]["text"])["tool"] == "price_crosscheck"
    assert result["isError"] is (body["status"] == "unavailable")   # honest either way


def test_bad_calls_are_json_rpc_errors_not_crashes():
    unknown = rpc({"jsonrpc": "2.0", "id": 4, "method": "tools/call",
                   "params": {"name": "nope", "arguments": {}}}).json()
    assert unknown["error"]["code"] == -32602 and "nope" in unknown["error"]["message"]
    missing = rpc({"jsonrpc": "2.0", "id": 5, "method": "tools/call",
                   "params": {"name": "price_crosscheck", "arguments": {}}}).json()
    assert missing["error"]["code"] == -32602 and "symbol" in missing["error"]["message"]
    nomethod = rpc({"jsonrpc": "2.0", "id": 6, "method": "does/not/exist"}).json()
    assert nomethod["error"]["code"] == -32601
    assert rpc("not-a-message").status_code == 400
    assert client.post("/mcp", content=b"{oops", headers={"content-type": "application/json"}).status_code == 400


def test_transport_rules_notifications_batches_origin_and_methods():
    # a body of notifications only gets 202 with no content
    accepted = rpc({"jsonrpc": "2.0", "method": "notifications/initialized"})
    assert accepted.status_code == 202 and accepted.content == b""

    # a batch answers one response per request, and drops the notification
    batch = rpc([{"jsonrpc": "2.0", "id": 7, "method": "ping"},
                 {"jsonrpc": "2.0", "method": "notifications/initialized"},
                 {"jsonrpc": "2.0", "id": 8, "method": "tools/list"}]).json()
    assert [m["id"] for m in batch] == [7, 8]

    # stateless server: no stream to open, no session to delete
    assert client.get("/mcp").status_code == 405 and client.delete("/mcp").status_code == 405
    # DNS-rebinding guard from the spec's security section
    assert client.post("/mcp", json={"jsonrpc": "2.0", "id": 9, "method": "ping"},
                       headers={"origin": "https://evil.example"}).status_code == 403
    assert client.post("/mcp", json={"jsonrpc": "2.0", "id": 9, "method": "ping"},
                       headers={"origin": "https://nota-ryo.vercel.app"}).status_code == 200


def test_input_schema_is_json_schema_a_client_can_actually_build_a_call_from():
    schema = input_schema(next(d for d in definitions() if d.name == "narrative_convergence"))
    assert schema["properties"]["voices"] == {"type": "array", "items": {"type": "string"},
                                              "description": schema["properties"]["voices"]["description"]}
    assert "voices" in schema["required"] and "tokens" not in schema["required"]
    assert len(tool_list()) == len(definitions())
