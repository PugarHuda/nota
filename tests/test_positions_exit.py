from pathlib import Path

import pytest

from nota.calibration import CannotResolve, close_position, role_scores
from nota.decide import decide
from nota.ledger import Ledger
from nota.ryo_client import RecordedRyoClient
from tests.test_decide_replay import make_llm

FIXTURES = Path(__file__).parent / "fixtures"


def test_close_position_at_stop_scores_immediately_and_keeps_horizon_honest():
    led = Ledger(":memory:")
    r = decide("SOL", RecordedRyoClient(FIXTURES, name="fixture"), make_llm(action="long", p=0.7), led)  # long from 150, stop 138
    out = close_position(r.id, led, 137.5, "exchange_median:3_sources", "stopped")
    assert out.closed_reason == "stopped" and out.horizon_reached is False and out.went_up is False
    assert out.trade_result_usd == pytest.approx(-104.17, abs=0.01) and out.price_now_source.startswith("exchange_median")
    assert led.unresolved() == [] and role_scores(led)["judge"]["brier_mean"] == pytest.approx(0.49)
    blocked = decide("SOL", RecordedRyoClient(FIXTURES, name="fixture"), make_llm(action="no_trade", p=0.5), led, use_cache=False)
    with pytest.raises(CannotResolve, match="no practice position"):
        close_position(blocked.id, led, 100.0, "x", "stopped")
    with pytest.raises(KeyError):
        close_position("nope", led, 100.0, "x", "stopped")
