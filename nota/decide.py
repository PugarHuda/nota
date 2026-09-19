"""Orchestration: gather -> council -> risk -> receipt -> ledger."""

from __future__ import annotations

from nota.calibration import role_scores, role_weights
from nota.council import council_without_primary, run_council
from nota.evidence import Extra, gather
from nota.ledger import Ledger
from nota.llm import LLM
from nota.receipt import Receipt, build_receipt
from nota.risk import RiskLimits, size_trade
from nota.ryo_client import RyoSource


def decide(symbol: str, source: RyoSource, llm: LLM, ledger: Ledger, limits: RiskLimits | None = None, use_cache: bool = True,
           extras: dict[str, Extra] | None = None) -> Receipt:
    pack = gather(source, symbol, extras=extras)
    ledger.save_pack(pack.pack_hash(), pack.symbol, pack.source, pack.model_dump_json())
    if pack.primary_ok:
        council = run_council(pack, llm, ledger, weights=role_weights(role_scores(ledger)), use_cache=use_cache)
    else:  # risk would block any trade anyway; an empty symbol or a RYO outage should not cost four model calls
        council = council_without_primary(llm.model)
    trade = size_trade(council.verdict, pack, limits)
    receipt = build_receipt(pack, council, trade)
    ledger.save_decision(receipt.id, receipt.pack_hash, receipt.symbol, receipt.model, receipt.model_dump_json())
    return receipt
