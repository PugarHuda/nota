"""What a decision cost to produce, measured rather than estimated."""

from pathlib import Path

from nota.decide import decide
from nota.ledger import Ledger
from nota.receipt import render_markdown
from nota.ryo_client import RecordedRyoClient
from tests.test_decide_replay import make_llm

FIXTURES = Path(__file__).parent / "fixtures"


def _reporting_llm(prompt_tokens=900, completion_tokens=120, usd=0.0004):
    llm = make_llm()
    llm.last_usage = {"prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens, "usd": usd}
    return llm


def test_spend_counts_every_call_and_adds_up_what_the_provider_reported():
    led = Ledger(":memory:")
    r = decide("SOL", RecordedRyoClient(FIXTURES, name="fixture"), _reporting_llm(), led)
    spend = r.spend
    assert spend["model_calls"] == 4 and spend["cached_calls"] == 0        # three agents and the judge
    assert spend["prompt_tokens"] == 3600 and spend["completion_tokens"] == 480
    assert spend["usd"] == 0.0016
    assert spend["ms"] >= 0
    assert "4 model call" in render_markdown(r)


def test_one_silent_provider_makes_the_total_unknown_not_smaller():
    """Summing only the calls that answered would print a precise-looking number that is wrong. A
    total nobody can vouch for is None, the same rule the evidence envelope follows."""
    led = Ledger(":memory:")
    llm = _reporting_llm()

    calls = {"n": 0}
    inner = llm.complete_json

    def sometimes_silent(system, user, schema):
        calls["n"] += 1
        llm.last_usage = None if calls["n"] == 2 else {"prompt_tokens": 900, "completion_tokens": 120, "usd": 0.0004}
        return inner(system=system, user=user, schema=schema)

    llm.complete_json = sometimes_silent
    r = decide("SOL", RecordedRyoClient(FIXTURES, name="fixture"), llm, led)
    assert r.spend["model_calls"] == 4
    assert r.spend["prompt_tokens"] is None and r.spend["usd"] is None


def test_a_cached_decision_spends_nothing_and_says_so():
    led = Ledger(":memory:")
    decide("SOL", RecordedRyoClient(FIXTURES, name="fixture"), _reporting_llm(), led)
    again = decide("SOL", RecordedRyoClient(FIXTURES, name="fixture"), _reporting_llm(), led)
    assert again.spend["model_calls"] == 0 and again.spend["cached_calls"] == 4
    assert again.spend["prompt_tokens"] is None and again.spend["usd"] is None
