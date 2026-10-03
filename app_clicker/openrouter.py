"""Discover currently-available free models on OpenRouter.

OpenRouter's free catalogue churns constantly — a given ``:free`` slug can become
paid or disappear (404) at any time — so hardcoding one is fragile. Instead we
query the live model list, filter to free + tool-capable (+ vision if needed),
and probe candidates to pick one that actually works right now.
"""

from __future__ import annotations

import json
import urllib.request

MODELS_URL = "https://openrouter.ai/api/v1/models"


def list_free_tool_models(require_vision: bool = False, timeout: float = 30.0) -> list[str]:
    """Return ids of free, tool-calling OpenRouter models (best candidates first).

    Ordering: vision-capable first (unless not needed), then larger context.
    """
    req = urllib.request.Request(MODELS_URL, headers={"User-Agent": "app-clicker"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        data = json.load(r)["data"]

    rows = []
    for m in data:
        pricing = m.get("pricing") or {}
        is_free = pricing.get("prompt") in ("0", 0) and pricing.get("completion") in ("0", 0)
        if not is_free:
            continue
        if "tools" not in (m.get("supported_parameters") or []):
            continue
        modalities = (m.get("architecture") or {}).get("input_modalities") or []
        has_vision = "image" in modalities
        if require_vision and not has_vision:
            continue
        rows.append((m["id"], has_vision, m.get("context_length", 0) or 0))

    rows.sort(key=lambda r: (not r[1], -r[2]))  # vision first, larger context first
    return [mid for mid, _, _ in rows]


_PROBE_TOOL = [{
    "type": "function",
    "function": {
        "name": "ok",
        "description": "Acknowledge.",
        "parameters": {
            "type": "object",
            "properties": {"x": {"type": "string"}},
            "required": ["x"],
            "additionalProperties": False,
        },
    },
}]

# 1x1 transparent PNG — sent in the probe when vision is required, so a model that
# is listed as vision-capable but rejects image content is filtered out up front.
_TINY_PNG = (
    "data:image/png;base64,"
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+M8AAAMBAQDJ/pLvAAAAAElFTkSuQmCC"
)


def pick_free_model(client, require_vision: bool = False, probe: bool = True,
                    max_probe: int = 6, log=print, candidates: list[str] | None = None) -> str:
    """Choose a free model that is live right now.

    With ``probe`` (default), sends a tiny tool-call request (plus a 1x1 image when
    ``require_vision``) to each candidate and returns the first that emits a
    well-formed tool call, falling back to any that merely responded. Raises if none
    are usable. Pass ``candidates`` to reuse an already-fetched model list.
    """
    if candidates is None:
        candidates = list_free_tool_models(require_vision=require_vision)
    if not candidates:
        raise RuntimeError(
            f"No free tool-capable{' vision' if require_vision else ''} models "
            "listed on OpenRouter right now."
        )
    if not probe:
        return candidates[0]

    if require_vision:
        content = [
            {"type": "text", "text": "Call the ok tool with x='1'."},
            {"type": "image_url", "image_url": {"url": _TINY_PNG}},
        ]
    else:
        content = "Call the ok tool with x='1'."

    responded = None
    last_err = None
    for mid in candidates[:max_probe]:
        try:
            r = client.chat.completions.create(
                model=mid,
                max_tokens=32,
                messages=[{"role": "user", "content": content}],
                tools=_PROBE_TOOL,
                tool_choice="auto",
            )
            if getattr(r.choices[0].message, "tool_calls", None):
                return mid                      # best: actually emits tool calls
            responded = responded or mid         # 200 but no tool call this time
        except Exception as e:                   # 400 / 404 / 403 / 429 -> try the next
            last_err = e
            if log:
                log(f"  (skip {mid}: {str(e)[:70]})")
            continue

    if responded:
        return responded
    raise RuntimeError(
        f"No free model responded successfully (tried {min(len(candidates), max_probe)}). "
        f"Last error: {last_err}"
    )
