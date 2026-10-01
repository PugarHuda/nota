"""Multi-KOL narrative agent (Track 1 spotlight): voices -> narrative_convergence -> rules -> practice trade.

The rules are code, not a model. Each is a row {rule, value, threshold, passed} whose value is read
straight from the narrative envelope, and a token signals only when every row passes. A fired signal
is sized by nota.risk on RYO's own deep_analysis (ATR stop and target, the user's account and risk %);
with no RYO evidence there is no trade, and the reason says so, because a stop is never invented.
A run is stored with its envelope and its evidence packs, so `replay` re-derives every decision
from the ledger alone: no network, no key.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from typing import Annotated, Any, Callable, Literal

from pydantic import BaseModel, Field, field_validator

from nota.envelope import Envelope
from nota.evidence import PRIMARY, SECTIONS, EvidencePack, Section, _fetch, ryo_args
from nota.ledger import Ledger, now_iso
from nota.receipt import Receipt
from nota.risk import Blocked, PracticeTrade, RiskLimits, size_side
from nota.ryo_client import RyoSource
from nota.skills.contract import clean_symbol
from nota.skills.narrative import narrative_convergence

HELD_DAYS = 7  # a KOL practice trade counts as open for the council's own seven-day horizon


class KolRules(BaseModel):
    min_voices: int = Field(2, ge=2, le=20, description="Voices with a stance that must agree; convergence needs two by definition")
    min_sentiment: float = Field(0.3, ge=0, le=1, description="Minimum |sentiment_mean| on the token")
    min_conviction: float = Field(0.0, ge=0, le=1, description="Minimum conviction_mean")
    min_urgency: float | None = Field(None, ge=0, le=1, description="Minimum urgency_max; null = not a rule")
    tokens: list[str] | None = Field(None, max_length=20, description="Allowed tokens; null = any token the voices name")
    hours: int = Field(24, ge=1, le=336, description="Look-back window")

    @field_validator("tokens")
    @classmethod
    def _symbols(cls, v: list[str] | None) -> list[str] | None:
        return sorted({clean_symbol(t) for t in v}) if v else None


class KolLimits(BaseModel):
    account_usd: float = Field(10_000.0, gt=0, le=1e9)
    risk_per_trade_pct: float = Field(1.0, gt=0, le=10)
    max_open_positions: int = Field(3, ge=0, le=10, description="Council and KOL practice positions together")


class Rule(BaseModel):
    rule: str
    value: Any
    threshold: Any
    passed: bool


class TokenDecision(BaseModel):
    token: str
    signal: Literal["long", "short", "none"]
    rules: list[Rule]
    reason: str
    trade: Annotated[PracticeTrade | Blocked, Field(discriminator="kind")] | None = None


class KolRun(BaseModel):
    id: str
    created_at: str
    voices: list[str]
    rules: KolRules
    limits: KolLimits
    held: list[str]                 # symbols already in a practice position when the run started
    envelope: Envelope              # narrative_convergence's answer, as it came
    pack_hashes: dict[str, str]     # token -> RYO evidence pack it was sized on (ledger `evidence` table)
    decisions: list[TokenDecision]
    headline: str


def _num(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _at_least(v: Any, t: float) -> bool:
    return _num(v) and v >= t   # null fails the rule; it never reads as 0


def rule_trail(row: dict[str, Any], rules: KolRules) -> list[Rule]:
    sm = row.get("sentiment_mean")
    out = [Rule(rule="voices converge (two or more with a stance, all the same sign)", value=row.get("converging"),
                threshold=True, passed=row.get("converging") is True),
           Rule(rule="voices with a stance >= min_voices", value=row.get("signed_voices"), threshold=rules.min_voices,
                passed=_at_least(row.get("signed_voices"), rules.min_voices)),
           Rule(rule="|sentiment_mean| >= min_sentiment", value=sm, threshold=rules.min_sentiment,
                passed=_num(sm) and abs(sm) >= rules.min_sentiment),
           Rule(rule="conviction_mean >= min_conviction", value=row.get("conviction_mean"), threshold=rules.min_conviction,
                passed=_at_least(row.get("conviction_mean"), rules.min_conviction))]
    if rules.min_urgency is not None:
        out.append(Rule(rule="urgency_max >= min_urgency", value=row.get("urgency_max"), threshold=rules.min_urgency,
                        passed=_at_least(row.get("urgency_max"), rules.min_urgency)))
    if rules.tokens is not None:
        out.insert(0, Rule(rule="token is allowed", value=row["symbol"], threshold=rules.tokens, passed=row["symbol"] in rules.tokens))
    return out


def evaluate(env: Envelope, rules: KolRules, limits: KolLimits, packs: dict[str, EvidencePack], held: list[str]) -> list[TokenDecision]:
    """Pure: the same envelope, rules, limits, packs and holdings always give the same decisions. Tokens
    are taken in the envelope's own order (converging first), which is also the order slots are filled."""
    risk = RiskLimits(account_usd=limits.account_usd, risk_per_trade_pct=limits.risk_per_trade_pct)
    out: list[TokenDecision] = []
    opened = 0
    for row in env.data.get("tokens") or []:
        sym, trail = row["symbol"], rule_trail(row, rules)
        side = {"bullish": "long", "bearish": "short"}.get(row.get("direction"))
        failed = [r.rule for r in trail if not r.passed]
        if failed or side is None:
            out.append(TokenDecision(token=sym, signal="none", rules=trail,
                                     reason="failed: " + "; ".join(failed) if failed else f"no direction ({row.get('direction')})"))
            continue
        open_now = len(held) + opened
        if sym in held:
            trade = Blocked(reason=f"already holding a practice position in {sym}")
        elif open_now >= limits.max_open_positions:
            trade = Blocked(reason=f"risk limit reached: {open_now} open practice position(s), max {limits.max_open_positions}")
        elif sym not in packs:
            trade = Blocked(reason=f"no RYO evidence for {sym} in this run; without RYO's ATR there is no stop, so no trade")
        else:
            trade = size_side(side, packs[sym], risk)
        opened += isinstance(trade, PracticeTrade)
        reason = (f"every rule passed; {side} {trade.size_usd:.2f} USD at {trade.entry_price:g}, stop {trade.stop_price:g}, "
                  f"target {trade.target_price:g} (ATR from {trade.source_paths['atr']})" if isinstance(trade, PracticeTrade)
                  else f"every rule passed; no trade: {trade.reason}")
        out.append(TokenDecision(token=sym, signal=side, rules=trail, reason=reason, trade=trade))
    return out


def headline(env: Envelope, decisions: list[TokenDecision]) -> str:
    trades = [d for d in decisions if isinstance(d.trade, PracticeTrade)]
    fired = [d for d in decisions if d.signal != "none"]
    if trades:
        return "; ".join(f"{d.token} {d.signal.upper()} practice trade" for d in trades)
    if fired:
        return f"{len(fired)} signal(s), no trade: " + "; ".join(f"{d.token}: {d.trade.reason}" for d in fired if d.trade)
    return f"no signal: {env.summary.headline}"


def run_id(env: Envelope, rules: KolRules, limits: KolLimits, held: list[str], pack_hashes: dict[str, str]) -> str:
    canon = {"envelope": env.model_dump(mode="json"), "rules": rules.model_dump(mode="json"),
             "limits": limits.model_dump(mode="json"), "held": held, "packs": pack_hashes}
    return hashlib.sha256(json.dumps(canon, sort_keys=True).encode()).hexdigest()[:12]


def held_symbols(ledger: Ledger) -> list[str]:
    """Symbols in a practice position now: council trades without an outcome, and KOL trades younger
    than HELD_DAYS. ponytail: KOL trades are not settled yet, so they age out instead of closing."""
    held: set[str] = set()
    for d in ledger.list_decisions(limit=200):
        raw = ledger.get_decision(d["id"])
        if raw and not ledger.get_outcome(d["id"]) and isinstance(Receipt.model_validate_json(raw).trade, PracticeTrade):
            held.add(d["symbol"])
    since = (datetime.now(timezone.utc) - timedelta(days=HELD_DAYS)).isoformat(timespec="seconds")
    for _, _, raw in ledger.list_kol(limit=200, since=since):
        held |= {d["token"] for d in json.loads(raw)["decisions"] if (d.get("trade") or {}).get("kind") == "trade"}
    return sorted(held)


def gather_primary(source: RyoSource, symbol: str) -> EvidencePack:
    """Only deep_analysis: it carries the price, the ATR and RYO's own plan and veto, which is all sizing reads."""
    pack = EvidencePack(symbol=symbol, created_at=now_iso(), source=source.name)
    try:
        pack.sections[PRIMARY] = _fetch(source, SECTIONS[PRIMARY], ryo_args(symbol)[PRIMARY])
    except Exception as exc:  # a network failure is a missing section, never a crashed run
        pack.sections[PRIMARY] = Section(tool=SECTIONS[PRIMARY], status="error", error=f"{type(exc).__name__}: {exc}")
    return pack


def run(voices: list[str], rules: KolRules, limits: KolLimits, source: RyoSource | None, held: list[str],
        narrative: Callable[..., Envelope] | None = None) -> tuple[KolRun, dict[str, EvidencePack]]:
    env = (narrative or narrative_convergence)(voices, tokens=rules.tokens, hours=rules.hours)
    first = evaluate(env, rules, limits, {}, held)
    # RYO is asked only about tokens that could take a free slot: deep_analysis is slow and rate limited.
    # ponytail: a slot whose token RYO then blocks is not handed to the next signal; it waits for the next run
    slots = max(0, limits.max_open_positions - len(held))
    want = [d.token for d in first if d.signal != "none" and d.token not in held][:slots]
    packs = {sym: gather_primary(source, sym) for sym in want} if source is not None else {}
    decisions = evaluate(env, rules, limits, packs, held)
    hashes = {s: p.pack_hash() for s, p in packs.items()}
    return KolRun(id=run_id(env, rules, limits, held, hashes), created_at=now_iso(), voices=voices, rules=rules,
                  limits=limits, held=held, envelope=env, pack_hashes=hashes, decisions=decisions,
                  headline=headline(env, decisions)), packs


def save(ledger: Ledger, kol_run: KolRun, packs: dict[str, EvidencePack]) -> None:
    for p in packs.values():
        ledger.save_pack(p.pack_hash(), p.symbol, p.source, p.model_dump_json())
    ledger.save_kol(kol_run.id, kol_run.model_dump_json())


def replay(id: str, ledger: Ledger) -> dict[str, Any]:
    """Re-derive every decision of a stored run from its stored envelope and packs. Reads the ledger only."""
    raw = ledger.get_kol(id)
    if raw is None:
        raise KeyError(f"no KOL run {id}")
    stored = KolRun.model_validate_json(raw)
    packs = {}
    for sym, h in stored.pack_hashes.items():
        pj = ledger.get_pack(h)
        if pj is None:
            raise KeyError(f"evidence {h} for {sym} missing from ledger")
        packs[sym] = EvidencePack.model_validate_json(pj)
    again = evaluate(stored.envelope, stored.rules, stored.limits, packs, stored.held)
    a = [d.model_dump(mode="json") for d in stored.decisions]
    b = [d.model_dump(mode="json") for d in again]
    diff = [f"{x['token']}: {x['signal']} -> {y['signal']} ({y['reason']})" for x, y in zip(a, b) if x != y]
    if len(a) != len(b):
        diff.append(f"token count {len(a)} -> {len(b)}")
    return {"id": id, "identical": not diff, "diff": diff, "decisions": b}
