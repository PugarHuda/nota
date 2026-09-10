"""The audit table on the landing page is the last set of figures typed into that file by hand.

Every other number there is fetched from the API now. These four rows are prose, so they get the
binding the fetched ones get for free: each value must appear in the evidence of a receipt this
repository ships, and the "Apart" column must be the arithmetic it claims to be. A hand-typed
figure that no receipt holds is fabricated data on a page whose argument is that nothing here is.
"""

import json
import re
from pathlib import Path

import pytest

from nota.ledger import Ledger

ROOT = Path(__file__).resolve().parents[1]
ROW = re.compile(r'<div>([^<]+)</div><div class="n">([\d.]+)</div><div class="n">([\d.]+)</div>'
                 r'<div class="agree">([^<]+)</div>')


def _evidence_numbers() -> list[float]:
    """Every numeric literal in every shipped receipt and its evidence pack.

    Compared as numbers, not as text: the page rounds for display, so the evidence holds 68.7 where
    the page prints 68.70, and a substring test would also accept 4.347 inside 14.3479."""
    led = Ledger(str(ROOT / "data" / "demo.db"), readonly=True)
    parts = []
    for d in led.list_decisions(limit=200):
        receipt = led.get_decision(d["id"])
        parts.append(receipt)
        pack = led.get_pack(json.loads(receipt)["pack_hash"])
        if pack:
            parts.append(pack)
    return [float(m) for m in re.findall(r"-?\d+\.?\d*(?:[eE][-+]?\d+)?", "".join(parts))]


def _holds(numbers: list[float], shown: str) -> bool:
    """The page's figure, at the precision the page shows it, is some evidence value rounded."""
    places = len(shown.partition(".")[2])
    return any(round(v, places) == float(shown) for v in numbers)


def test_every_figure_in_the_audit_table_is_one_a_shipped_receipt_holds():
    page = (ROOT / "nota" / "static" / "landing.html").read_text(encoding="utf-8")
    rows = ROW.findall(page)
    assert len(rows) == 3, f"the audit table changed shape: {len(rows)} rows parsed"

    evidence = _evidence_numbers()
    for label, nota, ryo, apart in rows:
        for value in (nota, ryo):
            assert _holds(evidence, value), f"{label}: {value} is on the page but in no shipped receipt"

        gap = abs(float(nota) - float(ryo))
        if apart.strip().endswith("%"):
            stated = float(apart.strip().rstrip("%"))
            assert stated == pytest.approx(gap / float(ryo) * 100, abs=0.01), f"{label}: {apart} is not the gap"
        else:
            stated = float(apart.strip().split()[0])
            assert stated == pytest.approx(gap, abs=0.05), f"{label}: {apart} is not the gap"
