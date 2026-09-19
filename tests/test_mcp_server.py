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


def test_receipts_are_readable_as_mcp_resources(tmp_path, monkeypatch):
    """A client that never touches this project's HTTP API can still list and read its receipts."""
    from tests.test_api import _seed

    first, second = _seed(tmp_path, monkeypatch)
    listed = rpc({"jsonrpc": "2.0", "id": 10, "method": "resources/list"}).json()["result"]["resources"]
    uris = [r["uri"] for r in listed]
    assert f"nota://receipt/{first.id}" in uris and f"nota://receipt/{second.id}" in uris
    receipts = [r for r in listed if r["uri"].startswith("nota://")]
    assert all(r["mimeType"] == "text/markdown" and r["name"] and r["description"] for r in receipts)

    read = rpc({"jsonrpc": "2.0", "id": 11, "method": "resources/read",
                "params": {"uri": f"nota://receipt/{second.id}"}}).json()["result"]["contents"]
    assert [c["mimeType"] for c in read] == ["text/markdown", "application/json"]
    assert read[0]["text"].startswith(f"# Decision receipt {second.id}")
    assert json.loads(read[1]["text"])["id"] == second.id          # the receipt's own JSON, unaltered

    missing = rpc({"jsonrpc": "2.0", "id": 12, "method": "resources/read",
                   "params": {"uri": "nota://receipt/nope"}}).json()
    assert missing["error"]["code"] == -32002
    off_scheme = rpc({"jsonrpc": "2.0", "id": 13, "method": "resources/read",
                      "params": {"uri": "file:///etc/passwd"}}).json()
    assert off_scheme["error"]["code"] == -32002       # only this scheme is served


def test_initialize_advertises_the_resources_capability():
    caps = rpc({"jsonrpc": "2.0", "id": 14, "method": "initialize",
                "params": {"protocolVersion": "2025-03-26", "capabilities": {}}}).json()["result"]["capabilities"]
    assert "tools" in caps and "resources" in caps


def test_tools_call_over_mcp_is_metered_like_the_rest_route(monkeypatch):
    """The endpoint is public and unauthenticated on purpose, so the expensive verb is capped;
    listing and initialising stay free because they touch nothing outside this process."""
    from nota.api import BACKING_LIMIT, _BACKING_HITS

    _BACKING_HITS.clear()
    for _ in range(70):                       # cheap verbs never consume the budget
        assert rpc({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}).status_code == 200
    assert not _BACKING_HITS

    for i in range(60):
        rpc({"jsonrpc": "2.0", "id": i, "method": "tools/call",
             "params": {"name": "price_crosscheck", "arguments": {}}})   # bad args, still metered
    assert rpc({"jsonrpc": "2.0", "id": 99, "method": "tools/call",
                "params": {"name": "price_crosscheck", "arguments": {}}}).status_code == 429
    _BACKING_HITS.clear()


def test_an_oversized_batch_is_refused_rather_than_executed():
    huge = [{"jsonrpc": "2.0", "id": i, "method": "ping"} for i in range(200)]
    r = rpc(huge)
    assert r.status_code == 400 and "exceeds the limit" in r.json()["error"]["message"]


def test_prompts_are_offered_rendered_and_argument_checked():
    listed = rpc({"jsonrpc": "2.0", "id": 20, "method": "prompts/list"}).json()["result"]["prompts"]
    assert [p["name"] for p in listed] == ["audit_a_token", "read_a_receipt"]
    assert all(p["description"] and p["arguments"] for p in listed)

    got = rpc({"jsonrpc": "2.0", "id": 21, "method": "prompts/get",
               "params": {"name": "audit_a_token", "arguments": {"symbol": "SOL"}}}).json()["result"]
    text = got["messages"][0]["content"]["text"]
    assert got["messages"][0]["role"] == "user" and "SOL" in text
    assert "{symbol}" not in text                       # rendered, not handed over as a template
    assert "price_crosscheck" in text and "technicals_crosscheck" in text
    assert "never write zero" in text                   # the honesty rule travels with the prompt

    missing = rpc({"jsonrpc": "2.0", "id": 22, "method": "prompts/get",
                   "params": {"name": "audit_a_token", "arguments": {}}}).json()
    assert missing["error"]["code"] == -32602 and "symbol" in missing["error"]["message"]
    unknown = rpc({"jsonrpc": "2.0", "id": 23, "method": "prompts/get",
                   "params": {"name": "nope"}}).json()
    assert unknown["error"]["code"] == -32602


def test_a_resource_template_is_published_for_the_receipt_uris():
    tpl = rpc({"jsonrpc": "2.0", "id": 24, "method": "resources/templates/list"}).json()["result"]["resourceTemplates"]
    assert tpl[0]["uriTemplate"] == "nota://receipt/{id}"
    assert tpl[0]["mimeType"] == "text/markdown" and tpl[0]["description"]


def test_completion_offers_only_what_this_deployment_actually_holds(tmp_path, monkeypatch):
    from tests.test_api import _seed

    first, second = _seed(tmp_path, monkeypatch)
    ids = rpc({"jsonrpc": "2.0", "id": 25, "method": "completion/complete",
               "params": {"ref": {"type": "ref/prompt", "name": "read_a_receipt"},
                          "argument": {"name": "id", "value": ""}}}).json()["result"]["completion"]
    assert set(ids["values"]) == {first.id, second.id} and ids["hasMore"] is False

    prefixed = rpc({"jsonrpc": "2.0", "id": 26, "method": "completion/complete",
                    "params": {"ref": {"type": "ref/prompt", "name": "read_a_receipt"},
                               "argument": {"name": "id", "value": second.id[:3]}}}).json()["result"]["completion"]
    assert prefixed["values"] == [second.id]

    symbols = rpc({"jsonrpc": "2.0", "id": 27, "method": "completion/complete",
                   "params": {"ref": {"type": "ref/prompt", "name": "audit_a_token"},
                              "argument": {"name": "symbol", "value": ""}}}).json()["result"]["completion"]
    assert symbols["values"] == ["SOL"]                 # what the seeded ledger holds, nothing invented


def test_initialize_advertises_all_four_primitives():
    caps = rpc({"jsonrpc": "2.0", "id": 28, "method": "initialize",
                "params": {"protocolVersion": "2026-07-28", "capabilities": {}}}).json()["result"]["capabilities"]
    assert set(caps) == {"tools", "resources", "prompts", "completions", "extensions"}
    assert caps["extensions"]["io.modelcontextprotocol/ui"]["mimeTypes"] == ["text/html;profile=mcp-app"]


# --- transport hardening and the 2025-06-18 .. 2026-07-28 rules ---------------------------------
import base64
import socket
import threading
import time
from pathlib import Path

import httpx
import pytest
import uvicorn

from nota import mcp_server
from nota.envelope import Envelope, parse_rest
from nota.skills import SKILLS


@pytest.mark.parametrize("method,params", [
    ("tools/call", {"name": ["price_crosscheck"], "arguments": {}}),
    ("tools/call", {"name": {"x": 1}, "arguments": {}}),
    ("tools/call", {"name": "price_crosscheck", "arguments": ["SOL"]}),
    ("resources/read", {"uri": 5}),
    ("resources/read", {"uri": None}),
    ("prompts/get", {"name": "audit_a_token", "arguments": ["SOL"]}),
    ("prompts/get", {"name": 7}),
    ("completion/complete", {"ref": {"type": "ref/prompt", "name": "audit_a_token"}, "argument": "symbol"}),
    ("completion/complete", {"ref": "ref/prompt", "argument": {"name": "symbol"}}),
    ("initialize", ["2025-06-18"]),
    ("initialize", "2025-06-18"),
    ("tools/list", {"cursor": "not base64!"}),
])
def test_malformed_params_are_invalid_params_not_500(method, params):
    r = rpc({"jsonrpc": "2.0", "id": 40, "method": method, "params": params})
    assert r.status_code == 200 and r.json()["error"]["code"] == -32602 and r.json()["id"] == 40


def test_a_deeply_nested_body_is_a_parse_error_not_a_crash():
    r = client.post("/mcp", content=b"[" * 100000, headers={"content-type": "application/json"})
    assert r.status_code == 400 and r.json()["error"]["code"] == -32700


def test_an_oversized_body_is_refused_before_it_is_parsed():
    r = client.post("/mcp", content=b" " * 2_000_000, headers={"content-type": "application/json"})
    assert r.status_code == 413 and r.json()["error"]["code"] == -32600


def test_requests_without_an_id_are_notifications_with_no_reply_and_no_charge():
    from nota.api import _BACKING_HITS

    for method in ("ping", "tools/list", "tools/call"):
        r = rpc({"jsonrpc": "2.0", "method": method, "params": {"name": "price_crosscheck",
                                                               "arguments": {"symbol": "SOL"}}})
        assert r.status_code == 202 and r.content == b""
    assert not _BACKING_HITS


def test_protocol_version_header_is_checked_against_what_the_server_speaks():
    ping = {"jsonrpc": "2.0", "id": 1, "method": "ping"}
    bad = rpc(ping, headers={"mcp-protocol-version": "1999-01-01"})
    assert bad.status_code == 400 and bad.json()["error"]["code"] == -32022
    assert bad.json()["error"]["data"]["supported"] == list(SUPPORTED_PROTOCOLS)
    assert rpc(ping, headers={"mcp-protocol-version": "2026-07-28"}).status_code == 200
    assert rpc(ping).status_code == 200                      # pre-2025-06-18 clients send none
    # the header must agree with the version the body claims
    meta = {**ping, "params": {"_meta": {"io.modelcontextprotocol/protocolVersion": "2025-11-25"}}}
    assert rpc(meta, headers={"mcp-protocol-version": "2026-07-28"}).json()["error"]["code"] == -32020
    unknown = {**ping, "params": {"_meta": {"io.modelcontextprotocol/protocolVersion": "1900-01-01"}}}
    r = rpc(unknown)
    assert r.status_code == 400 and r.json()["error"]["data"]["requested"] == "1900-01-01"


def test_initialize_echoes_a_supported_version_and_offers_the_newest_otherwise():
    def asked(v):
        return rpc({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                    "params": {"protocolVersion": v, "capabilities": {}}}).json()["result"]
    assert asked("2025-11-25")["protocolVersion"] == "2025-11-25"
    res = asked("1999-01-01")
    assert res["protocolVersion"] == "2026-07-28"
    count = int(res["instructions"].split()[0])              # "7 read-only research skills..."
    assert count == len(rpc({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}).json()["result"]["tools"])


def test_server_discover_has_the_2026_07_28_shape():
    res = rpc({"jsonrpc": "2.0", "id": "d", "method": "server/discover", "params": {"_meta": {
        "io.modelcontextprotocol/protocolVersion": "2026-07-28"}}}).json()["result"]
    assert res["resultType"] == "complete"
    assert res["supportedVersions"] == list(SUPPORTED_PROTOCOLS)
    assert {"tools", "resources", "prompts"} <= set(res["capabilities"])
    assert res["_meta"]["io.modelcontextprotocol/serverInfo"]["name"] == "nota"
    assert res["instructions"]


def test_an_unknown_method_is_404_with_its_json_rpc_body():
    r = rpc({"jsonrpc": "2.0", "id": 1, "method": "does/not/exist"})
    assert r.status_code == 404 and r.json()["error"]["code"] == -32601


def test_every_tool_is_read_only_titled_and_declares_its_output_and_view():
    tools = rpc({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}).json()["result"]["tools"]
    for t in tools:
        assert t["annotations"]["readOnlyHint"] is True and t["annotations"]["destructiveHint"] is False
        assert t["title"] and t["outputSchema"]["type"] == "object"
        assert t["_meta"]["ui"]["resourceUri"] == "ui://nota/receipt"
    assert {t["name"]: t["annotations"]["openWorldHint"] for t in tools}["verdict_track_record"] is False


def test_the_output_schema_is_the_envelope_every_recorded_ryo_answer_fits():
    assert tool_list()[0]["outputSchema"] == Envelope.model_json_schema()
    fixtures = list((Path(__file__).parent / "fixtures").glob("*/*.json"))
    assert fixtures
    for f in fixtures:
        env = parse_rest(json.loads(f.read_text(encoding="utf-8")))
        dumped = env.model_dump(mode="json")
        assert Envelope.model_validate(dumped).model_dump(mode="json") == dumped


def test_a_skill_that_blows_up_leaks_no_exception_detail(monkeypatch):
    definition, _ = SKILLS["verdict_track_record"]

    def boom(**_):
        raise RuntimeError("password=hunter2 at /srv/secret")
    monkeypatch.setitem(SKILLS, "verdict_track_record", (definition, boom))
    res = rpc({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
               "params": {"name": "verdict_track_record", "arguments": {}}}).json()["result"]
    text = res["content"][0]["text"]
    assert res["isError"] is True and "verdict_track_record" in text
    assert "hunter2" not in text and "RuntimeError" not in text


def test_completion_only_for_declared_refs_and_arguments():
    def comp(ref, name):
        return rpc({"jsonrpc": "2.0", "id": 1, "method": "completion/complete",
                    "params": {"ref": ref, "argument": {"name": name, "value": ""}}}).json()
    assert comp({"type": "ref/prompt", "name": "nope"}, "id")["error"]["code"] == -32602
    assert comp({"type": "ref/prompt", "name": "audit_a_token"}, "id")["error"]["code"] == -32602
    assert comp({"type": "ref/resource", "uri": "nota://other/{id}"}, "id")["error"]["code"] == -32602
    assert "completion" in comp({"type": "ref/resource", "uri": "nota://receipt/{id}"}, "id")["result"]


def test_the_json_uri_reads_back_as_the_json_alone(tmp_path, monkeypatch):
    from tests.test_api import _seed

    first, _ = _seed(tmp_path, monkeypatch)
    got = rpc({"jsonrpc": "2.0", "id": 1, "method": "resources/read",
               "params": {"uri": f"nota://receipt/{first.id}.json"}}).json()["result"]["contents"]
    assert [c["mimeType"] for c in got] == ["application/json"] and json.loads(got[0]["text"])["id"] == first.id


def test_the_app_view_is_listed_and_served_with_an_empty_csp():
    listed = rpc({"jsonrpc": "2.0", "id": 1, "method": "resources/list"}).json()["result"]["resources"]
    assert listed[0]["uri"] == "ui://nota/receipt" and listed[0]["mimeType"] == "text/html;profile=mcp-app"
    got = rpc({"jsonrpc": "2.0", "id": 2, "method": "resources/read",
               "params": {"uri": "ui://nota/receipt"}}).json()["result"]["contents"][0]
    assert got["text"].startswith("<!doctype html>") and "ui/initialize" in got["text"]
    assert got["_meta"]["ui"]["csp"] == {"connectDomains": [], "resourceDomains": []}
    assert "http" not in got["text"].split("<script>")[1]     # the view fetches nothing


def test_lists_page_with_an_opaque_cursor(monkeypatch):
    monkeypatch.setattr(mcp_server, "resource_list", lambda: [{"uri": f"nota://receipt/{i}"} for i in range(120)])
    seen, cursor = [], None
    while True:
        params = {"cursor": cursor} if cursor else {}
        res = rpc({"jsonrpc": "2.0", "id": 1, "method": "resources/list", "params": params}).json()["result"]
        seen += res["resources"]
        cursor = res.get("nextCursor")
        if not cursor:
            break
    assert len(seen) == 120 and len({r["uri"] for r in seen}) == 120
    neg = base64.b64encode(b"-5").decode()
    assert rpc({"jsonrpc": "2.0", "id": 1, "method": "resources/list",
                "params": {"cursor": neg}}).json()["error"]["code"] == -32602


def test_the_61st_call_is_a_json_rpc_429_with_retry_after_and_ledger_lookups_are_free():
    call = {"jsonrpc": "2.0", "method": "tools/call", "params": {"name": "price_crosscheck", "arguments": {}}}
    for i in range(60):
        assert rpc({**call, "id": i}).status_code == 200
    r = rpc({**call, "id": 61})
    assert r.status_code == 429 and r.json()["error"]["code"] == -32000
    assert int(r.headers["retry-after"]) == r.json()["error"]["data"]["retry_after_s"] > 0
    free = rpc({"jsonrpc": "2.0", "id": 62, "method": "tools/call",
                "params": {"name": "verdict_track_record", "arguments": {}}})
    assert free.status_code == 200 and "result" in free.json()


def test_a_batch_over_budget_is_refused_whole_and_charges_nothing():
    from nota.api import _BACKING_HITS

    call = {"jsonrpc": "2.0", "method": "tools/call", "params": {"name": "price_crosscheck", "arguments": {}}}
    for i in range(50):
        rpc({**call, "id": i})
    assert rpc([{**call, "id": 100 + i} for i in range(11)]).status_code == 429
    assert len(next(iter(_BACKING_HITS.values()))) == 50


def test_a_slow_tool_call_does_not_block_the_event_loop(monkeypatch):
    definition, _ = SKILLS["verdict_track_record"]

    def slow(**_):
        time.sleep(1)
        raise RuntimeError("slow source")
    monkeypatch.setitem(SKILLS, "verdict_track_record", (definition, slow))
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    srv = uvicorn.Server(uvicorn.Config(api.app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=srv.run, daemon=True)
    thread.start()
    try:
        for _ in range(120):
            if srv.started:
                break
            time.sleep(0.05)
        base = f"http://127.0.0.1:{port}"
        caller = threading.Thread(target=lambda: httpx.post(base + "/mcp", timeout=10, json={
            "jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": "verdict_track_record", "arguments": {}}}))
        caller.start()
        time.sleep(0.2)                                      # the call is now inside the slow skill
        started = time.perf_counter()
        assert httpx.get(base + "/api/skills/", timeout=10).status_code == 200
        assert time.perf_counter() - started < 0.5
        caller.join()
    finally:
        srv.should_exit = True
        thread.join(timeout=5)
