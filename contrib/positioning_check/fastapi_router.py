"""`app.include_router(router)` adds RYO's two skill paths for positioning_check."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Body, HTTPException
from fastapi.concurrency import run_in_threadpool

from .positioning_check import NAME, SKILL_DEFINITION, invoke

router = APIRouter(prefix=f"/api/skills/{NAME}", tags=["skills"])


@router.get("")
def skill_def() -> dict[str, Any]:
    return SKILL_DEFINITION


@router.post("/invoke")
async def invoke_skill(body: dict[str, Any] = Body(...)) -> dict[str, Any]:
    """Body is RYO's SkillCallRequest {name, args, conversation_id}."""
    if body.get("name") not in (None, NAME):
        raise HTTPException(422, "body.name does not match the path")
    try:  # four blocking HTTP calls: off the event loop
        return await run_in_threadpool(invoke, body.get("args"))
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
