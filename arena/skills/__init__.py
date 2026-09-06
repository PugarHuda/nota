"""Skill registry: name -> (definition, callable). Both return the RYO envelope."""

from __future__ import annotations

from typing import Any, Callable

from arena.envelope import Envelope
from arena.skills import narrative, news
from arena.skills.contract import SkillDefinition

SKILLS: dict[str, tuple[SkillDefinition, Callable[..., Envelope]]] = {
    narrative.DEFINITION.name: (narrative.DEFINITION, narrative.narrative_convergence),
    news.DEFINITION.name: (news.DEFINITION, news.news_verify),
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
