"""RYO research-tool clients.

`RyoClient` talks to the live REST surface (`POST {RYO_MCP_URL}/tools/{tool}/call`).
`RecordedRyoClient` replays envelopes captured from the live server so the pipeline can be
run and judged without a builder key. Both satisfy `RyoSource`.
"""

from __future__ import annotations

import hashlib
import json
import os
import random
import re
import time
from pathlib import Path
from typing import Any, Callable, Protocol

import httpx

from nota.envelope import Envelope, parse_rest, parse_mcp, RyoToolError

DEFAULT_URL = "https://app-ryochan.com/api/mcp"
RETRYABLE = {429, 503}


class RyoError(Exception):
    def __init__(self, status_code: int, code: str, message: str, trace_id: str | None = None):
        super().__init__(f"RYO {status_code} {code}: {message} (trace {trace_id})")
        self.status_code, self.code, self.message, self.trace_id = status_code, code, message, trace_id


class RyoSource(Protocol):
    name: str

    def call(self, tool: str, args: dict[str, Any] | None = None) -> Envelope: ...


def fixture_name(tool: str, args: dict[str, Any] | None) -> str:
    """Stable file name for a (tool, args) pair: the symbol when present, else a short hash.

    ponytail: symbol wins even with extra args (include_perp), so one fixture per token.
    """
    args = {k: v for k, v in (args or {}).items() if k != "top_n"}  # top_n only trims a list; same recording serves any size
    if "symbol" in args:
        return str(args["symbol"]).upper()
    if "symbols" in args:  # compare_tokens: "SOL, BTC, ETH" -> SOL-BTC-ETH
        return "-".join(s.strip().upper() for s in re.split(r"[,\s]+", str(args["symbols"])) if s.strip())
    if set(args) <= {"chain", "theme"} and args:  # scan_market: bsc-news, any-news, bsc-any
        return f"{args.get('chain') or 'any'}-{args.get('theme') or 'any'}".lower()
    if not args:
        return "default"
    digest = hashlib.sha256(json.dumps(args, sort_keys=True).encode()).hexdigest()[:12]
    return digest


class RyoClient:
    name = "live"

    def __init__(
        self,
        base_url: str | None = None,
        key: str | None = None,
        http: httpx.Client | None = None,
        max_retries: int = 4,
        sleep: Callable[[float], None] = time.sleep,
        timeout: float = 60.0,
        transport: str | None = None,
    ):
        self.base_url = (base_url or os.environ.get("RYO_MCP_URL", DEFAULT_URL)).rstrip("/")
        self.transport = (transport or os.environ.get("RYO_TRANSPORT", "rest")).lower()  # rest | mcp
        if self.transport not in ("rest", "mcp"):
            raise ValueError("RYO_TRANSPORT must be rest or mcp")
        self._rpc_id = 0
        self.key = key or os.environ.get("RYO_MCP_KEY", "")
        self.http = http or httpx.Client(timeout=timeout)
        self.max_retries = max_retries
        self.sleep = sleep
        self.last_rate_limit: dict[str, str] = {}

    # -- unauthenticated / metadata -------------------------------------------------
    def health(self) -> dict[str, Any]:
        return self._request("GET", "/health", auth=False).json()

    def whoami(self) -> dict[str, Any]:
        return self._request("GET", "/whoami").json()

    def tools(self) -> list[dict[str, Any]]:
        body = self._request("GET", "/tools").json()
        return body.get("tools", body) if isinstance(body, dict) else body

    # -- tool calls ------------------------------------------------------------------
    def call(self, tool: str, args: dict[str, Any] | None = None) -> Envelope:
        if self.transport == "mcp":
            return self.call_mcp(tool, args)
        resp = self._request("POST", f"/tools/{tool}/call", json=args or {})
        return parse_rest(resp.json(), trace_id=resp.headers.get("x-trace-id"))

    # -- MCP JSON-RPC (2024-11-05) on the same base URL ---------------------------------
    def rpc(self, method: str, params: dict[str, Any] | None = None) -> tuple[dict[str, Any], httpx.Response]:
        self._rpc_id += 1
        body = {"jsonrpc": "2.0", "id": self._rpc_id, "method": method, "params": params or {}}
        resp = self._request("POST", "", json=body)
        data = resp.json()
        if "error" in data:
            err = data["error"] or {}
            raise RyoError(resp.status_code, f"JSONRPC_{err.get('code')}", str(err.get("message")), resp.headers.get("x-trace-id"))
        return data, resp

    def initialize(self) -> dict[str, Any]:
        return self.rpc("initialize")[0].get("result", {})

    def tools_mcp(self) -> list[dict[str, Any]]:
        return self.rpc("tools/list")[0].get("result", {}).get("tools", [])

    def call_mcp(self, tool: str, args: dict[str, Any] | None = None) -> Envelope:
        data, resp = self.rpc("tools/call", {"name": tool, "arguments": args or {}})
        try:
            return parse_mcp(data, trace_id=resp.headers.get("x-trace-id"))
        except RyoToolError as exc:
            raise RyoError(resp.status_code, "TOOL_ERROR", str(exc), resp.headers.get("x-trace-id")) from exc

    # -- internals -------------------------------------------------------------------
    def _headers(self, auth: bool) -> dict[str, str]:
        h = {"Content-Type": "application/json"}
        if auth:
            h["Authorization"] = f"Bearer {self.key}"
        return h

    def _request(self, method: str, path: str, auth: bool = True, **kw: Any) -> httpx.Response:
        url = self.base_url + path
        last_exc: Exception | None = None
        for attempt in range(self.max_retries + 1):
            try:
                resp = self.http.request(method, url, headers=self._headers(auth), **kw)
            except httpx.TransportError as exc:  # network blip: retry
                last_exc = exc
                self.sleep(self._backoff(attempt, None))
                continue
            self._capture_rate_limit(resp)
            if resp.status_code < 400:
                return resp
            err = self._error_from(resp)
            if resp.status_code in RETRYABLE and attempt < self.max_retries:
                last_exc = err
                self.sleep(self._backoff(attempt, resp.headers.get("Retry-After")))
                continue
            raise err
        assert last_exc is not None
        raise last_exc

    def _capture_rate_limit(self, resp: httpx.Response) -> None:
        for k in ("X-RateLimit-Limit", "X-RateLimit-Remaining", "X-RateLimit-Reset"):
            if k in resp.headers:
                self.last_rate_limit[k] = resp.headers[k]

    @staticmethod
    def _backoff(attempt: int, retry_after: str | None) -> float:
        if retry_after and retry_after.isdigit():
            return float(retry_after)
        return min(1.0 * (2**attempt) + random.uniform(0, 0.5), 30.0)

    @staticmethod
    def _error_from(resp: httpx.Response) -> RyoError:
        try:
            body = resp.json()
        except ValueError:
            body = {}
        return RyoError(
            resp.status_code,
            str(body.get("code", "HTTP_ERROR")),
            str(body.get("message", resp.text[:200])),
            body.get("trace_id") or resp.headers.get("x-trace-id"),
        )


class RecordedRyoClient:
    """Replays envelopes from `<root>/<tool>/<fixture_name>.json`.

    Each file is exactly what RYO returned (plus the `trace_id` header). The envelope's own
    `as_of` and `data_mode` are preserved, so a consumer always sees the real observation time.
    """

    def __init__(self, root: str | Path, name: str = "recorded"):
        self.root = Path(root)
        self.name = name

    def call(self, tool: str, args: dict[str, Any] | None = None) -> Envelope:
        path = self.root / tool / f"{fixture_name(tool, args)}.json"
        if not path.exists():
            raise RyoError(404, "NO_FIXTURE", f"no recorded response at {path}")
        return Envelope.model_validate_json(path.read_text(encoding="utf-8"))


def record(client: RyoClient, tool: str, args: dict[str, Any] | None, root: str | Path) -> Path:
    """Capture one live response into the recorded-fixture tree."""
    env = client.call(tool, args)
    path = Path(root) / tool / f"{fixture_name(tool, args)}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(env.model_dump_json(indent=2), encoding="utf-8")
    return path
