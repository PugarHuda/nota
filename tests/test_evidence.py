from pathlib import Path

from nota import paths
from nota.evidence import EvidencePack, first_present, gather
from nota.ryo_client import RecordedRyoClient

FIXTURES = Path(__file__).parent / "fixtures"


def source():
    return RecordedRyoClient(FIXTURES, name="fixture")


def test_gather_builds_four_sections_with_statuses():
    pack = gather(source(), "sol")
    assert pack.symbol == "SOL" and pack.source == "fixture"
    assert pack.availability() == {
        "market_overview": "ok", "sentiment_shift": "partial", "deep_analysis": "partial", "analyze_token": "ok", "compare": "partial",
    }
    assert pack.primary_ok
    assert any("derivatives source rate-limited" in w for w in pack.warnings())


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
    pack = gather(source(), "SOL")
    assert pack.get("deep_analysis.data.trade_plan.atr_14_usd") == 6.0
    assert pack.get("deep_analysis.data.token_profile") is None
    assert pack.get("sentiment_shift.data.derivatives") is None
    assert pack.get("deep_analysis.status") == "partial"
    assert pack.get("nope.data.x") is None
    paths_ = pack.available_paths()
    assert "deep_analysis.data.trade_plan.atr_14_usd" in paths_
    assert "deep_analysis.data.token_profile" not in paths_
    assert "market_overview.data.top_movers.gainers.0.symbol" in paths_


def test_first_present_prefers_deep_analysis_and_returns_none_when_absent():
    pack = gather(source(), "SOL")
    assert first_present(pack, paths.PRICE_USD) == ("deep_analysis.data.market.price_usd", 150.0)
    assert first_present(pack, ["deep_analysis.data.token_profile.x", "none.data.y"]) == (None, None)


def test_provenance_carries_as_of_and_data_mode():
    prov = gather(source(), "SOL").provenance()
    assert prov["deep_analysis"]["as_of"] == "2026-09-01T00:00:00Z"
    assert prov["deep_analysis"]["data_mode"] == "live"
    assert prov["deep_analysis"]["status"] == "partial"


def test_pack_roundtrips_through_json():
    pack = gather(source(), "SOL")
    again = EvidencePack.model_validate_json(pack.model_dump_json())
    assert again.pack_hash() == pack.pack_hash()
