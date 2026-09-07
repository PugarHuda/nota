from pathlib import Path

from nota.council import Citation, Opinion, Verdict, run_council, validate_citations
from nota.evidence import gather
from nota.ledger import Ledger
from nota.llm import FakeLLM
from nota.ryo_client import RecordedRyoClient

FIXTURES = Path(__file__).parent / "fixtures"


def opinion(role, stance="bullish", p=0.62, cites=None):
    return lambda _user: Opinion(
        role=role, stance=stance, p_up_7d=p, confidence="medium", thesis="t",
        citations=cites if cites is not None else [Citation(path="deep_analysis.data.technical_analysis.rsi_14", value="61.3")],
        invalidation="RSI < 45",
    )


def verdict(action="long", p=0.6):
    return lambda _user: Verdict(action=action, p_up_7d=p, rationale="r", agreed_with=["technician"], key_risks=["k"])


def fake(action="long"):
    return FakeLLM({
        "macro": opinion("macro"),
        "technician": opinion("technician", cites=[
            Citation(path="deep_analysis.data.trade_plan.atr_14_usd", value="6.0"),
            Citation(path="deep_analysis.data.made.up", value="1"),
            Citation(path="deep_analysis.data.token_profile", value="null"),
        ]),
        "narrative": opinion("narrative", "neutral", 0.5, cites=[Citation(path="nowhere.data.x", value="9")]),
        "judge": verdict(action),
    })


def pack():
    return gather(RecordedRyoClient(FIXTURES, name="fixture"), "SOL")


def test_council_runs_three_roles_and_judge_with_citation_validation():
    llm = fake()
    res = run_council(pack(), llm, Ledger(":memory:"))
    assert [o.role for o in res.opinions] == ["macro", "technician", "narrative"]
    assert llm.calls == ["macro", "technician", "narrative", "judge"]
    tech = res.opinions[1]
    assert [c.path for c in tech.citations] == ["deep_analysis.data.trade_plan.atr_14_usd"]
    assert tech.dropped_citations == 2 and tech.confidence == "medium"
    narr = res.opinions[2]
    assert narr.citations == [] and narr.dropped_citations == 1 and narr.confidence == "low"
    assert res.verdict.action == "long" and res.cache_hits == 0


def test_second_run_is_served_from_cache_and_bypass_calls_again():
    led = Ledger(":memory:")
    llm = fake()
    p = pack()
    run_council(p, llm, led)
    res2 = run_council(p, llm, led)
    assert res2.cache_hits == 4 and len(llm.calls) == 4
    res3 = run_council(p, llm, led, use_cache=False)
    assert res3.cache_hits == 0 and len(llm.calls) == 8


def test_validate_citations_downgrades_when_no_citations_at_all():
    op = Opinion(role="macro", stance="bullish", p_up_7d=0.7, confidence="high", thesis="t", invalidation="i")
    assert validate_citations(op, {"a"}).confidence == "low"


def test_judge_prompt_receives_weights(monkeypatch):
    seen = {}

    def judge(user):
        seen["user"] = user
        return Verdict(action="no_trade", p_up_7d=0.5, rationale="split")

    llm = FakeLLM({"macro": opinion("macro"), "technician": opinion("technician"), "narrative": opinion("narrative"), "judge": judge})
    run_council(pack(), llm, Ledger(":memory:"), weights={"macro": 0.4, "technician": 1.0, "narrative": 0.7})
    assert '"technician": 1.0' in seen["user"] and '"primary_evidence_ok": true' in seen["user"]
