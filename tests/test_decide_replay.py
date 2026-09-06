from pathlib import Path

from arena.council import Citation, Opinion, Verdict
from arena.decide import decide
from arena.ledger import Ledger
from arena.llm import FakeLLM
from arena.receipt import Receipt, render_markdown
from arena.replay import replay
from arena.risk import PracticeTrade
from arena.ryo_client import RecordedRyoClient

FIXTURES = Path(__file__).parent / "fixtures"


def make_llm(action="long", p=0.65):
    op = lambda role: (lambda _u: Opinion(role=role, stance="bullish", p_up_7d=0.6, confidence="medium", thesis="t",
                                          citations=[Citation(path="deep_analysis.data.technicals.rsi_14", value="61.3")], invalidation="i"))
    return FakeLLM({"macro": op("macro"), "technician": op("technician"), "narrative": op("narrative"),
                    "judge": lambda _u: Verdict(action=action, p_up_7d=p, rationale="r")})


def test_decide_stores_pack_and_receipt_and_renders():
    led = Ledger(":memory:")
    r = decide("sol", RecordedRyoClient(FIXTURES, name="fixture"), make_llm(), led)
    assert isinstance(r.trade, PracticeTrade) and r.symbol == "SOL" and r.source == "fixture"
    assert led.get_pack(r.pack_hash) is not None
    assert Receipt.model_validate_json(led.get_decision(r.id)).id == r.id
    md = render_markdown(r)
    assert "Decision receipt" in md and "LONG" in md and "as_of 2026-09-01" in md and "No order was placed" in md


def test_same_evidence_same_receipt_id():
    led = Ledger(":memory:")
    a = decide("SOL", RecordedRyoClient(FIXTURES, name="fixture"), make_llm(), led)
    b = decide("SOL", RecordedRyoClient(FIXTURES, name="fixture"), make_llm(), led)
    assert a.id == b.id and b.cache_hits == 4


def test_replay_survives_a_prompt_version_bump(monkeypatch):
    from arena import council

    led = Ledger(":memory:")
    r = decide("SOL", RecordedRyoClient(FIXTURES, name="fixture"), make_llm(), led)
    monkeypatch.setattr(council, "PROMPT_VERSION", "v99")  # a later prompt bump must not orphan old receipts
    drifted = make_llm(action="no_trade", p=0.5)
    res = replay(r.id, led, drifted)
    assert res.identical and drifted.calls == [] and res.replayed.id == r.id and res.replayed.prompt_version == r.prompt_version


def test_replay_is_identical_from_cache_and_fresh_reports_drift():
    led = Ledger(":memory:")
    r = decide("SOL", RecordedRyoClient(FIXTURES, name="fixture"), make_llm(), led)
    drifted = make_llm(action="no_trade", p=0.5)
    res = replay(r.id, led, drifted)  # cached: the drifted model is never consulted
    assert res.identical and res.diff == [] and drifted.calls == []
    fresh = replay(r.id, led, drifted, fresh=True)
    assert not fresh.identical
    assert any(d.startswith("verdict.action: 'long' -> 'no_trade'") for d in fresh.diff)
    assert any(d.startswith("trade.kind") for d in fresh.diff)
    # fresh replay must not have overwritten the original cached outputs
    again = replay(r.id, led, drifted)
    assert again.identical
