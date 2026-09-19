"""Skill contract: RYO's tool definition shape plus RYO's public response envelope.

A skill is a plain function that returns an `Envelope` (nota.envelope), exactly the shape
RYO's six builder tools return, so a consumer that already reads RYO output can read ours
without new code. The definition mirrors RYO's internal `SkillDefinition`
(`docs/ryo-openapi-subset.json`): name, description, args[], requires_guard, xp.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, Field

from nota.envelope import DataMode, Envelope, Status, Summary

SCHEMA_VERSION = "nota-skill-1"
ArgType = Literal["string", "integer", "number", "boolean", "array", "object"]
# A symbol ends up in exchange URL paths, LLM prompts and the ledger, so it is checked once, here.
SYMBOL_RE = re.compile(r"[A-Z0-9]{1,15}")


def clean_symbol(raw: Any) -> str:
    """`" sol "` -> `"SOL"`; anything that is not 1-15 letters/digits is refused, never passed on."""
    s = raw.strip().upper() if isinstance(raw, str) else None
    if s is None or not SYMBOL_RE.fullmatch(s):
        raise ValueError(f"symbol must be 1-15 letters/digits, got {raw!r}")
    return s


class SkillArg(BaseModel):
    name: str
    type: ArgType
    required: bool = True
    description: str
    enum: list[str] | None = None
    items: dict[str, Any] | None = None


class SkillDefinition(BaseModel):
    name: str
    description: str
    args: list[SkillArg]
    requires_guard: bool = False  # research only: never routed through a signer
    xp: int = 0
    read_only: bool = True


class SourceUnavailable(Exception):
    """A dependency (network source) failed. The skill reports it; it never fabricates."""


def status_from(availability: dict[str, str], primary: list[str] | None = None) -> Status:
    """ok when every section is ok; unavailable when every primary section failed; else partial."""
    keys = primary or list(availability)
    if not keys:
        return "unavailable"
    vals = [availability.get(k, "unavailable") for k in keys]
    if all(v == "ok" for v in availability.values()):
        return "ok"
    if all(v in ("unavailable", "error") for v in vals):
        return "unavailable"
    return "partial"


def make_envelope(
    tool: str,
    request: dict[str, Any],
    data: dict[str, Any],
    availability: dict[str, str],
    warnings: list[str],
    headline: str,
    key_points: list[str] | None = None,
    data_mode: DataMode = "live",
    primary: list[str] | None = None,
) -> Envelope:
    status = status_from(availability, primary)
    return Envelope(
        schema_version=SCHEMA_VERSION,
        tool=tool,
        status=status,
        # Nothing was observed, so claiming the data is "live" would describe data that does not
        # exist. RYO's own contract has `unknown` for exactly this.
        data_mode="unknown" if status == "unavailable" else data_mode,
        as_of=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        request=request,
        data=data,
        summary=Summary(headline=headline, key_points=key_points or []),
        availability=availability,
        warnings=warnings,
    )
