"""Decision receipt: everything a reader needs to audit one decision, in one document.

The id is derived from (pack_hash, model, prompt_version), so deciding twice on identical
evidence produces the same receipt id instead of a duplicate.
"""

from __future__ import annotations

import hashlib
from typing import Any

from pydantic import BaseModel, Field

from arena.council import CouncilResult, Opinion, Verdict
from arena.evidence import EvidencePack
from arena.ledger import now_iso
from arena.risk import Blocked, PracticeTrade


class Receipt(BaseModel):
    id: str
    created_at: str
    symbol: str
    pack_hash: str
    source: str
    model: str
    prompt_version: str
    headline: str
    availability: dict[str, str]
    warnings: list[str]
    provenance: dict[str, dict[str, Any]]
    opinions: list[Opinion]
    verdict: Verdict
    trade: PracticeTrade | Blocked = Field(discriminator="kind")
    cache_hits: int = 0


def receipt_id(pack_hash: str, model: str, prompt_version: str) -> str:
    return hashlib.sha256(f"{pack_hash}|{model}|{prompt_version}".encode()).hexdigest()[:12]


def build_receipt(pack: EvidencePack, council: CouncilResult, trade: PracticeTrade | Blocked) -> Receipt:
    if isinstance(trade, PracticeTrade):
        headline = f"{pack.symbol}: {trade.side.upper()} practice trade, {trade.size_usd:.0f} USD, stop {trade.stop_price:g}"
    else:
        headline = f"{pack.symbol}: no trade ({trade.reason})"
    return Receipt(
        id=receipt_id(pack.pack_hash(), council.model, council.prompt_version),
        created_at=now_iso(),
        symbol=pack.symbol,
        pack_hash=pack.pack_hash(),
        source=pack.source,
        model=council.model,
        prompt_version=council.prompt_version,
        headline=headline,
        availability=pack.availability(),
        warnings=pack.warnings(),
        provenance=pack.provenance(),
        opinions=council.opinions,
        verdict=council.verdict,
        trade=trade,
        cache_hits=council.cache_hits,
    )


def render_markdown(r: Receipt) -> str:
    lines = [f"# Decision receipt {r.id}", "", f"**{r.headline}**", "",
             f"- Symbol: {r.symbol}  |  Source: `{r.source}`  |  Model: `{r.model}` (prompts {r.prompt_version})",
             f"- Evidence hash: `{r.pack_hash[:16]}...`  |  Created: {r.created_at}", "", "## Evidence availability", ""]
    for k, p in r.provenance.items():
        lines.append(f"- `{k}` ({p['tool']}): **{p['status']}** | as_of {p['as_of']}, data_mode {p['data_mode']}, trace {p['trace_id']}")
    if r.warnings:
        lines += ["", "## Warnings", ""] + [f"- {w}" for w in r.warnings]
    lines += ["", "## Council", ""]
    for o in r.opinions:
        lines.append(f"### {o.role} | {o.stance} (p_up_7d {o.p_up_7d:.2f}, confidence {o.confidence})")
        lines.append(o.thesis)
        for c in o.citations:
            lines.append(f"- `{c.path}` = {c.value} {('- ' + c.note) if c.note else ''}".rstrip())
        if o.dropped_citations:
            lines.append(f"- _{o.dropped_citations} citation(s) dropped: path not present in evidence_")
        lines.append(f"- Invalidation: {o.invalidation}")
        lines.append("")
    v = r.verdict
    lines += [f"## Verdict: {v.action} (p_up_7d {v.p_up_7d:.2f})", "", v.rationale, ""]
    if v.agreed_with:
        lines.append(f"- Agreed with: {', '.join(v.agreed_with)}")
    if v.disagreed_with:
        lines.append(f"- Disagreed with: {', '.join(v.disagreed_with)}")
    for k in v.key_risks:
        lines.append(f"- Risk: {k}")
    lines += ["", "## Practice trade", ""]
    t = r.trade
    if isinstance(t, PracticeTrade):
        lines += [f"- {t.side.upper()} {t.size_units:g} {t.symbol} ~ {t.size_usd:.2f} USD at {t.entry_price:g}",
                  f"- Stop {t.stop_price:g}  |  Target {t.target_price:g}  |  Risk {t.risk_usd:.2f} USD  |  ATR(14) {t.atr:g}  |  Edge {t.edge:.2f}",
                  f"- Price from `{t.source_paths['price']}`, ATR from `{t.source_paths['atr']}`"]
    else:
        lines.append(f"- Blocked: {t.reason}")
    lines += ["", "_Research on read-only RYO evidence. No order was placed. Not financial advice._"]
    return "\n".join(lines)
