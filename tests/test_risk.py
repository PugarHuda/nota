from pathlib import Path

import pytest

from nota.council import Verdict
from nota.evidence import gather
from nota.risk import Blocked, PracticeTrade, RiskLimits, size_trade
from nota.ryo_client import RecordedRyoClient

FIXTURES = Path(__file__).parent / "fixtures"


def pack(symbol="SOL"):
    return gather(RecordedRyoClient(FIXTURES, name="fixture"), symbol)


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
    p.sections["deep_analysis"].envelope.data["technicals"]["atr_14"] = None
    p.sections["analyze_token"].envelope.data["technicals"]["atr_14"] = None
    out = size_trade(v(), p)
    assert isinstance(out, Blocked) and "ATR" in out.reason


def test_long_sizing_math():
    # price 150, ATR 6, stop mult 2 -> stop distance 12, risk 1% of 10000 = 100 -> 8.3333 units = 1250 USD
    t = size_trade(v("long", 0.65), pack(), RiskLimits())
    assert isinstance(t, PracticeTrade)
    assert t.side == "long" and t.entry_price == 150.0 and t.stop_price == 138.0 and t.target_price == 168.0
    assert t.risk_usd == 100.0 and t.size_units == pytest.approx(8.3333, abs=1e-3) and t.size_usd == 1250.0
    assert t.edge == 0.65 and t.source_paths["atr"] == "deep_analysis.data.technicals.atr_14"


def test_short_sizing_and_position_cap():
    # cap at 5% of 10000 = 500 USD -> 3.3333 units, risk shrinks to 40 USD
    t = size_trade(v("short", 0.3), pack(), RiskLimits(max_position_pct=5.0))
    assert isinstance(t, PracticeTrade)
    assert t.side == "short" and t.stop_price == 162.0 and t.target_price == 132.0
    assert t.size_usd == 500.0 and t.risk_usd == 40.0 and t.edge == 0.7



def _priced(price, atr):
    pk = pack()
    d = pk.sections["deep_analysis"].envelope.data
    d["market"]["price_usd"] = price
    d["technicals"]["atr_14"] = atr
    return pk


def test_blocks_when_atr_puts_a_level_through_zero():
    """A cheap, wildly volatile token would otherwise get a negative stop or target on a receipt."""
    long_ = size_trade(v("long", 0.8), _priced(1.0, 0.6))          # 2x ATR = 1.2 > price
    assert isinstance(long_, Blocked) and "at or below zero" in long_.reason

    short = size_trade(v("short", 0.2), _priced(1.0, 0.4))         # 3x ATR target = 1.2 > price
    assert isinstance(short, Blocked) and "at or below zero" in short.reason

    assert isinstance(size_trade(v("long", 0.8), _priced(100.0, 6.0)), PracticeTrade)
