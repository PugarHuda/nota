"""Publish a receipt to Telegram and/or Discord. Both are opt-in through env and report honestly.

TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID  -> Bot API sendMessage (HTML parse mode)
DISCORD_WEBHOOK_URL                     -> incoming webhook with one embed
ARENA_PUBLIC_URL (optional)             -> base for the receipt permalink in the message
"""

from __future__ import annotations

import html
import os
from typing import Any

import httpx

from arena.receipt import Receipt
from arena.risk import PracticeTrade


def _permalink(r: Receipt) -> str | None:
    base = os.environ.get("ARENA_PUBLIC_URL", "").rstrip("/")
    return f"{base}/r/{r.id}" if base else None


def _lines(r: Receipt) -> list[str]:
    t = r.trade
    trade = (f"{t.side.upper()} {t.size_usd:.0f} USD at {t.entry_price:g}, stop {t.stop_price:g}, target {t.target_price:g}"
             if isinstance(t, PracticeTrade) else f"no trade: {t.reason}")
    degraded = [k for k, s in r.availability.items() if s != "ok"]
    return [
        r.headline,
        f"verdict {r.verdict.action} (p_up_7d {r.verdict.p_up_7d:.2f}); practice trade: {trade}",
        f"council: " + ", ".join(f"{o.role} {o.stance} {o.p_up_7d:.2f}" for o in r.opinions),
        f"evidence {r.pack_hash[:12]} from {r.source}, model {r.model}" + (f"; degraded: {', '.join(degraded)}" if degraded else ""),
        "Research on read-only RYO evidence. No order was placed. Not financial advice.",
    ]


def telegram(r: Receipt, http: httpx.Client | None = None) -> dict[str, Any]:
    token, chat = os.environ.get("TELEGRAM_BOT_TOKEN", ""), os.environ.get("TELEGRAM_CHAT_ID", "")
    if not token or not chat:
        return {"channel": "telegram", "status": "not_configured"}
    lines = [html.escape(x) for x in _lines(r)]
    lines[0] = f"<b>{lines[0]}</b>"
    link = _permalink(r)
    if link:
        lines.append(f'<a href="{html.escape(link)}">receipt {r.id}</a>')
    http = http or httpx.Client(timeout=20.0)
    resp = http.post(f"https://api.telegram.org/bot{token}/sendMessage",
                     json={"chat_id": chat, "text": "\n".join(lines), "parse_mode": "HTML", "disable_web_page_preview": True})
    ok = resp.status_code == 200 and resp.json().get("ok") is True
    return {"channel": "telegram", "status": "sent" if ok else "error", "http": resp.status_code,
            "message_id": (resp.json().get("result") or {}).get("message_id") if ok else None,
            "error": None if ok else resp.text[:200]}


def discord(r: Receipt, http: httpx.Client | None = None) -> dict[str, Any]:
    url = os.environ.get("DISCORD_WEBHOOK_URL", "")
    if not url:
        return {"channel": "discord", "status": "not_configured"}
    lines = _lines(r)
    colour = {"long": 0x3ECF8E, "short": 0xFF6B6B}.get(r.verdict.action, 0x8B93A7)
    embed: dict[str, Any] = {
        "title": lines[0][:256], "description": "\n".join(lines[1:4])[:4000], "color": colour,
        "fields": [{"name": "receipt", "value": r.id, "inline": True}, {"name": "source", "value": r.source, "inline": True},
                   {"name": "model", "value": r.model, "inline": True}],
        "footer": {"text": lines[4]},
    }
    link = _permalink(r)
    if link:
        embed["url"] = link
    http = http or httpx.Client(timeout=20.0)
    resp = http.post(url, params={"wait": "true"}, json={"embeds": [embed]})
    ok = resp.status_code in (200, 204)
    return {"channel": "discord", "status": "sent" if ok else "error", "http": resp.status_code,
            "message_id": (resp.json().get("id") if ok and resp.content else None), "error": None if ok else resp.text[:200]}


def notify_receipt(r: Receipt) -> list[dict[str, Any]]:
    """Post to every configured channel; a channel that is not configured says so instead of pretending."""
    return [telegram(r), discord(r)]
