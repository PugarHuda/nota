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


def test_merge_from_unions_two_snapshots_and_keeps_this_sides_copy(tmp_path):
    """The ledger cycle's push-conflict path: neither side's day may be lost."""
    mine, theirs = Ledger(str(tmp_path / "mine.db")), Ledger(str(tmp_path / "theirs.db"))
    mine.save_lock("L1", "SOL", "2026-09-20T08:30:00+00:00", "locked", '{"side":"mine"}')
    theirs.save_lock("L1", "SOL", "2026-09-20T08:30:00+00:00", "locked", '{"side":"theirs"}')
    theirs.save_lock("L2", "BTC", "2026-09-20T08:31:00+00:00", "locked", '{"b":1}')
    theirs.save_settlement("L2", 24, '{"r":1}')
    theirs.save_stamp("L2", "lock", "ab", b"\x00ots", "pending", "2026-09-20T09:00:00+00:00")
    theirs.conn.close()
    added = mine.merge_from(str(tmp_path / "theirs.db"))
    assert added["locks"] == 1 and added["settlements"] == 1 and added["stamps"] == 1 and added["decisions"] == 0
    assert mine.get_lock("L1") == '{"side":"mine"}' and mine.get_lock("L2") == '{"b":1}'
    assert mine.get_stamp("L2")["ots"] == b"\x00ots"
    assert mine.merge_from(str(tmp_path / "theirs.db"))["locks"] == 0  # idempotent
