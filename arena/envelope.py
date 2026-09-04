"""RYO public response contract.

Every successful RYO tool call returns the same envelope (see docs/MCP-Builder-Guide.md,
"Public response contract"). We keep every field RYO sends (extra="allow") and never
invent a value for a field it did not send.
"""

from __future__ import annotations

import json
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

Status = Literal["ok", "partial", "unavailable"]
DataMode = Literal["live", "mixed", "simulated", "unknown"]


class RyoToolError(Exception):
    """The tool ran but reported failure (MCP `result.isError: true`)."""


class Summary(BaseModel):
    model_config = ConfigDict(extra="allow")
    headline: str = ""
    key_points: list[str] = Field(default_factory=list)


class Envelope(BaseModel):
    model_config = ConfigDict(extra="allow")

    schema_version: str | int | None = None
    tool: str
    status: Status
    data_mode: DataMode = "unknown"
    as_of: str | None = None
    request: dict[str, Any] = Field(default_factory=dict)
    data: dict[str, Any] = Field(default_factory=dict)
    summary: Summary = Field(default_factory=Summary)
    availability: dict[str, Any] = Field(default_factory=dict)
    warnings: list[str] = Field(default_factory=list)
    trace_id: str | None = None  # from the x-trace-id response header, not the body

    def get(self, path: str) -> Any | None:
        """Dotted lookup into `data`. Missing or null stays None; it is never coerced to 0."""
        node: Any = self.data
        for part in path.split("."):
            if isinstance(node, dict):
                node = node.get(part)
            elif isinstance(node, list) and part.isdigit() and int(part) < len(node):
                node = node[int(part)]
            else:
                return None
            if node is None:
                return None
        return node


def parse_rest(body: dict[str, Any], trace_id: str | None = None) -> Envelope:
    """REST `POST /api/mcp/tools/{tool}/call` returns `{"result": <envelope>}`."""
    payload = body.get("result", body)
    env = Envelope.model_validate(payload)
    if trace_id:
        env.trace_id = trace_id
    return env


def parse_mcp(body: dict[str, Any], trace_id: str | None = None) -> Envelope:
    """JSON-RPC `tools/call`: the envelope is a JSON string inside the first text content block."""
    if "error" in body:
        err = body["error"]
        raise RyoToolError(f"jsonrpc error {err.get('code')}: {err.get('message')}")
    result = body["result"]
    text = next((c.get("text") for c in result.get("content", []) if c.get("type") == "text"), None)
    if result.get("isError"):
        raise RyoToolError(text or "RYO tool failed")
    if text is None:
        raise RyoToolError("RYO returned no text content")
    env = Envelope.model_validate(json.loads(text))
    if trace_id:
        env.trace_id = trace_id
    return env
