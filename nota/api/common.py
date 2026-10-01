"""What every route module shares: the ledger handle, absolute links, page templating, the receipt
loader, the per-address hourly budget and the small response helpers."""

from __future__ import annotations

import csv
import html
import io
import logging
import math
import os
import re
import time
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from fastapi import HTTPException, Request
from fastapi.responses import HTMLResponse, Response

from nota.backings import PostgresBackings
from nota.ledger import Ledger
from nota.receipt import Receipt

load_dotenv()
STATIC = Path(__file__).parents[1] / "static"


# Static assets change only with a deploy: the CDN may serve them a day and revalidate in the background for
# a week (vercel.json sets the same rule at the edge), so a page's CSS, script and images come from the POP.
CDN_CACHE = {"Cache-Control": "public, s-maxage=86400, stale-while-revalidate=604800"}


def _ledger() -> Ledger:
    # ponytail: one connection per request; sqlite objects cannot cross FastAPI's worker threads
    return Ledger(os.environ.get("NOTA_DB", "nota.db"))


def _base(request: Request) -> str:
    """The absolute origin links are built on: NOTA_PUBLIC_URL when set, else what the request came in on.
    The fallback comes from the Host header, so anything put into HTML goes through html.escape."""
    return os.environ.get("NOTA_PUBLIC_URL", "").rstrip("/") or str(request.base_url).rstrip("/")


FEED_LINKS = ['<link rel="alternate" type="application/atom+xml" title="Nota receipts and settled RYO plans" href="/feed.xml">',
              '<link rel="alternate" type="application/feed+json" title="Nota receipts and settled RYO plans" href="/feed.json">']


def _page(file: str, request: Request, path: str, extra: tuple[str, ...] | list[str] = (), status: int = 200,
          image: str = "/img/card.png") -> HTMLResponse:
    """A static page with its head completed at <!--OG-->: canonical, og:url, og:image and the feed links,
    all absolute, because link previews and crawlers resolve nothing relative in these tags."""
    base = html.escape(_base(request))
    tags = [f'<link rel="canonical" href="{base}{path}">', f'<meta property="og:url" content="{base}{path}">',
            f'<meta property="og:image" content="{base}{image}">', f'<meta name="twitter:image" content="{base}{image}">',
            '<meta name="twitter:card" content="summary_large_image">', *FEED_LINKS, *extra]
    page = (STATIC / file).read_text(encoding="utf-8")
    return HTMLResponse(page.replace("<!--OG-->", "\n".join(tags), 1), status_code=status)


def _hreflang(request: Request) -> list[str]:
    base = html.escape(_base(request))
    return [f'<link rel="alternate" hreflang="en" href="{base}/">', f'<link rel="alternate" hreflang="ja" href="{base}/ja">',
            f'<link rel="alternate" hreflang="x-default" href="{base}/">']


def _receipt(led: Ledger, id: str) -> Receipt:
    raw = led.get_decision(id)
    if raw is None:
        raise HTTPException(404, f"no decision {id}")
    return Receipt.model_validate_json(raw)


def _ots(stamp: dict[str, Any] | None, name: str) -> Response:
    if stamp is None:
        raise HTTPException(404, f"no timestamp proof for {name} yet")
    return Response(bytes(stamp["ots"]), media_type="application/octet-stream",
                    headers={"Content-Disposition": f'attachment; filename="{name}.ots"'})


HANDLE = re.compile(r"[A-Za-z0-9_.\-]{3,32}")  # fullmatch: `$` would also accept a trailing newline


_BACKING_HITS: dict[str, list[float]] = {}
BACKING_LIMIT, BACKING_WINDOW = 30, 3600.0
HITS_MAX_KEYS = 10_000   # a flood of distinct addresses clears the table rather than growing it without bound


def _throttle_n(key: str, n: int, limit: int, now: float | None = None) -> int | None:
    """Record n hits if all n fit in the window; otherwise record nothing and return the seconds until
    the oldest hit ages out, which is when a retry can succeed.

    With DATABASE_URL the count lives in Postgres, so every serverless instance shares one budget
    instead of each granting its own. Without it, or if Postgres cannot be reached, the count is
    this process's own: a limiter that fails closed would take the whole public surface down with
    the database."""
    if n <= 0:
        return None
    dsn = os.environ.get("DATABASE_URL", "").strip()
    if dsn and now is None:           # an explicit `now` is a test's clock, which only the local count follows
        try:
            allowed, wait = PostgresBackings(dsn).hit(key, n, limit, BACKING_WINDOW)
            return None if allowed else wait
        except Exception:
            logging.getLogger("nota.api").exception("shared rate limit unreachable; counting in this process")
    now = now if now is not None else time.time()
    # hits are appended in time order, so a key whose newest hit is expired is wholly expired
    for k in [k for k, v in _BACKING_HITS.items() if now - v[-1] >= BACKING_WINDOW]:
        del _BACKING_HITS[k]
    if len(_BACKING_HITS) > HITS_MAX_KEYS:
        _BACKING_HITS.clear()
    hits = [t for t in _BACKING_HITS.get(key, []) if now - t < BACKING_WINDOW]
    if len(hits) + n > limit:
        if hits:
            _BACKING_HITS[key] = hits
        return max(1, math.ceil(hits[0] + BACKING_WINDOW - now)) if hits else int(BACKING_WINDOW)
    _BACKING_HITS[key] = hits + [now] * n
    return None


def _throttle(key: str, now: float | None = None, limit: int = BACKING_LIMIT, cost: int = 1) -> None:
    wait = _throttle_n(key, cost, limit, now)
    if wait is not None:
        raise HTTPException(429, f"too many requests from this address; limit {limit} per hour",
                            headers={"Retry-After": str(wait)})


def _skill_cost(name: Any, args: Any) -> int:
    """Units of the hourly budget one call spends: roughly the upstream fetches it makes. One
    narrative call with 20 voices is 20 fetches, so it costs 20, not the 1 a price check costs;
    the ledger-only track record fetches nothing and is free."""
    if name == "verdict_track_record":
        return 0
    if name == "narrative_convergence":
        voices = args.get("voices") if isinstance(args, dict) else None
        if not (isinstance(voices, list) and voices):
            return 1
        # an x: voice may fall back to a paid Tavily search after syndication refuses: two fetches
        return sum(2 if isinstance(v, str) and v.strip().startswith("x:") else 1 for v in voices)
    return 2 if name == "news_verify" else 1


def _handle(raw: str) -> str:
    handle = raw.strip(" ").lstrip("@").lower()  # "@Abc" and "abc" are one backer; a newline is refused, not trimmed
    if not HANDLE.fullmatch(handle):
        raise HTTPException(422, "handle must be 3-32 characters: letters, digits, _ . - (a leading @ is dropped)")
    return handle


def _csv(header: list[str], rows: list[list[Any]], name: str) -> Response:
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(header)
    w.writerows(["" if v is None else v for v in r] for r in rows)
    return Response(buf.getvalue(), media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": f'inline; filename="{name}"'})
