"""Test doubles. Kept out of the `nota` package so no production path can reach them."""

from __future__ import annotations

import re
from typing import Callable, TypeVar

from pydantic import BaseModel

T = TypeVar("T", bound=BaseModel)
ROLE_TAG = re.compile(r"\[role:([a-z_]+)\]")


class FakeLLM:
    """handlers: role -> fn(user_prompt) -> BaseModel. Dispatches on the `[role:<name>]` tag every
    council system prompt starts with, and counts calls so tests can assert caching."""

    model = "fake"

    def __init__(self, handlers: dict[str, Callable[[str], BaseModel]]):
        self.handlers = handlers
        self.calls: list[str] = []

    def complete_json(self, system: str, user: str, schema: type[T]) -> T:
        m = ROLE_TAG.search(system)
        role = m.group(1) if m else "?"
        if role not in self.handlers:
            raise KeyError(f"FakeLLM has no handler for role {role!r}")
        self.calls.append(role)
        out = self.handlers[role](user)
        return schema.model_validate(out.model_dump())
