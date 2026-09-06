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
def test_retries_transient_errors_then_succeeds():
    import httpx

    route = respx.post("https://x.test/v1/chat/completions").mock(side_effect=[httpx.ConnectTimeout("slow"), Response(503), _reply('{"city":"P","n":2}')])
    llm = OpenAICompatLLM(model="m", base_url="https://x.test/v1", api_key="k")
    waits = []
    llm.sleep = waits.append
    assert llm.complete_json("s", "u", Out) == Out(city="P", n=2) and route.call_count == 3 and waits == [2.0, 4.0]
    route.mock(side_effect=[httpx.ConnectTimeout("slow")] * 4)
    with pytest.raises(RuntimeError, match="unreachable after 4 attempts"):
        llm.complete_json("s", "u", Out)


@respx.mock
def test_truncation_and_http_errors_are_explicit():
    respx.post("https://x.test/v1/chat/completions").mock(side_effect=[_reply('{"city":"P', "length"), Response(402, text="no credits")])
    llm = OpenAICompatLLM(model="m", base_url="https://x.test/v1", api_key="k", max_tokens=5)
    with pytest.raises(RuntimeError, match="max_tokens=5"):
        llm.complete_json("s", "u", Out)
    with pytest.raises(RuntimeError, match="HTTP 402"):
        llm.complete_json("s", "u", Out)
