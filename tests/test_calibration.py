import json
from pathlib import Path

import pytest

from nota.calibration import CannotResolve, resolve, role_scores, role_weights
from nota.council import Citation, Opinion, Verdict
from nota.decide import decide
from nota.envelope import Envelope
from nota.ledger import Ledger
from nota.llm import FakeLLM
from nota.ryo_client import RecordedRyoClient

FIXTURES = Path(__file__).parent / "fixtures"


class PriceSource:
    name = "test"

    def __init__(self, price):
        self.price = price

    def call(self, tool, args=None):
        data = {"market": {"price_usd": self.price}} if self.price is not None else {"market": {"price_usd": None}}
        return Envelope(tool=tool, status="ok", data_mode="live", as_of="2026-09-10T00:00:00Z", request=args or {}, data=data)


def llm():
    op = lambda role, p: (lambda _u: Opinion(role=role, stance="bullish", p_up_7d=p, confidence="medium", thesis="t",
                                             citations=[Citation(path="deep_analysis.data.technical_analysis.rsi_14")], invalidation="i"))
    return FakeLLM({"macro": op("macro", 0.9), "technician": op("technician", 0.6), "narrative": op("narrative", 0.3),
                    "judge": lambda _u: Verdict(action="long", p_up_7d=0.7, rationale="r")})


def test_resolve_scores_brier_and_trade_result():
    led = Ledger(":memory:")
    r = decide("SOL", RecordedRyoClient(FIXTURES), llm(), led)
    entry = r.trade.entry_price  # RYO's recorded price for SOL
    out = resolve(r.id, led, PriceSource(entry * 1.1))  # +10%
    assert out.went_up and out.return_pct == pytest.approx(10.0, abs=0.001)
    assert out.brier == {"macro": 0.01, "technician": 0.16, "narrative": 0.49, "judge": 0.09}
    assert out.trade_result_usd == pytest.approx(entry * 0.1 * r.trade.size_units, abs=0.01)
    assert json.loads(led.get_outcome(r.id))["went_up"] is True
    assert led.unresolved() == []


def test_resolve_refuses_when_price_missing():
    import respx
    from httpx import Response

    led = Ledger(":memory:")
    r = decide("SOL", RecordedRyoClient(FIXTURES), llm(), led)
    with respx.mock:  # RYO has no price and every exchange fallback is down: refuse, never guess
        respx.get(url__regex=r"https://api\.coingecko\.com/.*").mock(return_value=Response(500))
        respx.get(url__regex=r"https://api\.coinbase\.com/.*").mock(return_value=Response(500))
        respx.get(url__regex=r"https://api\.kraken\.com/.*").mock(return_value=Response(500))
        respx.get(url__regex=r"https://data-api\.binance\.vision/.*").mock(return_value=Response(500))
        respx.get(url__regex=r"https://coins\.llama\.fi/.*").mock(return_value=Response(500))
        respx.get(url__regex=r"https://api\.alternative\.me/.*").mock(return_value=Response(500))
        with pytest.raises(CannotResolve):
            resolve(r.id, led, PriceSource(None))


def test_scores_and_weights_favour_low_brier():
    led = Ledger(":memory:")
    r = decide("SOL", RecordedRyoClient(FIXTURES), llm(), led)
    resolve(r.id, led, PriceSource(165.0))
    s = role_scores(led)
    assert s["macro"] == {"n": 1.0, "brier_mean": 0.01}
    w = role_weights(s)
    assert w["macro"] == 1.0 and w["macro"] > w["technician"] > w["narrative"] >= 0.2


def test_weights_default_to_one_without_outcomes():
    assert role_weights({}) == {"macro": 1.0, "technician": 1.0, "narrative": 1.0}


def test_skill_against_the_base_rate_is_filled_once_and_scored_per_role():
    from nota.calibration import fill_base_rates, skill_vs_base

    led = Ledger(":memory:")
    r = decide("SOL", RecordedRyoClient(FIXTURES), llm(), led)
    resolve(r.id, led, PriceSource(165.0))  # went up
    asked = []
    rate = lambda sym, day: asked.append((sym, day)) or 0.6
    assert fill_base_rates(led, rate) == [(r.id, 0.6)] and asked == [("SOL", r.created_at[:10])]
    assert fill_base_rates(led, rate) == [] and len(asked) == 1  # already filled: not counted again
    out = skill_vs_base(led)
    # base Brier (0.6 - 1)^2 = 0.16; judge said 0.7 -> 0.09, so it beat the base rate; narrative 0.3 -> 0.49 did not
    assert out["roles"]["judge"] == {"n": 1, "brier_mean": 0.09, "base_brier_mean": 0.16, "skill": 0.4375}
    assert out["roles"]["narrative"]["skill"] < 0 and out["enough_to_read"] is False


def test_a_base_rate_that_cannot_be_counted_stays_absent():
    from nota.calibration import fill_base_rates, skill_vs_base

    led = Ledger(":memory:")
    r = decide("SOL", RecordedRyoClient(FIXTURES), llm(), led)
    resolve(r.id, led, PriceSource(165.0))
    assert fill_base_rates(led, lambda s, d: None) == [(r.id, None)]
    assert json.loads(led.get_outcome(r.id)).get("base_rate_p") is None and skill_vs_base(led)["roles"] == {}


def test_a_receipt_not_built_on_ryo_evidence_is_never_scored():
    """A hand-built or test-double source may produce a receipt, but no trade and no score."""
    from datetime import datetime, timedelta, timezone

    from nota.calibration import Outcome, due, reliability, skill_vs_base, source_scores

    led = Ledger(":memory:")
    r = decide("SOL", RecordedRyoClient(FIXTURES, name="fixture"), llm(), led)
    assert r.source == "fixture" and r.trade.kind == "blocked" and "not RYO's" in r.trade.reason
    assert due(led, now=datetime.now(timezone.utc) + timedelta(days=30)) == []
    led.save_outcome(r.id, Outcome(decision_id=r.id, symbol="SOL", resolved_at="x", decided_as_of=None, horizon_reached=True,
                                   price_then=1.0, price_now=2.0, return_pct=100.0, went_up=True, brier={"judge": 0.09},
                                   base_rate_p=0.5).model_dump_json())
    assert role_scores(led) == {} and skill_vs_base(led)["roles"] == {}
    assert source_scores(led)["scored_decisions"] == 0 and reliability(led)["n"] == 0
    fresh = Ledger(":memory:")  # same evidence, so the same receipt id: keep the two apart
    real = decide("SOL", RecordedRyoClient(FIXTURES), llm(), fresh)
    assert due(fresh, now=datetime.now(timezone.utc) + timedelta(days=30)) == [real.id]
