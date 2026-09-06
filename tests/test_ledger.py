from nota.ledger import Ledger


def test_pack_roundtrip_and_idempotent():
    led = Ledger(":memory:")
    led.save_pack("h1", "SOL", "fixture", '{"a":1}')
    led.save_pack("h1", "SOL", "fixture", '{"a":2}')  # ignored: same hash, same content by definition
    assert led.get_pack("h1") == '{"a":1}'
    assert led.get_pack("nope") is None


def test_llm_cache_roundtrip():
    led = Ledger(":memory:")
    assert led.get_cached("k") is None
    led.put_cached("k", "h1", "macro", "v1", "claude-opus-5", '{"stance":"bullish"}')
    assert led.get_cached("k") == '{"stance":"bullish"}'


def test_decisions_outcomes_and_unresolved():
    led = Ledger(":memory:")
    led.save_decision("d1", "h1", "SOL", "m", '{"id":"d1"}')
    led.save_decision("d2", "h2", "BTC", "m", '{"id":"d2"}')
    assert led.get_decision("d1") == '{"id":"d1"}'
    assert {d["id"] for d in led.list_decisions()} == {"d1", "d2"}
    assert [d["id"] for d in led.list_decisions(symbol="BTC")] == ["d2"]
    assert led.unresolved() == ["d1", "d2"]
    led.save_outcome("d1", '{"went_up":true}')
    assert led.unresolved() == ["d2"]
    assert led.get_outcome("d1") == '{"went_up":true}'
    assert led.list_outcomes() == ['{"went_up":true}']
