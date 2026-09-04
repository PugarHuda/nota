"""LLM abstraction: one method, structured output.

`AnthropicLLM` uses `client.messages.parse` so the reply is validated against the pydantic
schema by the SDK. `FakeLLM` is for tests and dry runs: it dispatches on the `[role:<name>]`
tag every council prompt starts with.
"""

from __future__ import annotations

import os
import re
from typing import Callable, Protocol, TypeVar

from pydantic import BaseModel

T = TypeVar("T", bound=BaseModel)
DEFAULT_MODEL = "claude-opus-5"
ROLE_TAG = re.compile(r"\[role:([a-z_]+)\]")


class LLM(Protocol):
    model: str

    def complete_json(self, system: str, user: str, schema: type[T]) -> T: ...


class AnthropicLLM:
    def __init__(self, model: str | None = None, max_tokens: int = 16000):
        import anthropic  # local import keeps tests free of the SDK

        self.client = anthropic.Anthropic()
        self.model = model or os.environ.get("ARENA_MODEL", DEFAULT_MODEL)
        self.max_tokens = max_tokens

    def complete_json(self, system: str, user: str, schema: type[T]) -> T:
        response = self.client.messages.parse(
            model=self.model,
            max_tokens=self.max_tokens,
            system=system,
            messages=[{"role": "user", "content": user}],
            output_format=schema,
        )
        if response.stop_reason == "refusal":
            raise RuntimeError(f"model refused: {getattr(response.stop_details, 'explanation', '')}")
        parsed = response.parsed_output
        if parsed is None:
            raise RuntimeError("model returned no parsed output")
        return parsed


class FakeLLM:
    """handlers: role -> fn(user_prompt) -> BaseModel. Counts calls so tests can assert caching."""

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
