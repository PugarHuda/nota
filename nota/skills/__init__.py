"""Skill registry: name -> (definition, callable). Both return the RYO envelope."""

from __future__ import annotations

from typing import Any, Callable

from nota.envelope import Envelope
from nota.skills import base_rate, narrative, news, positioning, price_check, technicals, track_record
from nota.skills.contract import SkillDefinition

SKILLS: dict[str, tuple[SkillDefinition, Callable[..., Envelope]]] = {
    narrative.DEFINITION.name: (narrative.DEFINITION, narrative.narrative_convergence),
    news.DEFINITION.name: (news.DEFINITION, news.news_verify),
    price_check.DEFINITION.name: (price_check.DEFINITION, price_check.price_crosscheck),
    technicals.DEFINITION.name: (technicals.DEFINITION, technicals.technicals_crosscheck),
    positioning.DEFINITION.name: (positioning.DEFINITION, positioning.positioning_check),
    base_rate.DEFINITION.name: (base_rate.DEFINITION, base_rate.move_base_rate),
    track_record.DEFINITION.name: (track_record.DEFINITION, track_record.verdict_track_record),
}


def definitions() -> list[SkillDefinition]:
    return [d for d, _ in SKILLS.values()]


def invoke(name: str, args: dict[str, Any], **deps: Any) -> Envelope:
    if name not in SKILLS:
        raise KeyError(f"unknown skill {name!r}; known: {sorted(SKILLS)}")
    definition, fn = SKILLS[name]
    missing = [a.name for a in definition.args if a.required and a.name not in args]
    if missing:
        raise ValueError(f"{name}: missing required args {missing}")
    unknown = set(args) - {a.name for a in definition.args}
    if unknown:
        raise ValueError(f"{name}: unknown args {sorted(unknown)}")
    return fn(**args, **deps)
