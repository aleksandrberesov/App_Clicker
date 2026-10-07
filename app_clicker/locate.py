"""Find UI elements by name / automation id / type, for scripted (model-free) runs.

A model picks elements by the ids in the tree it was shown. A script has to *name* them,
and ids change on every observation, so scripts address elements by what they are:

    name: "Применить"            exact name (whitespace/case/Unicode-normalized)
    name_contains / name_regex   partial name
    id:   "ApplyButton"          UIA AutomationId (most stable when the app sets it)
    type: Button                 control type
    class: "TextBox"             window class
    index: 2                     2nd match (1-based) when the rest is ambiguous
    within: {name: "Tips"}       only search inside another element

The search walks the live UIA tree of the app window, so it is not limited to the
(truncated) tree the model sees, and it polls until the element appears.
"""

from __future__ import annotations

import difflib
import re
import time
import unicodedata
from dataclasses import dataclass, field
from typing import Any, Callable, Iterator, Optional

LOCATOR_KEYS = ("name", "name_contains", "name_regex", "id", "type", "class", "index", "within")
_SELECTING = ("name", "name_contains", "name_regex", "id", "type", "class")

MAX_WALK_DEPTH = 40
MAX_WALK_NODES = 5000
POLL_INTERVAL = 0.3

_END = object()


class LocatorError(ValueError):
    """A locator in a script is malformed."""


class ElementNotFound(Exception):
    """No matching element appeared in time; the message says what is on screen instead."""


def _safe(getter, default=None):
    try:
        return getter()
    except Exception:
        return default


def squash(text) -> str:
    """UI text with Unicode normalized and every kind of whitespace (NBSP, newlines) collapsed."""
    return " ".join(unicodedata.normalize("NFC", text or "").split())


def norm(text) -> str:
    """Comparison form of UI text: squashed and case-folded."""
    return squash(text).casefold()


def type_key(name) -> str:
    """'ButtonControl' / 'Button' / 'button' -> 'button'."""
    n = (name or "").strip()
    if n.lower().endswith("control"):
        n = n[: -len("control")]
    return n.casefold()


@dataclass
class Locator:
    name: Optional[str] = None
    name_contains: Optional[str] = None
    name_regex: Optional[str] = None
    id: Optional[str] = None
    type: Optional[str] = None
    cls: Optional[str] = None
    index: int = 1
    within: Optional["Locator"] = None
    _rx: Any = field(default=None, init=False, repr=False, compare=False)

    def __post_init__(self):
        if self.name_regex is not None:
            try:
                self._rx = re.compile(self.name_regex, re.IGNORECASE)
            except re.error as e:
                raise LocatorError(f"name_regex {self.name_regex!r} is not a valid regex ({e})")

    def describe(self) -> str:
        bits = []
        if self.type:
            bits.append(self.type)
        if self.name is not None:
            bits.append(f'"{self.name}"')
        if self.name_contains is not None:
            bits.append(f'name containing "{self.name_contains}"')
        if self.name_regex is not None:
            bits.append(f"name matching /{self.name_regex}/")
        if self.id is not None:
            bits.append(f"#{self.id}")
        if self.cls is not None:
            bits.append(f"class {self.cls}")
        text = " ".join(bits) or "any element"
        if self.index != 1:
            text += f" (match #{self.index})"
        if self.within is not None:
            text += f" inside {self.within.describe()}"
        return text

    def matches(self, ctrl) -> bool:
        if self.name is not None or self.name_contains is not None or self._rx is not None:
            raw = _safe(lambda: ctrl.Name, "") or ""
            if self.name is not None and norm(raw) != norm(self.name):
                return False
            if self.name_contains is not None and norm(self.name_contains) not in norm(raw):
                return False
            if self._rx is not None and not self._rx.search(squash(raw)):
                return False
        if self.id is not None and (_safe(lambda: ctrl.AutomationId, "") or "") != self.id:
            return False
        if self.type is not None and type_key(_safe(lambda: ctrl.ControlTypeName, "")) != type_key(self.type):
            return False
        if self.cls is not None and (_safe(lambda: ctrl.ClassName, "") or "") != self.cls:
            return False
        return True


def build_locator(spec, where: str = "locator") -> Locator:
    """A Locator from a YAML value: a bare string means an exact name."""
    if isinstance(spec, str):
        spec = {"name": spec}
    if not isinstance(spec, dict):
        raise LocatorError(f"{where} must be a name or a mapping with {', '.join(LOCATOR_KEYS)}")
    unknown = [k for k in spec if k not in LOCATOR_KEYS]
    if unknown:
        raise LocatorError(f"{where} has unknown key(s) {', '.join(map(repr, unknown))}; "
                           f"allowed: {', '.join(LOCATOR_KEYS)}")
    if not any(k in spec for k in _SELECTING):
        raise LocatorError(f"{where} needs at least one of: {', '.join(_SELECTING)}")

    fields: dict = {}
    for key in ("name", "name_contains", "name_regex", "id", "type", "class"):
        if key in spec:
            value = spec[key]
            if not isinstance(value, str) or not value.strip():
                raise LocatorError(f"{where}: '{key}' must be a non-empty string")
            fields["cls" if key == "class" else key] = value
    if "index" in spec:
        index = spec["index"]
        if isinstance(index, bool) or not isinstance(index, int) or index < 1:
            raise LocatorError(f"{where}: 'index' must be a whole number >= 1 (1 = first match)")
        fields["index"] = index
    if "within" in spec:
        fields["within"] = build_locator(spec["within"], f"{where}.within")
    return Locator(**fields)


def take_locator(args: dict, where: str = "locator") -> Optional[Locator]:
    """Remove the locator keys from ``args`` and build them into a Locator (None if there are none)."""
    found = {k: args.pop(k) for k in LOCATOR_KEYS if k in args}
    return build_locator(found, where) if found else None


def walk(root, max_depth: int = MAX_WALK_DEPTH, max_nodes: int = MAX_WALK_NODES) -> Iterator:
    """Descendants of ``root`` in document order (depth-first), tolerating COM errors."""
    count = 0
    stack = [(iter(_safe(root.GetChildren, []) or []), 1)]
    while stack:
        children, depth = stack[-1]
        child = next(children, _END)
        if child is _END:
            stack.pop()
            continue
        count += 1
        if count > max_nodes:
            return
        yield child
        if depth < max_depth:
            stack.append((iter(_safe(child.GetChildren, []) or []), depth + 1))


def is_offscreen(ctrl) -> bool:
    return bool(_safe(lambda: ctrl.IsOffscreen, False))


class Resolver:
    """Finds elements under an application window, waiting for them to appear.

    ``visible`` modes: "prefer" picks on-screen matches when there are any (a collapsed
    panel's twin "Tips" header must not win), falling back to off-screen ones; "only"
    ignores off-screen elements (what an assertion about the screen needs).
    """

    def __init__(self, window, poll: float = POLL_INTERVAL,
                 sleep: Callable[[float], None] = time.sleep,
                 clock: Callable[[], float] = time.monotonic):
        self.window = window
        self.poll = poll
        self.sleep = sleep
        self.clock = clock

    def _root(self, loc: Locator):
        return self.window if loc.within is None else self.pick(loc.within, "prefer")

    def pick(self, loc: Locator, visible: str = "prefer"):
        """One look at the screen: the matching element, or None."""
        root = self._root(loc)
        if root is None:
            return None
        found = [c for c in walk(root) if loc.matches(c)]
        shown = [c for c in found if not is_offscreen(c)]
        pool = shown if (visible == "only" or shown) else found
        return pool[loc.index - 1] if len(pool) >= loc.index else None

    def wait(self, loc: Locator, timeout: float, visible: str = "prefer"):
        """The matching element, polling until ``timeout``; raises ElementNotFound."""
        deadline = self.clock() + timeout
        while True:
            ctrl = self.pick(loc, visible)
            if ctrl is not None:
                return ctrl
            if self.clock() >= deadline:
                raise ElementNotFound(self.explain(loc, timeout, visible))
            self.sleep(self.poll)

    def explain(self, loc: Locator, timeout: float, visible: str = "prefer") -> str:
        """Why nothing matched, with the closest names actually on screen."""
        message = f"no {'visible ' if visible == 'only' else ''}element matching {loc.describe()} after {timeout:g}s"
        hints = self._similar(loc)
        if hints:
            message += ". Similar on screen: " + "; ".join(hints)
        return message

    def _similar(self, loc: Locator, limit: int = 5) -> list[str]:
        by_id = bool(loc.id) and not (loc.name or loc.name_contains)
        target = loc.id if by_id else (loc.name or loc.name_contains)
        if not target:
            return []
        labels: dict = {}
        for ctrl in walk(self._root(loc) or self.window, max_nodes=3000):
            ctype = _safe(lambda: ctrl.ControlTypeName, "") or ""
            if loc.type is not None and type_key(ctype) != type_key(loc.type):
                continue
            kind = ctype.replace("Control", "") or "?"
            if by_id:
                text = _safe(lambda: ctrl.AutomationId, "") or ""
                shown = f"{kind} #{text}"
            else:
                text = squash(_safe(lambda: ctrl.Name, "") or "")
                shown = f'{kind} "{text}"'
            if text:
                labels.setdefault(norm(text), shown)
        close = difflib.get_close_matches(norm(target), list(labels), n=limit, cutoff=0.5)
        return [labels[c] for c in close]
