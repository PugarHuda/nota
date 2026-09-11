"""Two languages of one page, held to the same structure and the same honesty rules.

A translation is where a site quietly grows a second, worse version of itself: the English page
gains a section and the other one silently stops matching. These bind them - same sections, same
ids, same behaviour file - so a page that drifts fails a test instead of a reader.
"""

import re
from pathlib import Path

import pytest

from nota.ledger import Ledger
from tests.test_landing_numbers import ROW, _evidence_numbers, _holds

ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "nota" / "static"
PAGES = {"en": STATIC / "landing.html", "ja": STATIC / "landing.ja.html"}


def _read(lang: str) -> str:
    return PAGES[lang].read_text(encoding="utf-8")


def _ids(page: str) -> set[str]:
    return set(re.findall(r'id="([a-z0-9-]+)"', page))


@pytest.mark.parametrize("lang", sorted(PAGES))
def test_each_language_shares_the_one_stylesheet_and_the_one_script(lang):
    """The prose is translated; the machinery is the same object, not a copy of it."""
    page = _read(lang)
    assert '<link rel="stylesheet" href="/landing.css">' in page
    assert '<script src="/landing.js" defer></script>' in page
    assert "<style>" not in page and "<script>" not in page, "behaviour or style was inlined again"
    # and that one script is what reads the ledger both pages draw
    assert "/api/decisions?limit=" in (STATIC / "landing.js").read_text(encoding="utf-8")


def test_the_two_pages_carry_the_same_sections():
    en, ja = _ids(_read("en")), _ids(_read("ja"))
    assert en == ja, f"only in English: {sorted(en - ja)} / only in Japanese: {sorted(ja - en)}"
    # the ids the shared script writes into
    assert {"mark", "ledger", "broken-panel", "broken-verdict", "verify", "verify-out"} <= en


def test_each_language_declares_itself_and_points_at_the_other():
    en, ja = _read("en"), _read("ja")
    assert '<html lang="en">' in en and 'href="/ja"' in en
    assert '<html lang="ja">' in ja and 'href="/"' in ja


@pytest.mark.parametrize("lang", sorted(PAGES))
def test_no_language_states_a_number_of_its_own(lang):
    """The rule that holds for the English page holds for every translation of it: no evidence hash
    and no receipt id written into the file, because a fact typed by hand can be wrong and a fact
    fetched from the ledger cannot."""
    page = _read(lang)
    assert re.findall(r"\b[0-9a-f]{64}\b", page) == [], "an evidence hash is written into the page"
    body = page.split("</head>", 1)[1]
    ids = set(re.findall(r"\b[0-9a-f]{12}\b", body))
    led = Ledger(str(ROOT / "data" / "demo.db"), readonly=True)
    shipped = {d["id"] for d in led.list_decisions(limit=200)}
    # One receipt is quoted on purpose, in the transcript showing what a shared card looks like.
    assert ids <= shipped, f"receipt ids on the page that the ledger does not hold: {sorted(ids - shipped)}"
    assert len(ids) <= 1, f"the page names receipts it should be fetching: {sorted(ids)}"


@pytest.mark.parametrize("lang", sorted(PAGES))
def test_the_audit_table_holds_in_every_language(lang):
    """The figures are the same in both; only the row labels are translated."""
    rows = ROW.findall(_read(lang))
    assert len(rows) == 3
    evidence = _evidence_numbers()
    for label, nota, ryo, apart in rows:
        for value in (nota, ryo):
            assert _holds(evidence, value), f"{lang}: {label} {value} is in no shipped receipt"
        gap = abs(float(nota) - float(ryo))
        if apart.strip().endswith("%"):
            assert float(apart.strip().rstrip("%")) == pytest.approx(gap / float(ryo) * 100, abs=0.01)
        else:
            assert float(apart.strip().split()[0]) == pytest.approx(gap, abs=0.05)
