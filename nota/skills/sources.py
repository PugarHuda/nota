"""External sources for skills: Telegram public-channel previews and Tavily search.

Both are thin, honest wrappers: a network or parse failure raises `SourceUnavailable` and the
skill turns that into an `unavailable` section with a warning.
"""

from __future__ import annotations

import functools
import html
import os
import json
import re
import time
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from typing import Any
from xml.etree.ElementTree import ParseError

from defusedxml import ElementTree  # external feeds: no entity expansion, no external DTD fetches
from defusedxml.common import DefusedXmlException

import httpx
from pydantic import BaseModel

from nota.skills.contract import SourceUnavailable

UA = "Mozilla/5.0 (compatible; nota/0.1; +https://ryobuild.com)"

# A voice id arrives from whoever called the skill, and the skill endpoints are public. Interpolating
# it straight into a URL let `../../evil` resolve to https://t.me/evil, which hands a caller the path
# on the target host, and with redirects followed that is a step from making this server fetch
# somewhere of their choosing. Handles are letters, digits, underscore, dot and hyphen, and nothing
# else gets near a URL.
_HANDLE = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")


# The skills are public, and the same popular channel or feed is asked for again and again: a
# narrative call names up to 20 voices, and every news check reads the same four feeds. A fetch that
# succeeded is kept for five minutes, keyed by its URL, so repeats cost nothing upstream and do not
# get this server's address rate limited. Failures are never kept: the next caller tries again.
CACHE_TTL = 300.0
CACHE_MAX = 500                       # ponytail: cleared whole when full; an LRU if hit rates ever matter
_CACHE: dict[str, tuple[float, Any]] = {}


def cached(url: str, fetch: Any) -> Any:
    hit = _CACHE.get(url)
    if hit and time.monotonic() - hit[0] < CACHE_TTL:
        return hit[1]
    value = fetch()
    if len(_CACHE) >= CACHE_MAX:
        _CACHE.clear()
    _CACHE[url] = (time.monotonic(), value)
    return value


def clean_handle(raw: str, voice: str) -> str:
    handle = raw.strip().lstrip("@")
    if not _HANDLE.match(handle) or handle.startswith(".") or ".." in handle:
        raise SourceUnavailable(
            f"{voice}: {raw!r} is not a handle. Letters, digits, underscore, dot and hyphen only, "
            "up to 64 characters.")
    return handle


class Message(BaseModel):
    voice: str
    id: str
    url: str
    at: str | None  # ISO 8601 when the source provides it, else None (never guessed)
    text: str
    views: str | None = None


# --- Telegram ------------------------------------------------------------------------------
_WRAP = re.compile(r'<div class="tgme_widget_message_wrap.*?(?=<div class="tgme_widget_message_wrap|</section>)', re.S)
_POST = re.compile(r'data-post="([^"]+)"')
_TEXT = re.compile(r'<div class="tgme_widget_message_text[^"]*"[^>]*>(.*?)</div>', re.S)
_DATE = re.compile(r'<a class="tgme_widget_message_date" href="([^"]+)"><time datetime="([^"]+)"')
_VIEWS = re.compile(r'<span class="tgme_widget_message_views">([^<]*)</span>')
_TAGS = re.compile(r"<[^>]+>")


def _strip(fragment: str) -> str:
    return html.unescape(_TAGS.sub("", fragment.replace("<br/>", "\n").replace("<br>", "\n"))).strip()


def parse_telegram_preview(page: str, voice: str) -> list[Message]:
    if "tgme_widget_message_wrap" not in page:
        raise SourceUnavailable(f"{voice}: no public preview (private channel, group, or not found)")
    out: list[Message] = []
    for block in _WRAP.findall(page):
        post = _POST.search(block)
        date = _DATE.search(block)
        text = _TEXT.search(block)
        if not post:
            continue
        views = _VIEWS.search(block)
        out.append(Message(
            voice=voice, id=post.group(1), url=date.group(1) if date else f"https://t.me/{post.group(1)}",
            at=date.group(2) if date else None, text=_strip(text.group(1)) if text else "",
            views=views.group(1) if views else None,
        ))
    return out


class TelegramPublic:
    def __init__(self, http: httpx.Client | None = None):
        self.http = http or httpx.Client(timeout=20.0, headers={"User-Agent": UA}, follow_redirects=True)

    def fetch(self, channel: str) -> list[Message]:
        voice = f"tg:{channel}"
        url = f"https://t.me/s/{clean_handle(channel, voice)}"

        def get() -> list[Message]:
            try:
                resp = self.http.get(url)
            except httpx.HTTPError as exc:
                raise SourceUnavailable(f"{voice}: network error {type(exc).__name__}") from exc
            if resp.status_code != 200:
                raise SourceUnavailable(f"{voice}: HTTP {resp.status_code}")
            return parse_telegram_preview(resp.text, voice)

        return cached(url, get)


# --- X via its own public syndication endpoint (unofficial, keyless, rate limited) ------------
_SYNDICATION = "https://syndication.twitter.com/srv/timeline-profile/screen-name"
_NEXT_DATA = re.compile(r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>', re.S)


def parse_syndication(page: str, voice: str) -> list[Message]:
    """Pull the timeline out of the embed page's own `__NEXT_DATA__` payload."""
    m = _NEXT_DATA.search(page)
    if not m:
        raise SourceUnavailable(f"{voice}: syndication returned no timeline payload (rate limit, "
                                "protected account, or the handle does not exist)")
    try:
        entries = json.loads(m.group(1))["props"]["pageProps"]["timeline"]["entries"]
    except (KeyError, ValueError) as exc:
        raise SourceUnavailable(f"{voice}: unexpected syndication payload ({type(exc).__name__})") from exc
    out: list[Message] = []
    for entry in entries:
        tweet = (entry.get("content") or {}).get("tweet")
        if entry.get("type") != "tweet" or not tweet or not tweet.get("full_text"):
            continue
        at = None
        if tweet.get("created_at"):                      # "Mon Aug 17 09:00:50 +0000 2026"
            try:
                at = datetime.strptime(tweet["created_at"], "%a %b %d %H:%M:%S %z %Y").astimezone(
                    timezone.utc).isoformat(timespec="seconds")
            except ValueError:
                at = None
        out.append(Message(voice=voice, id=str(tweet.get("id_str") or entry.get("entry_id", "")),
                           url=tweet.get("permalink") and f"https://x.com{tweet['permalink']}",
                           at=at, text=_strip(tweet["full_text"])))
    return out


class XPublic:
    """`x:<handle>` voices through X's own public syndication endpoint, the one that serves embedded
    timelines. No key, no login, and the posts are dated.

    Nitter, which this used to go through, is gone: X served cease-and-desist letters in August 2026
    and the public mirrors including xcancel went dark, so a mirror-based reader is a feature that
    does not work. This reads the syndication payload instead. It is unofficial and rate limited, so
    every failure raises SourceUnavailable and the voice is reported `unavailable`, never guessed.
    """

    # the endpoint serves embedded timelines to browsers and answers a bot agent with a rate-limit
    # page, so this one source asks the way a browser does
    BROWSER_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
                  "Chrome/140.0 Safari/537.36")

    def __init__(self, http: httpx.Client | None = None, base: str = _SYNDICATION):
        self.http = http or httpx.Client(timeout=25.0, follow_redirects=True,
                                         headers={"User-Agent": self.BROWSER_UA,
                                                  "Accept": "text/html,application/xhtml+xml"})
        self.base = base.rstrip("/")

    def fetch(self, handle: str) -> list[Message]:
        voice = f"x:{handle}"
        url = f"{self.base}/{clean_handle(handle, voice)}"

        def get() -> list[Message]:
            try:
                resp = self.http.get(url)
            except httpx.HTTPError as exc:
                raise SourceUnavailable(f"{voice}: network error {type(exc).__name__}") from exc
            if resp.status_code != 200:
                raise SourceUnavailable(f"{voice}: syndication HTTP {resp.status_code}")
            if "Rate limit exceeded" in resp.text[:2000]:
                raise SourceUnavailable(f"{voice}: syndication rate limit reached for this address")
            return parse_syndication(resp.text, voice)

        return cached(url, get)


# --- Bluesky (public AppView API, no key) ----------------------------------------------------
class BlueskyPublic:
    """`bs:<handle>` voices via app.bsky.feed.getAuthorFeed on the public AppView. Dated, official, keyless."""

    def __init__(self, http: httpx.Client | None = None):
        self.http = http or httpx.Client(timeout=20.0, headers={"User-Agent": UA})

    def fetch(self, handle: str, limit: int = 30) -> list[Message]:
        voice = f"bs:{handle}"
        actor = clean_handle(handle, voice)
        return cached(f"bsky:{actor}:{limit}", lambda: self._get(voice, actor, handle, limit))

    def _get(self, voice: str, actor: str, handle: str, limit: int) -> list[Message]:
        try:
            resp = self.http.get("https://public.api.bsky.app/xrpc/app.bsky.feed.getAuthorFeed",
                                 params={"actor": actor, "limit": limit,
                                         "filter": "posts_no_replies"})
        except httpx.HTTPError as exc:
            raise SourceUnavailable(f"{voice}: network error {type(exc).__name__}") from exc
        if resp.status_code != 200:
            detail = ""
            try:
                detail = resp.json().get("message", "")
            except ValueError:
                pass
            raise SourceUnavailable(f"{voice}: HTTP {resp.status_code} {detail}".rstrip())
        out: list[Message] = []
        for item in resp.json().get("feed", []):
            post = item.get("post") or {}
            rec = post.get("record") or {}
            uri = post.get("uri", "")
            rkey = uri.rsplit("/", 1)[-1]
            author = (post.get("author") or {}).get("handle", handle)
            out.append(Message(voice=voice, id=uri, url=f"https://bsky.app/profile/{author}/post/{rkey}",
                               at=rec.get("createdAt"), text=str(rec.get("text", "")),
                               views=str(post.get("likeCount")) if post.get("likeCount") is not None else None))
        return out


# --- RSS news feeds (dated, free, no key) ---------------------------------------------------
RSS_FEEDS = {
    "coindesk.com": "https://www.coindesk.com/arc/outboundfeeds/rss/",
    "cointelegraph.com": "https://cointelegraph.com/rss",
    "theblock.co": "https://www.theblock.co/rss.xml",
    "decrypt.co": "https://decrypt.co/feed",
}
_STOP = {"the", "a", "an", "of", "for", "in", "on", "to", "and", "is", "are", "this", "that", "with", "by", "at", "as", "news",
         "week", "crypto", "price", "today", "market"}


@functools.cache
def _asset_words() -> frozenset[str]:
    """Tickers and names of the majors. Nearly every crypto headline names one, so a claim matching a
    headline on "bitcoin" and "price" says nothing about whether the headline reports the claim."""
    from nota.scorecard import UNIVERSE
    from nota.skills.narrative import NAMES
    from nota.skills.price_check import COINGECKO_IDS

    ids = {cid.split("-")[0] for cid in COINGECKO_IDS.values()}
    return frozenset(w.lower() for w in (*UNIVERSE, *COINGECKO_IDS, *NAMES, *ids))


def _keywords(text: str, drop_assets: bool = True) -> set[str]:
    """The claim's distinctive terms: no stopwords, no generic market words, no asset names."""
    return {w for w in re.findall(r"[a-z0-9]{2,}", text.lower())
            if w not in _STOP and not (drop_assets and w in _asset_words())}


def _rfc822(s: str | None) -> str | None:
    if not s:
        return None
    try:
        return parsedate_to_datetime(s).astimezone(timezone.utc).isoformat(timespec="seconds")
    except (TypeError, ValueError):
        return None


class RssNews:
    """Headline search over four major crypto outlets' RSS feeds. `score` = share of the claim's
    distinctive terms (asset names and generic market words removed) present in title+description
    (0..1); an item needs at least one. `published_date` from the feed. Deterministic and dated."""

    name = "rss_headlines"
    has_dates = True

    def __init__(self, http: httpx.Client | None = None, feeds: dict[str, str] | None = None):
        self.http = http or httpx.Client(timeout=20.0, headers={"User-Agent": UA}, follow_redirects=True)
        self.feeds = feeds or RSS_FEEDS
        self.failed: list[str] = []

    @property
    def configured(self) -> bool:
        return True

    def _items(self, domain: str, url: str) -> list[dict[str, str | None]]:
        try:
            return cached(url, lambda: self._fetch(domain, url))
        except (httpx.HTTPError, ParseError, DefusedXmlException, SourceUnavailable) as exc:
            self.failed.append(f"{domain}: {exc if isinstance(exc, SourceUnavailable) else type(exc).__name__}")
            return []

    def _fetch(self, domain: str, url: str) -> list[dict[str, str | None]]:
        resp = self.http.get(url)
        if resp.status_code != 200:
            raise SourceUnavailable(f"rss {domain}: HTTP {resp.status_code}")
        root = ElementTree.fromstring(resp.content)
        out = []
        for item in root.iter("item"):
            out.append({"title": (item.findtext("title") or "").strip(), "url": (item.findtext("link") or "").strip(),
                        "content": _strip(item.findtext("description") or "")[:400], "published_date": _rfc822(item.findtext("pubDate"))})
        return out

    def search(self, query: str, max_results: int = 6, topic: str = "general", time_range: str | None = None,
               include_domains: list[str] | None = None) -> list[TavilyResult]:
        self.failed = []
        # a topic query ("SOL news this week") has nothing but the asset to match on, and then the asset is the topic
        keys = _keywords(query)
        if not keys:
            from nota.skills.narrative import NAMES

            keys = _keywords(query, drop_assets=False)
            # and a headline says "Solana" far more often than "SOL"
            keys |= {name for name, sym in NAMES.items() if sym.lower() in keys}
        if not keys:
            raise SourceUnavailable("rss: claim has no searchable keywords")
        since = None
        if time_range:
            days = {"day": 1, "week": 7, "month": 30, "year": 365}.get(time_range)
            since = datetime.now(timezone.utc) - timedelta(days=days) if days else None
        hits: list[TavilyResult] = []
        for domain, url in self.feeds.items():
            if include_domains and domain not in include_domains:
                continue
            for it in self._items(domain, url):
                if not it["url"]:
                    continue
                score = len(keys & _keywords(f"{it['title']} {it['content']}", drop_assets=False)) / len(keys)
                if score == 0:
                    continue
                if since and it["published_date"] and it["published_date"] < since.isoformat(timespec="seconds"):
                    continue
                hits.append(TavilyResult(title=it["title"], url=it["url"], content=it["content"], score=round(score, 3), published_date=it["published_date"]))
        if not hits and len(self.failed) == len(self.feeds):
            raise SourceUnavailable("rss: every feed failed: " + "; ".join(self.failed))
        hits.sort(key=lambda h: (-(h.score or 0), h.published_date or ""))
        return hits[:max_results]


# --- Tavily --------------------------------------------------------------------------------
class TavilyResult(BaseModel):
    title: str = ""
    url: str
    content: str = ""
    score: float | None = None
    published_date: str | None = None


class Tavily:
    URL = "https://api.tavily.com/search"

    def __init__(self, api_key: str | None = None, http: httpx.Client | None = None):
        self.api_key = api_key or os.environ.get("TAVILY_API_KEY", "")
        self.http = http or httpx.Client(timeout=30.0)

    @property
    def configured(self) -> bool:
        return bool(self.api_key)

    def search(self, query: str, max_results: int = 6, topic: str = "general", time_range: str | None = None,
               include_domains: list[str] | None = None) -> list[TavilyResult]:
        if not self.configured:
            raise SourceUnavailable("tavily: TAVILY_API_KEY not set")
        body: dict[str, Any] = {"query": query, "max_results": max_results, "topic": topic, "search_depth": "basic"}
        if time_range:
            body["time_range"] = time_range
        if include_domains:
            body["include_domains"] = include_domains
        try:
            resp = self.http.post(self.URL, json=body, headers={"Authorization": f"Bearer {self.api_key}"})
        except httpx.HTTPError as exc:
            raise SourceUnavailable(f"tavily: network error {type(exc).__name__}") from exc
        if resp.status_code != 200:
            raise SourceUnavailable(f"tavily: HTTP {resp.status_code}")
        return [TavilyResult.model_validate(r) for r in resp.json().get("results", [])]


# --- Venice web search (via chat completions citations) -------------------------------------
class VeniceSearch:
    """Web search through Venice's OpenAI-compatible chat endpoint (`enable_web_search`).

    Same `search()` shape as Tavily. Honest limits: no relevance score (None), usually no date,
    no time filter and no domain filter, so callers must say so in their warnings.
    ponytail: max_completion_tokens=1 because only the citations are wanted, not the answer.
    """

    name = "venice_web_search"
    has_dates = False

    def __init__(self, api_key: str | None = None, base_url: str | None = None, model: str | None = None, http: httpx.Client | None = None):
        self.api_key = api_key or os.environ.get("OPENAI_API_KEY") or os.environ.get("VENICE_API_KEY", "")
        self.base_url = (base_url or os.environ.get("OPENAI_BASE_URL", "https://api.venice.ai/api/v1")).rstrip("/")
        self.model = model or os.environ.get("NOTA_MODEL", "qwen3-235b-a22b-instruct-2507")
        self.http = http or httpx.Client(timeout=90.0)

    @property
    def configured(self) -> bool:
        return bool(self.api_key) and "venice.ai" in self.base_url

    def search(self, query: str, max_results: int = 6, topic: str = "general", time_range: str | None = None,
               include_domains: list[str] | None = None) -> list[TavilyResult]:
        if not self.configured:
            raise SourceUnavailable("venice: OPENAI_API_KEY not set or OPENAI_BASE_URL is not Venice")
        if include_domains:
            raise SourceUnavailable("venice: domain-restricted search not supported")
        body = {"model": self.model, "max_completion_tokens": 1, "messages": [{"role": "user", "content": query}],
                "venice_parameters": {"enable_web_search": "on", "enable_web_citations": True, "include_venice_system_prompt": False,
                                      "disable_thinking": True}}
        try:
            resp = self.http.post(f"{self.base_url}/chat/completions", json=body, headers={"Authorization": f"Bearer {self.api_key}"})
        except httpx.HTTPError as exc:
            raise SourceUnavailable(f"venice: network error {type(exc).__name__}") from exc
        if resp.status_code != 200:
            raise SourceUnavailable(f"venice: HTTP {resp.status_code}")
        cites = (resp.json().get("venice_parameters") or {}).get("web_search_citations") or []
        return [TavilyResult(title=c.get("title", ""), url=c["url"], content=c.get("content", ""), score=None,
                             published_date=c.get("date") or None) for c in cites[:max_results] if c.get("url")]


def search_backend() -> Tavily | VeniceSearch:
    """Tavily when its key is set, else Venice when the LLM key points at Venice, else an unconfigured Tavily
    (whose search() raises a clear SourceUnavailable)."""
    t = Tavily()
    if t.configured:
        return t
    v = VeniceSearch()
    return v if v.configured else t
