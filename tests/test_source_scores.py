"""Which sources help, not just which agents are right."""

import json
from pathlib import Path

from nota.calibration import MEANINGFUL_N, source_scores
from nota.decide import decide
from nota.ledger import Ledger
from nota.ryo_client import RecordedRyoClient
from tests.test_decide_replay import make_llm

FIXTURES = Path(__file__).parent / "fixtures"


def _scored(led: Ledger, decision_id: str, availability: dict[str, str], judge_brier: float) -> None:
    """Store a decision whose availability we control, plus the outcome that scored it."""
    raw = json.loads(led.get_decision(decision_id))
    raw["availability"] = availability
    led.save_decision(raw["id"], raw["pack_hash"], raw["symbol"], raw["model"], json.dumps(raw))
    led.save_outcome(decision_id, json.dumps({"decision_id": decision_id, "brier": {"judge": judge_brier},
                                              "went_up": True, "horizon_reached": True}))


def test_a_source_that_helps_shows_a_lower_brier_when_it_answered():
    led = Ledger(":memory:")
    # A receipt id is hash(evidence, model, prompt version), so two decisions over the same fixture
    # need different models or the second silently replaces the first.
    one, two = make_llm(p=0.7), make_llm(action="short", p=0.3)
    one.model, two.model = "fake-a", "fake-b"
    good = decide("SOL", RecordedRyoClient(FIXTURES, name="fixture"), one, led)
    bad = decide("SOL", RecordedRyoClient(FIXTURES, name="fixture"), two, led)
    _scored(led, good.id, {"deep_analysis": "ok", "narrative_signal": "error"}, 0.09)
    _scored(led, bad.id, {"deep_analysis": "error", "narrative_signal": "error"}, 0.49)

    out = source_scores(led)
    deep = out["sources"]["deep_analysis"]
    assert deep["answered"] == {"n": 1, "brier_mean": 0.09}
    assert deep["missing"] == {"n": 1, "brier_mean": 0.49}
    assert deep["helps_by"] == 0.4                       # lower Brier with it: it earned its place

    narrative = out["sources"]["narrative_signal"]
    assert narrative["answered"] == {"n": 0, "brier_mean": None}   # never zero for an empty bucket
    assert narrative["helps_by"] is None                           # nothing to compare against


def test_the_table_refuses_to_be_read_as_a_finding_until_there_is_enough_of_it():
    """Two three-sample means differ by noise. Publishing that as 'this source helps' would be the
    same offence as turning a null into a zero, so the table says when it cannot be read yet."""
    led = Ledger(":memory:")
    r = decide("SOL", RecordedRyoClient(FIXTURES, name="fixture"), make_llm(), led)
    _scored(led, r.id, {"deep_analysis": "partial"}, 0.2)

    out = source_scores(led)
    assert out["scored_decisions"] == 1 and out["enough_to_read"] is False
    assert out["meaningful_at"] == MEANINGFUL_N
    # a partial answer is still an answer
    assert out["sources"]["deep_analysis"]["answered"]["n"] == 1


def test_an_unscored_ledger_produces_an_empty_table_rather_than_zeros():
    led = Ledger(":memory:")
    decide("SOL", RecordedRyoClient(FIXTURES, name="fixture"), make_llm(), led)
    out = source_scores(led)
    assert out == {"scored_decisions": 0, "enough_to_read": False, "meaningful_at": MEANINGFUL_N, "sources": {}}
