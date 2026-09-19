"""Nota as an MCP server: the nine skills, callable by any MCP client over Streamable HTTP.

Nota is already an MCP *client* of RYO. This is the other direction, and it is the point of Track 3:
RYO, Claude Desktop, Cursor or any other MCP client can point at `https://<host>/mcp` and call
`price_crosscheck` or `technicals_crosscheck` directly, with no wrapper and no port.

Implements the Streamable HTTP transport (revisions 2024-11-05 through 2026-07-28, negotiated per
client): one endpoint that takes POST, validates `Origin` and `MCP-Protocol-Version`, answers
`server/discover` and a batch or a single JSON-RPC message, returns `202 Accepted` with no body when
the input carries only notifications or responses, and answers requests as `application/json`. The
server is stateless, so it issues no `Mcp-Session-Id` and GET and DELETE are answered with 405.

Every tool also links an MCP Apps view (`ui://nota/receipt`), so a host that renders apps shows the
envelope as a stamped card instead of a JSON dump.
"""

from __future__ import annotations

import base64
import binascii
import json
import logging
from pathlib import Path
from typing import Any

from nota.envelope import Envelope
from nota.ledger import Ledger
from nota.receipt import Receipt, render_markdown
from nota.skills import SKILLS, definitions, invoke, live_deps

SERVER_NAME = "nota"
SERVER_VERSION = "0.2.0"
# newest first: an unknown version asked for in `initialize` gets the newest, as the lifecycle spec says
SUPPORTED_PROTOCOLS = ("2026-07-28", "2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05")
VERSION_META = "io.modelcontextprotocol/protocolVersion"

# JSON-RPC error codes from the spec
PARSE_ERROR, INVALID_REQUEST, METHOD_NOT_FOUND, INVALID_PARAMS, INTERNAL_ERROR = -32700, -32600, -32601, -32602, -32603
UNSUPPORTED_VERSION, HEADER_MISMATCH = -32022, -32020   # 2026-07-28 schema: UnsupportedProtocolVersionError, HeaderMismatch
PAGE = 50
log = logging.getLogger("nota.mcp")

APP_URI = "ui://nota/receipt"
APP_MIME = "text/html;profile=mcp-app"
APP_HTML = (Path(__file__).parent / "static" / "mcp_app.html").read_text(encoding="utf-8")

_JSON_TYPE = {"string": "string", "integer": "integer", "number": "number",
              "boolean": "boolean", "array": "array", "object": "object"}

CAPABILITIES: dict[str, Any] = {
    "tools": {"listChanged": False},
    "resources": {"listChanged": False, "subscribe": False},
    "prompts": {"listChanged": False},
    "completions": {},
    "extensions": {"io.modelcontextprotocol/ui": {"mimeTypes": [APP_MIME]}},
}
INSTRUCTIONS = (f"{len(SKILLS)} read-only research skills, plus every decision receipt in the ledger as a "
                "resource under nota://receipt/. Every skill result is RYO's public envelope: status, "
                "data_mode, as_of, availability per source and warnings. A value that could not be fetched "
                "stays null, never zero.")
# computed once: every tool returns the same envelope, so every tool declares the same output schema
OUTPUT_SCHEMA = Envelope.model_json_schema()


def input_schema(definition) -> dict[str, Any]:
    """The skill's own argument list as a JSON Schema, so a client can build the call itself."""
    props: dict[str, Any] = {}
    for a in definition.args:
        prop: dict[str, Any] = {"type": _JSON_TYPE.get(a.type, "string"), "description": a.description}
        if a.enum:
            prop["enum"] = [int(x) for x in a.enum] if a.type == "integer" else list(a.enum)  # stored as text, typed on the wire
        if a.type == "array":
            prop["items"] = {"type": _JSON_TYPE.get((a.items or {}).get("type", "string"), "string")}
        props[a.name] = prop
    return {"type": "object", "properties": props,
            "required": [a.name for a in definition.args if a.required],
            "additionalProperties": False}


def tool_list() -> list[dict[str, Any]]:
    # every skill only reads; all but the ledger lookup reach third-party sources, and none promises
    # the same answer twice because markets move between calls
    return [{"name": d.name, "title": d.name.replace("_", " ").title(), "description": d.description,
             "inputSchema": input_schema(d), "outputSchema": OUTPUT_SCHEMA,
             "annotations": {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": False,
                             "openWorldHint": d.name != "verdict_track_record"},
             "_meta": {"ui": {"resourceUri": APP_URI}}} for d in definitions()]


def _page(items: list[Any], cursor: Any) -> tuple[list[Any], str | None]:
    """Opaque cursor = base64 of the offset. Anything else a client hands back is a bad param."""
    offset = 0
    if cursor is not None:
        try:
            offset = int(base64.b64decode(str(cursor), validate=True).decode())
        except (ValueError, binascii.Error):
            raise ValueError("invalid cursor") from None
        if offset < 0:
            raise ValueError("invalid cursor")
    end = offset + PAGE
    return items[offset:end], (base64.b64encode(str(end).encode()).decode() if end < len(items) else None)


RESOURCE_SCHEME = "nota://receipt/"
RESOURCE_TEMPLATE = "nota://receipt/{id}"
RESOURCE_NOT_FOUND = -32002          # the spec's own code for an unknown resource


def _ledger() -> Ledger:
    import os

    return Ledger(os.environ.get("NOTA_DB", "nota.db"))


def resource_list(limit: int = 200) -> list[dict[str, Any]]:
    """The app view, then every receipt in the ledger, addressable. A client can read one without
    knowing this API. ponytail: the ledger caps one listing at 200; page in SQL if it ever holds more."""
    out = [{"uri": APP_URI, "name": "Receipt card", "mimeType": APP_MIME,
            "description": "MCP Apps view that renders any Nota tool result as a stamped receipt card."}]
    for d in _ledger().list_decisions(limit=limit):
        out.append({"uri": f"{RESOURCE_SCHEME}{d['id']}",
                    "name": f"{d['symbol']} receipt {d['id']}",
                    "description": f"Decision receipt for {d['symbol']} recorded {d['created_at']} "
                                   f"by {d['model']}. Replayable from the stored evidence.",
                    "mimeType": "text/markdown"})
    return out


def resource_read(uri: str) -> list[dict[str, Any]]:
    """Markdown for a human or an LLM, and the receipt's own JSON beside it for a parser. The `.json`
    URI handed out beside the markdown reads back as the JSON alone."""
    if uri == APP_URI:   # self-contained: the view fetches nothing, so its sandbox may allow nothing
        return [{"uri": uri, "mimeType": APP_MIME, "text": APP_HTML,
                 "_meta": {"ui": {"csp": {"connectDomains": [], "resourceDomains": []}}}}]
    if not uri.startswith(RESOURCE_SCHEME):
        raise KeyError(uri)
    rid = uri[len(RESOURCE_SCHEME):]
    raw = _ledger().get_decision(rid.removesuffix(".json"))
    if raw is None:
        raise KeyError(uri)
    if rid.endswith(".json"):
        return [{"uri": uri, "mimeType": "application/json", "text": raw}]
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
    rather than making the user guess them. Only for an argument the referenced prompt or template
    actually declares."""
    kind = ref.get("type")
    if kind == "ref/prompt" and isinstance(ref.get("name"), str) and ref["name"] in PROMPTS:
        declared = {a["name"] for a in PROMPTS[ref["name"]]["arguments"]}
    elif kind == "ref/resource" and ref.get("uri") == RESOURCE_TEMPLATE:
        declared = {"id"}
    else:
        raise ValueError(f"unknown completion ref {ref.get('name') or ref.get('uri')!r}")
    name, value = argument.get("name", ""), str(argument.get("value", "")).lower()
    if name not in declared:
        raise ValueError(f"argument {name!r} is not declared by that ref")
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


def _ok(id_: Any, result: dict[str, Any]) -> dict[str, Any]:
    # 2026-07-28 makes resultType required on every result; earlier clients ignore the extra field
    return {"jsonrpc": "2.0", "id": id_, "result": {"resultType": "complete", **result}}


def _typed(params: dict[str, Any], key: str, kind: type, default: Any = None) -> Any:
    """params[key] if it has the right JSON type (absent or null gives `default`), else ValueError."""
    v = params.get(key)
    if v is None and default is not None:
        return default
    if not isinstance(v, kind):
        raise ValueError(f"params.{key} must be {'a string' if kind is str else 'an object'}")
    return v


def handle(message: dict[str, Any], deps: dict[str, Any] | None = None) -> dict[str, Any] | None:
    """One JSON-RPC message in, one response out, or None when the message needs no response."""
    if message.get("jsonrpc") != "2.0":
        return _error(message.get("id"), INVALID_REQUEST, "not a JSON-RPC 2.0 request")
    if "method" not in message and ("result" in message or "error" in message):
        return None                                   # a response; this server never asked anything
    if not isinstance(message.get("method"), str):
        return _error(message.get("id"), INVALID_REQUEST, "not a JSON-RPC 2.0 request")
    if "id" not in message:
        return None                                   # every notification: accepted, and no side effects
    method, id_ = message["method"], message["id"]
    params = message.get("params", {})
    if not isinstance(params, dict):
        return _error(id_, INVALID_PARAMS, "params must be an object")
    meta = params.get("_meta")
    if isinstance(meta, dict) and VERSION_META in meta and meta[VERSION_META] not in SUPPORTED_PROTOCOLS:
        return _error(id_, UNSUPPORTED_VERSION, "Unsupported protocol version",
                      {"supported": list(SUPPORTED_PROTOCOLS), "requested": meta[VERSION_META]})
    try:
        return _dispatch(method, id_, params, deps)
    except ValueError as exc:                         # a malformed param: the caller can fix this
        return _error(id_, INVALID_PARAMS, str(exc))


def _dispatch(method: str, id_: Any, params: dict[str, Any], deps: dict[str, Any] | None) -> dict[str, Any]:
    if method == "initialize":
        asked = params.get("protocolVersion")
        version = asked if asked in SUPPORTED_PROTOCOLS else SUPPORTED_PROTOCOLS[0]
        return _ok(id_, {"protocolVersion": version, "capabilities": CAPABILITIES,
                         "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
                         "instructions": INSTRUCTIONS})
    if method == "server/discover":
        return _ok(id_, {"supportedVersions": list(SUPPORTED_PROTOCOLS), "capabilities": CAPABILITIES,
                         "_meta": {"io.modelcontextprotocol/serverInfo": {"name": SERVER_NAME,
                                                                          "version": SERVER_VERSION}},
                         "instructions": INSTRUCTIONS})
    if method == "ping":
        return _ok(id_, {})
    if method == "tools/list":
        tools, nxt = _page(tool_list(), params.get("cursor"))
        return _ok(id_, {"tools": tools, **({"nextCursor": nxt} if nxt else {})})
    if method == "resources/templates/list":
        return _ok(id_, {"resourceTemplates": [{
            "uriTemplate": RESOURCE_TEMPLATE,
            "name": "Decision receipt",
            "description": "One stored decision, as markdown plus the receipt's own JSON. Ids come from "
                           "resources/list, or from completion on the id argument.",
            "mimeType": "text/markdown"}]})
    if method == "prompts/list":
        return _ok(id_, {"prompts": prompt_list()})
    if method == "prompts/get":
        name = _typed(params, "name", str)
        arguments = _typed(params, "arguments", dict, {})
        if name not in PROMPTS:
            return _error(id_, INVALID_PARAMS, f"unknown prompt {name!r}", {"prompts": sorted(PROMPTS)})
        return _ok(id_, prompt_get(name, arguments))
    if method == "completion/complete":
        return _ok(id_, complete(_typed(params, "ref", dict), _typed(params, "argument", dict)))
    if method == "resources/list":
        resources, nxt = _page(resource_list(), params.get("cursor"))
        return _ok(id_, {"resources": resources, **({"nextCursor": nxt} if nxt else {})})
    if method == "resources/read":
        uri = _typed(params, "uri", str)
        try:
            contents = resource_read(uri)
        except KeyError:
            return _error(id_, RESOURCE_NOT_FOUND, f"no such resource {uri!r}", {"scheme": RESOURCE_SCHEME})
        return _ok(id_, {"contents": contents})
    if method == "tools/call":
        name = _typed(params, "name", str)
        args = _typed(params, "arguments", dict, {})
        if name not in SKILLS:
            return _error(id_, INVALID_PARAMS, f"unknown tool {name!r}", {"tools": sorted(SKILLS)})
        try:
            # a caller-supplied deps map (tests, embedding) wins; otherwise the same live deps as REST
            envelope = invoke(name, args, **(deps if deps else live_deps(name, args)))
        except ValueError:                             # bad arguments: handle() answers -32602
            raise
        except Exception:                              # a source failed in a way the skill could not absorb
            log.exception("tools/call %s failed", name)  # the detail goes to the log, never to the caller
            return _ok(id_, {"isError": True, "content": [
                {"type": "text", "text": f"source failure while running {name}; see warnings"}]})
        payload = envelope.model_dump(mode="json")
        return _ok(id_, {
            # text for clients that only read text, structuredContent for those that parse
            "content": [{"type": "text", "text": json.dumps(payload, indent=1, default=str)}],
            "structuredContent": payload,
            "isError": envelope.status == "unavailable",
        })
    return _error(id_, METHOD_NOT_FOUND, f"unknown method {method!r}")
