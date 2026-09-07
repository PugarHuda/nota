"""Replay a stored decision from its stored evidence.

Default replay reuses cached LLM outputs, so it must reproduce the receipt exactly; that is the
repeatability guarantee - and because it only reads the ledger, it needs no API key and no network:
anyone who clones the repo can verify a receipt. `fresh=True` calls the configured model again on
the same evidence and reports the differences honestly as model drift.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel

from nota.calibration import role_scores, role_weights
from nota.council import run_council
from nota.evidence import EvidencePack
from nota.ledger import Ledger
from nota.llm import LLM
from nota.receipt import Receipt, build_receipt
from nota.risk import RiskLimits, size_trade

COMPARE_FIELDS = ("opinions", "verdict", "trade", "headline", "availability")


class CacheOnlyLLM:
    """Stands in for the receipt's own model during a cached replay: it carries that model's name so
    the cache keys match, and refuses to be called, so a verification can never spend money or drift."""

    def __init__(self, model: str):
        self.model = model

    def complete_json(self, system: str, user: str, schema: Any) -> Any:
        raise RuntimeError(
            "cache miss: this receipt's model outputs are not in the ledger, so it cannot be "
            "verified from the cache. Re-run it with --fresh to see how today's model differs."
        )


class ReplayResult(BaseModel):
    original: Receipt
    replayed: Receipt
    fresh: bool
    identical: bool
    diff: list[str]


def _flat(prefix: str, node: Any, out: dict[str, Any]) -> None:
    if isinstance(node, dict):
        for k, v in node.items():
            _flat(f"{prefix}.{k}" if prefix else k, v, out)
    elif isinstance(node, list):
        for i, v in enumerate(node):
            _flat(f"{prefix}.{i}", v, out)
    else:
        out[prefix] = node


def diff_receipts(a: Receipt, b: Receipt) -> list[str]:
    fa: dict[str, Any] = {}
    fb: dict[str, Any] = {}
    _flat("", a.model_dump(mode="json", include=set(COMPARE_FIELDS)), fa)
    _flat("", b.model_dump(mode="json", include=set(COMPARE_FIELDS)), fb)
    out = []
    for k in sorted(set(fa) | set(fb)):
        if fa.get(k) != fb.get(k):
            out.append(f"{k}: {fa.get(k)!r} -> {fb.get(k)!r}")
    return out


def replay(decision_id: str, ledger: Ledger, llm: LLM | None = None, limits: RiskLimits | None = None, fresh: bool = False) -> ReplayResult:
    raw = ledger.get_decision(decision_id)
    if raw is None:
        raise KeyError(f"no decision {decision_id}")
    original = Receipt.model_validate_json(raw)
    pack_json = ledger.get_pack(original.pack_hash)
    if pack_json is None:
        raise KeyError(f"evidence {original.pack_hash} missing from ledger")
    pack = EvidencePack.model_validate_json(pack_json)
    # Cached outputs are keyed by the model that produced them, so a verification runs against the
    # receipt's own model, never whatever happens to be configured now. That also means no key.
    if not fresh:
        llm = CacheOnlyLLM(original.model)
    elif llm is None:
        raise ValueError("a fresh replay calls the model again, so it needs a configured LLM")
    weights = role_weights(role_scores(ledger))
    council = run_council(pack, llm, ledger, weights=weights, use_cache=not fresh, prompt_version=original.prompt_version)
    trade = size_trade(council.verdict, pack, limits)
    replayed = build_receipt(pack, council, trade)
    diff = diff_receipts(original, replayed)
    return ReplayResult(original=original, replayed=replayed, fresh=fresh, identical=not diff, diff=diff)
