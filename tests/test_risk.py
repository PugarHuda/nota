import json
from pathlib import Path

import pytest

from nota.council import Verdict
from nota.evidence import gather
from nota.risk import Blocked, PracticeTrade, RiskLimits, size_trade
from nota.ryo_client import RecordedRyoClient

FIXTURES = Path(__file__).parent / "fixtures"


def pack(symbol="SOL"):
    return gather(RecordedRyoClient(FIXTURES), symbol)


def v(action="long", p=0.65):
    return Verdict(action=action, p_up_7d=p, rationale="r")


def test_no_trade_is_blocked():
    out = size_trade(v("no_trade"), pack())
    assert isinstance(out, Blocked) and "no_trade" in out.reason


def test_missing_primary_evidence_is_blocked():
    out = size_trade(v(), pack("BTC"))
    assert isinstance(out, Blocked) and "primary evidence" in out.reason


def test_low_edge_is_blocked():
    assert isinstance(size_trade(v("long", 0.52), pack()), Blocked)
    assert isinstance(size_trade(v("short", 0.48), pack()), Blocked)  # edge 0.52


def test_missing_atr_is_blocked_not_zeroed():
    p = pack()
    p.sections["deep_analysis"].envelope.data["trade_plan"]["atr_14_usd"] = None
    p.sections["deep_analysis"].envelope.data["technical_analysis"]["atr_14_pct"] = None
    p.sections["analyze_token"].envelope.data["technical_analysis"]["atr_14_pct"] = None
    out = size_trade(v(), p)
    assert isinstance(out, Blocked) and "ATR" in out.reason


def _price_atr():
    """RYO's recorded SOL price and absolute ATR: the numbers the sizing rule is applied to."""
    deep = json.loads((FIXTURES / "deep_analysis" / "SOL.json").read_text(encoding="utf-8"))["data"]
    return deep["market"]["price_usd"], deep["trade_plan"]["atr_14_usd"]


def test_long_sizing_math():
    # stop 2 ATR below, target 3 ATR above; risk 1% of 10000 = 100 USD over a 2-ATR stop distance
    price, atr = _price_atr()
    t = size_trade(v("long", 0.65), pack(), RiskLimits())
    assert isinstance(t, PracticeTrade)
    assert t.side == "long" and t.entry_price == price
    assert t.stop_price == pytest.approx(price - 2 * atr) and t.target_price == pytest.approx(price + 3 * atr)
    units = 100.0 / (2 * atr)
    assert units * price < 2000  # under the 20% position cap, so the full 1% risk is taken
    assert t.risk_usd == 100.0 and t.size_units == pytest.approx(units, abs=1e-5) and t.size_usd == pytest.approx(units * price, abs=0.01)
    assert t.edge == 0.65 and t.source_paths["atr"] == "deep_analysis.data.trade_plan.atr_14_usd"


def test_short_sizing_and_position_cap():
    # cap at 5% of 10000 = 500 USD, so the risk shrinks below 100 USD
    price, atr = _price_atr()
    t = size_trade(v("short", 0.3), pack(), RiskLimits(max_position_pct=5.0))
    assert isinstance(t, PracticeTrade)
    assert t.side == "short" and t.stop_price == pytest.approx(price + 2 * atr) and t.target_price == pytest.approx(price - 3 * atr)
    assert t.size_usd == 500.0 and t.risk_usd == pytest.approx(500.0 / price * 2 * atr, abs=0.01) and t.edge == 0.7


def test_evidence_that_is_not_from_ryo_never_sizes_a_trade():
    p = pack()
    p.source = "fixture"
    out = size_trade(v("long", 0.8), p)
    assert isinstance(out, Blocked) and "not RYO's" in out.reason


def _priced(price, atr):
    pk = pack()
    d = pk.sections["deep_analysis"].envelope.data
    d["market"]["price_usd"] = price
    d["trade_plan"]["atr_14_usd"] = atr
    return pk


def test_blocks_when_atr_puts_a_level_through_zero():
    """A cheap, wildly volatile token would otherwise get a negative stop or target on a receipt."""
    long_ = size_trade(v("long", 0.8), _priced(1.0, 0.6))          # 2x ATR = 1.2 > price
    assert isinstance(long_, Blocked) and "at or below zero" in long_.reason

    short = size_trade(v("short", 0.2), _priced(1.0, 0.4))         # 3x ATR target = 1.2 > price
    assert isinstance(short, Blocked) and "at or below zero" in short.reason

    assert isinstance(size_trade(v("long", 0.8), _priced(100.0, 6.0)), PracticeTrade)


def test_sizing_is_held_against_ryos_own_published_plan():
    """RYO ships an ATR plan of its own. Nota sizes independently and then reports the gap, because
    a provider's plan is evidence about the provider, not an instruction to follow."""
    from pathlib import Path

    from nota.ryo_client import RecordedRyoClient

    recorded = Path(__file__).resolve().parents[1] / "fixtures" / "recorded"
    if not (recorded / "deep_analysis" / "SOL.json").exists():
        pytest.skip("no recorded live fixtures in this checkout")
    live = gather(RecordedRyoClient(recorded, name="recorded"), "SOL")
    t = size_trade(v("long", 0.7), live, RiskLimits())
    assert isinstance(t, PracticeTrade)
    plan = t.vs_ryo_plan
    assert plan["path"] == "deep_analysis.data.trade_plan"
    assert plan["ryo_atr_multiplier"] == 1.5 and plan["nota_atr_multiplier"] == 2.0
    assert plan["agrees_on_direction"] is True          # both long, from the same ATR
    assert plan["stop_diff_pct"] < 0 and plan["target_diff_pct"] > 0   # a wider stop and a further target
    assert "wilder" in plan["method"]

    # and a pack where RYO published no plan reports nothing rather than inventing a comparison
    bare = _priced(100.0, 6.0)
    bare.sections["deep_analysis"].envelope.data.pop("trade_plan")
    assert size_trade(v("long", 0.8), bare, RiskLimits()).vs_ryo_plan is None


def _veto_pack():
    """RYO's real SOL deep_analysis with only the veto flipped on (see the file's _note)."""
    from nota.envelope import Envelope
    from nota.evidence import Section

    raw = json.loads((FIXTURES / "deep_analysis_veto_case.json").read_text(encoding="utf-8"))
    assert raw.pop("_note").startswith("MODIFIED COPY")
    p = pack()
    p.sections["deep_analysis"] = Section(tool="deep_analysis", status=raw["status"], envelope=Envelope.model_validate(raw))
    return p


def test_ryos_own_derivatives_veto_blocks_the_practice_trade():
    p = _veto_pack()
    out = size_trade(v("long", 0.8), p)
    assert isinstance(out, Blocked) and out.reason == "RYO derivatives veto: crowded longs into rising open interest"
    p.sections["deep_analysis"].envelope.data["derivatives"]["veto_reason"] = None
    assert size_trade(v("short", 0.2), p).reason == "RYO derivatives veto: no reason given"
    # a veto the positioning gate ruled not citable does not block
    from nota.envelope import Envelope
    from nota.evidence import Section

    path = "deep_analysis.data.derivatives.veto"
    gate = {"gate": [{"path": path, "verdict": "not_token_specific"}], "withheld_paths": [path]}
    p.sections["positioning_check"] = Section(tool="positioning_check", status="ok", envelope=Envelope(
        tool="positioning_check", status="ok", data_mode="live", as_of="x", request={}, data=gate))
    assert isinstance(size_trade(v("long", 0.8), p), PracticeTrade)


def test_vs_ryo_plan_carries_ryos_squeeze_and_liquidation_read():
    t = size_trade(v("long", 0.7), pack())
    deriv = json.loads((FIXTURES / "deep_analysis" / "SOL.json").read_text(encoding="utf-8"))["data"]["derivatives"]
    assert t.vs_ryo_plan["squeeze_risk"] == deriv["squeeze_risk"] and t.vs_ryo_plan["liquidation_pressure"] == deriv["liquidation_pressure"]
