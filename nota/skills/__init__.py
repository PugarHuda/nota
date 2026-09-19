"""Skill registry: name -> (definition, callable). Both return the RYO envelope."""

from __future__ import annotations

import math
from typing import Any, Callable

from nota.envelope import Envelope
from nota.skills import base_rate, narrative, news, positioning, price_check, technicals, track_record
from nota.skills.contract import SkillArg, SkillDefinition, clean_symbol

SKILLS: dict[str, tuple[SkillDefinition, Callable[..., Envelope]]] = {
    narrative.DEFINITION.name: (narrative.DEFINITION, narrative.narrative_convergence),
    news.DEFINITION.name: (news.DEFINITION, news.news_verify),
    price_check.DEFINITION.name: (price_check.DEFINITION, price_check.price_crosscheck),
    technicals.DEFINITION.name: (technicals.DEFINITION, technicals.technicals_crosscheck),
    positioning.DEFINITION.name: (positioning.DEFINITION, positioning.positioning_check),
    base_rate.DEFINITION.name: (base_rate.DEFINITION, base_rate.move_base_rate),
    track_record.DEFINITION.name: (track_record.DEFINITION, track_record.verdict_track_record),
}


# 20 voices / tokens; 25 derivatives blocks, so the whole scorecard universe fits as positioning peers
MAX_ARRAY = {"string": 20, "object": 25}
_CHECKS: dict[str, Callable[[Any], bool]] = {
    "string": lambda v: isinstance(v, str),
    "integer": lambda v: isinstance(v, int) and not isinstance(v, bool),
    "number": lambda v: isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v),
    "boolean": lambda v: isinstance(v, bool),
    "array": lambda v: isinstance(v, list),
    "object": lambda v: isinstance(v, dict),
}


def _check_arg(skill: str, a: SkillArg, v: Any) -> Any:
    """The declared schema, enforced. REST, MCP and the CLI all come through `invoke`, so this is the
    one place a malformed value is stopped before it reaches a URL, a prompt or the ledger."""
    bad = f"{skill}: arg {a.name} must be {a.type}"
    if not _CHECKS[a.type](v):
        raise ValueError(bad)
    if a.type == "string":
        cap = 500 if a.name == "claim" else 64
        if len(v) > cap:
            raise ValueError(f"{bad} of at most {cap} characters")
        if a.required and not v.strip():
            raise ValueError(f"{bad}, not empty")
    if a.type == "array":
        item_type = (a.items or {}).get("type", "string")
        cap = MAX_ARRAY.get(item_type, 20)
        if len(v) > cap or not all(_CHECKS[item_type](x) and (item_type != "string" or len(x) <= 100) for x in v):
            raise ValueError(f"{bad} of at most {cap} {item_type} items" + (" (100 characters each)" if item_type == "string" else ""))
    if a.enum is not None and str(v) not in a.enum:
        raise ValueError(f"{skill}: arg {a.name} must be one of {a.enum}")
    try:
        if a.name == "symbol":
            return clean_symbol(v)
        if a.name == "tokens":
            return [clean_symbol(x) for x in v]
    except ValueError as exc:
        raise ValueError(f"{skill}: {exc}") from None
    return v


def live_deps(name: str, args: dict[str, Any]) -> dict[str, Any]:
    """What a public call (REST or MCP) gets beyond its arguments: RYO for the two skills that read it
    when a RYO key is configured, and the ledger's same-day locks for positioning_check when a ledger
    file exists. A missing key or file leaves the dependency out and the skill reports that itself."""
    import os

    deps: dict[str, Any] = {}
    wants_ryo = name == "positioning_check" or (name == "news_verify" and isinstance(args, dict) and args.get("symbol"))
    if wants_ryo and os.environ.get("RYO_MCP_KEY"):
        from nota.ryo_client import RyoClient

        deps["ryo"] = RyoClient()
    db = os.environ.get("NOTA_DB", "nota.db")
    if name == "positioning_check" and os.path.exists(db):
        from nota.ledger import Ledger

        deps["ledger"] = Ledger(db)
    return deps


def definitions() -> list[SkillDefinition]:
    return [d for d, _ in SKILLS.values()]


def invoke(name: str, args: dict[str, Any], **deps: Any) -> Envelope:
    if name not in SKILLS:
        raise KeyError(f"unknown skill {name!r}; known: {sorted(SKILLS)}")
    definition, fn = SKILLS[name]
    if not isinstance(args, dict):
        raise ValueError(f"{name}: arguments must be an object")
    missing = [a.name for a in definition.args if a.required and a.name not in args]
    if missing:
        raise ValueError(f"{name}: missing required args {missing}")
    unknown = set(args) - {a.name for a in definition.args}
    if unknown:
        raise ValueError(f"{name}: unknown args {sorted(unknown)}")
    declared = {a.name: a for a in definition.args}
    # null on an optional arg means "use the default", which is what leaving it out means
    args = {k: _check_arg(name, declared[k], v) for k, v in args.items() if not (v is None and not declared[k].required)}
    return fn(**args, **deps)
