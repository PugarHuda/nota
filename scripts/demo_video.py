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
        pause(page, 4000)  # thirty-second summary + attention strip
        page.keyboard.press("j"); pause(page, 900)
        page.keyboard.press("Enter")
        page.wait_for_selector("#summary"); pause(page, 3500)
        page.keyboard.press("k"); pause(page, 800)
        page.keyboard.press("Enter"); pause(page, 2500)
        page.click("#verify")
        page.wait_for_function("document.querySelector('#verify-out').textContent.includes('identical')")
        page.locator("#verify").scroll_into_view_if_needed(); pause(page, 3000)
        page.locator("h2:has-text('Council')").scroll_into_view_if_needed(); pause(page, 3500)
        page.locator("h2:has-text('Evidence provenance')").scroll_into_view_if_needed(); pause(page, 3000)
        page.evaluate("window.scrollTo(0, 0); document.querySelector('#detail').scrollTop = 0"); pause(page, 800)
        page.select_option("#skill-name", "narrative_convergence")
        page.fill("#skill-args [data-arg='voices']", "tg:WatcherGuru, bs:decrypt.co")
        page.fill("#skill-args [data-arg='hours']", "48")
        page.click("#skill-run")
        page.wait_for_function("document.querySelector('#skill-out').textContent.length > 10", timeout=90000)
        page.locator("#skill-out").scroll_into_view_if_needed(); pause(page, 6000)
        page.locator("h2:has-text('Open practice positions')").scroll_into_view_if_needed(); pause(page, 3000)
        page.keyboard.press("?"); pause(page, 2500)
        page.keyboard.press("Escape"); pause(page, 1000)
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
