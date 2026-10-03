"""Anthropic tool definitions for the tester agent.

Each action the model can take is a tool. Every action tool carries a ``reason``
so the model states *why* it took the step — this is what makes the report
readable. ``note`` and ``finish`` are control tools, not UI actions.
"""

from __future__ import annotations

_REASON = {
    "type": "string",
    "description": "One short sentence: why this action moves the task forward.",
}
_ELEMENT = {
    "type": "string",
    "description": "Element id from the current screen, e.g. 'e12'.",
}


TOOLS = [
    {
        "name": "click",
        "description": "Left-click a control (button, link, menu item, list item, tab).",
        "input_schema": {
            "type": "object",
            "properties": {"element_id": _ELEMENT, "reason": _REASON},
            "required": ["element_id", "reason"],
        },
    },
    {
        "name": "double_click",
        "description": "Double-click a control (e.g. to open an item or enter edit mode).",
        "input_schema": {
            "type": "object",
            "properties": {"element_id": _ELEMENT, "reason": _REASON},
            "required": ["element_id", "reason"],
        },
    },
    {
        "name": "right_click",
        "description": "Right-click a control to open its context menu.",
        "input_schema": {
            "type": "object",
            "properties": {"element_id": _ELEMENT, "reason": _REASON},
            "required": ["element_id", "reason"],
        },
    },
    {
        "name": "type_text",
        "description": "Type text into an editable field (text box, combo box).",
        "input_schema": {
            "type": "object",
            "properties": {
                "element_id": _ELEMENT,
                "text": {"type": "string", "description": "Text to enter."},
                "clear_first": {
                    "type": "boolean",
                    "description": "Clear the field before typing (default true).",
                },
                "reason": _REASON,
            },
            "required": ["element_id", "text", "reason"],
        },
    },
    {
        "name": "select_item",
        "description": "Select an item in a list, tree, tab strip, or combo box by its id.",
        "input_schema": {
            "type": "object",
            "properties": {"element_id": _ELEMENT, "reason": _REASON},
            "required": ["element_id", "reason"],
        },
    },
    {
        "name": "set_toggle",
        "description": "Set a checkbox / radio / toggle to a specific state.",
        "input_schema": {
            "type": "object",
            "properties": {
                "element_id": _ELEMENT,
                "state": {
                    "type": "string",
                    "enum": ["on", "off", "toggle"],
                    "description": "Desired state; 'toggle' just flips it.",
                },
                "reason": _REASON,
            },
            "required": ["element_id", "state", "reason"],
        },
    },
    {
        "name": "expand_collapse",
        "description": "Expand or collapse a tree item, group, or drop-down.",
        "input_schema": {
            "type": "object",
            "properties": {
                "element_id": _ELEMENT,
                "action": {"type": "string", "enum": ["expand", "collapse"]},
                "reason": _REASON,
            },
            "required": ["element_id", "action", "reason"],
        },
    },
    {
        "name": "scroll",
        "description": (
            "Wheel-scroll a scrollable container (a list, tree, or panel) to "
            "REVEAL items that are not currently in the element list. Pass the "
            "container's element_id (omit to scroll the active window). Use this "
            "when the item you need isn't shown yet."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "element_id": {
                    "type": "string",
                    "description": "Id of the scrollable container to scroll over; omit to scroll the window.",
                },
                "direction": {"type": "string", "enum": ["down", "up", "left", "right"]},
                "amount": {"type": "integer", "description": "Wheel notches / steps (default 3, max 20)."},
                "reason": _REASON,
            },
            "required": ["direction", "reason"],
        },
    },
    {
        "name": "scroll_into_view",
        "description": (
            "Scroll an element that ALREADY exists in the list into view so it "
            "can be acted on (only works if the control supports it; otherwise "
            "use `scroll` on its container)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"element_id": _ELEMENT, "reason": _REASON},
            "required": ["element_id", "reason"],
        },
    },
    {
        "name": "press_keys",
        "description": (
            "Send keyboard input to the focused control. Special keys and "
            "modifiers use uiautomation syntax, e.g. '{Enter}', '{Tab}', "
            "'{Ctrl}s', '{Alt}{F4}', '{Ctrl}(ac)'."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "keys": {"type": "string", "description": "Key sequence to send."},
                "reason": _REASON,
            },
            "required": ["keys", "reason"],
        },
    },
    {
        "name": "wait",
        "description": "Pause to let the UI settle (loading, animations, async work).",
        "input_schema": {
            "type": "object",
            "properties": {
                "seconds": {"type": "number", "description": "Seconds to wait (max 15)."},
                "reason": _REASON,
            },
            "required": ["seconds", "reason"],
        },
    },
    {
        "name": "note",
        "description": (
            "Record a verification result or observation in the report WITHOUT "
            "acting on the UI. Use it to state whether an expected result held."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "The observation / check result."},
            },
            "required": ["text"],
        },
    },
    {
        "name": "finish",
        "description": "End the task and report the verdict. Call this exactly once at the end.",
        "input_schema": {
            "type": "object",
            "properties": {
                "status": {
                    "type": "string",
                    "enum": ["passed", "failed", "blocked"],
                    "description": (
                        "passed = task completed and expectations held; "
                        "failed = an expected result did not hold (a bug); "
                        "blocked = could not proceed (missing element, crash, unclear)."
                    ),
                },
                "summary": {
                    "type": "string",
                    "description": "What was done, what was verified, and any defects found.",
                },
            },
            "required": ["status", "summary"],
        },
    },
]


SYSTEM_PROMPT = """\
You are an experienced manual QA tester driving a native Windows desktop \
application through its UI. You work like a careful human tester: perform one \
action at a time, observe the result, and verify that the app behaves as expected.

Each turn you are given:
  - the TASK (on the first turn),
  - the current window title,
  - a text list of UI elements, each tagged with an id like [e12] plus its \
control type, name, value and state (disabled/offscreen/checked/selected),
  - a screenshot of the window.

The element list can be incomplete for custom-drawn controls — use the \
screenshot to fill the gaps, and reference elements by the ids shown in the \
MOST RECENT list (ids are reassigned every turn).

Rules:
  - Call EXACTLY ONE action tool per turn. Always include a short `reason`.
  - Prefer targeting elements by id. If the element you need is not listed, \
`scroll` its likely container to reveal more items, expand a container, open a \
menu, or press_keys to navigate.
  - After an action, check the new screen against what you expected. When you \
verify an expected result (or find it violated), record it with `note` before \
moving on — this is the evidence in the test report.
  - Do NOT perform destructive or irreversible actions (delete data, purchase, \
send, factory reset, uninstall) unless the task explicitly requires it.
  - If the app is loading or animating, use `wait` rather than clicking blindly.
  - Keep going until the task is complete or you are genuinely blocked, then \
call `finish` with the verdict:
      passed  — task done and all checks held,
      failed  — the app did something wrong (describe the defect),
      blocked — you could not proceed (say why).
  - Be efficient: no redundant clicks, no re-reading the same screen twice.
"""


# Optional OCR/vision tools — for content the UIA element list can't expose.
VISUAL_TOOLS = [
    {
        "name": "click_text",
        "description": (
            "Click on-screen TEXT located by OCR. Use this for content that is NOT "
            "in the element list — web views, embedded browsers, custom-drawn "
            "canvases. Matches the visible text you give it."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "Visible text to click (substring is fine)."},
                "button": {"type": "string", "enum": ["left", "right"]},
                "double": {"type": "boolean", "description": "Double-click (default false)."},
                "occurrence": {
                    "type": "integer",
                    "description": "Which match if several (1 = first, top-to-bottom).",
                },
                "reason": _REASON,
            },
            "required": ["text", "reason"],
        },
    },
    {
        "name": "assert_text",
        "description": (
            "Check via OCR whether text is currently visible on screen. Returns "
            "PASS/FAIL — a deterministic way to verify an expected result."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "Text you expect to be visible."},
                "reason": _REASON,
            },
            "required": ["text", "reason"],
        },
    },
    {
        "name": "click_at",
        "description": (
            "Click a position given as NORMALIZED 0-1000 window coordinates "
            "(x: 0=left..1000=right, y: 0=top..1000=bottom). Last resort when the "
            "target has no element id and no readable text."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "x": {"type": "integer", "description": "0-1000 across the window."},
                "y": {"type": "integer", "description": "0-1000 down the window."},
                "button": {"type": "string", "enum": ["left", "right"]},
                "double": {"type": "boolean"},
                "reason": _REASON,
            },
            "required": ["x", "y", "reason"],
        },
    },
]


VISUAL_HINT = """

Some content is NOT in the element list — web views, embedded browsers and \
custom-drawn canvases render as pixels with no id. To act on those:
  - `click_text` — click visible text located by OCR (preferred for web-view content),
  - `assert_text` — deterministically verify some text is visible (use it for checks),
  - `click_at` — click a normalized 0-1000 window position, only as a last resort.
Still prefer element ids when the target is listed; reach for these when it isn't.
"""


def build_tools(visual: bool = False) -> list:
    """Base tools, plus the OCR/vision tools when ``visual`` is enabled."""
    return TOOLS + (VISUAL_TOOLS if visual else [])


def build_system(visual: bool = False) -> str:
    return SYSTEM_PROMPT + (VISUAL_HINT if visual else "")
