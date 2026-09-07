"""Record the walkthrough, paced by the narration rather than by a guessed timer.

    uv run --with edge-tts python scripts/narration.py     # voice first: it sets the timing
    uv run python scripts/demo_video.py                    # then the picture
    npx remotion render                                    # then the two are composed

Playwright drives the real pages served from the shipped ledger. Nothing is staged: the verify
button is clicked and its answer is whatever the API returns. On top of the page it draws two
things a viewer needs and a raw screen capture lacks: a cursor that travels to whatever is being
described, and a frame around that element, so it is never ambiguous which part the voice means.

Each beat is held for exactly the length of its own audio, read from `docs/demo/audio/beats.json`,
so the composed video lines up without anyone tuning offsets by hand.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import uvicorn
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
os.environ.setdefault("NOTA_DB", str(ROOT / "data" / "demo.db"))
os.environ.setdefault("NOTA_READONLY", "1")

from nota.api import app  # noqa: E402

OUT = ROOT / "docs" / "demo"
AUDIO = OUT / "audio"
LANDING_BEATS = {"open", "mark", "verify", "verified", "audit", "ledger"}

OVERLAY_JS = r"""() => {
  if (document.getElementById('__nota_overlay')) return;
  const wrap = document.createElement('div');
  wrap.id = '__nota_overlay';
  wrap.innerHTML = `
    <div id="__nota_box"></div>
    <div id="__nota_cursor"><svg viewBox="0 0 24 24" width="26" height="26">
      <path d="M4 2l7.5 18 2.2-7.3L21 10.5z" fill="#fff" stroke="#111" stroke-width="1.4"
            stroke-linejoin="round"/></svg></div>
    <div id="__nota_cap"></div>`;
  const css = document.createElement('style');
  css.textContent = `
    #__nota_overlay { position:fixed; inset:0; z-index:2147483647; pointer-events:none }
    #__nota_box { position:fixed; border:2px solid #2f7df6; border-radius:4px; opacity:0;
      box-shadow:0 0 0 9999px rgba(8,10,14,.45), 0 6px 26px rgba(0,0,0,.35);
      transition:all .55s cubic-bezier(.3,.8,.3,1) }
    #__nota_cursor { position:fixed; left:0; top:0; transform:translate(-100px,-100px);
      transition:transform .65s cubic-bezier(.3,.8,.3,1); filter:drop-shadow(0 2px 3px rgba(0,0,0,.5)) }
    #__nota_cap { position:fixed; left:0; right:0; bottom:0; background:rgba(12,14,18,.94);
      color:#f2f4f8; font:16px/1.5 system-ui,sans-serif; padding:14px 26px;
      border-top:2px solid #2f7df6; opacity:0; transition:opacity .25s }
    #__nota_cap.on { opacity:1 }`;
  document.head.appendChild(css);
  document.body.appendChild(wrap);
}"""

POINT_JS = r"""(sel) => {
  const box = document.getElementById('__nota_box');
  const cur = document.getElementById('__nota_cursor');
  if (!sel) { box.style.opacity = '0'; return null; }
  const el = document.querySelector(sel);
  if (!el) { box.style.opacity = '0'; return null; }
  el.scrollIntoView({block: 'center', behavior: 'instant'});
  const r = el.getBoundingClientRect();
  const pad = 8;
  box.style.opacity = '1';
  box.style.left = (r.left - pad) + 'px';
  box.style.top = (r.top - pad) + 'px';
  box.style.width = (r.width + pad * 2) + 'px';
  box.style.height = (r.height + pad * 2) + 'px';
  cur.style.transform = `translate(${r.left + Math.min(r.width * 0.5, 220)}px, ${r.top + r.height / 2}px)`;
  return [r.left, r.top, r.width, r.height];
}"""

SAY_JS = r"""(text) => {
  const cap = document.getElementById('__nota_cap');
  cap.textContent = text;
  cap.classList.add('on');
}"""


def main() -> None:
    beats_file = AUDIO / "beats.json"
    if not beats_file.exists():
        raise SystemExit("run scripts/narration.py first: the voice sets the timing")
    beats = json.loads(beats_file.read_text(encoding="utf-8"))["beats"]
    by_id = {b["id"]: b for b in beats}
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
        ctx = browser.new_context(viewport={"width": 1280, "height": 720},
                                  record_video_dir=str(OUT),
                                  record_video_size={"width": 1280, "height": 720})
        page = ctx.new_page()
        t0 = time.monotonic()          # the recording starts with the page
        timeline: list[dict] = []

        def beat(name: str, before=None) -> None:
            """Hold this beat for exactly the length of its own narration, and record when it began.

            Page loads and clicks between beats take time the narration does not, so the audio
            cannot simply be concatenated: each line is placed at the moment its own beat actually
            appeared on screen."""
            b = by_id[name]
            if before:
                before()
            page.evaluate(SAY_JS, b["text"])
            page.evaluate(POINT_JS, b["target"])
            timeline.append({**b, "start": round(time.monotonic() - t0, 3)})
            page.wait_for_timeout(int(b["seconds"] * 1000) + 350)

        page.goto(base + "/", wait_until="networkidle")
        page.evaluate(OVERLAY_JS)
        beat("open")
        beat("mark")
        beat("verify")
        page.click("#verify")
        page.wait_for_function("document.querySelector('#verify-out').textContent.includes('identical')",
                               timeout=30000)
        beat("verified")
        beat("audit")
        beat("ledger")

        page.goto(base + "/app", wait_until="networkidle")
        page.wait_for_function("document.querySelector('#health').textContent.includes('receipts')",
                               timeout=20000)
        page.evaluate(OVERLAY_JS)
        beat("dashboard")
        beat("summary")
        beat("changed")
        beat("council", before=lambda: page.locator("h2:has-text('Council')").first.scroll_into_view_if_needed())
        beat("skills")
        beat("close")

        page.wait_for_timeout(600)
        duration = time.monotonic() - t0
        page.close()
        video_path = Path(page.video.path())
        ctx.close()
        browser.close()
    srv.should_exit = True

    (OUT / "timeline.json").write_text(
        json.dumps({"video": "walkthrough-latest.mp4", "seconds": round(duration, 3),
                    "fps": 30, "width": 1280, "height": 720, "beats": timeline}, indent=1),
        encoding="utf-8")
    print(f"wrote {OUT / 'timeline.json'} ({duration:.1f}s, {len(timeline)} beats)")

    # The same offsets are what /demo lists as chapters and as the transcript, so the page cannot
    # drift from the recording: both are written here, in one pass.
    chapters = [{"id": b["id"], "start": b["start"], "seconds": b["seconds"], "text": b["text"]}
                for b in timeline]
    (ROOT / "nota" / "static" / "demo.json").write_text(
        json.dumps({"seconds": round(duration, 3), "chapters": chapters}, indent=1),
        encoding="utf-8")
    print(f"wrote {ROOT / 'nota' / 'static' / 'demo.json'}")

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    final = OUT / f"walkthrough-{stamp}.webm"
    video_path.replace(final)
    print(f"wrote {final} ({final.stat().st_size / 1e6:.1f} MB)")

    if shutil_which("ffmpeg"):
        mp4 = final.with_suffix(".mp4")
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(final),
                        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(mp4)],
                       check=True)
        print(f"wrote {mp4}")
        latest = OUT / "walkthrough-latest.mp4"
        latest.write_bytes(mp4.read_bytes())
        print(f"wrote {latest}  (the composition step reads this)")


def shutil_which(name: str) -> str | None:
    import shutil

    return shutil.which(name)


if __name__ == "__main__":
    main()
