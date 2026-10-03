"""Tool definitions and system prompt for the web engine.

Same contract as the desktop tools — one action per turn, every action carries a
``reason``, ``note`` records evidence, ``finish`` gives the verdict — with the
actions a browser needs: native drop-downs, navigation, tabs and JavaScript
dialogs. ``assert_text`` checks the DOM, so it needs no OCR.
"""

from __future__ import annotations

from ..tools import _ELEMENT, _REASON, TOOLS


def _element_tool(name: str, description: str) -> dict:
    return {
        "name": name,
        "description": description,
        "input_schema": {
            "type": "object",
            "properties": {"element_id": _ELEMENT, "reason": _REASON},
            "required": ["element_id", "reason"],
        },
    }


_SHARED = {t["name"]: t for t in TOOLS if t["name"] in ("wait", "note", "finish")}

WEB_TOOLS = [
    _element_tool("click", "Left-click an element (button, link, tab, menu item, custom control)."),
    _element_tool("double_click", "Double-click an element."),
    _element_tool("right_click", "Right-click an element to open its context menu."),
    _element_tool("hover", "Move the mouse over an element, e.g. to open a hover menu or show a tooltip."),
    {
        "name": "type_text",
        "description": "Type text into a text field, text area or editable region.",
        "input_schema": {
            "type": "object",
            "properties": {
                "element_id": _ELEMENT,
                "text": {"type": "string", "description": "Text to enter."},
                "clear_first": {
                    "type": "boolean",
                    "description": "Replace the current content (default true); false appends.",
                },
                "reason": _REASON,
            },
            "required": ["element_id", "text", "reason"],
        },
    },
    {
        "name": "select_option",
        "description": (
            "Choose an option in a native drop-down (an element listed with options=[...]). "
            "For custom drop-downs, click to open them and click the option instead."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "element_id": _ELEMENT,
                "option": {"type": "string", "description": "The option's visible label (or its value)."},
                "reason": _REASON,
            },
            "required": ["element_id", "option", "reason"],
        },
    },
    {
        "name": "set_toggle",
        "description": "Set a checkbox, radio button or switch to a specific state.",
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
        "name": "scroll",
        "description": (
            "Scroll to REVEAL content that is not in the element list yet (lazy-loaded or cut-off "
            "lists). Pass an element inside the panel to scroll that panel; omit element_id to "
            "scroll the page. Listed elements marked offscreen don't need this — act on them directly."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "element_id": {
                    "type": "string",
                    "description": "An element inside the scrollable panel; omit to scroll the page.",
                },
                "direction": {"type": "string", "enum": ["down", "up", "left", "right"]},
                "amount": {"type": "integer", "description": "Steps of about 100px (default 3, max 20)."},
                "reason": _REASON,
            },
            "required": ["direction", "reason"],
        },
    },
    _element_tool(
        "scroll_into_view",
        "Scroll a listed element into view, e.g. to show it in the screenshot before verifying it.",
    ),
    {
        "name": "press_keys",
        "description": (
            "Press keys in the focused element, one or more separated by spaces. Playwright key "
            "names: 'Enter', 'Tab', 'Escape', 'ArrowDown', 'PageDown', 'Control+A', 'Shift+Tab'. "
            "To enter text use type_text."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "keys": {"type": "string", "description": "Key sequence, e.g. 'Tab Tab Enter'."},
                "reason": _REASON,
            },
            "required": ["keys", "reason"],
        },
    },
    {
        "name": "navigate",
        "description": "Load a URL in the current tab. Only when the task needs a specific address.",
        "input_schema": {
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "Absolute URL; https:// is assumed if omitted."},
                "reason": _REASON,
            },
            "required": ["url", "reason"],
        },
    },
    {
        "name": "go_back",
        "description": "Go back to the previous page in this tab's history, like the browser Back button.",
        "input_schema": {
            "type": "object",
            "properties": {"reason": _REASON},
            "required": ["reason"],
        },
    },
    {
        "name": "switch_tab",
        "description": "Make another open tab active, by its index from the 'Open tabs' line.",
        "input_schema": {
            "type": "object",
            "properties": {
                "index": {"type": "integer", "description": "Tab index, 0 = first."},
                "reason": _REASON,
            },
            "required": ["index", "reason"],
        },
    },
    {
        "name": "set_dialog_response",
        "description": (
            "Decide how the NEXT JavaScript confirm/prompt dialog is answered. Call it BEFORE the "
            "action that opens the dialog. Without it, confirm and prompt dialogs are dismissed."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["accept", "dismiss"]},
                "prompt_text": {"type": "string", "description": "Text to enter when accepting a prompt dialog."},
                "reason": _REASON,
            },
            "required": ["action", "reason"],
        },
    },
    {
        "name": "assert_text",
        "description": (
            "Check whether text is shown on the page (rendered and not hidden; it may be scrolled out "
            "of view) or is the value of a form field. Case-insensitive. Returns PASS/FAIL — a "
            "deterministic way to verify an expected result."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "Text you expect to be shown."},
                "reason": _REASON,
            },
            "required": ["text", "reason"],
        },
    },
    {
        "name": "click_at",
        "description": (
            "Click a position given as NORMALIZED 0-1000 viewport coordinates (x: 0=left..1000=right, "
            "y: 0=top..1000=bottom), as seen in the screenshot. Last resort for canvas or other "
            "content that has no element id."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "x": {"type": "integer", "description": "0-1000 across the viewport."},
                "y": {"type": "integer", "description": "0-1000 down the viewport."},
                "button": {"type": "string", "enum": ["left", "right"]},
                "double": {"type": "boolean"},
                "reason": _REASON,
            },
            "required": ["x", "y", "reason"],
        },
    },
    _SHARED["wait"],
    _SHARED["note"],
    _SHARED["finish"],
]


WEB_SYSTEM = """\
You are an experienced manual QA tester driving a web application in a real \
browser. You work like a careful human tester: perform one action at a time, \
observe the result, and verify that the app behaves as expected.

Each turn you are given:
  - the TASK (on the first turn),
  - the current page as "title — URL",
  - events since your last action, when there were any: JavaScript dialogs and how \
they were answered, tabs that opened or closed, uncaught script errors, console errors,
  - the open tabs, when there is more than one,
  - what the page renders, in document order:
      [e12] button "Save" (disabled)   an element you can act on, with its id
      heading "Orders"  text "..."  row "a | b | c"  alert "..."  img "..."   context, no id
    Indented lines are inside the dialog, frame or clickable region above them. \
Elements show their role and name, value="..." for fields (passwords masked), \
options=[...] for native drop-downs, and states: checked, selected, expanded, \
collapsed, disabled, readonly, required, invalid, focused, offscreen (scrolled \
out of view), covered by X (something such as a modal backdrop sits on top),
  - a screenshot of the visible part of the page.

Rules:
  - Call EXACTLY ONE action tool per turn. Always include a short `reason`.
  - Reference elements by the ids in the MOST RECENT list — ids are reassigned every turn.
  - Offscreen elements can be acted on directly; the browser scrolls to them. Use \
`scroll` only to reveal content that is not listed yet.
  - A covered element can't be clicked until whatever covers it is closed.
  - Use `type_text` for fields, `select_option` for native drop-downs, and \
`set_toggle` for checkboxes, radios and switches. Custom drop-downs: click to open, \
then click the option.
  - Confirm and prompt dialogs are dismissed unless you call `set_dialog_response` \
BEFORE the action that opens them; alerts are accepted. Every dialog is reported \
under events.
  - Verify expected results with `assert_text` where it fits, and record each \
verification (or violation) with `note` before moving on — this is the evidence in \
the test report. Uncaught script errors are possible defects worth noting.
  - Stay within the app under test; use `navigate` only when the task needs a specific URL.
  - Do NOT perform destructive or irreversible actions (delete data, purchase, send \
messages, change passwords) unless the task explicitly requires it.
  - If the page is loading, use `wait` rather than acting blindly.
  - Keep going until the task is complete or you are genuinely blocked, then call \
`finish` with the verdict:
      passed  — task done and all checks held,
      failed  — the app did something wrong (describe the defect),
      blocked — you could not proceed (say why).
  - Be efficient: no redundant actions, no re-reading the same page twice.
"""
