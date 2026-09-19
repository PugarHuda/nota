"""OpenTimestamps: anchor each scorecard lock and each receipt in Bitcoin, so "locked before the move"
does not rest on Nota's own clock or on a database Nota could rewrite.

`stamp(digest)` submits a SHA-256 digest to public OpenTimestamps calendars and returns a standard
`.ots` proof. A calendar answers at once with a *pending* attestation (a promise to include the digest
in a Bitcoin transaction); a few hours later `upgrade(ots)` fetches the path from the digest to a block
header. The block is not taken on the calendar's word: its merkle root is checked against the block
header mempool.space serves before the status becomes `bitcoin`. Anyone can re-check independently with
`ots verify` or at opentimestamps.org, which is the point.

The file format is the one python-opentimestamps writes (magic header, version 1, the file-hash op, the
digest, then the serialized timestamp tree); the parser below reads it back so every calendar reply is
validated before it is stored. Only what calendars actually emit is implemented: the append, prepend and
hash ops, pending and Bitcoin attestations (other attestation kinds are kept verbatim, never dropped).
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse

import httpx

HEADER = b"\x00OpenTimestamps\x00\x00Proof\x00\xbf\x89\xe2\xe8\x84\xe8\x92\x94"
VERSION = 1
OP_SHA256 = 0x08
PENDING = bytes.fromhex("83dfe30d2ef90c8e")
BITCOIN = bytes.fromhex("0588960d73d71901")
CALENDARS = ["https://a.pool.opentimestamps.org", "https://b.pool.opentimestamps.org", "https://a.pool.eternitywall.com"]
# A pending attestation names the calendar to ask later. Only these hosts are ever asked, so a stored
# proof cannot make the ledger job fetch an arbitrary URL.
CALENDAR_HOSTS = ("opentimestamps.org", "eternitywall.com")
MEMPOOL = "https://mempool.space/api"
ACCEPT = {"Accept": "application/vnd.opentimestamps.v1", "User-Agent": "nota-ots"}
_UNARY = {0x08: lambda m: hashlib.sha256(m).digest(), 0x02: lambda m: hashlib.sha1(m).digest(),
          0x03: lambda m: hashlib.new("ripemd160", m).digest(), 0xF2: lambda m: m[::-1], 0xF3: lambda m: m.hex().encode()}
_BINARY = {0xF0: lambda m, a: m + a, 0xF1: lambda m, a: a + m}
MAX_DEPTH = 256


class StampError(Exception):
    pass


@dataclass
class Node:
    """One message in the timestamp tree: attestations made on it, and ops leading to further messages."""
    msg: bytes
    attestations: list[tuple[bytes, bytes]] = field(default_factory=list)  # (8-byte tag, payload)
    ops: list[tuple[int, bytes | None, "Node"]] = field(default_factory=list)


class _Reader:
    def __init__(self, data: bytes):
        self.data, self.pos = data, 0

    def read(self, n: int) -> bytes:
        if self.pos + n > len(self.data):
            raise StampError("truncated timestamp")
        out = self.data[self.pos:self.pos + n]
        self.pos += n
        return out

    def byte(self) -> int:
        return self.read(1)[0]

    def varuint(self) -> int:
        value = shift = 0
        while True:
            b = self.byte()
            value |= (b & 0x7F) << shift
            if not b & 0x80:
                return value
            shift += 7

    def varbytes(self) -> bytes:
        return self.read(self.varuint())


def _varuint(n: int) -> bytes:
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        out.append(b | (0x80 if n else 0))
        if not n:
            return bytes(out)


def _varbytes(b: bytes) -> bytes:
    return _varuint(len(b)) + b


def parse(data: bytes, msg: bytes) -> Node:
    """A serialized timestamp (a calendar reply, or the body of an .ots after the digest) on MSG."""
    r = _Reader(data)
    node = _parse(r, msg, 0)
    if r.pos != len(data):
        raise StampError("trailing bytes after timestamp")
    return node


def _parse(r: _Reader, msg: bytes, depth: int) -> Node:
    if depth > MAX_DEPTH:
        raise StampError("timestamp nested too deep")
    node = Node(msg)

    def item(tag: int) -> None:
        if tag == 0x00:
            node.attestations.append((r.read(8), r.varbytes()))
        elif tag in _BINARY:
            arg = r.varbytes()
            node.ops.append((tag, arg, _parse(r, _BINARY[tag](msg, arg), depth + 1)))
        elif tag in _UNARY:
            node.ops.append((tag, None, _parse(r, _UNARY[tag](msg), depth + 1)))
        else:
            raise StampError(f"unsupported op 0x{tag:02x}")

    tag = r.byte()
    while tag == 0xFF:  # a fork: this item, then more on the same message
        item(r.byte())
        tag = r.byte()
    item(tag)
    return node


def serialize(node: Node) -> bytes:
    items = [b"\x00" + t + _varbytes(p) for t, p in node.attestations]
    items += [bytes([op]) + (_varbytes(arg) if arg is not None else b"") + serialize(child) for op, arg, child in node.ops]
    if not items:
        raise StampError("empty timestamp")
    return b"".join(b"\xff" + i for i in items[:-1]) + items[-1]


def merge(into: Node, other: Node) -> None:
    for a in other.attestations:
        if a not in into.attestations:
            into.attestations.append(a)
    for op, arg, child in other.ops:
        same = next((c for o, a, c in into.ops if o == op and a == arg), None)
        if same is None:
            into.ops.append((op, arg, child))
        else:
            merge(same, child)


def walk(node: Node):
    yield node
    for _, _, child in node.ops:
        yield from walk(child)


def pack_ots(digest: bytes, root: Node) -> bytes:
    return HEADER + _varuint(VERSION) + bytes([OP_SHA256]) + digest + serialize(root)


def read_ots(ots: bytes) -> tuple[bytes, Node]:
    r = _Reader(ots)
    if r.read(len(HEADER)) != HEADER:
        raise StampError("not an OpenTimestamps proof")
    if r.varuint() != VERSION:
        raise StampError("unsupported .ots version")
    if r.byte() != OP_SHA256:
        raise StampError("only sha256 file digests are used here")
    digest = r.read(32)
    return digest, parse(ots[r.pos:], digest)


def _pending_uri(payload: bytes) -> str:
    return _Reader(payload).varbytes().decode("utf-8", "replace")


def _height(payload: bytes) -> int:
    return _Reader(payload).varuint()


def info(ots: bytes) -> dict[str, Any]:
    """`bitcoin` with the lowest block height when any Bitcoin attestation is present, else `pending`."""
    _, root = read_ots(ots)
    blocks, calendars = [], []
    for n in walk(root):
        for tag, payload in n.attestations:
            if tag == BITCOIN:
                blocks.append(_height(payload))
            elif tag == PENDING:
                calendars.append(_pending_uri(payload))
    return {"status": "bitcoin" if blocks else "pending", "block": min(blocks) if blocks else None, "calendars": calendars}


def _client(http: httpx.Client | None) -> httpx.Client:
    return http or httpx.Client(timeout=20.0, headers=ACCEPT)


def stamp(digest: bytes, http: httpx.Client | None = None, calendars: list[str] | None = None) -> bytes:
    """Submit DIGEST to each calendar and merge their replies into one .ots. One calendar answering is
    enough; none answering raises StampError and the subject stays unstamped for the next run."""
    if len(digest) != 32:
        raise StampError("digest must be 32 bytes (sha256)")
    http = _client(http)
    root, errors = Node(digest), []
    for cal in calendars or CALENDARS:
        try:
            resp = http.post(f"{cal}/digest", content=digest, headers=ACCEPT)
            if resp.status_code != 200:
                raise StampError(f"HTTP {resp.status_code}")
            merge(root, parse(resp.content, digest))  # parsed first: a garbled reply is never stored
        except (httpx.HTTPError, StampError) as exc:
            errors.append(f"{cal}: {type(exc).__name__} {exc}")
    if not root.ops and not root.attestations:
        raise StampError("no calendar answered: " + "; ".join(errors))
    return pack_ots(digest, root)


def _allowed(uri: str) -> bool:
    u = urlparse(uri)
    host = u.hostname or ""
    return u.scheme == "https" and any(host == h or host.endswith("." + h) for h in CALENDAR_HOSTS)


def block_merkle_root(height: int, http: httpx.Client) -> bytes | None:
    """The merkle root of block HEIGHT from mempool.space, in the byte order OpenTimestamps commits to."""
    try:
        h = http.get(f"{MEMPOOL}/block-height/{height}")
        b = http.get(f"{MEMPOOL}/block/{h.text.strip()}") if h.status_code == 200 else None
        root = b.json().get("merkle_root") if b is not None and b.status_code == 200 else None
        return bytes.fromhex(root)[::-1] if isinstance(root, str) and len(root) == 64 else None
    except (httpx.HTTPError, ValueError):
        return None


def upgrade(ots: bytes, http: httpx.Client | None = None) -> tuple[bytes, dict[str, Any]]:
    """Ask each pending attestation's calendar for the completed path. A reply is merged only when every
    Bitcoin attestation it brings commits to that block's real merkle root; anything else leaves the
    proof as it was, to be tried again. Pending attestations are kept, as the reference client does."""
    http = _client(http)
    digest, root = read_ots(ots)
    for node in list(walk(root)):
        for tag, payload in list(node.attestations):
            uri = _pending_uri(payload) if tag == PENDING else ""
            if not uri or not _allowed(uri):
                continue
            try:
                resp = http.get(f"{uri}/timestamp/{node.msg.hex()}", headers=ACCEPT)
                if resp.status_code != 200:  # 404 = not in a block yet
                    continue
                got = parse(resp.content, node.msg)
            except (httpx.HTTPError, StampError):
                continue
            atts = [(n.msg, _height(p)) for n in walk(got) for t, p in n.attestations if t == BITCOIN]
            if atts and all(block_merkle_root(h, http) == m for m, h in atts):
                merge(node, got)
    out = pack_ots(digest, root)
    return out, info(out)


UPGRADE_AFTER_H = 3  # calendars commit to Bitcoin every few hours; asking sooner only returns 404


def digest_of(stored: str) -> bytes:
    """What is anchored: the SHA-256 of the lock row or receipt exactly as the ledger stores it, which is
    also what /api/scorecard/locks/<id>.json and /r/<id>.json serve, so anyone can recompute it."""
    return hashlib.sha256(stored.encode("utf-8")).digest()


def stamp_ledger(ledger: Any, http: httpx.Client | None = None, now: Any = None) -> list[str]:
    """Stamp every unstamped lock and decision, then try to upgrade proofs pending for over
    UPGRADE_AFTER_H hours. Returns one line per subject touched; a calendar outage leaves the subject for
    the next run rather than storing a proof that is not one."""
    from datetime import datetime, timedelta, timezone

    now = now or datetime.now(timezone.utc)
    iso = lambda t: t.isoformat(timespec="seconds")
    http = _client(http)
    out = []
    for kind, subject, body in ledger.unstamped():
        d = digest_of(body)
        try:
            ots = stamp(d, http)
        except StampError as exc:
            out.append(f"{kind} {subject}: not stamped ({exc})")
            continue
        ledger.save_stamp(subject, kind, d.hex(), ots, "pending", iso(now))
        out.append(f"{kind} {subject}: submitted to {len(info(ots)['calendars'])} calendar(s)")
    for row in ledger.pending_stamps(iso(now - timedelta(hours=UPGRADE_AFTER_H))):
        try:
            ots, meta = upgrade(bytes(row["ots"]), http)
        except StampError as exc:
            out.append(f"{row['kind']} {row['subject']}: upgrade failed ({exc})")
            continue
        if meta["status"] == "bitcoin":
            ledger.save_stamp(row["subject"], row["kind"], row["digest"], ots, "bitcoin", row["stamped_at"], iso(now), meta["block"])
            out.append(f"{row['kind']} {row['subject']}: anchored in Bitcoin block {meta['block']}")
    return out


def view(row: dict[str, Any] | None, url: str) -> dict[str, Any] | None:
    """What a page may say about a proof: only the stored status, never an assumed one."""
    if not row:
        return None
    return {"status": row["status"], "block": row["block"], "stamped_at": row["stamped_at"],
            "upgraded_at": row["upgraded_at"], "digest": row["digest"], "ots_url": url}
