"""OpenTimestamps proofs, on bytes real calendars returned.

digest_*.bin: what a.pool.opentimestamps.org, b.pool.opentimestamps.org and a.pool.eternitywall.com answered
on 2026-09-19 to POST /digest of sha256(b"nota ots fixture 2026-09-19"). incomplete.txt.ots is the official
client's pending example (opentimestamps-client/examples); timestamp_alice_428648.bin is what its calendar
answered for that commitment on 2026-09-19, and mempool_block_428648.json is mempool.space's block 428648.
"""

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

import httpx
import pytest

from nota import stamp as S
from nota.ledger import Ledger

OTS = Path(__file__).parent / "fixtures" / "ots"
DIGEST = hashlib.sha256(b"nota ots fixture 2026-09-19").digest()
BLOCK = json.loads((OTS / "mempool_block_428648.json").read_text())
INCOMPLETE = (OTS / "incomplete.txt.ots").read_bytes()
INCOMPLETE_TXT = "The timestamp on this file is incomplete, and can be upgraded.\n"
INCOMPLETE_DIGEST = hashlib.sha256(INCOMPLETE_TXT.encode()).digest()


def calendars(fail: set[str] = frozenset()):
    seen = []

    def handler(req):
        seen.append(str(req.url))
        host = req.url.host
        if req.method == "POST":
            assert req.url.path == "/digest"
            if host in fail:
                return httpx.Response(503)
            if req.content == INCOMPLETE_DIGEST:  # the official example's own pending reply, from alice
                return httpx.Response(200, content=INCOMPLETE[len(S.HEADER) + 2 + 32:]) if host == "a.pool.opentimestamps.org" else httpx.Response(503)
            assert req.content == DIGEST
            return httpx.Response(200, content=(OTS / f"digest_{host}.bin").read_bytes())
        if host == "alice.btc.calendar.opentimestamps.org":
            return httpx.Response(200, content=(OTS / "timestamp_alice_428648.bin").read_bytes())
        if req.url.path == "/api/block-height/428648":
            return httpx.Response(200, text=BLOCK["id"])
        if req.url.path == f"/api/block/{BLOCK['id']}":
            return httpx.Response(200, json=BLOCK)
        return httpx.Response(404, text="Pending confirmation in Bitcoin blockchain")
    return httpx.Client(transport=httpx.MockTransport(handler)), seen


def test_stamp_writes_the_standard_header_and_merges_every_calendar():
    http, _ = calendars()
    ots = S.stamp(DIGEST, http)
    assert ots.startswith(b"\x00OpenTimestamps\x00\x00Proof\x00\xbf\x89\xe2\xe8\x84\xe8\x92\x94" + b"\x01" + b"\x08" + DIGEST)
    digest, root = S.read_ots(ots)
    assert digest == DIGEST and len(root.ops) == 3  # one fork per calendar, each starting with its own nonce
    assert S.pack_ots(digest, root) == ots
    assert S.info(ots) == {"status": "pending", "block": None, "calendars": [
        "https://alice.btc.calendar.opentimestamps.org", "https://bob.btc.calendar.opentimestamps.org",
        "https://finney.calendar.eternitywall.com"]}
    # one calendar alone is the single-reply layout: header, version, sha256, digest, the reply verbatim
    one, _ = calendars()
    single = S.stamp(DIGEST, one, calendars=["https://a.pool.opentimestamps.org"])
    assert single == S.HEADER + b"\x01\x08" + DIGEST + (OTS / "digest_a.pool.opentimestamps.org.bin").read_bytes()


def test_stamp_survives_a_calendar_down_but_not_all_of_them():
    http, _ = calendars(fail={"a.pool.opentimestamps.org"})
    assert len(S.info(S.stamp(DIGEST, http))["calendars"]) == 2
    dead, _ = calendars(fail={"a.pool.opentimestamps.org", "b.pool.opentimestamps.org", "a.pool.eternitywall.com"})
    with pytest.raises(S.StampError, match="no calendar answered"):
        S.stamp(DIGEST, dead)


def test_a_garbled_reply_is_refused_not_stored():
    with pytest.raises(S.StampError):
        S.parse(b"\xf0\x08abc", DIGEST)
    with pytest.raises(S.StampError):
        S.read_ots(b"not a proof")


def test_upgrade_adopts_a_bitcoin_attestation_only_when_the_block_header_agrees():
    pending = (OTS / "incomplete.txt.ots").read_bytes()
    assert S.info(pending)["status"] == "pending"
    http, _ = calendars()
    upgraded, meta = S.upgrade(pending, http)
    assert meta["status"] == "bitcoin" and meta["block"] == 428648
    _, root = S.read_ots(upgraded)
    msgs = [n.msg for n in S.walk(root) for t, _ in n.attestations if t == S.BITCOIN]
    assert msgs == [bytes.fromhex(BLOCK["merkle_root"])[::-1]]

    def lying(req):  # a calendar path to a block whose merkle root is something else
        if "mempool" in req.url.host and "/block/" in req.url.path:
            return httpx.Response(200, json={**BLOCK, "merkle_root": "00" * 32})
        return calendars()[0]._transport.handle_request(req)
    same, meta = S.upgrade(pending, httpx.Client(transport=httpx.MockTransport(lying)))
    assert meta["status"] == "pending" and same == pending


def test_upgrade_never_asks_a_host_outside_the_calendar_list():
    root = S.Node(DIGEST, attestations=[(S.PENDING, S._varbytes(b"https://attacker.example"))])
    http, seen = calendars()
    _, meta = S.upgrade(S.pack_ots(DIGEST, root), http)
    assert meta["status"] == "pending" and seen == []


def test_stamp_ledger_stamps_then_upgrades_pending_proofs(tmp_path):
    # the lock's stored JSON is the official example's text, so its proof is the real one end to end; the
    # decision's digest is the fixture digest, whose calendars have not reached a block
    assert S.read_ots(INCOMPLETE)[0] == INCOMPLETE_DIGEST
    led = Ledger(str(tmp_path / "l.db"))
    led.save_lock("lock1", "BTC", "2026-09-19T09:00:00+00:00", "locked", INCOMPLETE_TXT)
    led.save_lock("fail1", "ETH", "2026-09-19T09:00:00+00:00", "ryo_unavailable", '{"id": "fail1"}')
    led.save_decision("dec1", "h", "BTC", "m", "nota ots fixture 2026-09-19")
    http, _ = calendars()
    t0 = datetime(2026, 9, 19, 10, tzinfo=timezone.utc)
    out = S.stamp_ledger(led, http, now=t0)
    assert sorted(out) == ["decision dec1: submitted to 3 calendar(s)", "lock lock1: submitted to 1 calendar(s)"]  # a failed lock is not final
    lock = led.get_stamp("lock1")
    assert lock["status"] == "pending" and lock["digest"] == INCOMPLETE_DIGEST.hex() and bytes(lock["ots"]) == INCOMPLETE
    assert S.stamp_ledger(led, http, now=t0) == []  # nothing new, and nothing pending for 3 h yet
    out = S.stamp_ledger(led, http, now=datetime(2026, 9, 19, 14, tzinfo=timezone.utc))
    assert out == ["lock lock1: anchored in Bitcoin block 428648"]
    lock, dec = led.get_stamp("lock1"), led.get_stamp("dec1")
    assert (lock["status"], lock["block"], lock["stamped_at"], lock["upgraded_at"]) == ("bitcoin", 428648, "2026-09-19T10:00:00+00:00", "2026-09-19T14:00:00+00:00")
    assert dec["status"] == "pending" and dec["block"] is None
    assert S.view(lock, "/x.ots") == {"status": "bitcoin", "block": 428648, "stamped_at": "2026-09-19T10:00:00+00:00",
                                      "upgraded_at": "2026-09-19T14:00:00+00:00", "digest": INCOMPLETE_DIGEST.hex(), "ots_url": "/x.ots"}
