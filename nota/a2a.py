"""Nota as an A2A agent (Agent2Agent protocol 1.0, JSON-RPC binding).

MCP lets a model call Nota's skills as tools; A2A lets another *agent* discover Nota from its Agent
Card at `/.well-known/agent-card.json` and delegate a skill to it with `SendMessage`. Both routes
end in the same `nota.skills.invoke`, so the argument checks and the envelope are identical.

Shapes follow the normative `specification/a2a.proto` in its ProtoJSON form: camelCase fields,
enums as their SCREAMING_SNAKE names (`ROLE_USER`, `TASK_STATE_COMPLETED`), a Part is one of
`text` / `data` / `url` / `raw` with no `kind` field, and `SendMessage` answers `{"task": Task}`.

Every skill finishes inside the request, so the task is returned already terminal (COMPLETED, or
FAILED when a source raised). Nothing is queued, so there is no task store: `GetTask` answers
TaskNotFound and streaming, cancel and push configs answer UnsupportedOperation, which the card
declares up front (streaming and pushNotifications false).
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Any

from nota.mcp_server import SERVER_VERSION
from nota.skills import SKILLS, definitions, invoke, live_deps

PROTOCOL_VERSION = "1.0"
# JSON-RPC codes, spec section 5.4 / 9.5
INVALID_REQUEST, METHOD_NOT_FOUND, INVALID_PARAMS, INTERNAL_ERROR = -32600, -32601, -32602, -32603
TASK_NOT_FOUND, UNSUPPORTED_OPERATION, CONTENT_TYPE_NOT_SUPPORTED, VERSION_NOT_SUPPORTED = -32001, -32004, -32005, -32009
UNSUPPORTED_METHODS = {"SendStreamingMessage", "CancelTask", "SubscribeToTask", "CreateTaskPushNotificationConfig",
                       "GetTaskPushNotificationConfig", "ListTaskPushNotificationConfigs",
                       "DeleteTaskPushNotificationConfig", "GetExtendedAgentCard"}
log = logging.getLogger("nota.a2a")

# One realistic call per skill, for the card's `examples`: the values a first caller would try.
EXAMPLE_ARGS: dict[str, dict[str, Any]] = {
    "narrative_convergence": {"voices": ["tg:WatcherGuru", "bs:cointelegraph.com"]},
    "news_verify": {"claim": "Solana ETF approved by the SEC"},
    "price_crosscheck": {"symbol": "SOL"},
    "technicals_crosscheck": {"symbol": "BTC"},
    "positioning_check": {"symbol": "ETH"},
    "move_base_rate": {"symbol": "SOL"},
    "verdict_track_record": {"horizon_hours": 24},
}
TAGS = {"narrative_convergence": ["social", "sentiment"], "news_verify": ["news", "fact-check"],
        "price_crosscheck": ["price", "cross-check"], "technicals_crosscheck": ["rsi", "atr", "cross-check"],
        "positioning_check": ["derivatives", "gate"], "move_base_rate": ["base-rate", "statistics"],
        "verdict_track_record": ["scorecard", "track-record"]}


def agent_card(base: str) -> dict[str, Any]:
    skills = [{"id": d.name, "name": d.name.replace("_", " ").capitalize(), "description": d.description,
               "tags": ["crypto", "research", "read-only", *TAGS.get(d.name, [])],
               "examples": [json.dumps({"skill": d.name, "args": EXAMPLE_ARGS.get(d.name, {})})],
               "inputModes": ["application/json", "text/plain"], "outputModes": ["application/json", "text/plain"]}
              for d in definitions()]
    return {
        "name": "Nota",
        "description": ("Read-only crypto research skills that cross-check RYO's market evidence against independent "
                        "exchanges, candles, derivatives venues and news, each answered in RYO's envelope "
                        "(status, data_mode, as_of, availability, warnings). No orders, no wallets."),
        "supportedInterfaces": [{"url": f"{base}/a2a", "protocolBinding": "JSONRPC", "protocolVersion": PROTOCOL_VERSION}],
        "provider": {"organization": "Nota (RYO-CHAN Hackathon 2026)", "url": "https://github.com/PugarHuda/nota"},
        "version": SERVER_VERSION,
        "documentationUrl": f"{base}/llms.txt",
        "iconUrl": f"{base}/img/card.png",
        "capabilities": {"streaming": False, "pushNotifications": False, "extendedAgentCard": False},
        "defaultInputModes": ["application/json", "text/plain"],
        "defaultOutputModes": ["application/json", "text/plain"],
        "skills": skills,
    }


class A2AError(Exception):
    def __init__(self, code: int, message: str, field: str | None = None):
        super().__init__(message)
        self.code, self.message, self.field = code, message, field

    def body(self) -> dict[str, Any]:
        # error.data is a list of typed details (spec 9.5); a bad field is a google.rpc.BadRequest
        detail: dict[str, Any] = ({"@type": "type.googleapis.com/google.rpc.BadRequest",
                                   "fieldViolations": [{"field": self.field, "description": self.message}]}
                                  if self.field else
                                  {"@type": "type.googleapis.com/google.rpc.ErrorInfo", "domain": "a2a-protocol.org",
                                   "reason": {TASK_NOT_FOUND: "TASK_NOT_FOUND", UNSUPPORTED_OPERATION: "UNSUPPORTED_OPERATION",
                                              CONTENT_TYPE_NOT_SUPPORTED: "CONTENT_TYPE_NOT_SUPPORTED",
                                              VERSION_NOT_SUPPORTED: "VERSION_NOT_SUPPORTED"}.get(self.code, "INVALID")})
        return {"code": self.code, "message": self.message, "data": [detail]}


def call_from(message: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """The skill and its arguments: a data part `{skill, args}` wins; else a text part that names a skill,
    either as that same JSON or as `<skill> <SYMBOL>` for the skills whose only required argument is a symbol."""
    parts = message.get("parts")
    if not isinstance(parts, list) or not parts or not all(isinstance(p, dict) for p in parts):
        raise A2AError(INVALID_PARAMS, "message.parts must be a non-empty list of parts", "message.parts")
    for i, p in enumerate(parts):
        if isinstance(p.get("data"), dict):
            d = p["data"]
            if not isinstance(d.get("skill"), str):
                raise A2AError(INVALID_PARAMS, "a data part must carry {skill, args}", f"message.parts[{i}].data.skill")
            args = d.get("args", {})
            if not isinstance(args, dict):
                raise A2AError(INVALID_PARAMS, "args must be an object", f"message.parts[{i}].data.args")
            return d["skill"], args
    for i, p in enumerate(parts):
        text = p.get("text")
        if not isinstance(text, str):
            continue
        try:
            d = json.loads(text)
        except ValueError:
            d = None
        if isinstance(d, dict) and isinstance(d.get("skill"), str):
            return d["skill"], d.get("args") if isinstance(d.get("args"), dict) else {}
        words = text.replace(",", " ").split()
        named = [w for w in words if w in SKILLS]
        if not named:
            raise A2AError(INVALID_PARAMS, f"no skill named in the text; known: {sorted(SKILLS)}", f"message.parts[{i}].text")
        name = named[0]
        rest = [w for w in words if w != name]
        declared = {a.name: a for a in SKILLS[name][0].args}
        if not rest:
            return name, {}
        # ponytail: free text maps only onto a lone symbol; any other argument needs a data part. Words that
        # cannot be placed are refused rather than dropped: "verdict_track_record ETH" must not quietly
        # answer for every token.
        if "symbol" in declared and len(rest) == 1 and all(a.name == "symbol" or not a.required for a in declared.values()):
            return name, {"symbol": rest[0]}
        raise A2AError(INVALID_PARAMS, f"could not map {' '.join(rest)!r} onto {name}'s arguments; send a data part "
                       f"{{\"skill\": \"{name}\", \"args\": {{...}}}}", f"message.parts[{i}].text")
    raise A2AError(CONTENT_TYPE_NOT_SUPPORTED, "only text and data parts are accepted", "message.parts")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def send_message(params: Any, deps: dict[str, Any] | None = None) -> dict[str, Any]:
    if not isinstance(params, dict) or not isinstance(params.get("message"), dict):
        raise A2AError(INVALID_PARAMS, "params.message is required", "message")
    message = params["message"]
    if not isinstance(message.get("messageId"), str) or message.get("role") != "ROLE_USER":
        raise A2AError(INVALID_PARAMS, "message needs a messageId and role ROLE_USER", "message.role")
    name, args = call_from(message)
    if name not in SKILLS:
        raise A2AError(INVALID_PARAMS, f"unknown skill {name!r}; known: {sorted(SKILLS)}", "skill")
    task_id = str(uuid.uuid4())
    context_id = message.get("contextId") if isinstance(message.get("contextId"), str) else str(uuid.uuid4())
    extra = deps if deps is not None else live_deps(name, args)
    try:
        env = invoke(name, args, **extra)
    except ValueError as exc:                     # the same checks REST and MCP apply
        raise A2AError(INVALID_PARAMS, str(exc), "args") from None
    except Exception:                             # a source crashed: a failed task, not a transport error
        log.exception("a2a skill %s failed", name)
        status = {"state": "TASK_STATE_FAILED", "timestamp": _now(),
                  "message": {"messageId": str(uuid.uuid4()), "role": "ROLE_AGENT", "taskId": task_id, "contextId": context_id,
                              "parts": [{"text": f"source failure while running {name}"}]}}
        return {"task": {"id": task_id, "contextId": context_id, "status": status, "history": [message]}}
    body = env.model_dump(mode="json")
    headline = env.summary.headline or f"{name}: {env.status}"
    artifact = {"artifactId": str(uuid.uuid4()), "name": name, "description": f"{name} envelope, status {env.status}",
                "parts": [{"data": body, "mediaType": "application/json"}, {"text": headline, "mediaType": "text/plain"}]}
    return {"task": {"id": task_id, "contextId": context_id, "artifacts": [artifact], "history": [message],
                     "status": {"state": "TASK_STATE_COMPLETED", "timestamp": _now()},
                     "metadata": {"skill": name, "envelopeStatus": env.status}}}


def handle(request: dict[str, Any], deps: dict[str, Any] | None = None) -> dict[str, Any] | None:
    """One JSON-RPC request in, one response out (None for a notification)."""
    rid = request.get("id")
    try:
        if request.get("jsonrpc") != "2.0" or not isinstance(request.get("method"), str):
            raise A2AError(INVALID_REQUEST, "Request payload validation error")
        method = request["method"]
        if method == "SendMessage":
            result = send_message(request.get("params"), deps)
        elif method == "GetTask":
            # tasks finish inside SendMessage and are not kept, so none can be looked up later
            raise A2AError(TASK_NOT_FOUND, "Task not found: tasks complete within SendMessage and are not stored")
        elif method == "ListTasks":
            result = {"tasks": [], "nextPageToken": "", "pageSize": 0, "totalSize": 0}
        elif method in UNSUPPORTED_METHODS:
            raise A2AError(UNSUPPORTED_OPERATION, f"{method} is not supported: every task completes synchronously")
        else:
            raise A2AError(METHOD_NOT_FOUND, "Method not found")
    except A2AError as exc:
        return None if "id" not in request else {"jsonrpc": "2.0", "id": rid, "error": exc.body()}
    return None if "id" not in request else {"jsonrpc": "2.0", "id": rid, "result": result}
