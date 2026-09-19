"""Browser QA over the shipped demo ledger: the landing, the dashboard, both themes, three widths,
every skill in the runner, the error paths, and the accessibility floor.

This runs against `data/demo.db`, the same snapshot the deployment serves, so what passes here is
what a judge meets. Nothing is stubbed: the verify button really calls the API, and the skill runner
really invokes the skills.
"""

import json
import re
import os
import socket
import sqlite3
import threading
import time
from pathlib import Path

import pytest
import uvicorn

playwright = pytest.importorskip("playwright.sync_api")

ROOT = Path(__file__).resolve().parents[1]
DEMO = ROOT / "data" / "demo.db"
RECEIPT = "dbd7727f5a25"
# Read from the snapshot rather than written down: a daily cycle adds receipts, and a test that
# counts them by hand goes red the first morning it runs.
SHIPPED = len(sqlite3.connect(DEMO).execute("SELECT id FROM decisions").fetchall())
SHIPPED_ETH = len(sqlite3.connect(DEMO).execute("SELECT id FROM decisions WHERE symbol='ETH'").fetchall())
# The landing draws the newest receipt, whichever that is after the last cycle.
NEWEST = json.loads(sqlite3.connect(DEMO).execute(
    "SELECT receipt_json FROM decisions ORDER BY created_at DESC LIMIT 1").fetchone()[0])


def _receipts():
    """Newest first, the order the API answers in."""
    rows = sqlite3.connect(DEMO).execute("SELECT receipt_json FROM decisions ORDER BY created_at DESC").fetchall()
    return [json.loads(r[0]) for r in rows]


def _most_failed():
    """The receipt the landing's failure panel shows: the most RYO sections in error or unavailable,
    newest first on a tie."""
    from nota.evidence import SECTIONS

    failed = lambda r: sum(r["availability"].get(k) in ("error", "unavailable") for k in SECTIONS)
    rs = _receipts()
    return max(rs, key=lambda r: (failed(r), -rs.index(r)))


@pytest.fixture(scope="module")
def server():
    # scoped, not global: leaking these would point every later test at the read-only snapshot
    before = {k: os.environ.get(k) for k in ("NOTA_DB", "NOTA_READONLY")}
    os.environ["NOTA_DB"] = str(DEMO)
    os.environ["NOTA_READONLY"] = "1"
    from nota import api

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    srv = uvicorn.Server(uvicorn.Config(api.app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=srv.run, daemon=True)
    thread.start()
    for _ in range(120):
        if srv.started:
            break
        time.sleep(0.05)
    assert srv.started
    yield f"http://127.0.0.1:{port}"
    srv.should_exit = True
    thread.join(timeout=5)
    for k, v in before.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v


@pytest.fixture(scope="module")
def browser():
    with playwright.sync_playwright() as p:
        try:
            b = p.chromium.launch()
        except playwright.Error as exc:
            pytest.skip(f"chromium not installed for playwright: {str(exc).splitlines()[0]}")
        yield b
        b.close()


def page_with_log(browser, **kw):
    """A page that fails the test on any console error or unhandled exception."""
    page = browser.new_page(**kw)
    problems: list[str] = []
    page.on("console", lambda m: problems.append(f"console.{m.type}: {m.text}") if m.type == "error" else None)
    page.on("pageerror", lambda e: problems.append(f"pageerror: {e}"))
    page.on("requestfailed", lambda r: None if _media_abort(r) else
            problems.append(f"requestfailed: {r.url} {r.failure}"))
    return page, problems


def _media_abort(request) -> bool:
    """A <video> aborting its own range request on a seek or a page close is browser behaviour.

    Chromium reports it as a failed request, but nothing is broken: the media element cancels the
    open range fetch and opens another at the new offset. Only that case is forgiven; a 404 on the
    file, or any other request failing, still fails the test."""
    return request.resource_type == "media" and "ERR_ABORTED" in (request.failure or "")


def test_landing_renders_draws_its_mark_and_verifies_a_receipt_for_real(server, browser):
    page, problems = page_with_log(browser, viewport={"width": 1440, "height": 900})
    page.goto(server + "/", wait_until="networkidle")

    assert page.locator("h1").inner_text() == "A trading call you can re-run."
    assert page.locator("h1").bounding_box()["height"] < 160          # the headline stays two lines
    assert page.locator("nav").bounding_box()["height"] <= 80         # nav discipline

    # the mark is generated from the receipt, so it has one tick per hex character of the hash
    assert page.locator("#mark line.tick").count() == 64
    assert page.locator("#mark path.arc").count() == 1
    assert page.locator("#mark text.p").text_content() == f"{NEWEST['verdict']['p_up_7d']:.2f}"  # SVG text, not an HTMLElement

    # the failure panel is the ledger's own record of a source going down, not a description of one
    page.wait_for_selector("#broken-panel .bad")
    shown = [c.inner_text() for c in page.locator("#broken-panel > div").all()]
    degraded = _most_failed()
    assert degraded["id"] in page.locator("#broken-verdict").inner_text()
    assert page.locator("#broken-panel .bad").count() == sum(
        1 for v in degraded["availability"].values() if v != "ok")
    for warning in degraded["warnings"]:
        assert warning in shown, f"the page dropped a warning the receipt holds: {warning}"

    # the CTA is reachable without scrolling
    cta = page.locator("a.btn").first.bounding_box()
    assert cta["y"] + cta["height"] < 900

    # the button really calls the deployment's own API
    page.click("#verify")
    page.wait_for_function("() => document.querySelector('#verify-out').textContent.includes('identical')", timeout=20000)
    assert "identical: true" in page.locator("#verify-out").inner_text()

    assert problems == [], problems
    page.close()


def test_every_landing_link_and_image_resolves(server, browser):
    page, problems = page_with_log(browser, viewport={"width": 1440, "height": 900})
    page.goto(server + "/", wait_until="networkidle")

    broken = page.evaluate("""() => [...document.images].filter(i => !i.complete || i.naturalWidth === 0).map(i => i.src)""")
    assert broken == [], f"images that did not load: {broken}"
    assert page.evaluate("""() => [...document.images].every(i => i.alt && i.alt.length > 12)"""), "every image needs real alt text"

    hrefs = page.eval_on_selector_all("a[href^='/']", "els => [...new Set(els.map(e => e.getAttribute('href')))]")
    assert "/app" in hrefs and "/demo" in hrefs
    for href in hrefs:
        r = page.request.get(server + href)
        assert r.status == 200, f"{href} -> {r.status}"
    assert problems == [], problems
    page.close()


def test_demo_page_plays_the_shipped_video_and_lists_its_real_chapters(server, browser):
    """The transcript is written by the recorder, so a chapter must not point past the video."""
    page, problems = page_with_log(browser, viewport={"width": 1440, "height": 900})
    page.goto(server + "/demo", wait_until="networkidle")

    page.wait_for_selector("#script button")
    chapters = page.eval_on_selector_all("#script button .t", "els => els.map(e => e.textContent)")
    assert len(chapters) >= 10, f"only {len(chapters)} chapters rendered"

    length = page.evaluate("""() => new Promise(done => {
        const v = document.getElementById('v');
        if (v.readyState >= 1) return done(v.duration);
        v.addEventListener('loadedmetadata', () => done(v.duration), {once: true});
    })""")
    assert length > 60, f"the shipped demo.mp4 is only {length}s long"

    last = page.evaluate("""async () => {
        const r = await fetch('/demo.json');
        const d = await r.json();
        return Math.max(...d.chapters.map(c => c.start));
    }""")
    assert last < length, f"a chapter starts at {last}s but the video ends at {length}s"

    # clicking a chapter seeks the video: the transcript is a control, not a caption
    page.locator("#script button").nth(3).click()
    page.wait_for_timeout(250)
    assert page.evaluate("() => document.getElementById('v').currentTime") > 1

    assert problems == [], problems
    page.close()


@pytest.mark.parametrize("width,height", [(320, 640), (390, 844), (768, 1024), (1440, 900)])
def test_no_horizontal_overflow_on_either_page_at_any_width(server, browser, width, height):
    page, problems = page_with_log(browser, viewport={"width": width, "height": height})
    for path in ("/", "/ja", "/app", "/scorecard", "/demo"):
        page.goto(server + path, wait_until="networkidle")
        page.wait_for_timeout(400)
        overflow = page.evaluate("() => document.documentElement.scrollWidth - document.documentElement.clientWidth")
        assert overflow <= 1, f"{path} at {width}px scrolls sideways by {overflow}px"
    assert problems == [], problems
    page.close()


@pytest.mark.parametrize("theme", ["light", "dark"])
def test_dashboard_is_legible_in_both_themes(server, browser, theme):
    page, problems = page_with_log(browser, viewport={"width": 1440, "height": 900})
    page.goto(server + "/app", wait_until="networkidle")
    page.wait_for_function("() => document.querySelector('#health').textContent.includes('receipts')", timeout=20000)
    while page.evaluate("document.documentElement.dataset.theme || 'system'") != theme:
        page.click("#theme")
    page.wait_for_timeout(250)

    # body text and its background must not collapse into each other
    same = page.evaluate("""() => {
      const s = getComputedStyle(document.body);
      return s.color === s.backgroundColor;
    }""")
    assert not same
    assert page.locator("#detail .headline").inner_text().strip() != ""
    assert page.locator("#list .row").count() == SHIPPED
    assert problems == [], problems
    page.close()


def test_keyboard_alone_reaches_the_receipt_the_diff_and_the_replay_check(server, browser):
    page, problems = page_with_log(browser, viewport={"width": 1440, "height": 900})
    page.goto(server + "/app", wait_until="networkidle")
    page.wait_for_selector("#list .row")

    page.keyboard.press("j")
    page.keyboard.press("Enter")
    # #summary is already on the page (the newest receipt opens on load), so waiting for it raced
    # the async open(); the URL changing is the thing Enter actually does
    page.wait_for_url("**/r/**")
    opened = page.url
    assert "/r/" in opened

    page.keyboard.press("/")
    assert page.evaluate("document.activeElement.id") == "filter"
    page.keyboard.type("eth")
    playwright.expect(page.locator("#list .row")).to_have_count(SHIPPED_ETH)
    page.locator("#filter").fill("")          # fill() refocuses the input, so blur before the next key
    # expect() over wait_for_function: same retry, but a failure names the count it actually saw
    playwright.expect(page.locator("#list .row")).to_have_count(SHIPPED)
    page.keyboard.press("Escape")
    assert page.evaluate("document.activeElement.id") != "filter"

    page.keyboard.press("?")
    assert page.locator("#help").is_visible()
    page.keyboard.press("Escape")

    page.click("#verify")
    page.wait_for_function("() => document.querySelector('#verify-out').textContent.includes('identical')", timeout=30000)
    assert "identical" in page.locator("#verify-out").inner_text()
    assert problems == [], problems
    page.close()


def test_the_skill_runner_lists_and_runs_every_skill(server, browser):
    page, problems = page_with_log(browser, viewport={"width": 1440, "height": 1200})
    page.goto(server + "/app", wait_until="networkidle")
    page.wait_for_selector("#skill-name option", state="attached")

    names = page.eval_on_selector_all("#skill-name option", "els => els.map(e => e.value)")
    assert names == ["narrative_convergence", "news_verify", "price_crosscheck", "technicals_crosscheck", "positioning_check", "move_base_rate", "verdict_track_record"]

    for skill, args in (("price_crosscheck", {"symbol": "SOL"}), ("technicals_crosscheck", {"symbol": "SOL"}), ("positioning_check", {"symbol": "SOL"}), ("move_base_rate", {"symbol": "SOL"})):
        page.select_option("#skill-name", skill)
        for key, value in args.items():
            page.fill(f"#skill-args [data-arg='{key}']", value)
        page.click("#skill-run")
        page.wait_for_function("() => document.querySelector('#skill-out').textContent.length > 40", timeout=90000)
        out = page.locator("#skill-out").inner_text()
        assert skill.split("_")[0] in out.lower() or "envelope" in out.lower() or "data_mode" in out.lower()
        assert "traceback" not in out.lower()
    assert problems == [], problems
    page.close()


def test_error_paths_say_what_is_wrong_instead_of_breaking(server, browser):
    page, problems = page_with_log(browser, viewport={"width": 1200, "height": 900})

    page.goto(f"{server}/r/doesnotexist", wait_until="networkidle")
    assert "No receipt with id" in page.locator("#detail").inner_text()
    # the 404 the page just handled is the point of this test, so it is not counted as a problem
    problems[:] = [p for p in problems if "404" not in p]

    page.goto(server + "/app", wait_until="networkidle")
    page.wait_for_selector("#backing")
    page.fill("#handle", "not a handle!!")
    page.locator("#backing button").first.click()
    page.wait_for_function("() => document.querySelector('#back-out').textContent.length > 0", timeout=15000)
    said = page.locator("#back-out").inner_text().lower()
    assert "handle" in said or "read-only" in said or "503" in said     # refused, and it says why
    problems[:] = [p for p in problems if "404" not in p and "503" not in p]
    assert problems == [], problems
    page.close()


def test_accessibility_floor_focus_alt_and_live_regions(server, browser):
    page, problems = page_with_log(browser, viewport={"width": 1440, "height": 900})
    page.goto(server + "/app", wait_until="networkidle")
    page.wait_for_selector("#list .row")

    # the skip link is the first thing a keyboard user meets, and it is visible when focused
    page.keyboard.press("Tab")
    first = page.evaluate("document.activeElement.className")
    assert "skip" in first
    assert page.evaluate("""() => {
      const el = document.querySelector('.skip');
      return el.getBoundingClientRect().left > -100;
    }""")

    # every list row is a real button, not a div with a click handler
    assert page.eval_on_selector_all("#list .row", "els => els.every(e => e.tagName === 'BUTTON')")
    # the panels that change without a page load announce themselves; the receipt itself does not,
    # or a screen reader would read the whole document out every time one opens
    for sel in ("#health", "#attention", "#sr-status"):
        assert page.get_attribute(sel, "aria-live") == "polite", sel
    assert page.get_attribute("#detail", "aria-live") is None
    playwright.expect(page.locator("#sr-status")).to_contain_text(NEWEST["id"])
    # form controls carry a label or an accessible name
    assert page.get_attribute("#filter", "aria-label")
    assert page.get_attribute("#skill-name", "aria-label")
    assert problems == [], problems
    page.close()


def test_reduced_motion_is_actually_honoured(server, browser):
    page, problems = page_with_log(browser, viewport={"width": 1440, "height": 900})
    page.emulate_media(reduced_motion="reduce")
    page.goto(server + "/", wait_until="networkidle")
    animated = page.evaluate("""() => [...document.querySelectorAll('h1, .sub, .actions, #mark line, #mark path')]
        .filter(el => getComputedStyle(el).animationName !== 'none').length""")
    assert animated == 0, "something still animates under prefers-reduced-motion: reduce"
    assert problems == [], problems
    page.close()


def test_mcp_and_llms_txt_answer_over_the_wire_not_just_through_the_test_client(server, browser):
    """An MCP host and an agent both reach this over real HTTP, so check it that way."""
    page, problems = page_with_log(browser)
    ctx = page.request

    init = ctx.post(server + "/mcp", data={"jsonrpc": "2.0", "id": 1, "method": "initialize",
                                           "params": {"protocolVersion": "2026-07-28", "capabilities": {}}})
    assert init.status == 200
    result = init.json()["result"]
    assert result["protocolVersion"] == "2026-07-28"
    assert {"tools", "resources"} <= set(result["capabilities"])

    tools = ctx.post(server + "/mcp", data={"jsonrpc": "2.0", "id": 2, "method": "tools/list"}).json()
    assert len(tools["result"]["tools"]) == 7

    resources = ctx.post(server + "/mcp", data={"jsonrpc": "2.0", "id": 3, "method": "resources/list"}).json()
    uris = [r["uri"] for r in resources["result"]["resources"]]
    assert f"nota://receipt/{RECEIPT}" in uris
    read = ctx.post(server + "/mcp", data={"jsonrpc": "2.0", "id": 4, "method": "resources/read",
                                           "params": {"uri": f"nota://receipt/{RECEIPT}"}}).json()
    assert read["result"]["contents"][0]["text"].startswith(f"# Decision receipt {RECEIPT}")

    # transport rules hold over the wire too
    assert ctx.get(server + "/mcp").status == 405
    assert ctx.post(server + "/mcp", data={"jsonrpc": "2.0", "method": "notifications/initialized"}).status == 202

    txt = ctx.get(server + "/llms.txt")
    assert txt.status == 200 and txt.headers["content-type"].startswith("text/plain")
    body = txt.text()
    assert "/mcp" in body and "nota://receipt/" in body and RECEIPT in body
    assert problems == [], problems
    page.close()


def _contrast(hex_fg: str, hex_bg: str) -> float:
    """WCAG relative-luminance contrast ratio, computed rather than eyeballed."""
    def lum(c):
        rgb = [int(c[i:i + 2], 16) / 255 for i in (0, 2, 4)]
        rgb = [v / 12.92 if v <= 0.04045 else ((v + 0.055) / 1.055) ** 2.4 for v in rgb]
        return 0.2126 * rgb[0] + 0.7152 * rgb[1] + 0.0722 * rgb[2]
    a, b = lum(hex_fg.lstrip("#")), lum(hex_bg.lstrip("#"))
    hi, lo = max(a, b), min(a, b)
    return (hi + 0.05) / (lo + 0.05)


@pytest.mark.parametrize("path", ["/", "/scorecard", "/demo"])
@pytest.mark.parametrize("theme", ["light", "dark"])
def test_every_visible_text_on_the_reading_pages_meets_wcag_aa(server, browser, path, theme):
    """Each text node's colour against the nearest opaque background behind it, in both themes.
    Ornaments marked aria-hidden are drawings, not text, and are skipped."""
    ctx = browser.new_context(viewport={"width": 1366, "height": 900})
    ctx.add_init_script(f"try{{localStorage.setItem('nota.theme','{theme}')}}catch(e){{}}")
    page = ctx.new_page()
    page.goto(server + path, wait_until="networkidle")
    pairs = page.evaluate(r"""() => {
      const rgb = c => (c.match(/[\d.]+/g) || []).map(Number);
      const hex = a => '#' + a.slice(0, 3).map(n => Math.round(n).toString(16).padStart(2, '0')).join('');
      const bgOf = el => { for (let e = el; e; e = e.parentElement) { const c = rgb(getComputedStyle(e).backgroundColor);
        if (c.length === 3 || c[3] === 1) return hex(c); } return hex(rgb(getComputedStyle(document.documentElement).backgroundColor)); };
      const out = [];
      const walk = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
      for (let n; (n = walk.nextNode());) {
        const el = n.parentElement;
        if (!n.textContent.trim() || el.closest('[aria-hidden="true"], script, style, .skip, .sr, svg, video')) continue;
        const r = el.getBoundingClientRect(), st = getComputedStyle(el);
        if (!r.width || st.visibility === 'hidden' || +st.opacity < 0.5) continue;
        out.push([el.tagName + '.' + el.className, hex(rgb(st.color)), bgOf(el)]);
      }
      return out;
    }""")
    assert len(pairs) > 20
    bad = sorted({(w, fg, bg, round(_contrast(fg, bg), 2)) for w, fg, bg in pairs if _contrast(fg, bg) < 4.5})
    assert bad == [], f"{path} {theme}: {bad[:8]}"
    ctx.close()


@pytest.mark.parametrize("theme", ["light", "dark"])
def test_body_and_muted_text_meet_wcag_aa_in_both_themes(server, browser, theme):
    """Track 2 is scored on working for everyone, so the contrast is measured, not assumed."""
    page, problems = page_with_log(browser, viewport={"width": 1440, "height": 900})
    page.goto(server + "/app", wait_until="networkidle")
    page.wait_for_function("() => document.querySelector('#health').textContent.includes('receipts')", timeout=20000)
    while page.evaluate("document.documentElement.dataset.theme || 'system'") != theme:
        page.click("#theme")
    page.wait_for_timeout(250)

    read = page.evaluate(r"""() => {
      const hex = c => {
        const m = c.match(/\d+/g);
        return '#' + m.slice(0, 3).map(n => (+n).toString(16).padStart(2, '0')).join('');
      };
      const body = getComputedStyle(document.body);
      const muted = document.querySelector('.muted, .meta, h2');
      return {bg: hex(body.backgroundColor), fg: hex(body.color),
              dim: hex(getComputedStyle(muted).color)};
    }""")
    body_ratio = _contrast(read["fg"], read["bg"])
    dim_ratio = _contrast(read["dim"], read["bg"])
    assert body_ratio >= 4.5, f"{theme}: body text {read['fg']} on {read['bg']} is {body_ratio:.2f}:1"
    assert dim_ratio >= 4.5, f"{theme}: secondary text {read['dim']} on {read['bg']} is {dim_ratio:.2f}:1"
    assert problems == [], problems
    page.close()


def test_the_mcp_server_serves_all_four_primitives_over_the_wire(server, browser):
    page, problems = page_with_log(browser)
    ctx = page.request
    caps = ctx.post(server + "/mcp", data={"jsonrpc": "2.0", "id": 1, "method": "initialize",
                                           "params": {"protocolVersion": "2026-07-28", "capabilities": {}}}
                    ).json()["result"]["capabilities"]
    assert set(caps) == {"tools", "resources", "prompts", "completions", "extensions"}

    prompts = ctx.post(server + "/mcp", data={"jsonrpc": "2.0", "id": 2, "method": "prompts/list"}).json()
    assert len(prompts["result"]["prompts"]) == 2
    got = ctx.post(server + "/mcp", data={"jsonrpc": "2.0", "id": 3, "method": "prompts/get",
                                          "params": {"name": "read_a_receipt",
                                                     "arguments": {"id": RECEIPT}}}).json()
    assert RECEIPT in got["result"]["messages"][0]["content"]["text"]

    tpl = ctx.post(server + "/mcp", data={"jsonrpc": "2.0", "id": 4,
                                          "method": "resources/templates/list"}).json()
    assert tpl["result"]["resourceTemplates"][0]["uriTemplate"] == "nota://receipt/{id}"

    comp = ctx.post(server + "/mcp", data={"jsonrpc": "2.0", "id": 5, "method": "completion/complete",
                                           "params": {"ref": {"type": "ref/prompt", "name": "read_a_receipt"},
                                                      "argument": {"name": "id", "value": ""}}}).json()
    assert RECEIPT in comp["result"]["completion"]["values"]

    reg = ctx.get(server + "/.well-known/mcp/server.json").json()
    assert reg["remotes"][0]["url"].endswith("/mcp")
    assert problems == [], problems
    page.close()


def test_the_japanese_landing_is_the_same_page_in_another_language(server, browser):
    """Everything the shared script does must work on the translation too - and the page it draws
    must be the same page: same marks, same failure panel, same receipt, no sideways scroll."""
    page, problems = page_with_log(browser, viewport={"width": 390, "height": 844})
    page.goto(server + "/ja", wait_until="networkidle")

    assert page.locator("html").get_attribute("lang") == "ja"
    assert page.locator("#mark line.tick").count() == 64
    assert page.locator("#mark text.p").text_content() == f"{NEWEST['verdict']['p_up_7d']:.2f}"
    assert page.locator("#ledger figure").count() == min(SHIPPED, 5)  # the landing shows the five newest (landing.js)
    page.wait_for_selector("#broken-panel .bad")
    assert NEWEST["id"] in page.locator(".seal figcaption").inner_text()   # caption follows the mark

    # the stylesheet really arrived: an unstyled page has no serif body and no rules
    assert "serif" in page.evaluate("getComputedStyle(document.body).fontFamily")
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth"), "sideways scroll at 390px"
    assert problems == [], problems
    page.close()


def test_the_mcp_apps_view_completes_the_handshake_and_renders_data_as_text(browser, tmp_path):
    """A real host page embeds the view in a sandboxed iframe, answers ui/initialize, and pushes a
    recorded RYO envelope in as a tool result, with markup planted in the data."""
    from nota.mcp_server import APP_HTML

    env = json.loads((ROOT / "tests" / "fixtures" / "deep_analysis" / "SOL.json").read_text(encoding="utf-8"))
    env["warnings"] = [*env.get("warnings", []), "<script>window.parent.pwned=1</script><img src=x onerror=alert(1)>"]
    page, problems = page_with_log(browser, viewport={"width": 520, "height": 700})
    page.set_content("<!doctype html><body style='margin:0'><iframe id=v sandbox='allow-scripts' "
                     "style='width:500px;height:640px;border:0'></iframe></body>")
    page.evaluate("""([html, env]) => new Promise(done => {
        window.seen = [];
        const f = document.getElementById('v');
        window.addEventListener('message', e => {
          const m = e.data; window.seen.push(m.method || ('reply:' + m.id));
          if (m.method === 'ui/initialize')
            f.contentWindow.postMessage({jsonrpc: '2.0', id: m.id, result: {protocolVersion: '2026-01-26',
              hostCapabilities: {}, hostInfo: {name: 'qa-host', version: '1'}, hostContext: {theme: 'light'}}}, '*');
          if (m.method === 'ui/notifications/initialized')
            f.contentWindow.postMessage({jsonrpc: '2.0', method: 'ui/notifications/tool-result',
              params: {content: [{type: 'text', text: JSON.stringify(env)}], structuredContent: env}}, '*');
          if (m.method === 'ui/notifications/size-changed') done();
        });
        f.srcdoc = html;
    })""", [APP_HTML, env])
    view = page.frame_locator("#v")
    assert view.locator("#headline").inner_text() == env["summary"]["headline"]
    assert view.locator("#status").inner_text().lower() == env["status"]
    assert view.locator("#points li").count() == len(env["summary"]["key_points"])
    assert view.locator("#availability .chip").count() == len(env["availability"])
    assert "<script>" in view.locator("#warnings li").last.inner_text()   # shown as text, not run
    assert view.locator("#warnings script, #warnings img").count() == 0
    assert page.evaluate("window.pwned") is None
    assert page.evaluate("window.seen")[:2] == ["ui/initialize", "ui/notifications/initialized"]
    page.screenshot(path=os.environ.get("NOTA_SHOT_DIR", str(tmp_path)) + "/mcp_app.png")
    assert problems == [], problems
    page.close()


def test_no_page_breaks_its_own_content_security_policy(server, browser):
    """The policy is 'self' only. Any inline handler, eval, third-party font or remote image a page
    still relied on would show here as a violation, and in production as a silently missing piece."""
    for path in ("/", "/ja", "/app", "/scorecard", "/demo"):
        page = browser.new_page(viewport={"width": 1200, "height": 900})
        violations: list[str] = []
        page.on("console", lambda m: violations.append(m.text) if "Content Security Policy" in m.text else None)
        page.on("pageerror", lambda e: violations.append(str(e)) if "Content Security Policy" in str(e) else None)
        resp = page.goto(server + path, wait_until="networkidle")
        assert "default-src 'self'" in resp.headers["content-security-policy"], path
        assert violations == [], (path, violations)
        page.close()


def test_a_head_probe_over_the_wire_matches_get(server):
    """uvicorn, not the test client: the length a player or link preview reads before it asks for bytes."""
    import httpx

    for path in ("/", "/app", "/demo.mp4", "/api/health", f"/r/{RECEIPT}.png"):
        get, head = httpx.get(server + path, timeout=30), httpx.head(server + path, timeout=30)
        assert head.status_code == get.status_code == 200, path
        assert head.content == b"" and head.headers.get("content-length") == get.headers.get("content-length"), path


# --- phones, keyboards, the seal, the theme, and third-party text -----------------------------------

def _rects_intersect(a, b) -> bool:
    return not (a["x"] + a["width"] <= b["x"] or b["x"] + b["width"] <= a["x"]
                or a["y"] + a["height"] <= b["y"] or b["y"] + b["height"] <= a["y"])


@pytest.mark.parametrize("width", [320, 768, 1440])
def test_the_verified_seal_never_covers_the_degraded_banner_or_the_headline(server, browser, width):
    """The receipt where all five RYO sections failed has the banner a reader must be able to read."""
    worst = _most_failed()
    page, problems = page_with_log(browser, viewport={"width": width, "height": 900})
    page.goto(f"{server}/r/{worst['id']}", wait_until="networkidle")
    page.wait_for_selector("#detail .banner")
    page.click("#verify")
    page.wait_for_function("() => document.querySelector('#detail').classList.contains('stamped')", timeout=30000)
    seal = page.locator("#detail .kakuin").bounding_box()
    for sel in ("#detail .banner", "#detail .headline"):
        text = page.evaluate("""s => { const r = document.createRange(); r.selectNodeContents(document.querySelector(s));
          return [...r.getClientRects()].map(q => ({x: q.x, y: q.y, width: q.width, height: q.height})); }""", sel)
        assert text and not any(_rects_intersect(seal, t) for t in text), f"{width}px: the seal covers {sel}"
    assert problems == [], problems
    page.close()


def test_a_tap_on_a_phone_brings_the_receipt_into_view_and_names_it(server, browser):
    ctx = browser.new_context(viewport={"width": 390, "height": 844}, has_touch=True, is_mobile=True,
                              reduced_motion="reduce")
    page = ctx.new_page()
    page.goto(server + "/app", wait_until="networkidle")
    page.wait_for_selector("#list .row")
    target = _receipts()[2]
    page.locator("#list .row").nth(2).tap()
    page.wait_for_url(f"**/r/{target['id']}")
    page.wait_for_function("() => document.activeElement && document.activeElement.id === 'receipt-title'")
    top = page.evaluate("() => document.querySelector('#detail').getBoundingClientRect().top")
    assert 0 <= top <= 200, f"the opened receipt sits {top}px down the screen"
    assert target["id"] in page.title() and target["symbol"] in page.title()
    playwright.expect(page.locator("#sr-status")).to_contain_text(target["id"])
    # the keyboard hints are for keyboards; a touch screen keeps only the theme switch
    assert not page.locator("header .keys .hint").is_visible() and page.locator("#theme").is_visible()
    ctx.close()


def test_enter_on_a_row_keeps_focus_somewhere_and_escape_returns_to_the_list(server, browser):
    page, problems = page_with_log(browser, viewport={"width": 1366, "height": 900})
    page.goto(server + "/app", wait_until="networkidle")
    page.wait_for_selector("#list .row")
    page.locator("#list .row").nth(1).focus()
    page.keyboard.press("Enter")
    page.wait_for_url(f"**/r/{_receipts()[1]['id']}")
    assert page.evaluate("() => document.activeElement.tagName") != "BODY"
    page.keyboard.press("Escape")
    assert page.evaluate("() => document.activeElement.classList.contains('row')")
    page.keyboard.press("j")
    assert page.evaluate("() => document.activeElement.classList.contains('sel')")
    assert problems == [], problems
    page.close()


def test_the_filter_says_when_nothing_matches_and_offers_to_clear(server, browser):
    page, problems = page_with_log(browser, viewport={"width": 1366, "height": 900})
    page.goto(server + "/app", wait_until="networkidle")
    page.wait_for_selector("#list .row")
    page.fill("#filter", "zzz")
    playwright.expect(page.locator("#list")).to_contain_text('No receipt matches "zzz"')
    page.click("#clear-filter")
    playwright.expect(page.locator("#list .row")).to_have_count(SHIPPED)
    page.fill("#filter", "sized")      # words the row shows, not field names
    playwright.expect(page.locator("#list .row")).to_have_count(sum(r["trade"]["kind"] == "trade" for r in _receipts()))
    assert problems == [], problems
    page.close()


def test_the_landing_keeps_the_language_switch_and_a_menu_on_a_phone(server, browser):
    page, problems = page_with_log(browser, viewport={"width": 390, "height": 844})
    page.goto(server + "/", wait_until="networkidle")
    assert page.locator("a[hreflang=ja]").is_visible()
    assert page.locator("nav a.cta").is_visible()
    assert not page.locator("nav .menu a[href='#proof']").is_visible()
    page.click("nav .menu summary")
    assert page.locator("nav .menu a[href='#proof']").is_visible()
    # wide, the same links read as a row with no menu button, on one line
    page.set_viewport_size({"width": 1366, "height": 900})
    page.wait_for_timeout(100)
    assert page.locator("nav .menu a[href='#proof']").is_visible() and not page.locator("nav .menu summary").is_visible()
    assert page.locator("body > nav").bounding_box()["height"] <= 80
    assert problems == [], problems
    page.close()


def test_the_dark_theme_chosen_anywhere_holds_everywhere(server, browser):
    ctx = browser.new_context(viewport={"width": 1366, "height": 900})
    page = ctx.new_page()
    page.goto(server + "/app", wait_until="networkidle")
    page.click("#theme")
    assert page.get_attribute("#theme", "aria-pressed") == "true"
    for path in ("/", "/ja", "/scorecard", "/demo"):
        page.goto(server + path, wait_until="networkidle")
        assert page.evaluate("() => document.documentElement.dataset.theme") == "dark", path
        assert page.get_attribute("#theme", "aria-pressed") == "true", path
    page.click("#theme")          # and switching back on a reading page is what the dashboard opens with
    page.goto(server + "/app", wait_until="networkidle")
    assert page.evaluate("() => document.documentElement.dataset.theme") == "light"
    ctx.close()


def test_the_scorecard_horizon_lives_in_the_url_and_times_say_utc(server, browser):
    page, problems = page_with_log(browser, viewport={"width": 1366, "height": 900})
    page.goto(server + "/scorecard?h=72", wait_until="networkidle")
    assert page.get_attribute(".horizon button[data-h='72']", "aria-pressed") == "true"
    page.click(".horizon button[data-h='24']")
    page.wait_for_url("**/scorecard?h=24")
    cells = page.locator("#open-t td:last-child").all_inner_texts()     # the Locked column
    assert cells and all(c.endswith("UTC") for c in cells), cells[:3]
    assert problems == [], problems
    page.close()


def test_the_skill_runner_sends_a_json_object_argument_as_an_object(server, browser):
    """positioning_check takes RYO's derivatives block. The network call is answered from the newest
    shipped receipt's real positioning_check envelope, so this stays offline."""
    from nota.ledger import Ledger

    led = Ledger(str(DEMO), readonly=True)
    pack = json.loads(led.get_pack(NEWEST["pack_hash"]))
    env = pack["sections"]["positioning_check"]["envelope"]
    sent = {}

    def answer(route):
        sent.update(route.request.post_data_json)
        route.fulfill(status=200, content_type="application/json",
                      body=json.dumps({"name": "positioning_check", "result": env, "latency_ms": 5}))

    page, problems = page_with_log(browser, viewport={"width": 1366, "height": 1100})
    page.route("**/api/skills/positioning_check/invoke", answer)
    page.goto(server + "/app", wait_until="networkidle")
    page.select_option("#skill-name", "positioning_check")
    box = page.locator("#skill-args textarea[data-arg='reference_derivatives']")
    playwright.expect(box).to_have_attribute("placeholder", re.compile("from receipt " + NEWEST["id"]))
    page.fill("#skill-args [data-arg='symbol']", NEWEST["symbol"])
    box.fill("{not json")
    page.click("#skill-run")
    playwright.expect(page.locator("#skill-status")).to_contain_text("reference_derivatives: not valid JSON")
    page.fill("#skill-args [data-arg='atr_stop_pct']", "NaN")
    box.fill(json.dumps(env["request"]["reference_derivatives"]))
    page.click("#skill-run")
    playwright.expect(page.locator("#skill-status")).to_contain_text("atr_stop_pct: expected a number")
    page.fill("#skill-args [data-arg='atr_stop_pct']", "")
    page.click("#skill-run")
    playwright.expect(page.locator("#skill-out")).to_contain_text(env["summary"]["headline"])
    assert sent["args"]["reference_derivatives"] == env["request"]["reference_derivatives"]
    assert problems == [], problems
    page.close()


@pytest.mark.parametrize("path", ["/app", "/scorecard"])
def test_third_party_text_never_becomes_markup(server, browser, path):
    """RYO's words and the ledger's reach these pages through innerHTML templates; a planted tag must
    arrive as text."""
    bad = "<img src=x onerror=window.__x=1>"
    page, problems = page_with_log(browser, viewport={"width": 1366, "height": 900})

    def health(route):
        body = route.fetch().json()
        body["ryo"] = {"status": bad, "tools": bad, "checked_at": body["checked_at"]}
        body["ledger"]["decisions"] = bad
        body["llm"] = {"kind": bad, "model": bad, "key_set": True}
        route.fulfill(json=body)

    def scorecard(route):
        body = route.fetch().json()
        for row in body["open"][:3] + body["settled"][:3]:
            row.update(symbol=bad, verdict=bad, side=bad, confluence_state=bad, result=bad, confluence_score=bad)
        body["coverage"][bad] = 1
        body["lanes"] = {bad: {bad: 1}}
        body["missing_inputs"] = {bad: 2}
        route.fulfill(json=body)

    page.route("**/api/health", health)
    page.route("**/api/scorecard*", scorecard)
    page.goto(server + path, wait_until="networkidle")
    page.wait_for_timeout(300)
    assert page.evaluate("() => window.__x") is None
    assert page.evaluate("() => document.querySelectorAll('img[src=\"x\"]').length") == 0
    shown = page.locator("#health" if path == "/app" else "#cov").inner_text()
    assert "<img" in shown                     # it is there, as text
    assert problems == [], problems
    page.close()


def test_the_landing_verify_button_keeps_focus_while_it_runs(server, browser):
    page, problems = page_with_log(browser, viewport={"width": 1366, "height": 900})
    page.goto(server + "/", wait_until="networkidle")
    page.focus("#verify")
    page.keyboard.press("Enter")
    assert page.evaluate("() => document.activeElement.id") == "verify"
    page.wait_for_function("() => document.querySelector('#verify-out').textContent.includes('identical')", timeout=20000)
    assert page.evaluate("() => document.activeElement.id") == "verify"
    assert page.get_attribute("#verify", "aria-disabled") is None
    assert page.get_attribute("#verify-out", "aria-busy") is None
    # the audit table is the API's numbers, with the receipt they came from named under it
    rid = page.get_attribute("#audit", "data-receipt")
    assert rid and rid in page.locator("#audit-note").inner_text()
    assert "—" not in page.locator("#audit").inner_text()
    assert problems == [], problems
    page.close()


def test_every_page_names_one_canonical_an_absolute_card_and_its_feed(server, browser):
    """What a crawler or a link preview reads from the rendered head, not the raw file."""
    for path in ("/", "/ja", "/app", "/scorecard", "/demo", f"/r/{RECEIPT}"):
        page, problems = page_with_log(browser, viewport={"width": 1200, "height": 900})
        page.goto(server + path, wait_until="domcontentloaded")
        canon = page.eval_on_selector_all('link[rel="canonical"]', "els => els.map(e => e.href)")
        assert canon == [server + path], (path, canon)
        img = page.eval_on_selector_all('meta[property="og:image"]', "els => els.map(e => e.content)")
        assert len(img) == 1 and img[0].startswith(server + "/"), (path, img)
        assert page.request.get(img[0]).status == 200, (path, img)
        feed = page.eval_on_selector('link[type="application/atom+xml"]', "e => e.href")
        assert page.request.get(feed).headers["content-type"].startswith("application/atom+xml")
        assert problems == [], (path, problems)
        page.close()
    page = browser.new_page()
    page.goto(server + "/scorecard", wait_until="domcontentloaded")
    ld = json.loads(page.eval_on_selector('script[type="application/ld+json"]', "e => e.textContent"))
    assert ld["@type"] == "Dataset" and any(d["encodingFormat"] == "text/csv" for d in ld["distribution"])
    assert page.request.get(ld["distribution"][-1]["contentUrl"]).status == 200
    for lang in ("en", "ja", "x-default"):
        page.goto(server + "/ja")
        assert page.eval_on_selector(f'link[hreflang="{lang}"]', "e => e.href").startswith(server)
    page.close()


def test_the_walkthrough_has_a_caption_track_with_one_cue_per_chapter(server, browser):
    page, problems = page_with_log(browser, viewport={"width": 1440, "height": 900})
    page.goto(server + "/demo", wait_until="networkidle")
    chapters = page.evaluate("async () => (await (await fetch('/demo.json')).json()).chapters.length")
    cues = page.evaluate("""() => new Promise(done => {
        const t = document.getElementById('v').textTracks[0];
        t.mode = 'showing';
        const read = () => t.cues && t.cues.length ? done(t.cues.length) : setTimeout(read, 50);
        read();
    })""")
    assert cues == chapters
    assert page.evaluate("() => document.getElementById('v').textTracks[0].kind") == "captions"
    # the spoken line stays in sight inside the transcript box, and the page itself does not move
    page.set_viewport_size({"width": 390, "height": 844})
    page.wait_for_selector("#script button")
    before = page.evaluate("() => window.scrollY")
    page.evaluate("() => { const v = document.getElementById('v'); v.currentTime = v.duration - 2; "
                  "v.dispatchEvent(new Event('timeupdate')); }")
    page.wait_for_timeout(200)
    visible = page.evaluate("""() => {
        const box = document.getElementById('script').getBoundingClientRect();
        const cur = document.querySelector('#script button[aria-current="true"]').getBoundingClientRect();
        return cur.top >= box.top && cur.bottom <= box.bottom + 1;
    }""")
    assert visible and page.evaluate("() => window.scrollY") == before
    assert problems == [], problems
    page.close()


def test_feeds_csv_and_agent_card_answer_over_the_wire(server, browser):
    import defusedxml.ElementTree as DET

    ctx = browser.new_context().request
    atom = DET.fromstring(ctx.get(server + "/feed.xml").body())
    assert len(atom.findall("{http://www.w3.org/2005/Atom}entry")) >= min(50, SHIPPED)
    assert ctx.get(server + "/feed.json").json()["version"] == "https://jsonfeed.org/version/1.1"
    csv_text = ctx.get(server + "/api/scorecard.csv").text()
    locks = sqlite3.connect(DEMO).execute("SELECT COUNT(*) FROM locks").fetchone()[0]
    assert len(csv_text.strip().splitlines()) == 1 + 2 * locks
    card = ctx.get(server + "/.well-known/agent-card.json").json()
    assert card["supportedInterfaces"][0]["url"] == server + "/a2a"
    task = ctx.post(server + "/a2a", headers={"A2A-Version": "1.0"}, data={
        "jsonrpc": "2.0", "id": 1, "method": "SendMessage",
        "params": {"message": {"messageId": "m", "role": "ROLE_USER",
                               "parts": [{"data": {"skill": "verdict_track_record", "args": {"horizon_hours": 24}}}]}}}).json()
    art = task["result"]["task"]["artifacts"][0]["parts"][0]["data"]
    assert art["tool"] == "verdict_track_record"
    assert "Sitemap: " + server + "/sitemap.xml" in ctx.get(server + "/robots.txt").text()
