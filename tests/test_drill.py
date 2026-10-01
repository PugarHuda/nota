"""The failure drill: each injected failure ends the way the real pipeline promises, offline, unstored."""

import httpx
import pytest
from fastapi.testclient import TestClient

from nota import api, drill
from nota.ledger import Ledger


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    def refuse(*a, **k):
        raise AssertionError("a drill must not touch the network")
    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", refuse)


def _bad(r):
    return {k: v for k, v in r["receipt"]["availability"].items() if v != "ok"}


def test_healthy_baseline_sizes_a_trade_labelled_drill():
    r = drill.run("healthy")
    assert r["outcome"] == "trade" and r["receipt"]["source"] == "drill" and r["stored"] is False
    assert _bad(r) == {} and r["receipt"]["model"] == drill.MODEL


@pytest.mark.parametrize("scenario", ["ryo_down", "ryo_401"])
def test_ryo_gone_means_every_section_errors_no_council_no_trade(scenario):
    r = drill.run(scenario)
    rc = r["receipt"]
    assert r["outcome"] == "blocked" and rc["source"] == "drill"
    assert all(rc["availability"][k] == "error" for k in drill.SECTIONS)
    assert rc["opinions"] == [] and rc["spend"]["model_calls"] == 0 and rc["verdict"]["action"] == "no_trade"
    assert "deep_analysis" in rc["trade"]["reason"]
    # retried only when retrying can help
    assert r["ryo_calls"]["attempts"] == (25 if scenario == "ryo_down" else 5)


def test_deep_analysis_missing_blocks_with_the_rest_ok():
    r = drill.run("deep_analysis_missing")
    assert _bad(r) == {"deep_analysis": "error"} and r["outcome"] == "blocked" and r["receipt"]["opinions"] == []
    assert any("VALIDATION_FAILED" in w for w in r["receipt"]["warnings"])


def test_rate_limited_retries_into_the_healthy_receipt():
    out = drill.compare("rate_limited")
    d, base = out["drill"], out["baseline"]
    assert d["ryo_calls"]["by_status"] == {"429": 4, "timeout": 1, "200": 5}
    assert d["ryo_calls"]["simulated_wait_s"] >= 60
    assert d["receipt"]["id"] == base["receipt"]["id"] and d["outcome"] == "trade"


def test_exchanges_down_never_replace_ryo_price():
    r = drill.run("exchange_down")
    pc = _bad(r)
    assert pc == {"price_check": "unavailable"} and r["outcome"] == "trade"
    assert r["receipt"]["trade"]["source_paths"]["price"] == "deep_analysis.data.market.price_usd"


def test_llm_down_is_a_clear_error_and_no_verdict():
    r = drill.run("llm_down")
    assert r["outcome"] == "error" and r["receipt"] is None and "LLM HTTP 503" in r["error"]
    assert r["throwaway_ledger"] == {"evidence_packs": 1, "decisions": 0}


def test_partial_keeps_nulls_null_and_refuses_to_assume_atr():
    r = drill.run("partial")
    rc = r["receipt"]
    assert _bad(r) == {"market_overview": "partial", "deep_analysis": "partial", "analyze_token": "partial"}
    assert "refusing to assume" in rc["trade"]["reason"]
    for o in rc["opinions"]:   # citations to the nulled RSI, ATR and Fear & Greed are dropped, price survives
        assert o["dropped_citations"] == 3 and [c["path"] for c in o["citations"]] == ["deep_analysis.data.market.price_usd"]


def test_api_runs_a_drill_and_writes_nothing_to_the_configured_ledger(tmp_path, monkeypatch):
    db = tmp_path / "nota.db"
    monkeypatch.setenv("NOTA_DB", str(db))
    Ledger(str(db))
    c = TestClient(api.app)
    body = c.post("/api/drill", json={"scenario": "ryo_401"}).json()
    assert body["drill"]["receipt"]["source"] == "drill" and body["baseline"]["receipt"]["source"] == "drill"
    assert body["drill"]["real_receipt"] == "2ae531ec2c9b" and body["drill"]["what_happened"]
    led = Ledger(str(db))
    assert [led.conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in ("evidence", "decisions", "llm_cache")] == [0, 0, 0]
    assert c.post("/api/drill", json={"scenario": "nope"}).status_code == 422


def test_api_drill_is_throttled_per_address():
    c = TestClient(api.app)
    for _ in range(api.DRILL_PER_IP):
        api._throttle_n("drill:testclient", 1, api.DRILL_PER_IP)
    assert c.post("/api/drill", json={"scenario": "partial"}).status_code == 429


def test_drill_page_is_served_and_listed(tmp_path, monkeypatch):
    monkeypatch.setenv("NOTA_DB", str(tmp_path / "nota.db"))
    c = TestClient(api.app)
    assert c.get("/drill").status_code == 200
    assert "/drill" in c.get("/sitemap.xml").text and "/drill" in c.get("/llms.txt").text
    for page in ("/", "/judges"):
        assert 'href="/drill"' in c.get(page).text
