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
                                             citations=[Citation(path="deep_analysis.data.technicals.rsi_14")], invalidation="i"))
    return FakeLLM({"macro": op("macro", 0.9), "technician": op("technician", 0.6), "narrative": op("narrative", 0.3),
                    "judge": lambda _u: Verdict(action="long", p_up_7d=0.7, rationale="r")})


def test_resolve_scores_brier_and_trade_result():
    led = Ledger(":memory:")
    r = decide("SOL", RecordedRyoClient(FIXTURES, name="fixture"), llm(), led)
    out = resolve(r.id, led, PriceSource(165.0))  # entry 150 -> +10%
    assert out.went_up and out.return_pct == 10.0
    assert out.brier == {"macro": 0.01, "technician": 0.16, "narrative": 0.49, "judge": 0.09}
    assert out.trade_result_usd == pytest.approx(15.0 * r.trade.size_units, abs=0.01)
    assert json.loads(led.get_outcome(r.id))["went_up"] is True
    assert led.unresolved() == []


def test_resolve_refuses_when_price_missing():
    import respx
    from httpx import Response

    led = Ledger(":memory:")
    r = decide("SOL", RecordedRyoClient(FIXTURES, name="fixture"), llm(), led)
    with respx.mock:  # RYO has no price and every exchange fallback is down: refuse, never guess
        respx.get(url__regex=r"https://api\.coingecko\.com/.*").mock(return_value=Response(500))
        respx.get(url__regex=r"https://api\.coinbase\.com/.*").mock(return_value=Response(500))
        respx.get(url__regex=r"https://api\.kraken\.com/.*").mock(return_value=Response(500))
        respx.get(url__regex=r"https://api\.alternative\.me/.*").mock(return_value=Response(500))
        with pytest.raises(CannotResolve):
            resolve(r.id, led, PriceSource(None))


def test_scores_and_weights_favour_low_brier():
    led = Ledger(":memory:")
    r = decide("SOL", RecordedRyoClient(FIXTURES, name="fixture"), llm(), led)
    resolve(r.id, led, PriceSource(165.0))
    s = role_scores(led)
    assert s["macro"] == {"n": 1.0, "brier_mean": 0.01}
    w = role_weights(s)
    assert w["macro"] == 1.0 and w["macro"] > w["technician"] > w["narrative"] >= 0.2


def test_weights_default_to_one_without_outcomes():
    assert role_weights({}) == {"macro": 1.0, "technician": 1.0, "narrative": 1.0}
