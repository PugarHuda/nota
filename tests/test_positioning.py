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


def _okx(premium="-0.00023", oi_now=110.0, oi_then=100.0, ratios=(1.35, 1.44, 1.47, 1.2)):
    base = "https://www.okx.com/api/v5"
    respx.get(f"{base}/public/funding-rate").mock(return_value=Response(200, json={"code": "0", "data": [
        {"premium": premium, "fundingRate": "0.0001", "interestRate": "0.0001"}]}))
    oi = [["t", "0", str(oi_now), "0"]] + [["t", "0", "105", "0"]] * 23 + [["t", "0", str(oi_then), "0"]]
    respx.get(f"{base}/rubik/stat/contracts/open-interest-history").mock(return_value=Response(200, json={"code": "0", "data": oi}))
    respx.get(f"{base}/rubik/stat/contracts/long-short-account-ratio-contract").mock(
        return_value=Response(200, json={"code": "0", "data": [["t", str(r)] for r in ratios]}))


@respx.mock
def test_okx_positioning_reads_premium_not_the_default_funding_rate():
    _okx()
    env = positioning_check("sol", reference_derivatives=RYO_SOL, peer_derivatives=PEERS, http=httpx.Client())
    okx = env.data["okx"]
    assert okx["premium_bps"] == -2.3 and okx["premium_state"] == "below_spot" and okx["funding_rate_bps_8h"] == 1.0
    assert okx["oi_change_24h_pct_coin"] == 10.0 and okx["long_short_percentile_100h"] == 50.0
    assert env.data["withheld_paths"] == ["deep_analysis.data.derivatives.funding_rate_bps", "deep_analysis.data.derivatives.open_interest_change_24h_pct"]
    assert env.status == "ok" and "2 of 3 RYO derivatives fields withheld" in env.summary.headline


@respx.mock
def test_an_unlisted_perp_is_reported_and_the_definition_free_gate_still_runs():
    respx.get(url__regex=r"https://www\.okx\.com/.*").mock(return_value=Response(200, json={"code": "51001", "msg": "Instrument ID doesn't exist.", "data": []}))
    env = positioning_check("DGAI", reference_derivatives=RYO_SOL, peer_derivatives=PEERS, http=httpx.Client())
    assert env.status == "unavailable" and env.data["okx"]["premium_bps"] is None
    assert env.data["withheld_paths"][0].endswith("funding_rate_bps")  # the cross-section needs no venue


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
