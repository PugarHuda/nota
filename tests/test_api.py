import json
from pathlib import Path

from fastapi.testclient import TestClient

from nota import api
from nota.decide import decide
from nota.ledger import Ledger
from nota.ryo_client import RecordedRyoClient
from tests.test_decide_replay import make_llm

FIXTURES = Path(__file__).parent / "fixtures"


def _seed(tmp_path, monkeypatch):
    db = str(tmp_path / "t.db")
    monkeypatch.setenv("NOTA_DB", db)
    led = Ledger(db)
    first = decide("SOL", RecordedRyoClient(FIXTURES, name="fixture"), make_llm(action="no_trade", p=0.5), led)
    # second run on perturbed evidence: price up, technicals RSI gone, verdict flips to long
    raw = json.loads(led.get_pack(first.pack_hash))
    da = raw["sections"]["deep_analysis"]["envelope"]["data"]
    da["market"]["price_usd"] = round(da["market"]["price_usd"] * 1.10, 4)
    da["technicals"]["rsi_14"] = None
    from nota.council import run_council
    from nota.evidence import EvidencePack
    from nota.receipt import build_receipt
    from nota.risk import size_trade

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
    assert "Nota" in c.get(f"/r/{second.id}").text


def test_new_section_is_one_availability_row_not_one_row_per_leaf(tmp_path, monkeypatch):
    from nota.council import run_council
    from nota.evidence import EvidencePack
    from nota.receipt import build_receipt
    from nota.risk import size_trade
    from nota.skills.contract import make_envelope

    first, second = _seed(tmp_path, monkeypatch)
    led = Ledger(str(tmp_path / "t.db"))
    raw = json.loads(led.get_pack(second.pack_hash))
    env = make_envelope("price_crosscheck", {}, {"median_usd": 151.0, "sources": [{"name": "a", "price_usd": 151.0}], "spread_pct": 0.1}, {"coingecko": "ok"}, [], "h")
    raw["sections"]["price_check"] = {"tool": "price_crosscheck", "status": "ok", "envelope": json.loads(env.model_dump_json())}
    pack = EvidencePack.model_validate(raw)
    led.save_pack(pack.pack_hash(), pack.symbol, pack.source, pack.model_dump_json())
    council = run_council(pack, make_llm(action="long", p=0.7), led)
    third = build_receipt(pack, council, size_trade(council.verdict, pack))
    led.save_decision(third.id, third.pack_hash, third.symbol, third.model, third.model_dump_json())
    changes = TestClient(api.app).get(f"/api/decisions/{third.id}").json()["changes"]
    paths = [c["path"] for c in changes]
    assert "availability.price_check" in paths and not any(p.startswith("price_check.data") for p in paths)


def test_leaves_treat_scalar_lists_as_sets_and_skip_noise():
    from nota.envelope import Envelope
    from nota.evidence import EvidencePack, Section

    def pack(domains, since):
        env = Envelope(schema_version="1", tool="news_verify", status="ok", data_mode="live", as_of="2026-09-01T00:00:00Z", request={},
                       data={"domains": domains, "since": since, "sources": [{"url": "u", "snippet": since}], "n": 1},
                       summary={"headline": "h", "key_points": []}, availability={}, warnings=[])
        return EvidencePack(symbol="SOL", created_at="x", source="fixture", sections={"news_check": Section(tool="news_verify", status="ok", envelope=env)})

    a = api._leaves(pack(["b.com", "a.com"], "t1"))
    b = api._leaves(pack(["a.com", "b.com"], "t2"))
    assert a == b and a["news_check.data.domains"] == ["a.com", "b.com"] and "news_check.data.since" not in a
    assert api._leaves(pack(["a.com"], "t1"))["news_check.data.domains"] == ["a.com"]


def test_demo_video_is_served_when_bundled(tmp_path, monkeypatch):
    from nota.api import STATIC
    _seed(tmp_path, monkeypatch)
    r = TestClient(api.app).get("/demo.mp4")
    if (STATIC / "demo.mp4").exists():
        assert r.status_code == 200 and r.headers["content-type"] == "video/mp4" and len(r.content) > 100_000
    else:
        assert r.status_code == 404  # a checkout without the bundled video says so instead of erroring
