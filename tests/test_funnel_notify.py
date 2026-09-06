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
    pack = gather(RecordedRyoClient(FIXTURES, name="fixture"), "SOL")
    assert pack.sections["compare"].status == "partial" and pack.get("compare.data.leader") == "SOL"
    assert "compare" in _section_view(pack, ROLE_SECTIONS["macro"]) and "compare" in _section_view(pack, ROLE_SECTIONS["technician"])
    assert "compare.data.tokens.0.momentum_score" in pack.available_paths()


def test_candidate_symbols_walks_any_shape():
    data = {"results": [{"symbol": "sol", "score": 1}, {"ticker": "AVAX"}, {"symbol": "SOL"}], "meta": {"symbol": None}, "top": {"symbol": "bnb"}}
    assert candidate_symbols(data) == ["SOL", "AVAX", "BNB"]
    assert candidate_symbols({"x": 1}) == []


def test_due_only_after_horizon():
    led = Ledger(":memory:")
    r = decide("SOL", RecordedRyoClient(FIXTURES, name="fixture"), make_llm(), led)
    assert due(led) == [] and led.unresolved() == [r.id]
    led.conn.execute("UPDATE decisions SET receipt_json=json_set(receipt_json,'$.created_at','2020-01-01T00:00:00+00:00') WHERE id=?", (r.id,))
    assert due(led) == [r.id]


def _receipt():
    led = Ledger(":memory:")
    return decide("SOL", RecordedRyoClient(FIXTURES, name="fixture"), make_llm(), led)


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
