"""Record a real walkthrough of the dashboard (Chromium, 1280x720, WebM) from the shipped demo ledger.

    uv run python scripts/demo_video.py          # writes docs/demo/dashboard-<timestamp>.webm

The video shows exactly what the software does; nothing is staged. If ffmpeg is on PATH an MP4 copy
is written next to it.
"""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import uvicorn
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
os.environ.setdefault("ARENA_DB", str(ROOT / "data" / "demo.db"))
os.environ.setdefault("ARENA_READONLY", "1")

from arena.api import app  # noqa: E402

OUT = ROOT / "docs" / "demo"


def pause(page, ms: int) -> None:
    page.wait_for_timeout(ms)


CAPTION_JS = """(text) => {
  let el = document.getElementById('__cap');
  if (!el) {
    el = document.createElement('div');
    el.id = '__cap';
    el.style.cssText = 'position:fixed;left:0;right:0;bottom:0;z-index:9999;pointer-events:none;'
      + 'background:rgba(12,14,18,.92);color:#f2f4f8;font:16px/1.45 system-ui,sans-serif;'
      + 'padding:12px 20px;border-top:2px solid #4c8dff';
    document.body.appendChild(el);
  }
  el.textContent = text;
}"""


def say(page, text: str, ms: int = 3200) -> None:
    """Caption what the viewer is looking at. The UI underneath is untouched and still live."""
    page.evaluate(CAPTION_JS, text)
    pause(page, ms)


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    srv = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    threading.Thread(target=srv.run, daemon=True).start()
    while not srv.started:
        time.sleep(0.05)
    base = f"http://127.0.0.1:{port}"
    with sync_playwright() as p:
        browser = p.chromium.launch()
        ctx = browser.new_context(viewport={"width": 1280, "height": 720}, record_video_dir=str(OUT), record_video_size={"width": 1280, "height": 720})
        page = ctx.new_page()
        page.goto(base + "/")
        page.wait_for_function("document.querySelector('#health').textContent.includes('receipts')", timeout=20000)
        page.wait_for_function("document.querySelector('#summary-position').textContent.includes('Position')")
        say(page, "RYO Arena: an AI trading opinion you can audit. Every decision is a receipt you can re-run "
                  "and get identical output, with every number traced back to the RYO path it came from.", 5500)
        say(page, "Header, left to right: read-only snapshot, RYO MCP up with 6 tools, NO builder key. "
                  "So these receipts are fixture-sourced, and the page says so rather than pretending.", 6500)
        say(page, "The 30-second summary answers: what now, what changed most, where the practice position stands, what happens next.", 5500)
        say(page, "The newest call is no_trade - and here is why that matters: an independent exchange cross-check "
                  "found the evidence price 42% away from the exchange median, so the judge refused to size anything.", 7000)
        page.keyboard.press("j"); pause(page, 700)
        say(page, "Keyboard-first: j / k move, Enter opens, / filters, ? shows the keys. No mouse needed.", 3000)
        page.keyboard.press("Enter")
        page.wait_for_selector("#summary"); pause(page, 2500)
        say(page, "Every receipt is diffed against the previous one for the same symbol, ranked by impact - "
                  "not a wall of JSON, just what actually moved.", 5000)
        try:
            page.locator("h2:has-text('What changed')").first.scroll_into_view_if_needed()
            say(page, "before -> after per dotted RYO path, with why it matters and how hard it hit.", 5000)
        except Exception:
            say(page, "This receipt has no earlier sibling to diff against yet.", 2500)
        page.click("#verify")
        page.wait_for_function("document.querySelector('#verify-out').textContent.includes('identical')")
        page.locator("#verify").scroll_into_view_if_needed()
        say(page, "Verify replay: re-runs the decision from the stored evidence and cached model output. "
                  "identical: true means nothing was rewritten after the fact.", 6000)
        page.locator("h2:has-text('Council')").scroll_into_view_if_needed()
        say(page, "Three specialists - macro, technician, narrative. Each cites dotted RYO paths, and the value shown "
                  "is read back out of the evidence, not retyped by the model.", 6500)
        say(page, "Citations pointing at absent evidence are dropped in code and counted, so a confident sentence "
                  "cannot rest on a number that was never there.", 5500)
        page.locator("h2:has-text('Evidence provenance')").scroll_into_view_if_needed()
        say(page, "Provenance per section: status, data_mode, as_of, trace id. A partial section stays partial; "
                  "null is never turned into 0; the risk layer refuses to size on simulated data.", 6500)
        page.evaluate("window.scrollTo(0, 0); document.querySelector('#detail').scrollTop = 0"); pause(page, 600)
        say(page, "Track 3: the skills are served on RYO's own /api/skills paths, so plugging them into RYO "
                  "is a route registration. This panel is generated from their definitions.", 5500)
        page.select_option("#skill-name", "narrative_convergence")
        page.fill("#skill-args [data-arg='voices']", "tg:WatcherGuru, bs:decrypt.co")
        page.fill("#skill-args [data-arg='hours']", "48")
        page.click("#skill-run")
        say(page, "Running narrative_convergence live right now against public Telegram and Bluesky - no key needed.", 3000)
        page.wait_for_function("document.querySelector('#skill-out').textContent.length > 10", timeout=90000)
        page.locator("#skill-out").scroll_into_view_if_needed()
        say(page, "Back comes RYO's envelope field for field: status, data_mode, as_of, availability per voice, "
                  "warnings, and the sentiment method named. Silence is null, never 0.", 6500)
        page.locator("h2:has-text('Open practice positions')").scroll_into_view_if_needed()
        say(page, "The practice position is sized from ATR: stop at 2x ATR, size from 1% risk. "
                  "It is marked against an independent exchange price, and it is past its stop here - shown, not hidden.", 6500)
        say(page, "Agents are weighted by their own Brier score once calls resolve at the 7-day horizon. "
                  "Nothing has resolved yet, so every weight is 1.0 and the table says so.", 6000)
        page.click("#theme")
        say(page, "Light, dark or system - and every control is reachable by keyboard, with focus rings and a skip link.", 4000)
        page.click("#theme"); page.click("#theme"); pause(page, 500)
        page.keyboard.press("?"); pause(page, 2000)
        say(page, "Read-only research on RYO evidence. No order is ever placed. Not financial advice.", 4500)
        page.keyboard.press("Escape"); pause(page, 800)
        page.close()
        video_path = Path(page.video.path())
        ctx.close()
        browser.close()
    srv.should_exit = True
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    final = OUT / f"dashboard-{stamp}.webm"
    video_path.replace(final)
    print("wrote", final, f"{final.stat().st_size / 1e6:.1f} MB")
    if shutil.which("ffmpeg"):
        mp4 = final.with_suffix(".mp4")
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(final), "-c:v", "libx264", "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(mp4)], check=True)
        print("wrote", mp4)


if __name__ == "__main__":
    main()
