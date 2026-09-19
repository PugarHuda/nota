"""The audit table on the landing page carries no figure of its own.

It used to hold four hand-typed rows. Now landing.js fills it from /api/decisions/<id>, whose `audit`
block is read out of the stored evidence pack; these tests hold both ends: the page's table has no
numeric literal left to go stale, and what the API answers is exactly what the pack holds.
"""

import json
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from nota.ledger import Ledger

ROOT = Path(__file__).resolve().parents[1]
DEMO = ROOT / "data" / "demo.db"
AUDIT = re.compile(r'<div class="audit"[^>]*>(.*?)</div>\s*<p id="audit-note"', re.S)


@pytest.mark.parametrize("page", ["landing.html", "landing.ja.html"])
def test_the_audit_table_holds_no_number_typed_into_the_page(page):
    html = (ROOT / "nota" / "static" / page).read_text(encoding="utf-8")
    table = AUDIT.search(html)
    assert table, "the audit table changed shape"
    # the labels ATR(14) and RSI(14) name the method; every value cell is a dash until the API answers
    values = re.findall(r'data-a="[a-z]+\.[a-z]+">([^<]*)<', table.group(1))
    assert len(values) == 9 and set(values) == {"—"}
    assert re.findall(r"\d", re.sub(r"\((14)\)", "", re.sub(r"<[^>]+>", "", table.group(1)))) == []


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("NOTA_DB", str(DEMO))
    monkeypatch.setenv("NOTA_READONLY", "1")
    from nota import api

    return TestClient(api.app)


def test_the_api_audit_is_the_evidence_packs_own_numbers(client):
    led = Ledger(str(DEMO), readonly=True)
    checked = 0
    for d in led.list_decisions(limit=200):
        receipt = json.loads(led.get_decision(d["id"]))
        sections = json.loads(led.get_pack(receipt["pack_hash"]))["sections"]
        audit = client.get(f"/api/decisions/{d['id']}").json()["audit"]
        env = {k: (v.get("envelope") or {}) for k, v in sections.items()}
        if not all(env.get(k) for k in ("technicals_check", "price_check", "deep_analysis")):
            assert audit is None
            continue
        t, p = env["technicals_check"]["data"], env["price_check"]["data"]
        if audit is None:   # a check that ran without RYO's reference has nothing to compare
            assert None in (t["reference"]["atr_14"], t["reference"]["rsi_14"], p["reference"]["price_usd"])
            continue
        assert audit["atr"] == {"nota": t["atr_14"], "ryo": t["reference"]["atr_14"], "warn": t["thresholds"]["atr_warn_pct"]}
        assert audit["rsi"] == {"nota": t["rsi_14"], "ryo": t["reference"]["rsi_14"], "warn": t["thresholds"]["rsi_warn_points"]}
        assert audit["price"] == {"nota": p["median_usd"], "ryo": p["reference"]["price_usd"],
                                  "warn": p["thresholds"]["deviation_warn_pct"]}
        checked += 1
    assert checked, "no shipped receipt holds all three checks"


def test_summaries_count_failed_ryo_sections(client):
    rows = client.get("/api/decisions?limit=200").json()
    worst = max(rows, key=lambda r: r["failed_sections"])
    # the 10 September receipt: the builder key answered 401 and all five RYO sections went to error
    assert worst["failed_sections"] == 5
    assert all(r["failed_sections"] == sum(r["availability"].get(k) in ("error", "unavailable") for k in
               ("market_overview", "sentiment_shift", "deep_analysis", "analyze_token", "compare")) for r in rows)
