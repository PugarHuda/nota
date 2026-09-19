import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest

from nota.calibration import CannotResolve, resolve, role_scores, role_weights
from nota.council import Citation, Opinion, Verdict
from nota.decide import decide
from nota.envelope import Envelope
from nota.ledger import Ledger
from tests.fakes import FakeLLM
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


def _week_later(led, decision_id, days=8):
    """Store an outcome as the daily cycle would have: resolved `days` after the decision."""
    o, rec = json.loads(led.get_outcome(decision_id)), json.loads(led.get_decision(decision_id))
    o["decided_as_of"] = rec["created_at"]
    o["resolved_at"] = (datetime.fromisoformat(rec["created_at"]) + timedelta(days=days)).isoformat()
    led.save_outcome(decision_id, json.dumps(o))


def _backdate(led, decision_id, created_at):
    raw = json.loads(led.get_decision(decision_id))
    raw["created_at"] = created_at
    raw["provenance"].get("deep_analysis", {})["as_of"] = created_at   # RYO answered when it was asked
    led.save_decision(raw["id"], raw["pack_hash"], raw["symbol"], raw["model"], json.dumps(raw))


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
    _week_later(led, r.id)
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


def test_skill_vs_ryo_counts_council_against_ryos_own_call():
    """Two recorded SOL packs (2026-09-19 and 2026-09-07); RYO called both 'constructive'. The council goes
    long on one and short on the other, and SOL rises after both: RYO 2/2, the council 1/2."""
    from nota.calibration import skill_vs_ryo

    led = Ledger(":memory:")

    def council(action, p):
        op = lambda role: (lambda _u: Opinion(role=role, stance="neutral", p_up_7d=p, confidence="medium", thesis="t",
                                              citations=[Citation(path="deep_analysis.data.technical_analysis.rsi_14")], invalidation="i"))
        return FakeLLM({r: op(r) for r in ("macro", "technician", "narrative")} | {"judge": lambda _u: Verdict(action=action, p_up_7d=p, rationale="r")})

    a = decide("SOL", RecordedRyoClient(FIXTURES), council("long", 0.7), led)
    b = decide("SOL", RecordedRyoClient(FIXTURES / "recorded_0907"), council("short", 0.3), led)
    assert a.ryo_view["deep_verdict"] == b.ryo_view["deep_verdict"] == "constructive"
    assert a.agrees_with_ryo is True and b.agrees_with_ryo is False
    assert skill_vs_ryo(led)["n"] == 0                       # nothing scored yet
    for r in (a, b):
        assert r.trade.kind == "trade"                       # both sized, so each is priced at its own entry
        resolve(r.id, led, PriceSource(r.trade.entry_price * 1.1))
        _week_later(led, r.id)
    s = skill_vs_ryo(led)
    assert s["n"] == 2 and s["council_hit_rate"] == 0.5 and s["ryo_hit_rate"] == 1.0
    assert s["disagreed"] == 1 and s["council_right_when_disagreeing"] == 0 and s["enough_to_read"] is False
    assert s["n_independent"] == 1   # two SOL calls minutes apart watched the same week


def test_an_outcome_resolved_a_week_later_counts_whatever_its_stored_flag_says():
    """The SOL outcome of 2026-09-10 was resolved eight days later and stored horizon_reached=false."""
    from nota.calibration import fill_base_rates, reliability, repair_outcomes, skill_vs_base

    led = Ledger(":memory:")
    r = decide("SOL", RecordedRyoClient(FIXTURES), llm(), led)
    resolve(r.id, led, PriceSource(165.0))
    _week_later(led, r.id)
    o = json.loads(led.get_outcome(r.id))
    o.update(horizon_reached=False, decided_as_of=None)
    led.save_outcome(r.id, json.dumps(o))
    fill_base_rates(led, lambda s, d: 0.5)
    assert reliability(led)["n"] == 1 and skill_vs_base(led)["n"] == 1
    assert repair_outcomes(led) == [r.id] and repair_outcomes(led) == []   # idempotent
    fixed = json.loads(led.get_outcome(r.id))
    assert fixed["horizon_reached"] is True and fixed["decided_as_of"] == r.provenance["deep_analysis"]["as_of"]


def test_an_early_stopped_outcome_answers_a_different_question():
    from nota.calibration import close_position, fill_base_rates, skill_vs_base, source_scores

    led = Ledger(":memory:")
    r = decide("SOL", RecordedRyoClient(FIXTURES), llm(), led)
    close_position(r.id, led, r.trade.stop_price, "okx", "stopped")
    _week_later(led, r.id)   # even resolved a week on, a stop hit early is not a seven-day answer
    fill_base_rates(led, lambda s, d: 0.5)
    base, src = skill_vs_base(led), source_scores(led)
    assert base["roles"] == {} and base["excluded_before_horizon"] == 1
    assert src["scored_decisions"] == 0 and src["excluded_before_horizon"] == 1
    assert role_scores(led)["judge"]["n"] == 1   # still in the trading feedback loop


def test_two_calls_minutes_apart_are_two_scores_but_one_independent_sample():
    from nota.calibration import fill_base_rates, n_independent, skill_vs_base

    led = Ledger(":memory:")
    one, two = llm(), llm()
    two.model = "fake-b"   # a different model, so a different receipt over the same evidence
    a = decide("SOL", RecordedRyoClient(FIXTURES), one, led)
    b = decide("SOL", RecordedRyoClient(FIXTURES), two, led)
    t0 = datetime(2026, 9, 1, 9, tzinfo=timezone.utc)
    _backdate(led, a.id, t0.isoformat())
    _backdate(led, b.id, (t0 + timedelta(minutes=2)).isoformat())
    for r in (a, b):
        resolve(r.id, led, PriceSource(165.0))
        _week_later(led, r.id)
    fill_base_rates(led, lambda s, d: 0.5)
    out = skill_vs_base(led)
    assert out["n"] == 2 and out["n_independent"] == 1 and out["enough_to_read"] is False
    week = [("SOL", (t0 + timedelta(days=7 * i)).isoformat()) for i in range(20)]
    assert n_independent(week) == 20 and n_independent(week + [("BTC", t0.isoformat())]) == 21


def test_a_late_resolution_is_priced_at_the_horizon_candle():
    led = Ledger(":memory:")
    r = decide("SOL", RecordedRyoClient(FIXTURES), llm(), led)
    created = datetime(2026, 9, 1, 9, 30, tzinfo=timezone.utc)
    _backdate(led, r.id, created.isoformat())
    asked = []

    def okx(request):
        asked.append(dict(request.url.params))
        return httpx.Response(200, json={"code": "0", "data": [[asked[-1]["before"], "1", "2", "1", "123.5", "0", "0", "0", "1"]]})

    now = created + timedelta(days=9)   # the cycle got to it two days late
    out = resolve(r.id, led, PriceSource(999.0), http=httpx.Client(transport=httpx.MockTransport(okx)), now=now)
    assert out.price_now == 123.5 and out.price_now_source == "okx_1h_close_at_horizon"
    assert out.price_now_as_of == "2026-09-08T09:00:00+00:00" and out.horizon_reached
    assert asked[0]["instId"] == "SOL-USDT" and asked[0]["bar"] == "1H"
    assert asked[0]["before"] == str(int(datetime(2026, 9, 8, 8, tzinfo=timezone.utc).timestamp() * 1000) - 1)
    led2 = Ledger(":memory:")
    r2 = decide("SOL", RecordedRyoClient(FIXTURES), llm(), led2)
    _backdate(led2, r2.id, created.isoformat())
    down = httpx.Client(transport=httpx.MockTransport(lambda req: httpx.Response(503)))
    late = resolve(r2.id, led2, PriceSource(165.0), http=down, now=now)
    assert late.price_now == 165.0 and late.price_now_source == "late:48h:ryo:test"   # how late, on the record


def test_weights_move_little_on_one_scored_call():
    w = role_weights({"macro": {"n": 1.0, "brier_mean": 0.0}, "technician": {"n": 1.0, "brier_mean": 1.0},
                      "narrative": {"n": 1.0, "brier_mean": 0.5}})
    assert all(abs(v - 1.0) <= 0.05 for v in w.values()) and w["macro"] == 1.0
    many = role_weights({"macro": {"n": 200.0, "brier_mean": 0.0}, "technician": {"n": 200.0, "brier_mean": 1.0}})
    assert many["technician"] < 0.3
