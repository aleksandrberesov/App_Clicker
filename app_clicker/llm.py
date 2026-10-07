"""LLM backends behind one small interface.

The agent speaks a neutral transcript (a list of dict messages); each backend
translates it to its provider's wire format. This lets the same agent loop run
on Anthropic *or* any OpenAI-compatible endpoint (OpenRouter, Groq, Gemini's
OpenAI endpoint, GitHub Models, a local Ollama, ...).

Neutral message shapes (dicts):
    {"role": "user",      "text": str, "image": bytes|None}
    {"role": "assistant", "text": str, "tool_call": {id,name,input}|None, "raw": Any}
    {"role": "tool",      "tool_call_id": str, "name": str, "text": str,
                          "image": bytes|None, "is_error": bool}
"""

from __future__ import annotations

import base64
import json
from dataclasses import dataclass
from typing import Any, Optional


class EmptyResponseError(Exception):
    """The provider answered without a usable completion; worth retrying."""


def _b64(png: bytes) -> str:
    return base64.standard_b64encode(png).decode("utf-8")


@dataclass
class ToolCall:
    id: str
    name: str
    input: dict


@dataclass
class AssistantTurn:
    text: str
    tool_call: Optional[ToolCall]
    stop_reason: str
    usage: dict            # {"input": int, "output": int, "cache_read": int}
    raw: Any = None        # provider-native assistant content, replayed verbatim


# ---------------------------------------------------------------------------
# Anthropic
# ---------------------------------------------------------------------------
def _anthropic_image(png: bytes) -> dict:
    return {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": _b64(png)}}


class AnthropicBackend:
    provider = "anthropic"

    def __init__(self, client, model: str = "claude-opus-5", max_tokens: int = 8000,
                 thinking: bool = True, cache: bool = True):
        self.client = client
        self.model = model
        self.max_tokens = max_tokens
        self.thinking = thinking
        self.cache = cache

    def _messages(self, messages: list[dict]) -> list[dict]:
        out = []
        for m in messages:
            role = m["role"]
            if role == "user":
                content = [{"type": "text", "text": m.get("text", "")}]
                if m.get("image"):
                    content.append(_anthropic_image(m["image"]))
                out.append({"role": "user", "content": content})
            elif role == "assistant":
                if m.get("raw") is not None:
                    out.append({"role": "assistant", "content": m["raw"]})
                    continue
                content = []
                if m.get("text"):
                    content.append({"type": "text", "text": m["text"]})
                tc = m.get("tool_call")
                if tc:
                    content.append({"type": "tool_use", "id": tc["id"], "name": tc["name"], "input": tc["input"]})
                out.append({"role": "assistant", "content": content})
            elif role == "tool":
                inner = [{"type": "text", "text": m.get("text", "")}]
                if m.get("image"):
                    inner.append(_anthropic_image(m["image"]))
                out.append({"role": "user", "content": [{
                    "type": "tool_result",
                    "tool_use_id": m["tool_call_id"],
                    "content": inner,
                    "is_error": m.get("is_error", False),
                }]})
        return out

    @staticmethod
    def _mark_last_cache(messages: list[dict]) -> None:
        """Put a rolling cache breakpoint at the end of the newest message.

        Each turn's write becomes the next turn's read: Anthropic serves the
        longest previously-cached prefix, so the whole growing transcript is read
        from cache (~0.1x) and only the newest observation is billed in full.
        """
        if not messages:
            return
        content = messages[-1].get("content")
        if isinstance(content, list) and content and isinstance(content[-1], dict):
            content[-1]["cache_control"] = {"type": "ephemeral"}

    def complete(self, system: str, messages: list[dict], tools: list[dict]) -> AssistantTurn:
        # Cache the stable prefix (tools render before system, so a breakpoint at
        # the end of system caches both) and roll a breakpoint over the history.
        if self.cache:
            system_param = [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}]
        else:
            system_param = system
        anth_messages = self._messages(messages)
        if self.cache:
            self._mark_last_cache(anth_messages)

        kwargs = dict(
            model=self.model,
            max_tokens=self.max_tokens,
            system=system_param,
            tools=tools,  # already in Anthropic format
            tool_choice={"type": "auto", "disable_parallel_tool_use": True},
            messages=anth_messages,
        )
        if self.thinking:
            kwargs["thinking"] = {"type": "adaptive"}
        resp = self.client.messages.create(**kwargs)

        text = " ".join(b.text for b in resp.content if b.type == "text").strip()
        tool_uses = [b for b in resp.content if b.type == "tool_use"]
        tool_call = None
        if tool_uses:
            b = tool_uses[0]
            tool_call = ToolCall(b.id, b.name, dict(b.input or {}))
        u = resp.usage
        usage = {
            "input": (getattr(u, "input_tokens", 0) or 0),          # uncached, full price
            "output": (getattr(u, "output_tokens", 0) or 0),
            "cache_read": (getattr(u, "cache_read_input_tokens", 0) or 0),      # ~0.1x
            "cache_write": (getattr(u, "cache_creation_input_tokens", 0) or 0),  # ~1.25x
        }
        return AssistantTurn(text, tool_call, resp.stop_reason or "end_turn", usage, raw=resp.content)


# ---------------------------------------------------------------------------
# OpenAI-compatible (OpenRouter, Groq, Gemini, GitHub Models, Ollama, ...)
# ---------------------------------------------------------------------------
def _openai_image(png: bytes) -> dict:
    return {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{_b64(png)}"}}


class OpenAICompatBackend:
    provider = "openai"

    def __init__(self, client, model: str, max_tokens: int = 4000, disable_parallel: bool = True,
                 fallback_models: list[str] | None = None):
        self.client = client
        self.model = model
        self.max_tokens = max_tokens
        self.disable_parallel = disable_parallel
        # Ordered alternates to rotate to if the current model hard-errors on a
        # real request (a model can pass the probe but 400 on the full payload).
        self.fallback_models = list(fallback_models or [])

    def _tools(self, tools: list[dict]) -> list[dict]:
        return [{
            "type": "function",
            "function": {
                "name": t["name"],
                "description": t.get("description", ""),
                "parameters": t["input_schema"],
            },
        } for t in tools]

    def _messages(self, system: str, messages: list[dict]) -> list[dict]:
        out = [{"role": "system", "content": system}]
        for m in messages:
            role = m["role"]
            if role == "user":
                if m.get("image"):
                    out.append({"role": "user", "content": [
                        {"type": "text", "text": m.get("text", "")},
                        _openai_image(m["image"]),
                    ]})
                else:
                    out.append({"role": "user", "content": m.get("text", "")})
            elif role == "assistant":
                tc = m.get("tool_call")
                if tc:
                    out.append({
                        "role": "assistant",
                        "content": m.get("text") or None,
                        "tool_calls": [{
                            "id": tc["id"],
                            "type": "function",
                            "function": {"name": tc["name"], "arguments": json.dumps(tc["input"])},
                        }],
                    })
                else:
                    out.append({"role": "assistant", "content": m.get("text", "")})
            elif role == "tool":
                # OpenAI tool messages are text-only; carry any screenshot in a
                # following user message so vision models still see it.
                out.append({"role": "tool", "tool_call_id": m["tool_call_id"], "content": m.get("text", "")})
                if m.get("image"):
                    out.append({"role": "user", "content": [
                        {"type": "text", "text": "Screenshot of the current screen:"},
                        _openai_image(m["image"]),
                    ]})
        return out

    def _create(self, params: dict):
        try:
            return self.client.chat.completions.create(**params)
        except Exception as e:
            # Some providers reject parallel_tool_calls — retry without it.
            if params.get("parallel_tool_calls") is False and "parallel_tool_calls" in str(e):
                params = dict(params)
                params.pop("parallel_tool_calls")
                return self.client.chat.completions.create(**params)
            raise

    def complete(self, system: str, messages: list[dict], tools: list[dict]) -> AssistantTurn:
        base = dict(
            max_tokens=self.max_tokens,
            messages=self._messages(system, messages),
            tools=self._tools(tools),
            tool_choice="auto",
        )
        if self.disable_parallel:
            base["parallel_tool_calls"] = False

        while True:
            try:
                resp = self._create(dict(base, model=self.model))
                break
            except Exception as e:
                code = getattr(e, "status_code", None)
                # 400/403/404 = this model can't serve the request; rotate to the
                # next candidate. Transient errors (429/5xx) bubble to the agent's
                # retry/backoff instead.
                if code in (400, 403, 404) and self.fallback_models:
                    nxt = self.fallback_models.pop(0)
                    print(f"  model {self.model} rejected the request (HTTP {code}); "
                          f"switching to {nxt}", flush=True)
                    self.model = nxt
                    continue
                raise

        choices = getattr(resp, "choices", None)
        if not choices or getattr(choices[0], "message", None) is None:
            # Providers (OpenRouter free tier especially) sometimes answer 200 with an error
            # body or no choices; surface it as a retryable failure, not a TypeError.
            err = getattr(resp, "error", None)
            raise EmptyResponseError(f"{self.model} returned no choices" + (f": {err}" if err else ""))
        msg = choices[0].message
        text = (msg.content or "").strip()
        tool_call = None
        tool_calls = getattr(msg, "tool_calls", None)
        if tool_calls:
            c = tool_calls[0]
            try:
                args = json.loads(c.function.arguments or "{}")
            except (json.JSONDecodeError, TypeError):
                args = {}
            tool_call = ToolCall(c.id, c.function.name, args)
        u = getattr(resp, "usage", None)
        prompt = (getattr(u, "prompt_tokens", 0) or 0) if u else 0
        cached = 0
        details = getattr(u, "prompt_tokens_details", None) if u else None
        if details is not None:
            cached = getattr(details, "cached_tokens", 0) or 0
        usage = {
            "input": max(prompt - cached, 0),  # keep input/cache_read disjoint (Anthropic convention)
            "output": (getattr(u, "completion_tokens", 0) or 0) if u else 0,
            "cache_read": cached,
            "cache_write": 0,
        }
        stop = choices[0].finish_reason or "stop"
        if not text and tool_call is None:
            # e.g. a reasoning model that spent its whole budget thinking (finish_reason=length)
            hint = " (raise max_tokens?)" if stop == "length" else ""
            raise EmptyResponseError(f"{self.model} produced no text or tool call, finish_reason={stop}{hint}")
        return AssistantTurn(text, tool_call, stop, usage, raw=None)

    def rotate_model(self) -> Optional[str]:
        """Switch to the next fallback model; returns it, or None if none are left."""
        if not self.fallback_models:
            return None
        self.model = self.fallback_models.pop(0)
        return self.model


# ---------------------------------------------------------------------------
# Cost estimation (Anthropic prices per 1M tokens; others usually free/unknown)
# ---------------------------------------------------------------------------
_PRICES = {  # model_id: (input_$per_1M, output_$per_1M)
    "claude-fable-5-1": (10.0, 50.0),
    "claude-opus-5": (5.0, 25.0),
    "claude-opus-4-8": (5.0, 25.0),
    "claude-opus-4-7": (5.0, 25.0),
    "claude-opus-4-6": (5.0, 25.0),
    "claude-sonnet-5": (2.0, 10.0),
    "claude-sonnet-4-6": (3.0, 15.0),
    "claude-haiku-4-5": (1.0, 5.0),
}


def is_daily_cap(e: Exception) -> bool:
    msg = str(e).lower()
    return "per-day" in msg or "free-models-per-day" in msg or ("daily" in msg and "limit" in msg)


def is_transient(e: Exception) -> bool:
    """Whether a model-call error is worth retrying (rate limit / server / network)."""
    if isinstance(e, EmptyResponseError):
        return True
    code = getattr(e, "status_code", None)
    if code in (408, 409, 429):
        return True
    if isinstance(code, int) and code >= 500:
        return True
    name = type(e).__name__.lower()
    return any(k in name for k in ("connection", "timeout"))


class BackendChain:
    """An ordered list of backends: free-first, paid last.

    Tries the current backend (retrying transient errors); on a persistent or
    provider-fatal failure — a spent free-tier daily cap, auth error, no live
    model — it advances to the next backend and retries the same request. The
    provider-neutral transcript makes switching providers mid-run safe.
    """

    def __init__(self, entries: list):  # [(backend, model, provider_name), ...]
        if not entries:
            raise ValueError("BackendChain needs at least one backend")
        self.entries = entries
        self.idx = 0
        self.switched = False

    @property
    def backend(self):
        return self.entries[self.idx][0]

    @property
    def model(self) -> str:
        entry = self.entries[self.idx]
        return getattr(entry[0], "model", None) or entry[1]  # backends may rotate models

    def rotate_model(self, log=None, reason: str = "") -> bool:
        """Move the current backend to its next fallback model, if it has one."""
        rotate = getattr(self.backend, "rotate_model", None)
        nxt = rotate() if rotate else None
        if nxt and log:
            log(f"      (switching model to {nxt}{': ' + reason if reason else ''})")
        return bool(nxt)

    @property
    def provider(self) -> str:
        return self.entries[self.idx][2]

    def complete(self, system, messages, tools, log=None) -> "AssistantTurn":
        import time

        say = log or (lambda *_: None)
        delay = 3.0
        transient_left = 3
        while True:
            backend, model, name = self.entries[self.idx]
            try:
                return backend.complete(system, messages, tools)
            except Exception as e:
                daily_cap = is_daily_cap(e)
                has_next = self.idx < len(self.entries) - 1
                # A 429 is usually per-model upstream throttling: try another free model
                # right away instead of burning backoff retries on the same one.
                if (not daily_cap and getattr(e, "status_code", None) == 429
                        and self.rotate_model(say, "rate limited (429)")):
                    transient_left, delay = 3, 3.0
                    continue
                if not daily_cap and is_transient(e) and transient_left > 0:
                    transient_left -= 1
                    say(f"      ({name} failed: {str(e)[:90]}; retry in {delay:g}s)")
                    time.sleep(delay)
                    delay *= 2
                    continue
                if not daily_cap and is_transient(e) and self.rotate_model(say, f"{str(e)[:60]}"):
                    transient_left, delay = 3, 3.0
                    continue
                if has_next:
                    nxt = self.entries[self.idx + 1][2]
                    say(f"      ({name} unavailable: {str(e)[:90]}) -> switching to {nxt}")
                    self.idx += 1
                    self.switched = True
                    delay = 3.0
                    transient_left = 3
                    continue
                raise


def estimate_cost(model: str, inp: int, out: int, cache_read: int = 0,
                  cache_write: int = 0) -> Optional[float]:
    """Rough $ estimate. ``inp`` is uncached input (disjoint from cache_read /
    cache_write). Cache reads bill ~0.1x, cache writes ~1.25x."""
    price = _PRICES.get(model)
    if not price:
        return None
    in_rate, out_rate = price
    return (
        inp * in_rate
        + cache_read * in_rate * 0.1
        + cache_write * in_rate * 1.25
        + out * out_rate
    ) / 1_000_000
