"""Round-two gap fixes: simulated-data guard, MCP transport, Bluesky voices, fear/greed cross-check, reliability, watch --scan-top."""

import json
from pathlib import Path

import httpx
import pytest
import respx
from httpx import Response
from typer.testing import CliRunner

from nota import cli
from nota.calibration import Outcome, reliability, role_scores
from nota.council import Verdict
from nota.decide import decide
from nota.evidence import gather
from nota.ledger import Ledger
from nota.risk import Blocked, size_trade
from nota.ryo_client import RecordedRyoClient, RyoClient, RyoError
from nota.skills.contract import SourceUnavailable
from nota.skills.narrative import narrative_convergence
from nota.skills.price_check import ExchangePrices, price_crosscheck
from nota.skills.sources import BlueskyPublic
from tests.test_decide_replay import make_llm

FIXTURES = Path(__file__).parent / "fixtures"


def test_simulated_primary_evidence_blocks_the_trade():
    pack = gather(RecordedRyoClient(FIXTURES, name="fixture"), "SOL")
    pack.sections["deep_analysis"].envelope.data_mode = "simulated"
    out = size_trade(Verdict(action="long", p_up_7d=0.8, rationale="r"), pack)
    assert isinstance(out, Blocked) and "simulated" in out.reason


@respx.mock
def test_mcp_transport_calls_tools_call_and_maps_errors():
    route = respx.post("https://app-ryochan.com/api/mcp").mock(side_effect=[
        Response(200, json={"jsonrpc": "2.0", "id": 1, "result": {"tools": [{"name": "analyze_token"}, {"name": "deep_analysis"}]}}),
        Response(200, json={"jsonrpc": "2.0", "id": 2, "result": {"content": [{"type": "text", "text": (FIXTURES / "analyze_token" / "SOL.json").read_text(encoding="utf-8")}], "isError": False}},
                 headers={"x-trace-id": "t-1"}),
        Response(200, json={"jsonrpc": "2.0", "id": 3, "result": {"content": [{"type": "text", "text": "symbol not supported"}], "isError": True}}),
        Response(200, json={"jsonrpc": "2.0", "id": 4, "error": {"code": -32602, "message": "invalid params"}}),
    ])
    c = RyoClient(key="k", transport="mcp", http=httpx.Client(), max_retries=0)
    assert [t["name"] for t in c.tools_mcp()] == ["analyze_token", "deep_analysis"]
    env = c.call("analyze_token", {"symbol": "SOL"})
    assert env.tool == "analyze_token" and env.trace_id == "t-1"
    body = json.loads(route.calls[1].request.content)
    assert body["method"] == "tools/call" and body["params"] == {"name": "analyze_token", "arguments": {"symbol": "SOL"}}
    with pytest.raises(RyoError, match="TOOL_ERROR"):
        c.call("analyze_token", {"symbol": "XXX"})
    with pytest.raises(RyoError, match="JSONRPC_-32602"):
        c.call("analyze_token", {})
    with pytest.raises(ValueError):
        RyoClient(key="k", transport="grpc")


BSKY = {"feed": [
    {"post": {"uri": "at://did:plc:abc/app.bsky.feed.post/3k1", "author": {"handle": "alpha.bsky.social"}, "likeCount": 12,
              "record": {"text": "$SOL breakout, buying more. Bullish on solana", "createdAt": "2026-09-06T01:00:00.000Z"}}},
    {"post": {"uri": "at://did:plc:abc/app.bsky.feed.post/3k2", "author": {"handle": "alpha.bsky.social"},
              "record": {"text": "gm", "createdAt": "2026-09-06T00:00:00.000Z"}}},
]}


@respx.mock
def test_bluesky_voice_parses_and_scores():
    respx.get("https://public.api.bsky.app/xrpc/app.bsky.feed.getAuthorFeed").mock(return_value=Response(200, json=BSKY))
    msgs = BlueskyPublic(http=httpx.Client()).fetch("@alpha.bsky.social")
    assert msgs[0].url == "https://bsky.app/profile/alpha.bsky.social/post/3k1" and msgs[0].at == "2026-09-06T01:00:00.000Z" and msgs[0].views == "12"
    env = narrative_convergence(["bs:alpha.bsky.social"], hours=24 * 30, bluesky=BlueskyPublic(http=httpx.Client()))
    assert env.availability == {"bs:alpha.bsky.social": "ok"} and env.data["tokens"][0]["symbol"] == "SOL" and env.data["tokens"][0]["sentiment_mean"] > 0
    respx.get("https://public.api.bsky.app/xrpc/app.bsky.feed.getAuthorFeed").mock(return_value=Response(400, json={"message": "Profile not found"}))
    with pytest.raises(SourceUnavailable, match="Profile not found"):
        BlueskyPublic(http=httpx.Client()).fetch("nobody")


@respx.mock
def test_fear_greed_crosscheck_in_price_check():
    respx.get("https://api.alternative.me/fng/").mock(return_value=Response(200, json={"data": [{"value": "73", "value_classification": "Greed", "timestamp": "1788652800"}]}))
    respx.get("https://api.coingecko.com/api/v3/simple/price").mock(return_value=Response(500))
    respx.get("https://api.coinbase.com/v2/prices/SOL-USD/spot").mock(return_value=Response(500))
    respx.get("https://api.kraken.com/0/public/Ticker").mock(return_value=Response(500))
    env = price_crosscheck("SOL", reference_fear_greed=55, exchanges=ExchangePrices(http=httpx.Client()))
    fg = env.data["fear_greed"]
    assert fg["value"] == 73 and fg["delta"] == 18 and fg["as_of"] == "2026-09-06T00:00:00+00:00" and any("differs" in w for w in env.warnings)
    assert env.status == "unavailable" and env.availability["fear_greed"] == "ok"  # exchanges are the primary sections


def test_reliability_bins_judge_probabilities():
    led = Ledger(":memory:")
    a = decide("SOL", RecordedRyoClient(FIXTURES, name="fixture"), make_llm(action="long", p=0.7), led)
    assert reliability(led) == {"n": 0, "judge_brier": None, "excluded_before_horizon": 0,
                                "bins": [{"bin": f"{i / 5:.1f}-{(i + 1) / 5:.1f}", "n": 0, "mean_p": None, "hit_rate": None} for i in range(5)]}
    led.save_outcome(a.id, Outcome(decision_id=a.id, symbol="SOL", resolved_at="x", decided_as_of=None, horizon_reached=True, price_then=150.0,
                                   price_now=160.0, return_pct=6.7, went_up=True, brier={}).model_dump_json())
    rel = reliability(led)
    assert rel["n"] == 1 and rel["judge_brier"] == 0.09 and rel["bins"][3] == {"bin": "0.6-0.8", "n": 1, "mean_p": 0.7, "hit_rate": 1.0}


def test_calibration_table_ignores_positions_closed_before_the_horizon():
    """A stop hit on day one does not answer "is the price higher in seven days"."""
    led = Ledger(":memory:")
    a = decide("SOL", RecordedRyoClient(FIXTURES, name="fixture"), make_llm(action="long", p=0.7), led)
    led.save_outcome(a.id, Outcome(decision_id=a.id, symbol="SOL", resolved_at="x", decided_as_of=None,
                                   horizon_reached=False, closed_reason="stopped", price_then=150.0, price_now=138.0,
                                   return_pct=-8.0, went_up=False, brier={"judge": 0.49}).model_dump_json())
    rel = reliability(led)
    assert rel["n"] == 0 and rel["judge_brier"] is None and rel["excluded_before_horizon"] == 1
    assert role_scores(led)["judge"]["n"] == 1  # still counted where it belongs: the trading loop


def test_watch_scan_top_picks_candidates(tmp_path, monkeypatch):
    monkeypatch.setenv("NOTA_DB", str(tmp_path / "w.db"))
    monkeypatch.setattr(cli, "_llm", lambda kind: make_llm())
    res = CliRunner().invoke(cli.app, ["watch", "--scan-top", "1", "--source", "fixture", "--cycles", "1", "--every", "0", "--no-price-check"])
    assert res.exit_code == 0, res.output
    assert "scan_market ok: SOL" in res.output and "SOL: SOL: LONG practice trade" in res.output
    assert CliRunner().invoke(cli.app, ["watch", "--source", "fixture", "--cycles", "1"]).exit_code != 0
