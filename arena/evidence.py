"""Evidence pack: the fixed set of RYO reads a decision is made from.

Gathering is tolerant: a failed tool becomes a section with status `error` and the pack is
still built. The pack hash covers only RYO's envelopes, not our own timestamps, so the same
evidence always hashes the same.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Callable, Literal

from pydantic import BaseModel, Field

from arena.envelope import Envelope
from arena.ledger import now_iso
from arena.ryo_client import RyoError, RyoSource

SectionStatus = Literal["ok", "partial", "unavailable", "error"]

# section key -> tool
SECTIONS: dict[str, str] = {
    "market_overview": "market_overview",
    "sentiment_shift": "monitor_market_sentiment_shift",
    "deep_analysis": "deep_analysis",
    "analyze_token": "analyze_token",
    "compare": "compare_tokens",
}
PRIMARY = "deep_analysis"
BENCHMARKS = ("BTC", "ETH", "SOL")  # relative-strength peers; the token itself is excluded


def ryo_args(symbol: str, include_perp: bool = True) -> dict[str, dict[str, Any]]:
    """The exact arguments every section is called with. `record` uses the same map, so a recorded
    fixture is guaranteed to match what `gather` asks for."""
    symbol = symbol.upper()
    peers = [b for b in BENCHMARKS if b != symbol][:2]
    return {
        "market_overview": {},
        "sentiment_shift": {},
        "deep_analysis": {"symbol": symbol, "include_perp": include_perp},
        "analyze_token": {"symbol": symbol},
        "compare": {"symbols": ", ".join([symbol, *peers]), "intent": "swing"},
    }


class Section(BaseModel):
    tool: str
    status: SectionStatus
    envelope: Envelope | None = None
    error: str | None = None


class EvidencePack(BaseModel):
    symbol: str
    created_at: str
    source: str
    sections: dict[str, Section] = Field(default_factory=dict)

    # -- identity ------------------------------------------------------------------------
    def pack_hash(self) -> str:
        canon = {
            k: (s.envelope.model_dump(mode="json", exclude={"trace_id"}) if s.envelope else {"error": s.error})
            for k, s in sorted(self.sections.items())
        }
        return hashlib.sha256(json.dumps({"symbol": self.symbol, "sections": canon}, sort_keys=True).encode()).hexdigest()

    # -- lookups ---------------------------------------------------------------------------
    def get(self, path: str) -> Any | None:
        """`<section>.data.<...>` or `<section>.<envelope field>`. Missing/null -> None."""
        section, _, rest = path.partition(".")
        sec = self.sections.get(section)
        if not sec or sec.envelope is None:
            return None
        if rest.startswith("data."):
            return sec.envelope.get(rest[len("data.") :])
        if rest == "data":
            return sec.envelope.data
        node: Any = sec.envelope.model_dump(mode="json")
        for part in rest.split("."):
            if isinstance(node, dict):
                node = node.get(part)
            else:
                return None
            if node is None:
                return None
        return node

    def available_paths(self) -> dict[str, Any]:
        """Every leaf path under `<section>.data` that holds a non-null value, mapped to that value.
        Used to validate citations and to fill each citation with the evidence's own number."""
        out: dict[str, Any] = {}

        def walk(prefix: str, node: Any) -> None:
            if isinstance(node, dict):
                for k, v in node.items():
                    walk(f"{prefix}.{k}", v)
            elif isinstance(node, list):
                for i, v in enumerate(node):
                    walk(f"{prefix}.{i}", v)
            elif node is not None:
                out[prefix] = node

        for key, sec in self.sections.items():
            if sec.envelope is not None:
                walk(f"{key}.data", sec.envelope.data)
        return out

    @property
    def primary_ok(self) -> bool:
        sec = self.sections.get(PRIMARY)
        return bool(sec and sec.status in ("ok", "partial"))

    def availability(self) -> dict[str, SectionStatus]:
        return {k: s.status for k, s in self.sections.items()}

    def warnings(self) -> list[str]:
        out: list[str] = []
        for k, s in self.sections.items():
            if s.error:
                out.append(f"{k}: {s.error}")
            if s.envelope:
                out.extend(f"{k}: {w}" for w in s.envelope.warnings)
        return out

    def provenance(self) -> dict[str, dict[str, Any]]:
        return {
            k: {"tool": s.tool, "as_of": s.envelope.as_of if s.envelope else None,
                "data_mode": s.envelope.data_mode if s.envelope else None,
                "trace_id": s.envelope.trace_id if s.envelope else None, "status": s.status}
            for k, s in self.sections.items()
        }


def first_present(pack: EvidencePack, candidates: list[str]) -> tuple[str | None, Any]:
    for p in candidates:
        v = pack.get(p)
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            return p, v
        if isinstance(v, str):
            try:
                return p, float(v)
            except ValueError:
                continue
    return None, None


def candidate_symbols(node: Any) -> list[str]:
    """Symbols found anywhere in a scan_market payload (`symbol`/`ticker` keys), in order, de-duplicated.
    ponytail: schema-tolerant walk; trim to the real key once a live scan is recorded."""
    out: list[str] = []

    def walk(n: Any) -> None:
        if isinstance(n, dict):
            for k in ("symbol", "ticker"):
                v = n.get(k)
                if isinstance(v, str) and v.strip() and v.upper() not in out:
                    out.append(v.strip().upper())
            for v in n.values():
                walk(v)
        elif isinstance(n, list):
            for v in n:
                walk(v)

    walk(node)
    return out


Extra = Callable[[str, "EvidencePack"], Envelope]  # our own skill, called with the symbol and the RYO sections gathered so far


def gather(source: RyoSource, symbol: str, include_perp: bool = True, extras: dict[str, Extra] | None = None) -> EvidencePack:
    symbol = symbol.upper()
    args = ryo_args(symbol, include_perp)
    pack = EvidencePack(symbol=symbol, created_at=now_iso(), source=source.name)
    for key, tool in SECTIONS.items():
        try:
            env = source.call(tool, args[key])
            pack.sections[key] = Section(tool=tool, status=env.status, envelope=env)
        except RyoError as exc:
            pack.sections[key] = Section(tool=tool, status="error", error=f"{exc.code}: {exc.message}")
    for key, fn in (extras or {}).items():
        try:
            env = fn(symbol, pack)
            pack.sections[key] = Section(tool=env.tool, status=env.status, envelope=env)
        except Exception as exc:  # a skill must never take the decision down with it
            pack.sections[key] = Section(tool=key, status="error", error=f"{type(exc).__name__}: {exc}")
    return pack
