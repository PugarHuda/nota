import json
from pathlib import Path

from fastapi.testclient import TestClient

from arena import api
from arena.decide import decide
from arena.ledger import Ledger
from arena.ryo_client import RecordedRyoClient
from tests.test_decide_replay import make_llm

FIXTURES = Path(__file__).parent / "fixtures"


def _seed(tmp_path, monkeypatch):
    db = str(tmp_path / "t.db")
    monkeypatch.setenv("ARENA_DB", db)
    led = Ledger(db)
    first = decide("SOL", RecordedRyoClient(FIXTURES, name="fixture"), make_llm(action="no_trade", p=0.5), led)
    # second run on perturbed evidence: price up, technicals RSI gone, verdict flips to long
    raw = json.loads(led.get_pack(first.pack_hash))
    da = raw["sections"]["deep_analysis"]["envelope"]["data"]
    da["market"]["price_usd"] = round(da["market"]["price_usd"] * 1.10, 4)
    da["technicals"]["rsi_14"] = None
    from arena.council import run_council
    from arena.evidence import EvidencePack
    from arena.receipt import build_receipt
    from arena.risk import size_trade

    pack = EvidencePack.model_validate(raw)
    led.save_pack(pack.pack_hash(), pack.symbol, pack.source, pack.model_dump_json())
    council = run_council(pack, make_llm(action="long", p=0.7), led)
    second = build_receipt(pack, council, size_trade(council.verdict, pack))
    led.conn.execute("UPDATE decisions SET created_at='2020-01-01T00:00:00+00:00' WHERE id=?", (first.id,))
    led.save_decision(second.id, second.pack_hash, second.symbol, second.model, second.model_dump_json())
    return first, second


def test_list_detail_diff_positions_scores(tmp_path, monkeypatch):
    first, second = _seed(tmp_path, monkeypatch)
    c = TestClient(api.app)
    rows = c.get("/api/decisions").json()
    assert [r["id"] for r in rows] == [second.id, first.id] and rows[0]["action"] == "long"

    d = c.get(f"/api/decisions/{second.id}").json()
    assert d["previous_id"] == first.id
    paths = [ch["path"] for ch in d["changes"]]
    # verdict flip and trade unlock outrank everything; price (feeds sizing) outranks a plain leaf; null is reported, not 0
    assert paths[:2] == ["verdict.action", "trade.kind"]
    assert paths.index("deep_analysis.data.market.price_usd") < paths.index("deep_analysis.data.technicals.rsi_14")
    rsi = next(ch for ch in d["changes"] if ch["path"].endswith("rsi_14"))
    assert rsi["after"] is None and rsi["why"] == "value became unavailable"
    assert c.get(f"/api/decisions/{first.id}").json()["changes"] == []
    assert c.get("/api/decisions/nope").status_code == 404

    pos = c.get("/api/positions").json()
    assert len(pos) == 1 and pos[0]["symbol"] == "SOL" and pos[0]["side"] == "long" and pos[0]["move_pct"] == 0.0

    s = c.get("/api/scores").json()
    assert s["scores"] == {} and s["unresolved"] == 2 and set(s["weights"]) == {"macro", "technician", "narrative"}
    assert "RYO Arena" in c.get(f"/r/{second.id}").text


def test_leaves_treat_scalar_lists_as_sets_and_skip_noise():
    from arena.envelope import Envelope
    from arena.evidence import EvidencePack, Section

    def pack(domains, since):
        env = Envelope(schema_version="1", tool="news_verify", status="ok", data_mode="live", as_of="2026-09-01T00:00:00Z", request={},
                       data={"domains": domains, "since": since, "sources": [{"url": "u", "snippet": since}], "n": 1},
                       summary={"headline": "h", "key_points": []}, availability={}, warnings=[])
        return EvidencePack(symbol="SOL", created_at="x", source="fixture", sections={"news_check": Section(tool="news_verify", status="ok", envelope=env)})

    a = api._leaves(pack(["b.com", "a.com"], "t1"))
    b = api._leaves(pack(["a.com", "b.com"], "t2"))
    assert a == b and a["news_check.data.domains"] == ["a.com", "b.com"] and "news_check.data.since" not in a
    assert api._leaves(pack(["a.com"], "t1"))["news_check.data.domains"] == ["a.com"]
