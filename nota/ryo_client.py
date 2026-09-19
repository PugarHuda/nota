"""RYO research-tool clients.

`RyoClient` talks to the live REST surface (`POST {RYO_MCP_URL}/tools/{tool}/call`).
`RecordedRyoClient` replays envelopes captured from the live server so the pipeline can be
run and judged without a builder key. Both satisfy `RyoSource`.
"""

from __future__ import annotations

import hashlib
import itertools
import json
import os
import random
import re
import threading
import time
from collections import deque
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, Callable, Protocol

import httpx

from nota.envelope import Envelope, parse_rest, parse_mcp, RyoToolError

DEFAULT_URL = "https://app-ryochan.com/api/mcp"
RETRYABLE = {429, 503}
PROTOCOL_VERSION = "2024-11-05"  # what RYO's initialize answers with
CLIENT_VERSION = "0.1.0"
DEFAULT_TIMEOUT = 30.0
TIMEOUTS = {"deep_analysis": 120.0, "compare_tokens": 120.0}  # deep_analysis takes 20-46 s when healthy
MAX_WAIT_S = 60.0  # the longest any Retry-After may hold a call
FANOUT_PER_MIN = 6  # RYO's mcp_fanout bucket: tool calls per minute per key


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
    if args.get("filter_direction") == "all":  # RYO's documented default: the same answer as not sending it
        args.pop("filter_direction")
    if "symbol" in args:
        return str(args["symbol"]).upper()
    if "symbols" in args:  # compare_tokens: "SOL, BTC, ETH" -> SOL-BTC-ETH
        return "-".join(s.strip().upper() for s in re.split(r"[,\s]+", str(args["symbols"])) if s.strip())
    if set(args) <= {"chain", "theme", "filter_direction"} and args:  # scan_market: bsc-news, any-news, any-any-negative
        direction = f"-{args['filter_direction']}" if args.get("filter_direction") else ""
        return f"{args.get('chain') or 'any'}-{args.get('theme') or 'any'}{direction}".lower()
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
        timeout: float = DEFAULT_TIMEOUT,
        transport: str | None = None,
        fanout_per_min: int = FANOUT_PER_MIN,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.base_url = (base_url or os.environ.get("RYO_MCP_URL", DEFAULT_URL)).rstrip("/")
        self.transport = (transport or os.environ.get("RYO_TRANSPORT", "rest")).lower()  # rest | mcp
        if self.transport not in ("rest", "mcp"):
            raise ValueError("RYO_TRANSPORT must be rest or mcp")
        self._rpc_ids = itertools.count(1)  # next() on a count is atomic, so parallel gathers never share an id
        self.key = key or os.environ.get("RYO_MCP_KEY", "")
        self.http = http or httpx.Client(timeout=timeout)
        self.timeout = timeout
        self.max_retries = max_retries
        self.sleep = sleep
        self.clock = clock
        self.fanout_per_min = fanout_per_min
        self._calls: deque[float] = deque()  # start times of this client's tool calls in the last minute
        self._pace_lock = threading.Lock()
        self._init_lock = threading.Lock()
        self.server_info: dict[str, Any] | None = None
        self.protocol_version: str | None = None
        self.last_rate_limit: dict[str, str] = {}

    # -- unauthenticated / metadata -------------------------------------------------
    # The catalog, whoami and health cost no tool-call quota, so none of them goes through the pacer.
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
        resp = self._request("POST", f"/tools/{tool}/call", tool=tool, json=args or {})
        return parse_rest(resp.json(), trace_id=resp.headers.get("x-trace-id"))

    # -- MCP JSON-RPC (2024-11-05) on the same base URL ---------------------------------
    def rpc(self, method: str, params: dict[str, Any] | None = None) -> tuple[dict[str, Any], httpx.Response]:
        if method != "initialize":
            self.initialize()
        body = {"jsonrpc": "2.0", "id": next(self._rpc_ids), "method": method, "params": params or {}}
        tool = (params or {}).get("name") if method == "tools/call" else None
        resp = self._request("POST", "", tool=tool, json=body)
        data = _json_body(resp)
        if "error" in data:
            raise _rpc_error(resp, data["error"] or {})
        return data, resp

    def initialize(self) -> dict[str, Any]:
        """The MCP handshake, once per client: `initialize`, then the `notifications/initialized` the
        lifecycle requires before any other request. The server's name and protocol version are kept."""
        with self._init_lock:
            if self.protocol_version is not None:
                return {"protocolVersion": self.protocol_version, "serverInfo": self.server_info}
            body = {"jsonrpc": "2.0", "id": next(self._rpc_ids), "method": "initialize",
                    "params": {"protocolVersion": PROTOCOL_VERSION, "capabilities": {},
                               "clientInfo": {"name": "nota", "version": CLIENT_VERSION}}}
            resp = self._request("POST", "", json=body)
            data = _json_body(resp)
            if "error" in data:
                raise _rpc_error(resp, data["error"] or {})
            result = data.get("result") or {}
            self._request("POST", "", json={"jsonrpc": "2.0", "method": "notifications/initialized"})  # RYO answers 202
            self.server_info = result.get("serverInfo")
            self.protocol_version = result.get("protocolVersion") or PROTOCOL_VERSION
            return result

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
        # Streamable HTTP clients must accept both; RYO answers JSON today and `_json_body` reads SSE too.
        h = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
        if auth:
            if not self.key:
                # Sending "Bearer " with nothing after it makes httpx raise LocalProtocolError, which
                # tells the caller nothing. Say what is actually missing.
                raise RyoError(0, "NO_KEY", "RYO_MCP_KEY is not set, so authenticated RYO calls are "
                                           "unavailable; use --source recorded or set the key in .env")
            h["Authorization"] = f"Bearer {self.key}"
        return h

    def _pace(self) -> None:
        """Hold a tool call until the fan-out bucket has room. RYO allows six tool calls a minute per key
        (`mcp_fanout`, captured live in tests/fixtures/ryo_errors) and refuses the seventh with
        Retry-After: 60, so waiting here costs less than being refused. Shared by every thread of this
        client. ponytail: per client, not per key; another process on the same key can still hit the
        limit, and that 429 is retried in `_request`."""
        with self._pace_lock:
            while True:
                now = self.clock()
                while self._calls and now - self._calls[0] >= 60.0:
                    self._calls.popleft()
                if len(self._calls) < self.fanout_per_min:
                    self._calls.append(now)
                    return
                self.sleep(60.0 - (now - self._calls[0]))

    def _request(self, method: str, path: str, auth: bool = True, tool: str | None = None, **kw: Any) -> httpx.Response:
        url = self.base_url + path
        secs = TIMEOUTS.get(tool, self.timeout) if tool else self.timeout
        timed_out = False
        for attempt in range(self.max_retries + 1):
            last = attempt == self.max_retries
            if tool:
                self._pace()
            try:
                resp = self.http.request(method, url, headers=self._headers(auth), timeout=httpx.Timeout(secs), **kw)
            except httpx.TimeoutException as exc:
                # A tool that ran out the clock is usually still slow a moment later: one more try, not four.
                err = RyoError(0, "TIMEOUT", f"{tool or path or 'mcp'} gave no answer within {secs:g}s")
                if timed_out or last:
                    raise err from exc
                timed_out = True
                self.sleep(self._backoff(attempt, None))
                continue
            except httpx.TransportError as exc:  # network blip: retry
                if last:
                    raise RyoError(0, "NETWORK", f"{type(exc).__name__}: {exc}") from exc
                self.sleep(self._backoff(attempt, None))
                continue
            self._capture_rate_limit(resp)
            if resp.status_code >= 400:
                err: RyoError | None = self._error_from(resp)
            else:
                err = _mcp_rate_limited(resp) if path == "" else None
            if err is None:
                return resp
            if err.status_code in RETRYABLE and not last:
                self.sleep(self._backoff(attempt, resp.headers.get("Retry-After")))
                continue
            raise err
        raise AssertionError("unreachable: the last attempt returns or raises")

    def _capture_rate_limit(self, resp: httpx.Response) -> None:
        for k in ("X-RateLimit-Limit", "X-RateLimit-Remaining", "X-RateLimit-Reset"):
            if k in resp.headers:
                self.last_rate_limit[k] = resp.headers[k]
        if resp.status_code == 429:  # the fan-out bucket shows only in a refusal's details
            try:
                reset = (resp.json().get("details") or {}).get("reset_at")
            except (ValueError, AttributeError):
                reset = None
            if reset is not None:
                self.last_rate_limit["fanout_reset_at"] = str(reset)

    @staticmethod
    def _backoff(attempt: int, retry_after: str | None) -> float:
        """Retry-After in seconds or as an HTTP date, capped at MAX_WAIT_S so no header can park a run
        for an hour; without one, exponential with jitter."""
        wait: float | None = None
        if retry_after:
            s = retry_after.strip()
            if s.isdigit():
                wait = float(s)
            else:
                try:
                    wait = (parsedate_to_datetime(s) - datetime.now(timezone.utc)).total_seconds()
                except (TypeError, ValueError):
                    wait = None
        if wait is not None:
            return min(max(wait, 0.0), MAX_WAIT_S)
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


def _rpc_error(resp: httpx.Response, err: dict[str, Any]) -> RyoError:
    # RYO puts the trace id in error.data as well as the header (e.g. "Unknown tool: check_safety")
    trace = (err.get("data") or {}).get("trace_id") if isinstance(err.get("data"), dict) else None
    return RyoError(resp.status_code, f"JSONRPC_{err.get('code')}", str(err.get("message")), trace or resp.headers.get("x-trace-id"))


def _json_body(resp: httpx.Response) -> dict[str, Any]:
    """A JSON-RPC reply sent as JSON or, as Streamable HTTP allows, as an SSE stream (its last `data:` line)."""
    if resp.headers.get("content-type", "").startswith("text/event-stream"):
        lines = [ln[5:].strip() for ln in resp.text.splitlines() if ln.startswith("data:")]
        return json.loads(lines[-1]) if lines else {}
    return resp.json() if resp.content else {}


def _mcp_rate_limited(resp: httpx.Response) -> RyoError | None:
    """Over MCP a rate limit is not a 429: RYO answers HTTP 200 with `result.isError`, Retry-After: 60 and
    the text 'Rate limit exceeded (6/min for mcp_fanout)...'. Map it to the REST error so it is retried
    the same way instead of surfacing as a tool failure."""
    try:
        body = _json_body(resp)
    except ValueError:
        return None
    result = body.get("result") if isinstance(body, dict) else None
    if not isinstance(result, dict) or not result.get("isError"):
        return None
    text = next((c.get("text") or "" for c in result.get("content", []) if c.get("type") == "text"), "")
    if "Retry-After" not in resp.headers and not text.startswith("Rate limit exceeded"):
        return None
    return RyoError(429, "RATE_LIMITED", text or "rate limited", resp.headers.get("x-trace-id"))


def check_args(tool: str, args: dict[str, Any], catalog: list[dict[str, Any]]) -> list[str]:
    """What the live catalog (`GET /tools`, free of quota) says is wrong with ARGS for TOOL: a missing
    required key, a key the schema does not allow, a value outside an enum. Empty means RYO accepts it."""
    spec = next((t for t in catalog if t.get("name") == tool), None)
    if spec is None:
        return [f"{tool}: not in the catalog"]
    schema = spec.get("inputSchema") or {}
    props = schema.get("properties") or {}
    out = [f"{tool}: missing required argument {k}" for k in schema.get("required", []) if k not in args]
    if schema.get("additionalProperties") is False:
        out += [f"{tool}: unknown argument {k}" for k in args if k not in props]
    for k, v in args.items():
        enum = (props.get(k) or {}).get("enum")
        if enum and v not in enum:
            out.append(f"{tool}: {k}={v!r} is not one of {enum}")
    return out


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
