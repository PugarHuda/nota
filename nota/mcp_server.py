"""Nota as an MCP server: the four skills, callable by any MCP client over Streamable HTTP.

Nota is already an MCP *client* of RYO. This is the other direction, and it is the point of Track 3:
RYO, Claude Desktop, Cursor or any other MCP client can point at `https://<host>/mcp` and call
`price_crosscheck` or `technicals_crosscheck` directly, with no wrapper and no port.

Implements the Streamable HTTP transport (spec 2025-03-26, revision 2026-07-28 negotiated when a
client asks for it): one endpoint that takes POST, validates `Origin`, answers a batch or a single
JSON-RPC message, returns `202 Accepted` with no body when the input carries only notifications or
responses, and answers requests as `application/json`. The server is stateless, so it issues no
`Mcp-Session-Id` and GET and DELETE are answered with 405 as the spec allows.
"""

from __future__ import annotations

import json
from typing import Any

from nota.ledger import Ledger
from nota.receipt import Receipt, render_markdown
from nota.skills import SKILLS, definitions, invoke

SERVER_NAME = "nota"
SERVER_VERSION = "0.1.0"
DEFAULT_PROTOCOL = "2025-03-26"
SUPPORTED_PROTOCOLS = ("2026-07-28", "2025-06-18", "2025-03-26", "2024-11-05")

# JSON-RPC error codes from the spec
PARSE_ERROR, INVALID_REQUEST, METHOD_NOT_FOUND, INVALID_PARAMS, INTERNAL_ERROR = -32700, -32600, -32601, -32602, -32603

_JSON_TYPE = {"string": "string", "integer": "integer", "number": "number",
              "boolean": "boolean", "array": "array", "object": "object"}


def input_schema(definition) -> dict[str, Any]:
    """The skill's own argument list as a JSON Schema, so a client can build the call itself."""
    props: dict[str, Any] = {}
    for a in definition.args:
        prop: dict[str, Any] = {"type": _JSON_TYPE.get(a.type, "string"), "description": a.description}
        if a.enum:
            prop["enum"] = list(a.enum)
        if a.type == "array":
            prop["items"] = {"type": _JSON_TYPE.get((a.items or {}).get("type", "string"), "string")}
        props[a.name] = prop
    return {"type": "object", "properties": props,
            "required": [a.name for a in definition.args if a.required],
            "additionalProperties": False}


def tool_list() -> list[dict[str, Any]]:
    return [{"name": d.name, "description": d.description, "inputSchema": input_schema(d)} for d in definitions()]


RESOURCE_SCHEME = "nota://receipt/"
RESOURCE_TEMPLATE = "nota://receipt/{id}"
RESOURCE_NOT_FOUND = -32002          # the spec's own code for an unknown resource


def _ledger() -> Ledger:
    import os

    return Ledger(os.environ.get("NOTA_DB", "nota.db"))


def resource_list(limit: int = 100) -> list[dict[str, Any]]:
    """Every receipt in the ledger, addressable. A client can read one without knowing this API."""
    out = []
    for d in _ledger().list_decisions(limit=limit):
        out.append({"uri": f"{RESOURCE_SCHEME}{d['id']}",
                    "name": f"{d['symbol']} receipt {d['id']}",
                    "description": f"Decision receipt for {d['symbol']} recorded {d['created_at']} "
                                   f"by {d['model']}. Replayable from the stored evidence.",
                    "mimeType": "text/markdown"})
    return out


def resource_read(uri: str) -> list[dict[str, Any]]:
    """Markdown for a human or an LLM, and the receipt's own JSON beside it for a parser."""
    if not uri.startswith(RESOURCE_SCHEME):
        raise KeyError(uri)
    raw = _ledger().get_decision(uri[len(RESOURCE_SCHEME):])
    if raw is None:
        raise KeyError(uri)
    receipt = Receipt.model_validate_json(raw)
    return [{"uri": uri, "mimeType": "text/markdown", "text": render_markdown(receipt)},
            {"uri": f"{uri}.json", "mimeType": "application/json", "text": raw}]


PROMPTS: dict[str, dict[str, Any]] = {
    "audit_a_token": {
        "description": "Cross-check what RYO says about one token against independent sources, and say "
                       "plainly where they disagree.",
        "arguments": [{"name": "symbol", "description": "Token symbol, for example SOL", "required": True}],
        "template": (
            "Audit {symbol} using the nota tools, and do not accept a single source.\n"
            "\n"
            "1. Call price_crosscheck for {symbol}. Note the exchange median, how many sources answered, "
            "and any deviation it reports against a reference price.\n"
            "2. Call technicals_crosscheck for {symbol}. It recomputes RSI(14) and ATR(14) with Wilder's "
            "method from public candles.\n"
            "3. Put the two results side by side, in the units each one uses.\n"
            "\n"
            "Rules for your answer: every figure you give must come from a tool result you actually "
            "received, and you must name which one. A field that came back null stays null, and you never "
            "write zero in its place. If a source reported unavailable, say so and say what that removes "
            "from the conclusion. Finish with one sentence a reader can act on, or say the evidence does "
            "not support one."),
    },
    "read_a_receipt": {
        "description": "Explain a stored Nota decision receipt: what was decided, from what evidence, and "
                       "whether it still reproduces.",
        "arguments": [{"name": "id", "description": "Receipt id, for example dbd7727f5a25", "required": True}],
        "template": (
            "Read the resource nota://receipt/{id} and explain it to someone who has not seen it.\n"
            "\n"
            "Cover, in this order: what the council decided and the probability the judge stated; which of "
            "the three agents disagreed and on what; which evidence sections were degraded and what that "
            "cost; whether a practice trade was sized or blocked, and from which ATR path; and how the "
            "receipt's own levels compare with RYO's published trade plan when it carries that comparison.\n"
            "\n"
            "Quote the dotted RYO paths the way the receipt does. Do not introduce a number that is not "
            "in it."),
    },
}


def prompt_list() -> list[dict[str, Any]]:
    return [{"name": name, "description": p["description"], "arguments": p["arguments"]}
            for name, p in PROMPTS.items()]


def prompt_get(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    prompt = PROMPTS[name]
    missing = [a["name"] for a in prompt["arguments"] if a.get("required") and not arguments.get(a["name"])]
    if missing:
        raise ValueError(f"{name}: missing required arguments {missing}")
    text = prompt["template"].format(**{a["name"]: arguments.get(a["name"], "") for a in prompt["arguments"]})
    return {"description": prompt["description"],
            "messages": [{"role": "user", "content": {"type": "text", "text": text}}]}


def complete(ref: dict[str, Any], argument: dict[str, Any]) -> dict[str, Any]:
    """Argument completion, so a client can offer the ids and symbols this deployment actually has
    rather than making the user guess them."""
    name, value = argument.get("name", ""), str(argument.get("value", "")).lower()
    values: list[str] = []
    if name == "id":
        values = [d["id"] for d in _ledger().list_decisions(limit=100)]
    elif name == "symbol":
        values = sorted({d["symbol"] for d in _ledger().list_decisions(limit=100)})
    values = [v for v in values if v.lower().startswith(value)][:100]
    return {"completion": {"values": values, "total": len(values), "hasMore": False}}


def _error(id_: Any, code: int, message: str, data: Any = None) -> dict[str, Any]:
    err: dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        err["data"] = data
    return {"jsonrpc": "2.0", "id": id_, "error": err}


def handle(message: dict[str, Any], deps: dict[str, Any] | None = None) -> dict[str, Any] | None:
    """One JSON-RPC message in, one response out, or None when the message needs no response."""
    if message.get("jsonrpc") != "2.0" or not isinstance(message.get("method"), str):
        return _error(message.get("id"), INVALID_REQUEST, "not a JSON-RPC 2.0 request")
    method, id_, params = message["method"], message.get("id"), message.get("params") or {}
    is_notification = "id" not in message

    if method == "initialize":
        asked = params.get("protocolVersion")
        version = asked if asked in SUPPORTED_PROTOCOLS else DEFAULT_PROTOCOL
        return {"jsonrpc": "2.0", "id": id_, "result": {
            "protocolVersion": version,
            "capabilities": {"tools": {"listChanged": False},
                             "resources": {"listChanged": False, "subscribe": False},
                             "prompts": {"listChanged": False},
                             "completions": {}},
            "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
            "instructions": "Four read-only research skills, plus every decision receipt in the "
                            "ledger as a resource under nota://receipt/. Every skill result is RYO's "
                            "public envelope: status, data_mode, as_of, availability per source and "
                            "warnings. A value that could not be fetched stays null, never zero.",
        }}
    if method in ("notifications/initialized", "notifications/cancelled"):
        return None
    if method == "ping":
        return {"jsonrpc": "2.0", "id": id_, "result": {}}
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": id_, "result": {"tools": tool_list()}}
    if method == "resources/templates/list":
        return {"jsonrpc": "2.0", "id": id_, "result": {"resourceTemplates": [{
            "uriTemplate": RESOURCE_TEMPLATE,
            "name": "Decision receipt",
            "description": "One stored decision, as markdown plus the receipt's own JSON. Ids come from "
                           "resources/list, or from completion on the id argument.",
            "mimeType": "text/markdown"}]}}
    if method == "prompts/list":
        return {"jsonrpc": "2.0", "id": id_, "result": {"prompts": prompt_list()}}
    if method == "prompts/get":
        name = params.get("name")
        if name not in PROMPTS:
            return _error(id_, INVALID_PARAMS, f"unknown prompt {name!r}", {"prompts": sorted(PROMPTS)})
        try:
            return {"jsonrpc": "2.0", "id": id_, "result": prompt_get(name, params.get("arguments") or {})}
        except ValueError as exc:
            return _error(id_, INVALID_PARAMS, str(exc))
    if method == "completion/complete":
        return {"jsonrpc": "2.0", "id": id_,
                "result": complete(params.get("ref") or {}, params.get("argument") or {})}
    if method == "resources/list":
        return {"jsonrpc": "2.0", "id": id_, "result": {"resources": resource_list()}}
    if method == "resources/read":
        try:
            contents = resource_read(params.get("uri", ""))
        except KeyError:
            return _error(id_, RESOURCE_NOT_FOUND, f"no such resource {params.get('uri')!r}",
                          {"scheme": RESOURCE_SCHEME})
        return {"jsonrpc": "2.0", "id": id_, "result": {"contents": contents}}
    if method == "tools/call":
        name, args = params.get("name"), params.get("arguments") or {}
        if name not in SKILLS:
            return _error(id_, INVALID_PARAMS, f"unknown tool {name!r}", {"tools": sorted(SKILLS)})
        try:
            envelope = invoke(name, args, **(deps or {}))
        except ValueError as exc:                      # bad arguments: the caller can fix this
            return _error(id_, INVALID_PARAMS, str(exc))
        except Exception as exc:                       # a source failed in a way the skill could not absorb
            return {"jsonrpc": "2.0", "id": id_, "result": {
                "isError": True,
                "content": [{"type": "text", "text": f"{type(exc).__name__}: {exc}"}]}}
        payload = envelope.model_dump(mode="json")
        return {"jsonrpc": "2.0", "id": id_, "result": {
            # text for clients that only read text, structuredContent for those that parse
            "content": [{"type": "text", "text": json.dumps(payload, indent=1, default=str)}],
            "structuredContent": payload,
            "isError": envelope.status == "unavailable",
        }}
    if is_notification:
        return None
    return _error(id_, METHOD_NOT_FOUND, f"unknown method {method!r}")
