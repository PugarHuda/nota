import pytest
from pydantic import BaseModel

from nota.llm import FakeLLM


class Out(BaseModel):
    x: int


def test_fake_llm_dispatches_on_role_tag_and_counts_calls():
    llm = FakeLLM({"macro": lambda user: Out(x=len(user))})
    assert llm.complete_json("[role:macro] sys", "abcd", Out).x == 4
    assert llm.calls == ["macro"]


def test_fake_llm_unknown_role_raises():
    with pytest.raises(KeyError):
        FakeLLM({}).complete_json("[role:judge]", "u", Out)
