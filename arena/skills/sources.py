"""External sources for skills: Telegram public-channel previews and Tavily search.

Both are thin, honest wrappers: a network or parse failure raises `SourceUnavailable` and the
skill turns that into an `unavailable` section with a warning.
"""

from __future__ import annotations

import html
import os
import re
from typing import Any

import httpx
from pydantic import BaseModel

from arena.skills.contract import SourceUnavailable

UA = "Mozilla/5.0 (compatible; ryo-arena/0.1; +https://ryobuild.com)"


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
        try:
            resp = self.http.get(f"https://t.me/s/{channel}")
        except httpx.HTTPError as exc:
            raise SourceUnavailable(f"{voice}: network error {type(exc).__name__}") from exc
        if resp.status_code != 200:
            raise SourceUnavailable(f"{voice}: HTTP {resp.status_code}")
        return parse_telegram_preview(resp.text, voice)


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
