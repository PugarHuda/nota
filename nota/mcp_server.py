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
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
            "instructions": "Four read-only research skills. Every result is RYO's public envelope: "
                            "status, data_mode, as_of, availability per source and warnings. A value "
                            "that could not be fetched stays null and is never replaced with zero.",
        }}
    if method in ("notifications/initialized", "notifications/cancelled"):
        return None
    if method == "ping":
        return {"jsonrpc": "2.0", "id": id_, "result": {}}
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": id_, "result": {"tools": tool_list()}}
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
