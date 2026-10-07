"""Scripted (model-free) test cases: a list of steps run straight against the UI.

A model-driven run is good for exploring but not for gating a build: it can take a
different path each time, and its verdict is a judgement. A case with ``steps:`` instead of
``task:`` runs the same actions deterministically, with no model and no API key, and ends
in explicit assertions:

    tests:
      - name: Applying the tip highlights lead II
        steps:
          - click: "Применить"                           # exact name
          - click: {type: CheckBox, name: "Показать отведение"}
          - assert_value: {id: StatusLabel, contains: "применено"}
          - assert_ocr: {text: "II", in: {name: "Ритм"}}   # text drawn on a canvas
          - assert_no_ocr: {text: "aVL", in: {name: "Ритм"}}

Steps reuse :class:`ActionExecutor` (UIA patterns first, mouse fallback), so a click here
behaves exactly like a model's click; only the way the element is chosen differs
(:mod:`app_clicker.locate`). Every step is validated before anything runs, so a typo in
step 9 does not surface after the app has been driven through steps 1-8.
"""

from __future__ import annotations

import difflib
import re
import time
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any, Callable, Optional

from .actions import ActionExecutor, _pattern
from .agent import RunResult
from .locate import (LOCATOR_KEYS, ElementNotFound, Locator, LocatorError, Resolver, build_locator,
                     squash, take_locator)

# Measured on a real canvas (a green label over a waveform): "sparse text" (11) finds the label at
# every scale, whereas "one block" (6) tries to read the waveform as text and returns junk. A lone
# "II" is misread as Il / i / II depending on the scale, so a region is read at several scales and
# the words are combined.
DEFAULT_REGION_PSM = 11
DEFAULT_REGION_SCALES = (2, 3, 4)
MAX_TIMEOUT = 300.0


class ScriptError(ValueError):
    """A script is malformed; the message names the case, the step and the problem."""


class AssertionFailed(Exception):
    """An assertion step did not hold (the case verdict is 'failed', not 'blocked')."""


# -- argument validation -----------------------------------------------------------
def _text(v):
    if isinstance(v, bool) or not isinstance(v, (str, int, float)):
        raise ValueError("must be text")
    return str(v)


def _bool(v):
    if not isinstance(v, bool):
        raise ValueError("must be true or false")
    return v


def _number(lo, hi):
    def check(v):
        if isinstance(v, bool) or not isinstance(v, (int, float)) or not lo <= v <= hi:
            raise ValueError(f"must be a number from {lo:g} to {hi:g}")
        return v
    return check


def _integer(lo, hi):
    def check(v):
        if isinstance(v, bool) or not isinstance(v, int) or not lo <= v <= hi:
            raise ValueError(f"must be a whole number from {lo} to {hi}")
        return v
    return check


def _choice(*options):
    def check(v):
        if not isinstance(v, str) or v.strip().lower() not in options:
            raise ValueError(f"must be one of: {', '.join(options)}")
        return v.strip().lower()
    return check


def _scales(v):
    values = v if isinstance(v, list) else [v]
    check = _number(0.5, 8)
    if not values or len(values) > 5:
        raise ValueError("must be a number from 0.5 to 8, or a list of up to 5 of them")
    try:
        return [check(n) for n in values]
    except ValueError:
        raise ValueError("must be a number from 0.5 to 8, or a list of up to 5 of them")


def _regex(v):
    try:
        re.compile(_text(v))
    except re.error as e:
        raise ValueError(f"is not a valid regex ({e})")
    return _text(v)


def _tristate(v):
    if isinstance(v, bool) or v == "mixed":
        return v
    raise ValueError("must be true, false or mixed")


def _invert(v):
    if isinstance(v, bool) or v == "auto":
        return v
    raise ValueError("must be true, false or auto")


def _region(v):
    ok = isinstance(v, list) and len(v) == 4 and all(
        isinstance(n, (int, float)) and not isinstance(n, bool) and 0 <= n <= 1 for n in v)
    if not ok or not (v[0] < v[2] and v[1] < v[3]):
        raise ValueError("must be [left, top, right, bottom] as fractions of the window, "
                         "e.g. [0.1, 0.1, 0.9, 0.5] (left < right, top < bottom)")
    return [float(n) for n in v]


ARG_CHECKS: dict[str, Callable] = {
    "text": _text, "keys": _text, "equals": _text, "contains": _text, "regex": _regex,
    "clear_first": _bool, "double": _bool, "ignore_case": _bool, "lenient": _bool,
    "state": _choice("on", "off", "toggle"),
    "action": _choice("expand", "collapse"),
    "direction": _choice("up", "down", "left", "right"),
    "button": _choice("left", "right"),
    "amount": _integer(1, 20), "occurrence": _integer(1, 100), "psm": _integer(0, 13),
    "min_conf": _integer(0, 100),
    "seconds": _number(0, 15), "x": _number(0, 1000), "y": _number(0, 1000),
    "scale": _scales,
    "enabled": _bool, "selected": _bool, "focused": _bool, "checked": _tristate, "expanded": _bool,
    "region": _region, "match": _choice("word", "contains"), "invert": _invert,
}

STATE_KEYS = ("enabled", "selected", "checked", "expanded", "focused")
_OCR_ARGS = ("text", "regex", "region", "in", "match", "ignore_case", "lenient", "scale", "psm",
             "min_conf", "invert")
_TEXT_MATCH = ("equals", "contains", "regex", "ignore_case")


@dataclass(frozen=True)
class Spec:
    args: tuple = ()
    required: tuple = ()
    locator: str = "none"            # "none" | "required" | "optional"
    scalar: Optional[str] = None     # what a bare value means: `click: "Tips"` -> name "Tips"
    one_of: tuple = ()               # exactly one of these keys
    any_of: tuple = ()               # at least one of these keys
    nonempty: tuple = ()
    ocr: bool = False


ACTIONS: dict[str, Spec] = {
    "click": Spec(locator="required", scalar="name"),
    "double_click": Spec(locator="required", scalar="name"),
    "right_click": Spec(locator="required", scalar="name"),
    "type_text": Spec(args=("text", "clear_first"), required=("text",), locator="required"),
    "select_item": Spec(locator="required", scalar="name"),
    "set_toggle": Spec(args=("state",), locator="required"),
    "expand_collapse": Spec(args=("action",), locator="required"),
    "scroll_into_view": Spec(locator="required", scalar="name"),
    "scroll": Spec(args=("direction", "amount"), locator="optional", scalar="direction"),
    "press_keys": Spec(args=("keys",), required=("keys",), scalar="keys"),
    "wait": Spec(args=("seconds",), required=("seconds",), scalar="seconds"),
    "click_text": Spec(args=("text", "button", "double", "occurrence"), required=("text",),
                       scalar="text", nonempty=("text",), ocr=True),
    "click_at": Spec(args=("x", "y", "button", "double"), required=("x", "y")),
    "assert_exists": Spec(locator="required", scalar="name"),
    "assert_missing": Spec(locator="required", scalar="name"),
    "assert_state": Spec(args=STATE_KEYS, any_of=STATE_KEYS, locator="required"),
    "assert_value": Spec(args=_TEXT_MATCH, one_of=("equals", "contains", "regex"), locator="required"),
    "assert_title": Spec(args=_TEXT_MATCH, one_of=("equals", "contains", "regex"), scalar="contains"),
    "assert_ocr": Spec(args=_OCR_ARGS, one_of=("text", "regex"), scalar="text", nonempty=("text",), ocr=True),
    "assert_no_ocr": Spec(args=_OCR_ARGS, one_of=("text", "regex"), scalar="text", nonempty=("text",), ocr=True),
}
_EXECUTOR_LOCATOR = ("click", "double_click", "right_click", "type_text", "select_item", "set_toggle",
                     "expand_collapse", "scroll_into_view", "scroll")
_EXECUTOR_PLAIN = ("press_keys", "wait", "click_text", "click_at")


# -- steps -------------------------------------------------------------------------
@dataclass
class Step:
    n: int
    action: str
    args: dict = field(default_factory=dict)
    locator: Optional[Locator] = None
    region_in: Optional[Locator] = None
    timeout: Optional[float] = None
    reason: Optional[str] = None

    @property
    def needs_ocr(self) -> bool:
        return ACTIONS[self.action].ocr

    def summary(self) -> str:
        bits = [self.action]
        if self.locator is not None:
            bits.append(self.locator.describe())
        for key, value in self.args.items():
            shown = repr(value)
            bits.append(f"{key}={shown if len(shown) <= 60 else shown[:57] + '...'}")
        if self.region_in is not None:
            bits.append(f"in {self.region_in.describe()}")
        return " ".join(bits)

    def report_args(self) -> dict:
        shown = dict(self.args)
        if self.locator is not None:
            shown["element"] = self.locator.describe()
        if self.region_in is not None:
            shown["in"] = self.region_in.describe()
        return shown


def _expand(value: Any, spec: Spec, action: str) -> dict:
    """The mapping a step's value stands for: a bare value is the action's shorthand."""
    if value is None:
        return {}
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, list):
        raise ValueError(f"'{action}' takes a single value or a mapping, not a list")
    if spec.scalar is None:
        raise ValueError(f"'{action}' needs a mapping of arguments, e.g. {action}: {{...}}")
    return {spec.scalar: value}


def _parse_step(n: int, raw: Any) -> Step:
    if not isinstance(raw, dict) or len(raw) != 1:
        extra = ""
        if isinstance(raw, dict) and len(raw) > 1:
            extra = (" A step has exactly one action key: put options such as timeout inside it, "
                     f"e.g. {next(iter(raw))}: {{..., timeout: 10}}")
        raise ValueError("each step must be a one-key mapping like `click: \"Tips\"`." + extra)
    (action, value), = raw.items()
    if action not in ACTIONS:
        close = difflib.get_close_matches(str(action), list(ACTIONS), n=1)
        raise ValueError(f"unknown action {action!r}" + (f" (did you mean {close[0]!r}?)" if close else
                         f"; known: {', '.join(ACTIONS)}"))
    spec = ACTIONS[action]
    args = _expand(value, spec, action)

    step = Step(n=n, action=action)
    for key, check, store in (("timeout", _number(0.01, MAX_TIMEOUT), "timeout"), ("reason", _text, "reason")):
        if key in args:
            try:
                setattr(step, store, check(args.pop(key)))
            except ValueError as e:
                raise ValueError(f"'{key}' {e}")

    if spec.locator != "none":
        try:
            step.locator = take_locator(args, "locator")
        except LocatorError as e:
            raise ValueError(str(e))
    if "in" in spec.args:
        if "in" in args:
            try:
                step.region_in = build_locator(args.pop("in"), "in")
            except LocatorError as e:
                raise ValueError(str(e))
            if "region" in args:
                raise ValueError("give either 'region' or 'in', not both")

    unknown = [k for k in args if k not in spec.args]
    if unknown:
        allowed = list(spec.args) + (list(LOCATOR_KEYS) if spec.locator != "none" else [])
        parts = []
        for key in unknown:
            close = difflib.get_close_matches(str(key), allowed, n=1)
            parts.append(repr(key) + (f" (did you mean {close[0]!r}?)" if close else ""))
        raise ValueError(f"'{action}' does not take {', '.join(parts)}; it accepts: "
                         + (", ".join(allowed) or "(nothing)"))
    if spec.locator == "required" and step.locator is None:
        raise ValueError(f"'{action}' needs an element: give a name, or id / type / name_contains ...")
    for key in spec.required:
        if key not in args:
            raise ValueError(f"'{action}' needs '{key}'")
    if spec.one_of and sum(k in args for k in spec.one_of) != 1:
        raise ValueError(f"'{action}' needs exactly one of: {', '.join(spec.one_of)}")
    if spec.any_of and not any(k in args for k in spec.any_of):
        raise ValueError(f"'{action}' needs at least one of: {', '.join(spec.any_of)}")
    for key, value in list(args.items()):
        try:
            args[key] = ARG_CHECKS[key](value)
        except ValueError as e:
            raise ValueError(f"'{key}' {e}")
    for key in spec.nonempty:
        if key in args and not args[key].strip():
            raise ValueError(f"'{key}' must not be empty")
    if "lenient" in args and "text" not in args:
        raise ValueError("'lenient' applies to 'text'; with 'regex', write the look-alikes into the pattern "
                         "(e.g. [Il1|])")
    step.args = args
    return step


def parse_steps(raw: Any, case: str = "case") -> list[Step]:
    """Validate a ``steps:`` list; raises ScriptError naming the case and step."""
    if not isinstance(raw, list) or not raw:
        raise ScriptError(f"{case}: 'steps' must be a non-empty list")
    steps = []
    for n, item in enumerate(raw, 1):
        try:
            steps.append(_parse_step(n, item))
        except ValueError as e:
            action = next(iter(item)) if isinstance(item, dict) and len(item) == 1 else None
            raise ScriptError(f"{case}, step {n}" + (f" ({action})" if action else "") + f": {e}")
    return steps


def render_steps(steps: list[Step]) -> str:
    return "\n".join(f"{s.n}. {s.summary()}" for s in steps)


# -- matching ----------------------------------------------------------------------
def text_matches(actual: str, args: dict) -> bool:
    """equals / contains / regex comparison of an element's text (whitespace-squashed)."""
    flags = re.IGNORECASE if args.get("ignore_case") else 0
    text = squash(actual)
    if "regex" in args:
        return re.search(args["regex"], text, flags) is not None
    if "equals" in args:
        want = squash(args["equals"])
        return text.casefold() == want.casefold() if flags else text == want
    want = squash(args["contains"])
    return want.casefold() in text.casefold() if flags else want in text


# OCR cannot tell these apart reliably; ``lenient`` folds each group to one character
_LOOKALIKES = str.maketrans({"l": "I", "1": "I", "|": "I", "0": "O", "5": "S", "8": "B", "2": "Z"})


def ocr_matches(blob: str, args: dict) -> bool:
    """Whether OCR output contains the wanted text.

    By default the text must appear as whole words, so "II" does not match inside "III"
    (or "aVL" inside "aVLx"); ``match: contains`` is a plain substring test. ``lenient``
    treats look-alike glyphs as equal (I l 1 | and O 0, S 5, B 8, Z 2) on both sides, so a
    "II" misread as "Il" still matches while "III" still does not.
    """
    flags = re.IGNORECASE if args.get("ignore_case") else 0
    if "regex" in args:
        return re.search(args["regex"], blob, flags) is not None
    wanted = args["text"]
    if args.get("lenient"):
        blob, wanted = blob.translate(_LOOKALIKES), wanted.translate(_LOOKALIKES)
    if args.get("match", "word") == "contains":
        return wanted.casefold() in blob.casefold() if flags else wanted in blob
    tokens = wanted.split()
    pattern = r"(?<!\w)" + r"\s+".join(re.escape(t) for t in tokens) + r"(?!\w)"
    return re.search(pattern, blob, flags) is not None


def _short(text: str, limit: int = 200) -> str:
    text = squash(text)
    return text if len(text) <= limit else text[: limit - 3] + "..."


# -- element state -----------------------------------------------------------------
def _get(obj, attr: str, default=None):
    """An attribute read that survives the COM errors UIA raises for a vanished element."""
    try:
        return getattr(obj, attr)
    except Exception:
        return default


def _read_state(ctrl, key: str, describe: str):
    """The current value of one state of an element, or AssertionFailed if it has no such state."""
    def need(pattern_getter: str, what: str):
        pattern = _pattern(ctrl, pattern_getter)
        if pattern is None:
            raise AssertionFailed(f"{describe} does not expose a {what} state")
        return pattern

    if key == "enabled":
        return bool(_get(ctrl, "IsEnabled", True))
    if key == "focused":
        return bool(_get(ctrl, "HasKeyboardFocus", False))
    if key == "selected":
        return bool(need("GetSelectionItemPattern", "selected").IsSelected)
    if key == "checked":
        state = need("GetTogglePattern", "checked").ToggleState
        return {0: False, 1: True}.get(int(state), "mixed")
    state = int(need("GetExpandCollapsePattern", "expanded/collapsed").ExpandCollapseState)
    if state == 3:
        raise AssertionFailed(f"{describe} is a leaf: it cannot be expanded or collapsed")
    return {0: False, 1: True}.get(state, "partial")


def _read_text(ctrl) -> str:
    """An element's text: its value if it is an editable field, else its name (a label's text)."""
    pattern = _pattern(ctrl, "GetValuePattern")
    if pattern is not None:
        try:
            value = pattern.Value
        except Exception:
            value = None
        if value is not None:
            return value
    return _get(ctrl, "Name", "") or ""


# -- the runner --------------------------------------------------------------------
class ScriptRunner:
    def __init__(self, window, *, executor=None, resolver=None, reporter=None,
                 screenshots: bool = True, verbose: bool = True, step_timeout: float = 5.0,
                 sleep: Callable[[float], None] = time.sleep,
                 clock: Callable[[], float] = time.monotonic, shooter=None):
        self.window = window
        self.executor = executor or ActionExecutor(window)
        self.resolver = resolver or Resolver(window)
        self.reporter = reporter
        self.screenshots = screenshots
        self.verbose = verbose
        self.step_timeout = step_timeout
        self.sleep = sleep
        self.clock = clock
        self.poll = self.resolver.poll
        self._shooter = shooter

    def _log(self, msg: str) -> None:
        if self.verbose:
            print(msg, flush=True)

    # -- running -----------------------------------------------------------------
    def run(self, steps: list[Step]) -> RunResult:
        result = RunResult()
        for step in steps:
            self._log(f"[{step.n}] {step.summary()}")
            status = None
            try:
                outcome, ok = self._execute(step), True
            except AssertionFailed as e:
                outcome, ok, status = f"ASSERT FAIL: {e}", False, "failed"
            except ElementNotFound as e:
                # a missing element fails an assertion about it, but only blocks a click on it
                if step.action.startswith("assert_"):
                    outcome, ok, status = f"ASSERT FAIL: {e}", False, "failed"
                else:
                    outcome, ok, status = f"ERROR: {e}", False, "blocked"
            except Exception as e:  # ActionError, COM errors, a missing OCR engine ...
                outcome, ok, status = f"ERROR: {e}", False, "blocked"
            self._log(f"      -> {outcome}")
            result.steps = step.n
            self._record(step, outcome, ok)
            if not ok:
                result.status = status
                verb = "failed" if status == "failed" else "could not run"
                result.summary = f"Step {step.n} ({step.summary()}) {verb}: {outcome.split(': ', 1)[-1]}"
                return result
        result.status = "passed"
        result.summary = f"All {len(steps)} scripted steps passed (no model used)."
        return result

    def _record(self, step: Step, outcome: str, ok: bool) -> None:
        if self.reporter is None:
            return
        self.reporter.log_step(step.n, step.action, step.report_args(), step.reason, outcome, ok,
                               SimpleNamespace(screenshot=self._screenshot()))

    def _screenshot(self):
        if not self.screenshots:
            return None
        try:
            if self._shooter is None:
                from .perception import Perceiver
                self._shooter = Perceiver(self.window).screenshot
            return self._shooter()
        except Exception:
            return None  # a failed screenshot must not fail the step

    # -- one step ----------------------------------------------------------------
    def _execute(self, step: Step) -> str:
        timeout = step.timeout if step.timeout is not None else self.step_timeout
        action = step.action
        if action in _EXECUTOR_LOCATOR:
            if step.locator is None:  # `scroll` with no element scrolls the window
                return self.executor.dispatch(action, step.args, {})
            ctrl = self.resolver.wait(step.locator, timeout)
            out = self.executor.dispatch(action, {**step.args, "element_id": "e1"}, {"e1": ctrl})
            return re.sub(r"\be1\b", lambda _: step.locator.describe(), out)
        if action in _EXECUTOR_PLAIN:
            return self.executor.dispatch(action, step.args, {})
        return getattr(self, f"_{action}")(step, timeout)

    def _eventually(self, check: Callable[[], tuple], timeout: float) -> tuple:
        """Run ``check() -> (ok, detail)`` until it holds or ``timeout`` passes (at least once)."""
        deadline = self.clock() + timeout
        while True:
            ok, detail = check()
            if ok or self.clock() >= deadline:
                return ok, detail
            self.sleep(self.poll)

    # -- assertions --------------------------------------------------------------
    def _assert_exists(self, step: Step, timeout: float) -> str:
        loc = step.locator
        ok, _ = self._eventually(lambda: (self.resolver.pick(loc, "only") is not None, ""), timeout)
        if not ok:
            raise AssertionFailed(self.resolver.explain(loc, timeout, "only"))
        return f"assert_exists PASS: found {loc.describe()}"

    def _assert_missing(self, step: Step, timeout: float) -> str:
        loc = step.locator
        ok, _ = self._eventually(lambda: (self.resolver.pick(loc, "only") is None, ""), timeout)
        if not ok:
            raise AssertionFailed(f"{loc.describe()} is still visible after {timeout:g}s")
        return f"assert_missing PASS: {loc.describe()} is not on screen"

    def _assert_state(self, step: Step, timeout: float) -> str:
        loc, describe = step.locator, step.locator.describe()
        wanted = {k: v for k, v in step.args.items() if k in STATE_KEYS}

        def check():
            ctrl = self.resolver.pick(loc, "prefer")
            if ctrl is None:
                return False, f"no element matching {describe}"
            wrong = []
            for key, want in wanted.items():
                got = _read_state(ctrl, key, describe)
                if got != want:
                    wrong.append(f"{key} is {got!r}, expected {want!r}")
            return not wrong, "; ".join(wrong)

        ok, detail = self._eventually(check, timeout)
        if not ok:
            raise AssertionFailed(f"{describe}: {detail}")
        return f"assert_state PASS: {describe} " + ", ".join(f"{k}={v!r}" for k, v in wanted.items())

    def _assert_value(self, step: Step, timeout: float) -> str:
        loc, describe = step.locator, step.locator.describe()

        def check():
            ctrl = self.resolver.pick(loc, "prefer")
            if ctrl is None:
                return False, f"no element matching {describe}"
            text = _read_text(ctrl)
            return text_matches(text, step.args), text

        ok, text = self._eventually(check, timeout)
        if not ok:
            raise AssertionFailed(f"{describe} reads {_short(text)!r}, which does not satisfy {self._expect(step)}")
        return f"assert_value PASS: {describe} reads {_short(text)!r}"

    def _assert_title(self, step: Step, timeout: float) -> str:
        def check():
            title = _get(self.window, "Name", "") or ""
            return text_matches(title, step.args), title

        ok, title = self._eventually(check, timeout)
        if not ok:
            raise AssertionFailed(f"window title is {title!r}, which does not satisfy {self._expect(step)}")
        return f"assert_title PASS: window title is {title!r}"

    @staticmethod
    def _expect(step: Step) -> str:
        key = next(k for k in ("equals", "contains", "regex") if k in step.args)
        return f"{key} {step.args[key]!r}"

    def _assert_ocr(self, step: Step, timeout: float) -> str:
        return self._ocr_assertion(step, timeout, present=True)

    def _assert_no_ocr(self, step: Step, timeout: float) -> str:
        return self._ocr_assertion(step, timeout, present=False)

    def _ocr_assertion(self, step: Step, timeout: float, present: bool) -> str:
        a = step.args
        bounded = "region" in a or step.region_in is not None
        scales = a.get("scale", list(DEFAULT_REGION_SCALES) if bounded else [1])
        options = dict(psm=a.get("psm", DEFAULT_REGION_PSM if bounded else None),
                       min_conf=a.get("min_conf", 30), invert=a.get("invert", "auto"))
        area = self.resolver.wait(step.region_in, timeout, "prefer") if step.region_in is not None else None

        def check():
            bbox = self._bbox(a, area)
            words = [w for scale in scales for w in self.executor.visual.read_text(bbox, scale=scale, **options)]
            blob = " ".join(words)
            hit = ocr_matches(blob, a)
            return (hit if present else not hit), blob

        ok, blob = self._eventually(check, timeout)
        target = repr(a["text"]) if "text" in a else f"/{a['regex']}/"
        where = (f" in {step.region_in.describe()}" if step.region_in is not None
                 else " in the given region" if "region" in a else "")
        read = f"OCR read: {_short(blob) or '(nothing)'}"
        if not ok:
            raise AssertionFailed(f"{target} {'not found' if present else 'still present'}{where}. {read}")
        return f"{step.action} PASS: {target} {'found' if present else 'absent'}{where}. {read}"

    def _bbox(self, args: dict, area) -> tuple:
        """Screen rectangle to OCR: an element's, a fraction of the window, or the whole window."""
        if area is not None:
            r = area.BoundingRectangle
            return (r.left, r.top, r.right, r.bottom)
        w = self.window.BoundingRectangle
        left, top, width, height = w.left, w.top, w.right - w.left, w.bottom - w.top
        x0, y0, x1, y1 = args.get("region", (0, 0, 1, 1))
        return (left + x0 * width, top + y0 * height, left + x1 * width, top + y1 * height)
