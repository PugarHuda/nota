"""Decision receipt: everything a reader needs to audit one decision, in one document.

The id is derived from (pack_hash, model, prompt_version), so deciding twice on identical
evidence produces the same receipt id instead of a duplicate.
"""

from __future__ import annotations

import hashlib
from typing import Any

from pydantic import BaseModel, Field

from nota.council import CouncilResult, Opinion, Verdict
from nota.evidence import EvidencePack
from nota.ledger import now_iso
from nota.risk import Blocked, PracticeTrade
from nota.scorecard import BEARISH, BULLISH


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
    # What this decision cost to produce, as the provider reported it. Not compared on replay - a
    # cached rebuild spends nothing - so it carries no reproducibility claim, only a measurement.
    spend: dict[str, Any] | None = None
    # RYO's own call on the same evidence, read from the hashed pack, and whether the council's direction
    # matches it. None on receipts made before these existed, and when either side took no direction.
    ryo_view: dict[str, Any] | None = None
    agrees_with_ryo: bool | None = None


def receipt_id(pack_hash: str, model: str, prompt_version: str) -> str:
    return hashlib.sha256(f"{pack_hash}|{model}|{prompt_version}".encode()).hexdigest()[:12]


def ryo_view(pack: EvidencePack) -> dict[str, Any] | None:
    """What RYO itself concluded: deep_analysis's call and why, analyze_token's verdict, compare's pick."""
    view = {
        "deep_verdict": pack.get("deep_analysis.data.verdict.call"),
        "key_driver": pack.get("deep_analysis.data.verdict.key_driver"),
        "what_changes_it": pack.get("deep_analysis.data.verdict.what_changes_it"),
        "confluence_state": pack.get("deep_analysis.data.confluence.state"),
        "analyze_verdict": pack.get("analyze_token.data.verdict"),
        "compare_pick": pack.get("compare.data.conclusion.pick"),
        "compare_confidence": pack.get("compare.data.conclusion.confidence"),
    }
    return view if any(v is not None for v in view.values()) else None


def ryo_direction(view: dict[str, Any] | None) -> str | None:
    """RYO's call as a side: deep_analysis first, analyze_token when deep_analysis gave none."""
    for key in ("deep_verdict", "analyze_verdict"):
        call = str((view or {}).get(key) or "").lower()
        if call in BULLISH:
            return "long"
        if call in BEARISH:
            return "short"
    return None


def build_receipt(pack: EvidencePack, council: CouncilResult, trade: PracticeTrade | Blocked) -> Receipt:
    if isinstance(trade, PracticeTrade):
        headline = f"{pack.symbol}: {trade.side.upper()} practice trade, {trade.size_usd:.0f} USD, stop {trade.stop_price:g}"
    else:
        headline = f"{pack.symbol}: no trade ({trade.reason})"
    ryo = ryo_view(pack)
    ryo_side = ryo_direction(ryo)
    action = council.verdict.action
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
        spend=council.spend,
        ryo_view=ryo,
        agrees_with_ryo=(action == ryo_side) if ryo_side and action != "no_trade" else None,
    )


def ryo_said(r: Receipt) -> str:
    """One line: RYO's call beside the council's, e.g. 'RYO said: constructive (confluence CONFIRMED) - council: long, agrees'."""
    v = r.ryo_view or {}
    call = v.get("deep_verdict") or v.get("analyze_verdict") or "no call"
    extra = f" (confluence {v['confluence_state']})" if v.get("confluence_state") else ""
    agree = {True: "agrees", False: "disagrees", None: "no comparison"}[r.agrees_with_ryo]
    out = f"RYO said: {call}{extra} - council: {r.verdict.action}, {agree}"
    if v.get("key_driver"):
        out += f". RYO's driver: {v['key_driver']}"
    return out


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
    if r.ryo_view:
        lines += ["## RYO said", "", f"- {ryo_said(r)}", ""]
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
        v = t.vs_ryo_plan
        if v:
            agree = "same direction" if v.get("agrees_on_direction") else "opposite direction"
            lines.append(
                f"- Against RYO's own plan (`{v['path']}`, {v.get('method')}): RYO stops at "
                f"{v.get('ryo_stop')} and targets {v.get('ryo_target')} on a {v.get('ryo_atr_multiplier')}x ATR; "
                f"this sizing uses {v.get('nota_atr_multiplier')}x, so the stop sits "
                f"{v.get('stop_diff_pct')}% and the target {v.get('target_diff_pct')}% of entry away, {agree}.")
    else:
        lines.append(f"- Blocked: {t.reason}")
    if r.spend:
        s = r.spend
        parts = [f"{s['model_calls']} model call{'' if s['model_calls'] == 1 else 's'}"]
        if s["cached_calls"]:
            parts.append(f"{s['cached_calls']} served from cache")
        for label, key in (("prompt tokens", "prompt_tokens"), ("completion tokens", "completion_tokens")):
            parts.append(f"{s[key]:,} {label}" if s[key] is not None else f"{label} not reported")
        parts.append(f"{s['usd']} USD as the provider billed it" if s["usd"] is not None else "cost not reported")
        lines += ["", "## What this cost", "", f"- {', '.join(parts)}, in {s['ms']} ms."]
    lines += ["", "_Research on read-only RYO evidence. No order was placed. Not financial advice._"]
    return "\n".join(lines)
