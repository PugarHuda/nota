"""Rebuild the Japanese subset of Dela Gothic One from the characters the pages set in it.

    uv run python scripts/font_subset.py

The display face ships only the CJK characters the site actually draws in it: the /ja headings, and
every ornament that carries a word (seals, section numerals, tabs, the postmark). `display_chars()` is
what the test in tests/test_landing_languages.py checks against, so the list cannot drift from use.
Google Fonts' `text=` parameter returns a font holding exactly those glyphs.
"""

from __future__ import annotations

import re
import urllib.parse
import urllib.request
from pathlib import Path

STATIC = Path(__file__).resolve().parents[1] / "nota" / "static"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0 Safari/537.36"


def _cjk(text: str) -> set[str]:
    return {c for c in text if ord(c) > 0x2FFF}


def display_chars() -> str:
    ja = (STATIC / "landing.ja.html").read_text(encoding="utf-8")
    used = _cjk("".join(t for _, t in re.findall(r"<(h1|h2|strong)[^>]*>(.*?)</\1>", ja, re.S)))
    for page in ("landing.html", "landing.ja.html"):
        html = (STATIC / page).read_text(encoding="utf-8")
        used |= _cjk("".join(re.findall(r'data-tab="([^"]*)"', html)))                     # section tabs
        used |= _cjk("".join(re.findall(r"<svg\b.*?</svg>", html, re.S)))                 # seal and postmark text
        used |= _cjk("".join(re.findall(r'class="kakuin"[^>]*>(.*?)</div>', html, re.S)))  # the 照合済 seal
    used |= _cjk((STATIC / "landing.css").read_text(encoding="utf-8"))   # content: strings of ornaments
    used |= _cjk((STATIC / "index.html").read_text(encoding="utf-8"))    # the dashboard's seals only
    return "".join(sorted(used))


def main() -> None:
    chars = display_chars()
    req = urllib.request.Request("https://fonts.googleapis.com/css2?family=Dela+Gothic+One&text=" + urllib.parse.quote(chars),
                                 headers={"User-Agent": UA})
    css = urllib.request.urlopen(req).read().decode()
    src = re.search(r"url\((\S+?)\)", css).group(1)
    (STATIC / "fonts" / "DelaGothicOne-ja.woff2").write_bytes(urllib.request.urlopen(src).read())
    (STATIC / "fonts" / "DelaGothicOne-ja.txt").write_text(chars + "\n", encoding="utf-8")
    print(f"{len(chars)} characters, {(STATIC / 'fonts' / 'DelaGothicOne-ja.woff2').stat().st_size} bytes")


if __name__ == "__main__":
    main()
