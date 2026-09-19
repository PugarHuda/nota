import json
from pathlib import Path

from nota import paths
from nota.evidence import EvidencePack, first_present, gather
from nota.ryo_client import RecordedRyoClient

FIXTURES = Path(__file__).parent / "fixtures"
OLD = FIXTURES / "recorded_0907"  # RYO's real answers from 2026-09-07, when SOL's derivatives lane was down


def source():
    return RecordedRyoClient(FIXTURES)


def real(tool, name="SOL", root=FIXTURES):
    return json.loads((root / tool / f"{name}.json").read_text(encoding="utf-8"))


def test_gather_builds_five_sections_with_ryo_s_own_statuses():
    pack = gather(source(), "sol")
    assert pack.symbol == "SOL" and pack.source == "recorded"
    assert pack.availability() == {
        "market_overview": real("market_overview", "default")["status"],
        "sentiment_shift": real("monitor_market_sentiment_shift", "default")["status"],
        "deep_analysis": real("deep_analysis")["status"], "analyze_token": real("analyze_token")["status"],
        "compare": real("compare_tokens", "SOL-BTC-ETH")["status"],
    }
    assert pack.primary_ok


def test_a_real_partial_answer_stays_partial_and_keeps_its_warnings():
    """RYO's 2026-09-07 answer for SOL had no derivatives lane: partial, with RYO's own warning."""
    pack = gather(RecordedRyoClient(OLD), "SOL")
    assert pack.sections["deep_analysis"].status == "partial" and pack.primary_ok
    assert pack.get("deep_analysis.data.derivatives") is None
    assert "deep_analysis: Derivatives analysis is unavailable or was not requested." in pack.warnings()


def test_gather_survives_missing_tool_and_marks_error():
    pack = gather(source(), "BTC")  # no BTC fixtures
    assert pack.sections["deep_analysis"].status == "error"
    assert "NO_FIXTURE" in pack.sections["deep_analysis"].error
    assert pack.sections["market_overview"].status == "ok"
    assert not pack.primary_ok


def test_hash_ignores_created_at_and_trace_id():
    a = gather(source(), "SOL")
    b = gather(source(), "SOL")
    b.created_at = "1999-01-01T00:00:00+00:00"
    b.sections["deep_analysis"].envelope.trace_id = "other"
    assert a.pack_hash() == b.pack_hash()


def test_get_and_available_paths_never_zero_fill():
    pack = gather(RecordedRyoClient(OLD), "SOL")  # token_profile was null in this real answer
    deep = real("deep_analysis", root=OLD)
    assert pack.get("deep_analysis.data.trade_plan.atr_14_usd") == deep["data"]["trade_plan"]["atr_14_usd"]
    assert pack.get("deep_analysis.data.token_profile") is None
    assert pack.get("sentiment_shift.data.derivatives") is None
    assert pack.get("deep_analysis.status") == deep["status"]
    assert pack.get("nope.data.x") is None
    paths_ = pack.available_paths()
    assert "deep_analysis.data.trade_plan.atr_14_usd" in paths_
    assert "deep_analysis.data.token_profile" not in paths_
    assert "market_overview.data.top_movers.gainers.0.symbol" in paths_


def test_first_present_prefers_deep_analysis_and_returns_none_when_absent():
    pack = gather(source(), "SOL")
    price = real("deep_analysis")["data"]["market"]["price_usd"]
    assert first_present(pack, paths.PRICE_USD) == ("deep_analysis.data.market.price_usd", price)
    assert first_present(pack, ["deep_analysis.data.nope.x", "none.data.y"]) == (None, None)


def test_fear_greed_comes_from_where_ryo_puts_it():
    pack = gather(source(), "SOL")
    assert first_present(pack, paths.FEAR_GREED) == (
        "market_overview.data.sentiment.fear_greed_index", real("market_overview", "default")["data"]["sentiment"]["fear_greed_index"])
    del pack.sections["market_overview"]
    assert first_present(pack, paths.FEAR_GREED)[0] == "sentiment_shift.data.evidence.fear_greed.value"


def test_provenance_carries_as_of_and_data_mode():
    prov = gather(source(), "SOL").provenance()
    deep = real("deep_analysis")
    assert prov["deep_analysis"]["as_of"] == deep["as_of"]
    assert prov["deep_analysis"]["data_mode"] == deep["data_mode"]
    assert prov["deep_analysis"]["status"] == deep["status"]
    assert prov["deep_analysis"]["trace_id"] == deep["trace_id"]


def test_pack_roundtrips_through_json():
    pack = gather(source(), "SOL")
    again = EvidencePack.model_validate_json(pack.model_dump_json())
    assert again.pack_hash() == pack.pack_hash()


def test_parallel_gather_is_faster_and_hashes_like_a_sequential_one():
    import time

    from nota.evidence import SECTIONS, _fetch, ryo_args

    class Slow(RecordedRyoClient):
        def call(self, tool, args=None):
            time.sleep(0.2)
            return super().call(tool, args)

    t0 = time.perf_counter()
    pack = gather(Slow(FIXTURES), "SOL")
    wall = time.perf_counter() - t0
    assert wall < 0.2 * len(SECTIONS) - 0.2  # three at a time: two rounds, not five
    seq = EvidencePack(symbol="SOL", created_at="x", source="recorded")
    for key, tool in SECTIONS.items():
        seq.sections[key] = _fetch(RecordedRyoClient(FIXTURES), tool, ryo_args("SOL")[key])
    assert list(pack.sections) == list(SECTIONS) == list(seq.sections)
    assert pack.pack_hash() == seq.pack_hash()
    assert pack.model_dump(exclude={"created_at"}) == seq.model_dump(exclude={"created_at"})


def test_a_failed_section_keeps_ryo_s_trace_id_in_provenance():
    from nota.ryo_client import RyoError

    class Down(RecordedRyoClient):
        def call(self, tool, args=None):
            if tool == "deep_analysis":
                raise RyoError(503, "UNAVAILABLE", "upstream down", "trace-503")
            return super().call(tool, args)

    pack = gather(Down(FIXTURES), "SOL")
    assert pack.sections["deep_analysis"].trace_id == "trace-503"
    prov = pack.provenance()["deep_analysis"]
    assert prov == {"tool": "deep_analysis", "as_of": None, "data_mode": None, "trace_id": "trace-503", "status": "error"}
