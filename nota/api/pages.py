"""The HTML pages and the files they load: landings, dashboard, walkthrough, fonts and images.
/r/<id> is registered last on purpose: its `{id}` would otherwise swallow /r/<id>.md, .json, .ots and .png."""

from __future__ import annotations

import html
import json

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, Response

from nota.api.common import CDN_CACHE, STATIC, _base, _handle, _hreflang, _ledger, _page
from nota.receipt import Receipt

router = APIRouter()


@router.get("/judges")
def judges_page(request: Request) -> HTMLResponse:
    return _page("judges.html", request, "/judges")


@router.get("/drill")
def drill_page(request: Request) -> HTMLResponse:
    return _page("drill.html", request, "/drill")


@router.get("/feed")
def feed_page(request: Request) -> HTMLResponse:
    return _page("feed.html", request, "/feed")


@router.get("/u/{handle}")
def profile_page(handle: str, request: Request) -> HTMLResponse:
    """A handle's page. Handles are unverified claims, so these pages ask not to be indexed."""
    h = _handle(handle)
    return _page("profile.html", request, f"/u/{h}", ['<meta name="robots" content="noindex">'])


@router.get("/favicon.ico", include_in_schema=False)
def favicon() -> Response:
    return Response(status_code=204)


@router.get("/")
def landing(request: Request) -> HTMLResponse:
    """The reading room: what Nota claims and how to check it. The instrument itself is /app."""
    return _page("landing.html", request, "/", _hreflang(request))


@router.get("/ja")
def landing_ja(request: Request) -> HTMLResponse:
    """The same reading room in Japanese. Same script, same ids, translated prose only."""
    return _page("landing.ja.html", request, "/ja", _hreflang(request))


@router.get("/landing.js", include_in_schema=False)
def landing_script() -> FileResponse:
    # One behaviour and one appearance shared by every language of the landing, so a translation
    # cannot drift into being a different page.
    return FileResponse(STATIC / "landing.js", media_type="application/javascript", headers=CDN_CACHE)


@router.get("/landing.css", include_in_schema=False)
def landing_style() -> FileResponse:
    return FileResponse(STATIC / "landing.css", media_type="text/css", headers=CDN_CACHE)


FONTS = {p.name for p in (STATIC / "fonts").glob("*.woff2")}  # served by name from this set only


@router.get("/fonts/{name}", include_in_schema=False)
def font(name: str) -> FileResponse:
    """The five Latin subsets the pages set type in (SIL OFL), self-hosted so a page never waits on
    or fails over a third-party font request."""
    if name not in FONTS:
        raise HTTPException(404, "no such font")
    return FileResponse(STATIC / "fonts" / name, media_type="font/woff2",
                        headers={"Cache-Control": "public, max-age=31536000, s-maxage=31536000, immutable"})


@router.get("/app")
def index(request: Request) -> HTMLResponse:
    # index.html has no og:title of its own: /r/<id> serves the same file with the receipt's
    return _page("index.html", request, "/app", [
        '<meta property="og:title" content="Nota receipts dashboard">',
        '<meta property="og:description" content="Every decision receipt, diffed against the one before it, '
        'with the evidence path behind each number and a replay check.">',
        '<meta property="og:type" content="website">'], image="/img/dashboard.png")


@router.get("/img/{name}.png", include_in_schema=False)
def landing_image(name: str) -> FileResponse:
    path = (STATIC / "img" / f"{name}.png").resolve()
    if path.parent != (STATIC / "img").resolve() or not path.exists():
        raise HTTPException(404, "no such image")
    return FileResponse(path, media_type="image/png", headers=CDN_CACHE)


@router.get("/demo")
def demo_page(request: Request) -> HTMLResponse:
    """The walkthrough with its transcript, so the video is watchable and readable at one URL. og:video must
    be absolute for a link preview to play it inline; 1280x720 is what the recorder renders."""
    base = html.escape(_base(request))
    return _page("demo.html", request, "/demo", [
        f'<meta property="og:video" content="{base}/demo.mp4">', f'<meta property="og:video:secure_url" content="{base}/demo.mp4">',
        '<meta property="og:video:type" content="video/mp4">', '<meta property="og:video:width" content="1280">',
        '<meta property="og:video:height" content="720">'], image="/img/demo-poster.png")


def _vtt_time(t: float) -> str:
    ms = int(round(t * 1000))
    return f"{ms // 3_600_000:02d}:{ms // 60_000 % 60:02d}:{ms // 1000 % 60:02d}.{ms % 1000:03d}"


@router.get("/demo.vtt", include_in_schema=False)
def demo_captions() -> Response:
    """WebVTT captions from the same chapters the transcript shows: a cue runs from its line's start to
    the next line's, so the caption stays up through the pause, and the last one to the end of the video.
    Lines are not broken by hand: a player wraps to its own width, and a fixed break at 80 characters
    left one-word orphan lines on a 650 px video."""
    path = STATIC / "demo.json"
    if not path.exists():
        raise HTTPException(404, "demo chapters not bundled in this checkout")
    data = json.loads(path.read_text(encoding="utf-8"))
    ch = data["chapters"]
    cues = ["WEBVTT", ""]
    for i, c in enumerate(ch):
        end = ch[i + 1]["start"] if i + 1 < len(ch) else data["seconds"]
        cues += [c["id"], f"{_vtt_time(c['start'])} --> {_vtt_time(end)}", html.escape(c["text"], quote=False), ""]
    return Response("\n".join(cues), media_type="text/vtt; charset=utf-8", headers=CDN_CACHE)


@router.get("/demo.mp4", include_in_schema=False)
def demo_video() -> FileResponse:
    """The submission walkthrough, served from the app itself so the demo URL needs no third party."""
    path = STATIC / "demo.mp4"
    if not path.exists():
        raise HTTPException(404, "demo video not bundled in this checkout")
    return FileResponse(path, media_type="video/mp4", headers=CDN_CACHE)


@router.get("/demo.json", include_in_schema=False)
def demo_chapters() -> FileResponse:
    """Chapters and transcript, written by the recorder from the seconds each line was really spoken."""
    path = STATIC / "demo.json"
    if not path.exists():
        raise HTTPException(404, "demo chapters not bundled in this checkout")
    return FileResponse(path, media_type="application/json")


@router.get("/kol")
def kol_page(request: Request) -> HTMLResponse:
    return _page("kol.html", request, "/kol")


@router.get("/r/{id}")
def permalink(id: str, request: Request) -> HTMLResponse:
    """Same page as /app, with Open Graph / X card tags for this receipt so a shared link previews as a card."""
    raw = _ledger().get_decision(id)
    if raw is None:
        # the page still loads and says there is no such receipt, but the status says it too, so a
        # crawler or a link checker does not index a made-up id as a real receipt
        page = (STATIC / "index.html").read_text(encoding="utf-8")
        return HTMLResponse(page.replace("<!--OG-->", '<meta name="robots" content="noindex">', 1), status_code=404)
    r = Receipt.model_validate_json(raw)
    desc = html.escape(f"{r.verdict.action} (p_up_7d {r.verdict.p_up_7d:.2f}). {r.verdict.rationale}"[:200])
    return _page("index.html", request, f"/r/{r.id}", [
        f'<meta property="og:title" content="{html.escape(r.headline)}">',
        f'<meta property="og:description" content="{desc}">',
        '<meta property="og:type" content="article">',
        f'<meta name="twitter:title" content="{html.escape(r.headline)}">',
    ], image=f"/r/{r.id}.png")
