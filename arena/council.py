"""The council: specialised agents give cited opinions, a judge issues the verdict.

Determinism: each LLM output is cached in the ledger under
sha256(pack_hash, role, PROMPT_VERSION, model). Replaying the same evidence therefore
reproduces the same receipt without calling the model again.

Honesty: an agent may only cite dotted paths that exist in the evidence pack with a non-null
value. Citations that point at nothing are dropped and counted; an opinion with no surviving
citation is downgraded to low confidence in code, not by asking the model nicely.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from typing import Any, Literal

from pydantic import BaseModel, Field

from arena.evidence import EvidencePack
from arena.ledger import Ledger
from arena.llm import LLM

PROMPT_VERSION = "v2"  # v2: explicit list-index path syntax + cite-only-existing instruction
ROLES: tuple[str, ...] = ("macro", "technician", "narrative")
Confidence = Literal["low", "medium", "high"]


class Citation(BaseModel):
    path: str = Field(description="Dotted path into the evidence pack, e.g. deep_analysis.data.technicals.rsi_14")
    value: str = Field(default="", description="The value you read at that path, as text")
    note: str = Field(default="", description="Why this matters")


class Opinion(BaseModel):
    role: str
    stance: Literal["bullish", "bearish", "neutral"]
    p_up_7d: float = Field(ge=0.0, le=1.0, description="Probability price is higher in 7 days")
    confidence: Confidence
    thesis: str = Field(description="Two to four sentences of reasoning grounded in the citations")
    citations: list[Citation] = Field(default_factory=list)
    invalidation: str = Field(description="What observation would flip this view")
    dropped_citations: int = 0


class Verdict(BaseModel):
    action: Literal["long", "short", "no_trade"]
    p_up_7d: float = Field(ge=0.0, le=1.0)
    rationale: str = Field(description="How the opinions were weighed and why this action follows")
    agreed_with: list[str] = Field(default_factory=list, description="Roles whose view carried the verdict")
    disagreed_with: list[str] = Field(default_factory=list)
    key_risks: list[str] = Field(default_factory=list)


class CouncilResult(BaseModel):
    opinions: list[Opinion]
    verdict: Verdict
    cache_hits: int = 0
    model: str
    prompt_version: str = PROMPT_VERSION


COMMON_RULES = """
Rules you must follow:
- Use only the evidence JSON provided. Do not use outside knowledge about current prices or news.
- Every claim about a number must cite the exact dotted path where it appears, in `citations`.
- If a section is `unavailable`, `partial` or `error`, say so and lower your confidence. Never treat a missing value as zero.
- `p_up_7d` is your honest probability that the USD price is higher seven days after `as_of`.
- This is research for a practice trade, not financial advice, and no trade is executed.
- Be compact: thesis under 150 words, at most 8 citations, each `note` one short clause.
"""

ROLE_SYSTEM: dict[str, str] = {
    "macro": "[role:macro] You are the Macro agent. You read market regime, Fear & Greed, breadth, dominance, "
    "the seven-day sentiment shift, and `compare` (this token against BTC/ETH peers on momentum, activity and volatility), "
    "and judge whether broad conditions and relative strength favour or oppose a position in this token." + COMMON_RULES,
    "technician": "[role:technician] You are the Technician. You read price, multi-window performance, RSI(14), ATR(14), "
    "confluence, derivatives and the tool's own verdict, plus `compare` (peers) and `price_check` (independent exchange prices "
    "and how far RYO's price deviates from them; a large deviation is a data-quality risk, not a trade signal) and `technicals_check` (RSI/ATR recomputed independently from public OHLC; a large gap means the indicator inputs disagree), and judge trend, "
    "momentum and volatility for this token." + COMMON_RULES,
    "narrative": "[role:narrative] You are the Narrative agent. You read catalysts, risks, the token profile and intelligence "
    "narrative, plus, when present, `narrative_signal` (what selected voices say, lexicon-scored) and `news_check` "
    "(how many independent sources corroborate a story). Judge whether the story supports or undermines the price. "
    "Be explicit when profile data is unavailable and treat unavailable voices as silence, not agreement." + COMMON_RULES,
    "judge": "[role:judge] You are the Judge. You receive the council's opinions with their calibration weights and the evidence "
    "availability. Weigh them, resolve disagreement explicitly, and decide long, short or no_trade. Prefer no_trade when primary "
    "evidence is unavailable or the council is split with low confidence." + COMMON_RULES,
}

# Which sections each role sees. ponytail: everyone gets availability/warnings; slices keep prompts small.
ROLE_SECTIONS: dict[str, tuple[str, ...]] = {
    "macro": ("market_overview", "sentiment_shift", "compare"),
    "technician": ("deep_analysis", "analyze_token", "compare", "price_check", "technicals_check"),
    "narrative": ("deep_analysis", "analyze_token", "narrative_signal", "news_check"),
}


def cache_key(pack_hash: str, role: str, model: str, prompt_version: str = PROMPT_VERSION) -> str:
    return hashlib.sha256(f"{pack_hash}|{role}|{prompt_version}|{model}".encode()).hexdigest()


def _section_view(pack: EvidencePack, keys: tuple[str, ...]) -> dict[str, Any]:
    view: dict[str, Any] = {}
    for k in keys:
        sec = pack.sections.get(k)
        if sec is None:
            continue
        if sec.envelope is None:
            view[k] = {"status": sec.status, "error": sec.error}
        else:
            e = sec.envelope
            view[k] = {"status": e.status, "data_mode": e.data_mode, "as_of": e.as_of, "availability": e.availability,
                       "warnings": e.warnings, "summary": e.summary.model_dump(), "data": e.data}
    return view


def _role_prompt(pack: EvidencePack, role: str) -> str:
    body = {
        "symbol": pack.symbol,
        "availability": pack.availability(),
        "warnings": pack.warnings(),
        "evidence": _section_view(pack, ROLE_SECTIONS[role]),
    }
    return (f"Paths are written `<section>.data.<field>`; list items as `.0.`, e.g. `market_overview.data.top_movers.gainers.0.symbol`. "
            f"Cite only paths that exist below with a non-null value. Evidence pack:\n{json.dumps(body, indent=1, default=str)}")


def _judge_prompt(pack: EvidencePack, opinions: list[Opinion], weights: dict[str, float]) -> str:
    body = {
        "symbol": pack.symbol,
        "availability": pack.availability(),
        "primary_evidence_ok": pack.primary_ok,
        "warnings": pack.warnings(),
        "calibration_weights": weights,
        "opinions": [o.model_dump() for o in opinions],
        "headlines": {k: s.envelope.summary.headline for k, s in pack.sections.items() if s.envelope},
    }
    return f"Council output:\n{json.dumps(body, indent=1, default=str)}"


_INDEX = re.compile(r"\[(\d+)\]")


def _as_text(value: Any) -> str:
    return f"{value:g}" if isinstance(value, float) else str(value)


def normalize_path(path: str) -> str:
    """`market_overview.data.gainers[0].pct` -> `market_overview.data.gainers.0.pct`; strips backticks/quotes/space."""
    return _INDEX.sub(r".\1", path.strip().strip("`'\" ")).replace("..", ".")


def validate_citations(opinion: Opinion, allowed: set[str] | Mapping[str, Any]) -> Opinion:
    """Drop citations that point at nothing. When `allowed` maps paths to values (the normal case,
    `EvidencePack.available_paths()`), the citation's `value` is taken from the evidence rather than
    from whatever the model retyped, so a receipt can never show a number the evidence does not hold."""
    normalized = [c.model_copy(update={"path": normalize_path(c.path)}) for c in opinion.citations]
    kept = [c for c in normalized if c.path in allowed]
    if isinstance(allowed, Mapping):
        kept = [c.model_copy(update={"value": _as_text(allowed[c.path])}) for c in kept]
    dropped = len(opinion.citations) - len(kept)
    confidence = opinion.confidence
    if not kept and opinion.citations:
        confidence = "low"
    if not opinion.citations:
        confidence = "low"
    return opinion.model_copy(update={"citations": kept, "dropped_citations": dropped, "confidence": confidence})


def _cached_call(ledger: Ledger, llm: LLM, pack_hash: str, role: str, system: str, user: str, schema, use_cache: bool,
                 prompt_version: str = PROMPT_VERSION):
    key = cache_key(pack_hash, role, llm.model, prompt_version)
    if use_cache:
        hit = ledger.get_cached(key)
        if hit is not None:
            return schema.model_validate_json(hit), True
    out = llm.complete_json(system=system, user=user, schema=schema)
    if use_cache:  # a fresh (cache-bypassing) run must not overwrite the original decision's outputs
        ledger.put_cached(key, pack_hash, role, prompt_version, llm.model, out.model_dump_json())
    return out, False


def run_council(
    pack: EvidencePack, llm: LLM, ledger: Ledger, weights: dict[str, float] | None = None, use_cache: bool = True,
    prompt_version: str = PROMPT_VERSION,
) -> CouncilResult:
    """`prompt_version` is part of the cache key. Replay passes the version stored in the receipt, so a
    receipt made under an older prompt still replays from its own cached outputs after a prompt bump."""
    weights = weights or {r: 1.0 for r in ROLES}
    pack_hash = pack.pack_hash()
    allowed = pack.available_paths()
    hits = 0
    opinions: list[Opinion] = []
    for role in ROLES:
        raw, hit = _cached_call(ledger, llm, pack_hash, role, ROLE_SYSTEM[role], _role_prompt(pack, role), Opinion, use_cache, prompt_version)
        hits += hit
        opinions.append(validate_citations(raw.model_copy(update={"role": role}), allowed))
    verdict, hit = _cached_call(
        ledger, llm, pack_hash, "judge", ROLE_SYSTEM["judge"], _judge_prompt(pack, opinions, weights), Verdict, use_cache, prompt_version
    )
    hits += hit
    return CouncilResult(opinions=opinions, verdict=verdict, cache_hits=hits, model=llm.model, prompt_version=prompt_version)
