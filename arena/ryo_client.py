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
import time
from pathlib import Path
from typing import Any, Callable, Protocol

import httpx

from arena.envelope import Envelope, parse_rest

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
    args = args or {}
    if "symbol" in args:
        return str(args["symbol"]).upper()
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
    ):
        self.base_url = (base_url or os.environ.get("RYO_MCP_URL", DEFAULT_URL)).rstrip("/")
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
        resp = self._request("POST", f"/tools/{tool}/call", json=args or {})
        return parse_rest(resp.json(), trace_id=resp.headers.get("x-trace-id"))

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
