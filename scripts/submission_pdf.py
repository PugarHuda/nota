"""Render docs/project-submission-form.md to a committed PDF.

    uv run python scripts/submission_pdf.py

The organiser wants the submission form as a PDF in the repo but their own PDF link serves the
site's SPA shell (see the note at the top of the Markdown), so we render our filled copy.
markdown-it-py already ships with rich, and Chromium already ships with the Playwright dev dep.
"""

from __future__ import annotations

from pathlib import Path

from markdown_it import MarkdownIt
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "docs" / "project-submission-form.md"
OUT = SRC.with_suffix(".pdf")

CSS = """
body { font: 11pt/1.5 "Segoe UI", system-ui, sans-serif; color: #111; margin: 0; }
h1 { font-size: 18pt; margin: 0 0 .6em; }
h2 { font-size: 13pt; margin: 1.4em 0 .4em; border-bottom: 1px solid #ddd; padding-bottom: .2em; }
table { border-collapse: collapse; width: 100%; font-size: 10pt; }
th, td { border: 1px solid #ccc; padding: .35em .5em; text-align: left; vertical-align: top; }
th { background: #f4f4f4; }
code { font-family: Consolas, monospace; font-size: .92em; background: #f4f4f4; padding: 0 .2em; }
blockquote { border-left: 3px solid #bbb; margin: 0 0 1em; padding: .2em 0 .2em 1em; color: #444; }
a { color: #06c; word-break: break-all; }
"""


def main() -> None:
    html = f"<style>{CSS}</style>" + MarkdownIt("commonmark").enable("table").render(
        SRC.read_text(encoding="utf-8")
    )
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        page.set_content(html, wait_until="load")
        page.pdf(path=str(OUT), format="A4", print_background=True,
                 margin={"top": "18mm", "bottom": "18mm", "left": "16mm", "right": "16mm"})
        browser.close()
    print(f"wrote {OUT.relative_to(ROOT)} ({OUT.stat().st_size} bytes)")


if __name__ == "__main__":
    main()
