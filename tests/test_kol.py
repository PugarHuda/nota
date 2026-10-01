"""Multi-KOL agent: the rule engine, its replay, the CLI and the API. Offline: the narrative envelope is
canned and RYO's evidence is the recorded SOL deep_analysis."""

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from nota import api, kol
from nota.envelope import Envelope
from nota.ledger import Ledger
from nota.risk import Blocked, PracticeTrade
from nota.ryo_client import RecordedRyoClient

FIXTURES = Path(__file__).parent / "fixtures"
RYO = RecordedRyoClient(FIXTURES)


def row(symbol="SOL", converging=True, direction="bullish", signed=3, sentiment=0.6, conviction=0.4, urgency=0.4):
    return {"symbol": symbol, "voices": [], "voice_count": signed, "mentions": signed, "sentiment_mean": sentiment,
            "sentiment_samples": signed, "conviction_mean": conviction, "urgency_max": urgency, "direction": direction,
            "signed_voices": signed, "converging": converging, "coverage": 1.0, "samples": []}


def env(*rows):
    voices = [{"id": "tg:alpha", "status": "available", "messages": 5, "fetched": 5, "via": "telegram_preview", "coverage": "full"},
              {"id": "x:beta", "status": "partial", "messages": 2, "fetched": 2, "via": "tavily_search", "coverage": "partial"}]
    return Envelope(tool="narrative_convergence", status="partial", data={"voices": voices, "tokens": list(rows)})


def sol_pack():
    return kol.gather_primary(RYO, "SOL")


RULES, LIMITS = kol.KolRules(), kol.KolLimits()


def test_no_convergence_no_signal_and_the_trail_says_which_rule_failed():
    [d] = kol.evaluate(env(row(converging=False, signed=1)), RULES, LIMITS, {"SOL": sol_pack()}, [])
    assert d.signal == "none" and d.trade is None
    failed = [r.rule for r in d.rules if not r.passed]
    assert failed == ["voices converge (two or more with a stance, all the same sign)", "voices with a stance >= min_voices"]
    assert d.reason.startswith("failed: voices converge")


def test_conflicting_signs_do_not_signal():
    [d] = kol.evaluate(env(row(converging=False, direction="mixed", signed=2, sentiment=0.05)), RULES, LIMITS, {"SOL": sol_pack()}, [])
    assert d.signal == "none" and not d.rules[0].passed


def test_null_stays_null_and_fails_the_rule():
    [d] = kol.evaluate(env(row(sentiment=None)), RULES, LIMITS, {}, [])
    rule = next(r for r in d.rules if r.rule.startswith("|sentiment_mean|"))
    assert rule.value is None and rule.passed is False and d.signal == "none"
    [d] = kol.evaluate(env({"symbol": "SOL", "converging": True, "direction": "bullish"}), RULES, LIMITS, {}, [])  # an older envelope
    assert next(r for r in d.rules if "min_voices" in r.rule).value is None and d.signal == "none"


def test_signal_sizes_a_trade_on_ryos_atr_with_the_users_limits():
    deep = json.loads((FIXTURES / "deep_analysis" / "SOL.json").read_text(encoding="utf-8"))["data"]
    price, atr = deep["market"]["price_usd"], deep["trade_plan"]["atr_14_usd"]
    limits = kol.KolLimits(account_usd=5000, risk_per_trade_pct=0.5, max_open_positions=1)
    [d] = kol.evaluate(env(row(direction="bearish", sentiment=-0.6)), RULES, limits, {"SOL": sol_pack()}, [])
    t = d.trade
    assert d.signal == "short" and isinstance(t, PracticeTrade) and t.side == "short"
    assert t.entry_price == price and t.stop_price == pytest.approx(price + 2 * atr) and t.target_price == pytest.approx(price - 3 * atr)
    assert t.risk_usd == pytest.approx(25.0, abs=0.01) and t.edge is None  # 0.5% of 5000; no probability was estimated
    assert t.source_paths["atr"] == "deep_analysis.data.trade_plan.atr_14_usd" and "every rule passed" in d.reason


def test_no_ryo_atr_means_no_trade_never_an_invented_stop():
    [d] = kol.evaluate(env(row()), RULES, LIMITS, {}, [])
    assert d.signal == "long" and isinstance(d.trade, Blocked) and "no RYO evidence" in d.trade.reason
    pack = sol_pack()
    pack.sections["deep_analysis"].envelope.data["trade_plan"]["atr_14_usd"] = None
    pack.sections["deep_analysis"].envelope.data["technical_analysis"]["atr_14_pct"] = None
    [d] = kol.evaluate(env(row()), RULES, LIMITS, {"SOL": pack}, [])
    assert isinstance(d.trade, Blocked) and "ATR" in d.trade.reason
    [d] = kol.evaluate(env(row(symbol="BTC")), RULES, LIMITS, {"BTC": kol.gather_primary(RYO, "BTC")}, [])  # no recording: RYO error
    assert isinstance(d.trade, Blocked) and "deep_analysis" in d.trade.reason


def test_risk_limit_and_holdings_block_the_trade_but_keep_the_signal():
    limits = kol.KolLimits(max_open_positions=2)
    [d] = kol.evaluate(env(row()), RULES, limits, {"SOL": sol_pack()}, ["BTC", "ETH"])
    assert d.signal == "long" and "risk limit reached: 2 open" in d.trade.reason
    [d] = kol.evaluate(env(row()), RULES, limits, {"SOL": sol_pack()}, ["SOL"])
    assert "already holding" in d.trade.reason
    # slots fill in envelope order: the second signal finds the limit taken by the first
    a, b = kol.evaluate(env(row(), row(symbol="ETH")), RULES, kol.KolLimits(max_open_positions=1), {"SOL": sol_pack()}, [])
    assert isinstance(a.trade, PracticeTrade) and "risk limit reached: 1 open" in b.trade.reason


def test_allowed_tokens_and_optional_urgency_are_rules_in_the_trail():
    rules = kol.KolRules(tokens=["eth"], min_urgency=0.8)
    [d] = kol.evaluate(env(row()), rules, LIMITS, {}, [])
    trail = {r.rule: r for r in d.rules}
    assert trail["token is allowed"].threshold == ["ETH"] and not trail["token is allowed"].passed
    assert trail["urgency_max >= min_urgency"].value == 0.4 and not trail["urgency_max >= min_urgency"].passed


def fake_narrative(*rows):
    return lambda voices, tokens=None, hours=24: env(*rows)


def test_run_stores_and_replays_identically_offline(tmp_path):
    led = Ledger(str(tmp_path / "k.db"))
    r, packs = kol.run(["tg:alpha", "x:beta"], RULES, LIMITS, RYO, [], narrative=fake_narrative(row(), row(symbol="ETH", converging=False)))
    assert set(packs) == {"SOL"}  # RYO is asked only about the token that signalled
    kol.save(led, r, packs)
    assert kol.held_symbols(led) == ["SOL"]
    res = kol.replay(r.id, led)
    assert res["identical"] and res["diff"] == []
    # a tampered decision is caught
    stored = json.loads(led.get_kol(r.id))
    stored["decisions"][0]["signal"] = "short"
    led.conn.execute("UPDATE kol_runs SET run_json=? WHERE id=?", (json.dumps(stored), r.id))
    assert not kol.replay(r.id, led)["identical"]


def test_cli_kol_runs_prints_the_trail_and_replays(tmp_path, monkeypatch):
    from nota.cli import app

    monkeypatch.setenv("NOTA_DB", str(tmp_path / "c.db"))
    monkeypatch.setattr(kol, "narrative_convergence", fake_narrative(row()))
    runner = CliRunner()
    out = runner.invoke(app, ["kol", "--voices", "tg:alpha,x:beta", "--min-voices", "2", "--source", "recorded"])
    assert out.exit_code == 0, out.output
    assert "via tavily_search, coverage partial" in out.output and "[pass] voices converge" in out.output
    rid = Ledger(str(tmp_path / "c.db")).list_kol()[0][0]
    again = runner.invoke(app, ["kol-replay", rid])
    assert again.exit_code == 0 and "identical: True" in again.output
    assert runner.invoke(app, ["kol", "--voices", "tg:a", "--min-voices", "1"]).exit_code != 0


def test_api_runs_stores_lists_replays_and_validates(tmp_path, monkeypatch):
    monkeypatch.setenv("NOTA_DB", str(tmp_path / "a.db"))
    for k in ("RYO_MCP_KEY", "DATABASE_URL", "NOTA_READONLY"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setattr(kol, "narrative_convergence", fake_narrative(row()))
    c = TestClient(api.app)
    body = {"voices": ["tg:alpha", "x:beta"], "rules": {"min_voices": 2}, "limits": {"max_open_positions": 2}}
    no_key = c.post("/api/kol/run", json=body).json()
    assert no_key["stored"] is True and "no RYO builder key" in no_key["ryo_note"]
    assert "no RYO evidence" in no_key["run"]["decisions"][0]["trade"]["reason"]

    monkeypatch.setenv("RYO_MCP_KEY", "test")
    monkeypatch.setattr(api.live, "RyoClient", lambda: RYO)
    ok = c.post("/api/kol/run", json=body).json()
    trade = ok["run"]["decisions"][0]["trade"]
    assert ok["stored"] is True and trade["kind"] == "trade" and trade["edge"] is None
    listed = c.get("/api/kol").json()
    assert listed[0]["id"] == ok["run"]["id"] and listed[0]["trades"] == ["SOL"]
    got = c.get(f"/api/kol/{ok['run']['id']}").json()
    assert got["replay"]["identical"] is True
    assert c.get("/api/kol/nope").status_code == 404

    assert c.post("/api/kol/run", json={"voices": [f"tg:v{i}" for i in range(21)]}).status_code == 422
    assert c.post("/api/kol/run", json={"voices": ["tg:a"], "rules": {"min_voices": 1}}).status_code == 422
    assert c.post("/api/kol/run", json={"voices": ["tg:a"], "limits": {"risk_per_trade_pct": 50}}).status_code == 422
    assert c.post("/api/kol/run", json={"voices": ["tg:a"], "rules": {"tokens": ["not a symbol!"]}}).status_code == 422


def test_api_read_only_demo_returns_the_run_unstored(tmp_path, monkeypatch):
    db = tmp_path / "ro.db"
    Ledger(str(db)).conn.close()  # the snapshot exists, checkpointed, before it is opened read-only
    monkeypatch.setenv("NOTA_DB", str(db))
    monkeypatch.setenv("NOTA_READONLY", "1")
    monkeypatch.delenv("RYO_MCP_KEY", raising=False)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setattr(kol, "narrative_convergence", fake_narrative(row()))
    res = TestClient(api.app).post("/api/kol/run", json={"voices": ["tg:alpha", "tg:beta"]}).json()
    assert res["stored"] is False and "read-only" in res["note"] and res["run"]["decisions"][0]["signal"] == "long"
    assert sqlite_count(db) == 0


def sqlite_count(db):
    import sqlite3

    return sqlite3.connect(db).execute("SELECT COUNT(*) FROM kol_runs").fetchone()[0]


def test_api_kol_is_throttled_by_what_it_fetches(tmp_path, monkeypatch):
    monkeypatch.setenv("NOTA_DB", str(tmp_path / "t.db"))
    for k in ("RYO_MCP_KEY", "DATABASE_URL", "NOTA_READONLY"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setattr(kol, "narrative_convergence", fake_narrative())
    c = TestClient(api.app)
    twenty_x = {"voices": [f"x:h{i}" for i in range(20)]}  # 40 units each: an x: voice may fall back to Tavily
    assert c.post("/api/kol/run", json=twenty_x).status_code == 200
    r = c.post("/api/kol/run", json=twenty_x)
    assert r.status_code == 429 and int(r.headers["Retry-After"]) > 0


def test_kol_page_is_served_and_listed(tmp_path, monkeypatch):
    monkeypatch.setenv("NOTA_DB", str(tmp_path / "p.db"))
    c = TestClient(api.app)
    page = c.get("/kol")
    assert page.status_code == 200 and "Multi-KOL narrative agent" in page.text and 'rel="canonical"' in page.text
    assert "/kol</loc>" in c.get("/sitemap.xml").text
    assert "/kol)" in c.get("/llms.txt").text and "/api/kol)" in c.get("/llms.txt").text
    assert 'href="/kol"' in c.get("/judges").text and 'href="/kol"' in c.get("/").text
