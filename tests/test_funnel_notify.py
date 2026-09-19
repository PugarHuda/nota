import json
from pathlib import Path

import respx
from httpx import Response

from nota import notify
from nota.calibration import due
from nota.council import ROLE_SECTIONS, _section_view
from nota.decide import decide
from nota.evidence import candidate_symbols, gather, ryo_args
from nota.ledger import Ledger
from nota.ryo_client import RecordedRyoClient, fixture_name
from tests.test_decide_replay import make_llm

FIXTURES = Path(__file__).parent / "fixtures"


def test_ryo_args_cover_all_sections_and_exclude_self_from_peers():
    a = ryo_args("BTC")
    assert set(a) == {"market_overview", "sentiment_shift", "deep_analysis", "analyze_token", "compare"}
    assert a["compare"] == {"symbols": "BTC, ETH, SOL", "intent": "swing"}
    assert fixture_name("compare_tokens", ryo_args("SOL")["compare"]) == "SOL-BTC-ETH"


def test_gather_includes_compare_and_macro_sees_it():
    pack = gather(RecordedRyoClient(FIXTURES), "SOL")
    real = json.loads((FIXTURES / "compare_tokens" / "SOL-BTC-ETH.json").read_text(encoding="utf-8"))
    assert pack.sections["compare"].status == real["status"] and pack.get("compare.data.winner") == real["data"]["winner"]
    assert "compare" in _section_view(pack, ROLE_SECTIONS["macro"]) and "compare" in _section_view(pack, ROLE_SECTIONS["technician"])
    assert "compare.data.tokens.0.metrics.rsi_14" in pack.available_paths()


def test_candidate_symbols_walks_any_shape():
    data = {"results": [{"symbol": "sol", "score": 1}, {"ticker": "AVAX"}, {"symbol": "SOL"}], "meta": {"symbol": None}, "top": {"symbol": "bnb"}}
    assert candidate_symbols(data) == ["SOL", "AVAX", "BNB"]
    assert candidate_symbols({"x": 1}) == []


def test_due_only_after_horizon():
    led = Ledger(":memory:")
    r = decide("SOL", RecordedRyoClient(FIXTURES), make_llm(), led)
    assert due(led) == [] and led.unresolved() == [r.id]
    led.conn.execute("UPDATE decisions SET receipt_json=json_set(receipt_json,'$.created_at','2020-01-01T00:00:00+00:00') WHERE id=?", (r.id,))
    assert due(led) == [r.id]


def _receipt():
    led = Ledger(":memory:")
    return decide("SOL", RecordedRyoClient(FIXTURES), make_llm(), led)


def test_notify_reports_not_configured(monkeypatch):
    for k in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "DISCORD_WEBHOOK_URL"):
        monkeypatch.delenv(k, raising=False)
    assert [o["status"] for o in notify.notify_receipt(_receipt())] == ["not_configured", "not_configured"]


@respx.mock
def test_notify_telegram_and_discord_payloads(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:abc")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "-100")
    monkeypatch.setenv("DISCORD_WEBHOOK_URL", "https://discord.com/api/webhooks/1/x")
    monkeypatch.setenv("NOTA_PUBLIC_URL", "https://nota.example")
    tg = respx.post("https://api.telegram.org/bot123:abc/sendMessage").mock(return_value=Response(200, json={"ok": True, "result": {"message_id": 7}}))
    dc = respx.post("https://discord.com/api/webhooks/1/x").mock(return_value=Response(200, json={"id": "99"}))
    r = _receipt()
    out = notify.notify_receipt(r)
    assert out[0]["status"] == "sent" and out[0]["message_id"] == 7
    assert out[1]["status"] == "sent" and out[1]["message_id"] == "99"
    body = json.loads(tg.calls[0].request.content)
    assert body["chat_id"] == "-100" and body["parse_mode"] == "HTML" and "No order was placed" in body["text"] and f"/r/{r.id}" in body["text"]
    embed = json.loads(dc.calls[0].request.content)["embeds"][0]
    assert embed["url"] == f"https://nota.example/r/{r.id}" and embed["title"] == r.headline
    tg.mock(return_value=Response(400, json={"ok": False, "description": "chat not found"}))
    assert notify.telegram(r)["status"] == "error"


def test_scan_direction_is_sent_and_the_pick_reason_travels_into_the_pack(tmp_path, monkeypatch):
    """RYO's real losers scan (filter_direction=negative, recorded 2026-09-19): the CLI sends the direction,
    prints why each token is on the list, skips a ticker that is not letters/digits, and a decision on a
    candidate keeps its own scan row, which enters the pack hash and the technician's and narrative's view."""
    from typer.testing import CliRunner

    from nota import cli
    from nota.evidence import scan_for

    sent = []

    class Capturing(RecordedRyoClient):
        def call(self, tool, args=None):
            sent.append((tool, dict(args or {})))
            return super().call(tool, args)

    monkeypatch.setenv("NOTA_DB", str(tmp_path / "f.db"))
    monkeypatch.setattr(cli, "_source", lambda kind: Capturing(FIXTURES))
    monkeypatch.setattr(cli, "_llm", lambda kind: make_llm())
    res = CliRunner().invoke(cli.app, ["scan", "--source", "recorded", "--direction", "negative", "--decide-top", "1"])
    assert res.exit_code == 0, res.output
    assert sent[0] == ("scan_market", {"top_n": 5, "filter_direction": "negative"})
    scan = json.loads((FIXTURES / "scan_market" / "any-any-negative.json").read_text(encoding="utf-8"))
    first = scan["data"]["candidates"][0]
    assert first["change_24h_pct"] < 0 and f"{first['change_24h_pct']:g}" in res.output and first["reason"] in res.output
    assert "skipped candidate" in res.output          # RYO lists a meme coin whose ticker is not Latin
    assert CliRunner().invoke(cli.app, ["scan", "--source", "recorded", "--direction", "down"]).exit_code == 2

    led = Ledger(str(tmp_path / "f.db"))
    receipt = json.loads(led.get_decision(led.list_decisions(1)[0]["id"]))
    stored = json.loads(led.get_pack(receipt["pack_hash"]))["sections"]["scan"]["envelope"]["data"]
    assert [c["symbol"] for c in stored["candidates"]] == [first["symbol"]] and stored["selection_method"] == scan["data"]["selection_method"]

    from nota.envelope import Envelope

    env = Envelope.model_validate(scan)
    with_scan, without = gather(RecordedRyoClient(FIXTURES), "SOL", scan=env), gather(RecordedRyoClient(FIXTURES), "SOL")
    assert with_scan.pack_hash() != without.pack_hash() and scan_for(env, "SOL").data["candidates"] == []
    assert "scan" in _section_view(with_scan, ROLE_SECTIONS["technician"]) and "scan" in _section_view(with_scan, ROLE_SECTIONS["narrative"])
    assert "scan" not in _section_view(with_scan, ROLE_SECTIONS["macro"])
