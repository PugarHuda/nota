"""Read-only HTTP API over the ledger plus the diff-first dashboard (Track 2).

Nothing here writes to the ledger. Every endpoint is a view over receipts the CLI already
stored, so the dashboard can never show a number that has no receipt behind it.

This module builds the app (middleware, security headers, CORS) and mounts one router per area:
receipts, scorecard, agents (skills, MCP, A2A, discovery), social, live runs, and pages last.
"""

from __future__ import annotations

import re
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import Response

from nota.api import agents, live, pages, receipts, scorecard, social

app = FastAPI(title="Nota", description="Read-only view over decision receipts. No orders, no wallets.")


class HeadMiddleware:
    """FastAPI routes answer only the methods they name, so every GET page said 405 to HEAD, which is
    what uptime checks, link previews and video players probe with. HEAD is served as the GET it
    stands for, headers and all (content-length included), with the body dropped."""

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope["type"] != "http" or scope["method"] != "HEAD":
            await self.app(scope, receive, send)
            return
        done = False

        async def head_send(message: dict[str, Any]) -> None:
            nonlocal done
            if message["type"] == "http.response.start":
                await send(message)
            elif message["type"] == "http.response.body" and not done:
                done = True
                await send({"type": "http.response.body", "body": b"", "more_body": False})

        await self.app({**scope, "method": "GET"}, receive, head_send)


app.add_middleware(HeadMiddleware)

# Every page is served from this origin: fonts are self-hosted, scripts and styles are inline or
# local, and the only third-party URLs are links a reader clicks. So the policy can be 'self'.
# /docs and /redoc load Swagger UI from a CDN and are left without it.
CSP = ("default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
       "media-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'; object-src 'none'")
SECURITY_HEADERS = {"X-Content-Type-Options": "nosniff", "Referrer-Policy": "strict-origin-when-cross-origin",
                    "Permissions-Policy": "camera=(), microphone=(), geolocation=()"}
# Read-only JSON and text any page or agent may fetch cross-origin. /mcp is not here: it keeps its
# Origin allowlist, which the transport spec requires against DNS rebinding.
CORS_READ = ("/api/", "/r/", "/llms.txt", "/feed.", "/.well-known/", "/demo.vtt")
SKILL_INVOKE = re.compile(r"/api/skills/[^/]+/invoke")


@app.middleware("http")
async def security_headers(request: Request, call_next: Any) -> Response:
    path = request.url.path
    if request.method == "OPTIONS" and SKILL_INVOKE.fullmatch(path):
        # the preflight a browser sends before a cross-origin POST with a JSON body
        return Response(status_code=204, headers={
            "Access-Control-Allow-Origin": "*", "Access-Control-Allow-Methods": "GET, POST",
            "Access-Control-Allow-Headers": "content-type", "Access-Control-Max-Age": "86400"})
    response = await call_next(request)
    response.headers.update(SECURITY_HEADERS)
    if not path.startswith(("/docs", "/redoc")):
        response.headers["Content-Security-Policy"] = CSP
    if (request.method in ("GET", "HEAD") and path.startswith(CORS_READ)) or SKILL_INVOKE.fullmatch(path):
        response.headers["Access-Control-Allow-Origin"] = "*"
    return response


# pages goes last: /r/{id} must not shadow the /r/{id}.md, .json, .ots and .png routes in receipts
for module in (receipts, scorecard, agents, social, live, pages):
    app.include_router(module.router)
