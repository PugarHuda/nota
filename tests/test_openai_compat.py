import json

import pytest
import respx
from httpx import Response
from pydantic import BaseModel

from arena.llm import OpenAICompatLLM


class Out(BaseModel):
    city: str
    n: int


def _reply(content: str, finish: str = "stop"):
    return Response(200, json={"choices": [{"message": {"content": content}, "finish_reason": finish}], "cost": {"usd": 0.0003}})


@respx.mock
def test_sends_schema_and_parses():
    route = respx.post("https://api.venice.ai/api/v1/chat/completions").mock(return_value=_reply('{"city":"Paris","n":1}'))
    llm = OpenAICompatLLM(model="m", base_url="https://api.venice.ai/api/v1", api_key="k")
    out = llm.complete_json("[role:macro] sys", "user", Out)
    assert out == Out(city="Paris", n=1) and llm.last_cost_usd == 0.0003
    body = json.loads(route.calls[0].request.content)
    assert body["response_format"]["json_schema"]["name"] == "Out"
    assert body["venice_parameters"]["include_venice_system_prompt"] is False
    assert route.calls[0].request.headers["authorization"] == "Bearer k"


@respx.mock
def test_truncation_and_http_errors_are_explicit():
    respx.post("https://x.test/v1/chat/completions").mock(side_effect=[_reply('{"city":"P', "length"), Response(402, text="no credits")])
    llm = OpenAICompatLLM(model="m", base_url="https://x.test/v1", api_key="k", max_tokens=5)
    with pytest.raises(RuntimeError, match="max_tokens=5"):
        llm.complete_json("s", "u", Out)
    with pytest.raises(RuntimeError, match="HTTP 402"):
        llm.complete_json("s", "u", Out)
