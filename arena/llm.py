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
    def __init__(self, model: str | None = None, max_tokens: int | None = None):
        import anthropic  # local import keeps tests free of the SDK

        self.client = anthropic.Anthropic()
        self.model = model or os.environ.get("ARENA_MODEL", DEFAULT_MODEL)
        # ARENA_MAX_TOKENS lets a small prepaid balance (e.g. OpenRouter) fit; outputs are short JSON
        self.max_tokens = max_tokens or int(os.environ.get("ARENA_MAX_TOKENS", "16000"))

    def complete_json(self, system: str, user: str, schema: type[T]) -> T:
        from pydantic import ValidationError

        try:
            response = self.client.messages.parse(
                model=self.model,
                max_tokens=self.max_tokens,
                system=system,
                messages=[{"role": "user", "content": user}],
                output_format=schema,
            )
        except ValidationError as exc:  # the SDK parses the text; a cut-off JSON lands here
            raise RuntimeError(
                f"model output was not valid {schema.__name__} JSON, most likely truncated at max_tokens={self.max_tokens}; "
                "raise ARENA_MAX_TOKENS (needs ~2500 per council call)"
            ) from exc
        if response.stop_reason == "max_tokens":
            raise RuntimeError(f"model hit max_tokens={self.max_tokens}; raise ARENA_MAX_TOKENS")
        if response.stop_reason == "refusal":
            raise RuntimeError(f"model refused: {getattr(response.stop_details, 'explanation', '')}")
        parsed = response.parsed_output
        if parsed is None:
            raise RuntimeError("model returned no parsed output")
        return parsed


class OpenAICompatLLM:
    """Any OpenAI-compatible `/chat/completions` (Venice, OpenRouter, ...) with `response_format` json_schema.

    Env: OPENAI_BASE_URL, OPENAI_API_KEY, ARENA_MODEL, ARENA_MAX_TOKENS. ponytail: plain httpx, no SDK.
    """

    def __init__(self, model: str | None = None, base_url: str | None = None, api_key: str | None = None, max_tokens: int | None = None):
        import httpx

        self.base_url = (base_url or os.environ.get("OPENAI_BASE_URL", "https://api.venice.ai/api/v1")).rstrip("/")
        self.api_key = api_key or os.environ.get("OPENAI_API_KEY") or os.environ.get("VENICE_API_KEY", "")
        self.model = model or os.environ.get("ARENA_MODEL", "qwen3-235b-a22b-instruct-2507")
        self.max_tokens = max_tokens or int(os.environ.get("ARENA_MAX_TOKENS", "4000"))
        self.http = httpx.Client(timeout=180.0, headers={"Authorization": f"Bearer {self.api_key}"})
        self.last_cost_usd: float | None = None

    def complete_json(self, system: str, user: str, schema: type[T]) -> T:
        body = {
            "model": self.model,
            "max_completion_tokens": self.max_tokens,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "response_format": {"type": "json_schema", "json_schema": {"name": schema.__name__, "schema": schema.model_json_schema()}},
            # Venice-only knobs; other providers ignore unknown fields. Both save tokens.
            "venice_parameters": {"include_venice_system_prompt": False, "disable_thinking": True},
        }
        r = self.http.post(f"{self.base_url}/chat/completions", json=body)
        if r.status_code >= 400:
            raise RuntimeError(f"LLM HTTP {r.status_code}: {r.text[:300]}")
        data = r.json()
        self.last_cost_usd = (data.get("cost") or {}).get("usd")
        choice = data["choices"][0]
        content = choice["message"]["content"] or ""
        if choice.get("finish_reason") == "length":
            raise RuntimeError(f"model hit max_tokens={self.max_tokens}; raise ARENA_MAX_TOKENS. Output started: {content[:300]!r}")
        return schema.model_validate_json(content)


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
