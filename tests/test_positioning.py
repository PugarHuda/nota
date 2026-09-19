from pathlib import Path

import httpx
import respx
from httpx import Response

from nota.council import Citation, run_council
from nota.evidence import gather
from nota.ledger import Ledger
from nota.ryo_client import RecordedRyoClient
from nota.skills.contract import make_envelope
from nota.skills.positioning import gate, positioning_check
from tests.test_council import fake, opinion

FIXTURES = Path(__file__).parent / "fixtures"
RYO_SOL = {"funding_rate_bps": 0.0, "open_interest_change_24h_pct": -10.44, "long_short_ratio": None}
# the 2026-09-18 cross-section: one OI figure shared by unrelated tokens
PEERS = [{"symbol": s, "funding_rate_bps": 0.0, "open_interest_change_24h_pct": -10.44} for s in ("ETH", "WIF", "ONDO")] + \
        [{"symbol": "BTC", "funding_rate_bps": 0.0, "open_interest_change_24h_pct": -4.02}]


def verdicts(rows):
    return {r["field"]: r["verdict"] for r in rows}


def test_a_value_shared_by_other_tokens_the_same_day_is_not_about_this_token():
    out = verdicts(gate("SOL", RYO_SOL, PEERS, {"oi_change_24h_pct_coin": 10.72}, None))
    assert out == {"funding_rate_bps": "not_token_specific", "open_interest_change_24h_pct": "not_token_specific", "long_short_ratio": "absent"}


def test_the_symbol_itself_is_not_its_own_peer_and_a_venue_decides_the_rest():
    alone = [{"symbol": "SOL", "open_interest_change_24h_pct": -10.44}, {"symbol": "ETH", "open_interest_change_24h_pct": -10.44}]
    assert verdicts(gate("SOL", RYO_SOL, alone, {"oi_change_24h_pct_coin": 10.72}, None))["open_interest_change_24h_pct"] == "conflicts_with_venue"
    assert verdicts(gate("SOL", RYO_SOL, [], {"oi_change_24h_pct_coin": -6.1}, None))["open_interest_change_24h_pct"] == "citable"
    assert verdicts(gate("SOL", RYO_SOL, [], {"oi_change_24h_pct_coin": 1.2}, None))["open_interest_change_24h_pct"] == "unverified"  # too small to call a conflict
    assert verdicts(gate("SOL", RYO_SOL, [], {"oi_change_24h_pct_coin": None}, None))["open_interest_change_24h_pct"] == "unverified"


def test_btc_funding_of_exactly_zero_contradicts_ryos_own_market_tool():
    out = gate("BTC", {"funding_rate_bps": 0.0}, [], {}, ryo_btc_funding_bps=0.6801)
    assert out[0]["verdict"] == "conflicts_with_ryo" and "0.6801" in out[0]["why"]
    assert gate("ETH", {"funding_rate_bps": 0.0}, [], {}, ryo_btc_funding_bps=0.6801)[0]["verdict"] == "unverified"


def _okx(premium="-0.00023", oi_now=110.0, oi_then=100.0, ratios=(1.35, 1.44, 1.47, 1.2), hl_premium="-0.00018"):
    base = "https://www.okx.com/api/v5"
    respx.get(f"{base}/public/funding-rate").mock(return_value=Response(200, json={"code": "0", "data": [
        {"premium": premium, "fundingRate": "0.0001", "interestRate": "0.0001"}]}))
    oi = [["t", "0", str(oi_now), "0"]] + [["t", "0", "105", "0"]] * 23 + [["t", "0", str(oi_then), "0"]]
    respx.get(f"{base}/rubik/stat/contracts/open-interest-history").mock(return_value=Response(200, json={"code": "0", "data": oi}))
    respx.get(f"{base}/rubik/stat/contracts/long-short-account-ratio-contract").mock(
        return_value=Response(200, json={"code": "0", "data": [["t", str(r)] for r in ratios]}))
    respx.post("https://api.hyperliquid.xyz/info").mock(return_value=Response(200, json=[
        {"universe": [{"name": "SOL"}, {"name": "kPEPE"}]}, [{"premium": hl_premium}, {"premium": "0.0013"}]]))


@respx.mock
def test_okx_positioning_reads_premium_not_the_default_funding_rate():
    _okx()
    env = positioning_check("sol", reference_derivatives=RYO_SOL, peer_derivatives=PEERS, http=httpx.Client())
    okx = env.data["okx"]
    assert okx["premium_bps"] == -2.3 and okx["premium_state"] == "below_spot" and okx["funding_rate_bps_8h"] == 1.0
    assert okx["oi_change_24h_pct_coin"] == 10.0 and okx["long_short_percentile_100h"] == 50.0
    assert env.data["withheld_paths"] == ["deep_analysis.data.derivatives.funding_rate_bps", "deep_analysis.data.derivatives.open_interest_change_24h_pct"]
    assert env.status == "ok" and "2 of 3 RYO derivatives fields withheld" in env.summary.headline
    assert env.data["hyperliquid"]["premium_bps"] == -1.8 and env.data["premium_consensus"] == "below_spot_2_venues"
    assert env.data["plain"]["en"] == ("SOL open interest rose 10% in a day (OKX, in coins) and perps trade below spot on every venue "
                                       "checked, so the short side is the more eager one.")
    assert env.data["plain"]["ja"].startswith("SOLの建玉は1日で10%増加")


@respx.mock
def test_venues_that_disagree_are_reported_as_disagreeing_not_averaged():
    _okx(premium="0.0004", hl_premium="-0.0003")
    env = positioning_check("SOL", http=httpx.Client())
    assert env.data["premium_consensus"] == "venues_disagree" and "no side is called eager" in env.data["plain"]["en"]


@respx.mock
def test_an_unlisted_perp_is_reported_and_the_definition_free_gate_still_runs():
    respx.get(url__regex=r"https://www\.okx\.com/.*").mock(return_value=Response(200, json={"code": "51001", "msg": "Instrument ID doesn't exist.", "data": []}))
    respx.post("https://api.hyperliquid.xyz/info").mock(return_value=Response(200, json=[{"universe": [{"name": "SOL"}]}, [{"premium": "0"}]]))
    env = positioning_check("DGAI", reference_derivatives=RYO_SOL, peer_derivatives=PEERS, http=httpx.Client())
    assert env.status == "unavailable" and env.data["okx"]["premium_bps"] is None
    assert env.data["withheld_paths"][0].endswith("funding_rate_bps")  # the cross-section needs no venue
    assert env.data["premium_consensus"] == "unavailable" and "no perp for DGAI" in " ".join(env.warnings)


def test_the_council_never_sees_or_cites_a_withheld_field():
    withhold = "deep_analysis.data.derivatives.funding_rate"
    env = make_envelope("positioning_check", {"symbol": "SOL"},
                        {"gate": [{"field": "funding_rate", "path": withhold, "verdict": "not_token_specific"}], "withheld_paths": [withhold]},
                        {"okx_premium": "ok"}, [], "SOL: 1 withheld")
    pack = gather(RecordedRyoClient(FIXTURES, name="fixture"), "SOL", extras={"positioning_check": lambda s, p: env})
    seen = {}

    def tech(user):
        seen["prompt"] = user
        return opinion("technician", cites=[Citation(path=withhold, value="0.01"),
                                            Citation(path="deep_analysis.data.derivatives.open_interest_usd", value="1.1e9")])(user)

    llm = fake()
    llm.handlers["technician"] = tech
    res = run_council(pack, llm, Ledger(":memory:"))
    assert '"funding_rate": "withheld: not_token_specific"' in seen["prompt"] and '"open_interest_usd": "1.1e9"' in seen["prompt"]
    assert [c.path for c in res.opinions[1].citations] == ["deep_analysis.data.derivatives.open_interest_usd"]
    assert res.opinions[1].dropped_citations == 1


@respx.mock
def test_the_headline_names_both_venues_when_they_disagree():
    _okx(premium="-0.00026", hl_premium="0.00042")
    env = positioning_check("SOL", http=httpx.Client())
    assert "OKX -2.6 bps / Hyperliquid +4.2 bps: venues disagree" in env.summary.headline
    assert "more demand to be short" not in env.summary.headline


@respx.mock
def test_without_a_reference_the_gate_says_nothing_was_passed_not_that_ryo_returned_null():
    _okx()
    env = positioning_check("SOL", http=httpx.Client())
    assert {r["verdict"] for r in env.data["gate"]} == {"not_provided"}
    assert all(r["why"] == "no reference_derivatives passed" for r in env.data["gate"])
    assert "ryo_reference" not in env.availability


@respx.mock
def test_with_a_ryo_source_the_reference_is_fetched_and_peers_come_from_todays_locks():
    import json
    from datetime import datetime, timezone

    _okx()
    led = Ledger(":memory:")
    today = datetime.now(timezone.utc).isoformat(timespec="seconds")
    for s in ("ETH", "WIF"):
        row = {"symbol": s, "locked_at": today, "status": "locked",
               "envelope": {"data": {"derivatives": {"funding_rate_bps": 0.0, "open_interest_change_24h_pct": -10.44}}}}
        led.save_lock(f"l{s}", s, today, "locked", json.dumps(row))
    env = positioning_check("SOL", http=httpx.Client(), ryo=RecordedRyoClient(FIXTURES, name="fixture"), ledger=led)
    assert env.availability["ryo_reference"] == "available" and env.availability["ledger_peers"] == "available"
    assert env.data["peers_compared"] == ["ETH", "WIF"]
    # the recorded SOL block has no *_bps fields: RYO was asked and answered null, which is `absent`
    assert {r["verdict"] for r in env.data["gate"]} == {"absent"}
    assert env.status == "ok"  # RYO, ledger and DVOL are context; the venues are the primary sections


# Deribit's documented response shape for public/get_volatility_index_data: result.data rows of
# [timestamp_ms, open, high, low, close], DVOL in annualised percent. Built here, inside the test.
DVOL = {"jsonrpc": "2.0", "result": {"continuation": None, "data": [
    [1758067200000, 47.1, 48.0, 46.2, 46.9], [1758153600000, 46.9, 47.5, 44.8, 45.2], [1758240000000, 45.2, 45.9, 44.1, 44.6]]}}


@respx.mock
def test_dvol_gives_the_implied_week_and_flags_a_stop_inside_it():
    _okx()
    respx.post("https://api.hyperliquid.xyz/info").mock(return_value=Response(200, json=[
        {"universe": [{"name": "BTC"}]}, [{"premium": "-0.00018"}]]))
    route = respx.get("https://www.deribit.com/api/v2/public/get_volatility_index_data").mock(return_value=Response(200, json=DVOL))
    env = positioning_check("BTC", atr_stop_pct=2.0, http=httpx.Client())
    q = route.calls[0].request.url.params
    assert q["currency"] == "BTC" and q["resolution"] == "1D"
    iv = env.data["implied_vol"]
    assert iv["dvol"] == 44.6 and iv["implied_7d_move_pct"] == round(44.6 / 365 ** 0.5 * 7 ** 0.5, 2) == 6.18
    assert iv["as_of"].startswith("2025-09-19") and env.availability["deribit_dvol"] == "available"
    assert env.data["stop_check"] == {"atr_stop_pct": 2.0, "half_implied_7d_move_pct": 3.09, "inside_noise": True}
    assert any(w.startswith("stop inside normal 7-day noise") for w in env.warnings)
    assert positioning_check("BTC", atr_stop_pct=4.0, http=httpx.Client()).data["stop_check"]["inside_noise"] is False


@respx.mock
def test_no_dvol_is_guessed_for_a_token_deribit_does_not_index():
    _okx()
    env = positioning_check("SOL", atr_stop_pct=1.0, http=httpx.Client())
    assert env.availability["deribit_dvol"] == "unavailable" and env.data["implied_vol"] is None and env.data["stop_check"] is None
    assert any(w.startswith("no DVOL index for SOL") for w in env.warnings)


def test_a_side_called_from_one_venue_says_it_is_one_venue():
    from nota.skills.positioning import premium_clause

    okx = {"premium_bps": None}
    hl = {"premium_bps": 5.273}
    assert premium_clause("above_spot_1_venues", okx, hl) == "perp above spot (Hyperliquid +5.273 bps only): more demand to be long"
    assert premium_clause("unavailable", okx, {"premium_bps": None}) is None
