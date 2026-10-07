"""Model-call resilience: empty replies, 429 failover, and malformed tool calls."""

from types import SimpleNamespace as NS

import pytest

from app_clicker.actions import ActionExecutor, ArgumentError
from app_clicker.agent import TesterAgent
from app_clicker.llm import (AssistantTurn, BackendChain, EmptyResponseError,
                             OpenAICompatBackend, ToolCall, is_transient)

TOOLS = [{"name": "click", "input_schema": {"type": "object"}}]


class RateLimited(Exception):
    status_code = 429


def _resp(content="hi", tool_calls=None, finish="stop", choices="ok"):
    msg = NS(content=content, tool_calls=tool_calls)
    return NS(choices=[NS(message=msg, finish_reason=finish)] if choices == "ok" else choices,
              usage=None, error=None)


class FakeClient:
    """chat.completions.create replaying a script of responses/exceptions per model."""

    def __init__(self, script):
        self.script = script  # {model: [item, ...]}
        self.calls = []
        self.chat = NS(completions=NS(create=self._create))

    def _create(self, **kw):
        self.calls.append(kw["model"])
        item = self.script[kw["model"]].pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def _chain(client, model, fallbacks):
    backend = OpenAICompatBackend(client, model=model, fallback_models=fallbacks)
    return BackendChain([(backend, model, "openrouter")])


def test_none_choices_is_retryable_not_typeerror():
    client = FakeClient({"m": [_resp(choices=None), _resp(content="ok")]})
    be = OpenAICompatBackend(client, model="m")
    with pytest.raises(EmptyResponseError):
        be.complete("sys", [], TOOLS)
    assert is_transient(EmptyResponseError("x"))
    assert be.complete("sys", [], TOOLS).text == "ok"


def test_reasoning_model_with_no_output_raises_with_length_hint():
    be = OpenAICompatBackend(FakeClient({"m": [_resp(content=None, finish="length")]}), model="m")
    with pytest.raises(EmptyResponseError, match="max_tokens"):
        be.complete("sys", [], TOOLS)


def test_429_fails_over_to_next_model_without_backoff(monkeypatch):
    monkeypatch.setattr("time.sleep", lambda s: pytest.fail("should not back off"))
    client = FakeClient({"a": [RateLimited("slow down")], "b": [_resp(content="from b")]})
    chain = _chain(client, "a", ["b"])
    assert chain.complete("sys", [], TOOLS).text == "from b"
    assert client.calls == ["a", "b"] and chain.model == "b"


def test_empty_reply_rotates_model_after_retries(monkeypatch):
    monkeypatch.setattr("time.sleep", lambda s: None)
    client = FakeClient({"a": [_resp(choices=None)] * 4, "b": [_resp(content="from b")]})
    chain = _chain(client, "a", ["b"])
    assert chain.complete("sys", [], TOOLS).text == "from b"
    assert client.calls == ["a"] * 4 + ["b"]


def test_all_models_429_eventually_raises(monkeypatch):
    monkeypatch.setattr("time.sleep", lambda s: None)
    client = FakeClient({m: [RateLimited("x")] * 4 for m in "ab"})
    with pytest.raises(RateLimited):
        _chain(client, "a", ["b"]).complete("sys", [], TOOLS)


def test_missing_element_id_error_is_actionable():
    ex = ActionExecutor.__new__(ActionExecutor)  # dispatch needs no window for this path
    with pytest.raises(ArgumentError) as ei:
        ex.dispatch("click", {"x": 0.47, "y": 0.28}, {"e1": object(), "e2": object()})
    msg = str(ei.value)
    assert "element_id" in msg and "x/y" in msg and "e1, e2" in msg


class ScriptedChain:
    def __init__(self, turns):
        self.turns, self.rotations = list(turns), 0

    def complete(self, system, messages, tools, log=None):
        return self.turns.pop(0)

    def rotate_model(self, log=None, reason=""):
        self.rotations += 1
        return True


class BadClickExecutor:
    def dispatch(self, name, inp, elements):
        raise ArgumentError("Missing required argument 'element_id'")


class Perceiver:
    def observe(self):
        return NS(window_title="w", tree_text="t", screenshot=None, elements={})


def _turn(name, **inp):
    return AssistantTurn("", ToolCall("1", name, inp), "tool_use",
                         {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0})


def _agent(chain, max_steps):
    return TesterAgent(chain, max_steps=max_steps, perceiver=Perceiver(),
                       executor=BadClickExecutor(), verbose=False)


def test_argument_errors_are_refunded_and_rotate_model():
    chain = ScriptedChain([_turn("click", x=1, y=2)] * 3 + [_turn("finish", status="passed", summary="ok")])
    result = _agent(chain, max_steps=2).run("task")  # 3 bad calls would exceed 2 steps if charged
    assert result.status == "passed" and chain.rotations == 1


def test_argument_error_refunds_are_capped():
    chain = ScriptedChain([_turn("click", x=1, y=2)] * 20)
    result = _agent(chain, max_steps=2).run("task")
    assert result.status == "incomplete"  # loop cannot spin forever
