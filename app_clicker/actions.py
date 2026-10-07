"""Action executor: carry out the model's chosen action on a real control.

Actions are UIA-pattern-first (invoke / value / selection / toggle / expand /
scroll) with a physical mouse fallback, because pattern-based interaction does
not require the window to be foreground and is far more reliable than blind
clicks.

Note: this build of `uiautomation` attaches the ``GetXxxPattern`` convenience
methods *per control type* — a control only has ``GetScrollPattern`` if it
actually supports ScrollPattern. So we must fetch patterns via ``getattr`` and
tolerate their absence, never assume the method exists.
"""

from __future__ import annotations

import time

import uiautomation as auto

from .visual import VisualMatcher


class ActionError(Exception):
    """A recoverable failure the model should see and can retry differently."""


class ArgumentError(ActionError):
    """The tool call itself was malformed (missing/unknown arguments).

    Nothing happened on screen, so the agent doesn't charge it to the step budget.
    """


def missing_arg_error(name: str, missing: str, inp: dict, elements: dict | None) -> ArgumentError:
    """Build an error that tells a weak model exactly how to correct the call."""
    msg = f"Missing required argument {missing} for action '{name}'."
    if missing.strip("'\"") == "element_id":
        msg += " Pass `element_id` — an id such as 'e12' from the UI elements list."
        if {"x", "y"} & set(inp):
            msg += (" Raw x/y coordinates are not accepted by this action; "
                    "`click_at` (normalized 0-1000) exists only as a last resort when available.")
        ids = list(elements or {})
        if ids:
            shown = ", ".join(ids[:15]) + (" ..." if len(ids) > 15 else "")
            msg += f" Valid ids right now: {shown}."
    return ArgumentError(msg)


def _safe(getter, default=None):
    try:
        return getter()
    except Exception:
        return default


def _click_screen(x, y, button: str = "left", double: bool = False) -> None:
    """Click at absolute screen coordinates."""
    x, y = int(round(x)), int(round(y))
    if button == "right":
        auto.RightClick(x, y)
    elif double:
        auto.Click(x, y)
        auto.Click(x, y)
    else:
        auto.Click(x, y)


def _pattern(ctrl, getter_name: str):
    """Return a UIA pattern object, or None if the control doesn't support it.

    Guards both the missing-method case (control type lacks the pattern getter)
    and the getter raising a COM error.
    """
    getter = getattr(ctrl, getter_name, None)
    if getter is None:
        return None
    try:
        return getter()
    except Exception:
        return None


class ActionExecutor:
    def __init__(self, window, default_wait: float = 0.6):
        self.window = window
        self.default_wait = default_wait
        self.visual = VisualMatcher(window)

    # -- internals ---------------------------------------------------------
    def _focus_window(self) -> None:
        for name in ("SetActive", "SetFocus"):
            fn = getattr(self.window, name, None)
            if fn is None:
                continue
            try:
                fn()
                return
            except Exception:
                continue

    def _get(self, elements: dict, element_id: str):
        ctrl = elements.get(element_id)
        if ctrl is None:
            raise ActionError(
                f"Unknown element_id {element_id!r}. Use an id shown in the current screen."
            )
        return ctrl

    def _ensure_visible(self, ctrl) -> None:
        if _safe(lambda: ctrl.IsOffscreen, False):
            sp = _pattern(ctrl, "GetScrollItemPattern")
            if sp is not None:
                _safe(sp.ScrollIntoView)

    def _has_area(self, ctrl) -> bool:
        rect = _safe(lambda: ctrl.BoundingRectangle)
        if rect is None:
            return False
        return bool(_safe(lambda: rect.width(), 0)) and bool(_safe(lambda: rect.height(), 0))

    # -- actions -----------------------------------------------------------
    def click(self, elements, element_id, button: str = "left", double: bool = False) -> str:
        ctrl = self._get(elements, element_id)
        self._focus_window()
        self._ensure_visible(ctrl)
        has_area = self._has_area(ctrl)

        if button == "right":
            if not has_area:
                raise ActionError("Element has no on-screen area to right-click.")
            ctrl.RightClick(waitTime=self.default_wait)
            return f"Right-clicked {element_id}."

        if double:
            if not has_area:
                raise ActionError("Element has no on-screen area to double-click.")
            ctrl.DoubleClick(waitTime=self.default_wait)
            return f"Double-clicked {element_id}."

        if has_area:
            ctrl.Click(waitTime=self.default_wait)
            return f"Clicked {element_id}."

        # No clickable rectangle — fall back to invoke/select patterns.
        ip = _pattern(ctrl, "GetInvokePattern")
        if ip is not None and _safe(ip.Invoke) is not None:
            time.sleep(self.default_wait)
            return f"Invoked {element_id} (no clickable area)."
        sp = _pattern(ctrl, "GetSelectionItemPattern")
        if sp is not None and _safe(sp.Select) is not None:
            time.sleep(self.default_wait)
            return f"Selected {element_id} (no clickable area)."
        raise ActionError("Element is not clickable and exposes no invoke/select pattern.")

    def type_text(self, elements, element_id, text: str, clear_first: bool = True) -> str:
        ctrl = self._get(elements, element_id)
        self._focus_window()
        self._ensure_visible(ctrl)
        _safe(ctrl.SetFocus)

        vp = _pattern(ctrl, "GetValuePattern")
        if vp is not None:
            if clear_first:
                new_value = text
            else:
                current = _safe(lambda: vp.Value, "") or ""
                new_value = current + text
            _safe(lambda: vp.SetValue(new_value))
            time.sleep(self.default_wait)
            return f'Typed into {element_id}: "{text}"'

        # No ValuePattern: fall back to real keystrokes.
        if clear_first:
            ctrl.SendKeys("{Ctrl}a{Delete}", waitTime=0)
        # NOTE: uiautomation treats "{...}" as special keys. Plain text is fine;
        # for text containing braces use the press_keys action instead.
        ctrl.SendKeys(text, waitTime=self.default_wait)
        return f'Typed into {element_id}: "{text}"'

    def select_item(self, elements, element_id) -> str:
        ctrl = self._get(elements, element_id)
        self._focus_window()
        self._ensure_visible(ctrl)
        sp = _pattern(ctrl, "GetSelectionItemPattern")
        if sp is None:
            return self.click(elements, element_id)
        _safe(sp.Select)
        time.sleep(self.default_wait)
        return f"Selected {element_id}."

    def set_toggle(self, elements, element_id, state: str = "toggle") -> str:
        ctrl = self._get(elements, element_id)
        self._focus_window()
        self._ensure_visible(ctrl)
        tp = _pattern(ctrl, "GetTogglePattern")
        if tp is None:
            return self.click(elements, element_id)
        want = {"on": 1, "off": 0}.get(state)
        for _ in range(3):
            current = _safe(lambda: tp.ToggleState)
            if want is None or current == want:
                break
            _safe(tp.Toggle)
            time.sleep(0.2)
        time.sleep(self.default_wait)
        return f"Set toggle {element_id} to {state}."

    def expand_collapse(self, elements, element_id, action: str = "expand") -> str:
        ctrl = self._get(elements, element_id)
        self._focus_window()
        self._ensure_visible(ctrl)
        ep = _pattern(ctrl, "GetExpandCollapsePattern")
        if ep is None:
            return self.click(elements, element_id)
        _safe(ep.Expand if action == "expand" else ep.Collapse)
        time.sleep(self.default_wait)
        return f"{action.capitalize()}d {element_id}."

    def scroll_into_view(self, elements, element_id) -> str:
        ctrl = self._get(elements, element_id)
        sp = _pattern(ctrl, "GetScrollItemPattern")
        if sp is None:
            raise ActionError(
                "Element exposes no ScrollItemPattern. Use the `scroll` action on "
                "its scrollable container instead."
            )
        _safe(sp.ScrollIntoView)
        time.sleep(self.default_wait)
        return f"Scrolled {element_id} into view."

    def scroll(self, elements, element_id=None, direction: str = "down", amount: int = 3) -> str:
        """Wheel-scroll a container (or the window) to reveal off-screen content."""
        target = self._get(elements, element_id) if element_id else self.window
        self._focus_window()
        direction = str(direction).lower()
        amount = max(1, min(int(amount), 20))
        where = element_id or "window"

        if direction in ("down", "up"):
            wheel = getattr(target, "WheelDown" if direction == "down" else "WheelUp", None)
            if wheel is not None:
                try:
                    wheel(wheelTimes=amount, waitTime=self.default_wait)
                    return f"Scrolled {direction} {amount} on {where}."
                except Exception:
                    pass  # fall through to ScrollPattern
            if self._scroll_via_pattern(target, direction, amount):
                return f"Scrolled {direction} on {where} (ScrollPattern)."
            raise ActionError("Target does not support vertical scrolling.")

        if direction in ("left", "right"):
            if self._scroll_via_pattern(target, direction, amount):
                return f"Scrolled {direction} on {where}."
            raise ActionError("Target does not support horizontal scrolling via UIA.")

        raise ActionError(f"Unknown scroll direction {direction!r} (use up/down/left/right).")

    def _scroll_via_pattern(self, target, direction: str, amount: int) -> bool:
        sp = _pattern(target, "GetScrollPattern")
        if sp is None:
            return False
        amounts = {
            "down": (auto.ScrollAmount.NoAmount, auto.ScrollAmount.SmallIncrement),
            "up": (auto.ScrollAmount.NoAmount, auto.ScrollAmount.SmallDecrement),
            "right": (auto.ScrollAmount.SmallIncrement, auto.ScrollAmount.NoAmount),
            "left": (auto.ScrollAmount.SmallDecrement, auto.ScrollAmount.NoAmount),
        }[direction]
        did = False
        for _ in range(amount):
            try:
                if not sp.Scroll(*amounts):
                    break
            except Exception:
                break
            did = True
        time.sleep(self.default_wait)
        return did

    # -- visual (OCR) actions ---------------------------------------------
    def click_text(self, text: str, button: str = "left", double: bool = False,
                   occurrence: int = 1) -> str:
        self._focus_window()
        words, _ = self.visual.ocr_words()
        matches = self.visual.find(text, words)
        if not matches:
            seen = ", ".join(dict.fromkeys(w["text"] for w in words[:25]))
            raise ActionError(
                f"Text {text!r} not found on screen. Visible text includes: {seen or '(none read)'}"
            )
        # order top-to-bottom, then left-to-right (row-banded)
        matches = sorted(matches, key=lambda w: (round(w["cy"] / 8), w["cx"]))
        m = matches[min(max(int(occurrence) - 1, 0), len(matches) - 1)]
        sx, sy = m["screen"]
        _click_screen(sx, sy, button, double)
        time.sleep(self.default_wait)
        verb = "Right-clicked" if button == "right" else ("Double-clicked" if double else "Clicked")
        return f"{verb} text '{m['text']}' at screen ({int(sx)},{int(sy)})."

    def click_at(self, x, y, button: str = "left", double: bool = False) -> str:
        self._focus_window()
        rect = self.window.BoundingRectangle
        w, h = rect.right - rect.left, rect.bottom - rect.top
        sx = rect.left + (float(x) / 1000.0) * w
        sy = rect.top + (float(y) / 1000.0) * h
        _click_screen(sx, sy, button, double)
        time.sleep(self.default_wait)
        return f"Clicked at ({x},{y})/1000 -> screen ({int(sx)},{int(sy)})."

    def assert_text(self, text: str) -> str:
        words, _ = self.visual.ocr_words()
        matches = self.visual.find(text, words)
        if matches:
            return f"assert_text PASS: {text!r} is visible (matched '{matches[0]['text']}')."
        seen = ", ".join(dict.fromkeys(w["text"] for w in words[:25]))
        return f"assert_text FAIL: {text!r} not found. Visible text: {seen or '(none read)'}"

    def press_keys(self, keys: str) -> str:
        self._focus_window()
        auto.SendKeys(keys, waitTime=self.default_wait)
        return f"Pressed keys: {keys}"

    def wait(self, seconds: float = 1.0) -> str:
        seconds = min(max(float(seconds), 0.0), 15.0)
        time.sleep(seconds)
        return f"Waited {seconds:g}s."

    # -- dispatch ----------------------------------------------------------
    def dispatch(self, name: str, inp: dict, elements: dict) -> str:
        inp = dict(inp or {})
        inp.pop("reason", None)  # rationale is logged by the caller, not an arg
        try:
            if name == "click":
                return self.click(elements, inp["element_id"])
            if name == "double_click":
                return self.click(elements, inp["element_id"], double=True)
            if name == "right_click":
                return self.click(elements, inp["element_id"], button="right")
            if name == "type_text":
                return self.type_text(
                    elements, inp["element_id"], inp.get("text", ""),
                    inp.get("clear_first", True),
                )
            if name == "select_item":
                return self.select_item(elements, inp["element_id"])
            if name == "set_toggle":
                return self.set_toggle(elements, inp["element_id"], inp.get("state", "toggle"))
            if name == "expand_collapse":
                return self.expand_collapse(elements, inp["element_id"], inp.get("action", "expand"))
            if name == "scroll_into_view":
                return self.scroll_into_view(elements, inp["element_id"])
            if name == "scroll":
                return self.scroll(
                    elements, inp.get("element_id"), inp.get("direction", "down"),
                    inp.get("amount", 3),
                )
            if name == "click_text":
                return self.click_text(
                    inp["text"], inp.get("button", "left"),
                    inp.get("double", False), inp.get("occurrence", 1),
                )
            if name == "click_at":
                return self.click_at(
                    inp["x"], inp["y"], inp.get("button", "left"), inp.get("double", False),
                )
            if name == "assert_text":
                return self.assert_text(inp["text"])
            if name == "press_keys":
                return self.press_keys(inp["keys"])
            if name == "wait":
                return self.wait(inp.get("seconds", 1))
        except KeyError as e:
            raise missing_arg_error(name, str(e), inp, elements)
        raise ActionError(f"Unknown action '{name}'.")
