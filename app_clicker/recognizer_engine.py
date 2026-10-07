"""Screen_Recognizer-based perception and action, for the 'recognizer' engine.

The LLM acts strictly as a DECISION ENGINE: it never sees a screenshot and never
handles pixel coordinates. Local perception (Screen_Recognizer: OCR + OpenCV)
builds a full report of the window — every element with its container, row,
label, shape, colors and screen position. The model reads a coordinate-free
outline of that report and picks an integer id; this executor clicks the
element's exact screen position from the report. Details the outline leaves
out are available on demand through `inspect_element`.
"""

from __future__ import annotations

import io
import time
from dataclasses import dataclass

import uiautomation as auto
from PIL import ImageGrab

from .actions import ActionError, _click_screen, missing_arg_error


def _load_recognizer(ocr_backend: str, include_containers: bool = False):
    try:
        from screen_recognizer import ScreenRecognizer
    except ImportError as e:
        raise RuntimeError(
            "The 'recognizer' engine needs the Screen_Recognizer package. Install it:\n"
            "  pip install -e E:\\Automation_projects\\Screen_Recognizer --no-deps"
        ) from e
    return ScreenRecognizer(ocr_backend=ocr_backend, include_containers=include_containers)


def _clip(text: str, limit: int = 70) -> str:
    t = (text or "").strip().replace("\n", " ")
    return t[:limit - 3] + "..." if len(t) > limit else t


def _find(report, element_id):
    try:
        el = report.find_by_id(int(element_id))
    except (TypeError, ValueError):
        el = None
    if el is None:
        avail = ", ".join(str(e.id) for e in report.elements[:50])
        raise ActionError(
            f"No element with id {element_id} on screen. Available ids: {avail or '(none)'}"
        )
    return el


def _element_line(el, by_id: dict) -> str:
    """One element as `[id] type "text" label="..."` — never coordinates."""
    parts = [f"[{el.id}] {el.type.value}"]
    if el.type.value == "container":
        title = el.attributes.get("title")
        if title:
            parts.append(f'"{_clip(title)}"')
        return " ".join(parts)

    if el.text_fragments:
        parts.append(f'"{_clip(el.text)}"')
    elif el.type.value == "input":
        parts.append("(no text detected)")

    # Without OCR text of its own, an element's text was borrowed from a nearby label
    label = el.text if el.text and not el.text_fragments else None
    if label is None and el.relations.labelled_by is not None:
        label = by_id[el.relations.labelled_by].text
    if label:
        parts.append(f'label="{_clip(label, 40)}"')
    return " ".join(parts)


def format_report(report) -> str:
    """Coordinate-free outline of the screen for the LLM.

    Elements are indented under the container (card, panel, bar) that encloses
    them, and elements sharing a visual row are joined with " | ".
    """
    by_id = {el.id: el for el in report.elements}
    lines = [f"On-screen elements ({len(report.elements)}):"]

    def walk(ids: list[int], depth: int) -> None:
        indent = "  " * depth
        rows: dict[int, list] = {}
        for eid in ids:
            rows.setdefault(by_id[eid].layout.row, []).append(by_id[eid])
        for row in sorted(rows):
            pending: list[str] = []
            for el in sorted(rows[row], key=lambda e: e.layout.column):
                if not el.hierarchy.children_ids:
                    pending.append(_element_line(el, by_id))
                    continue
                # A group gets its own line so its children can nest under it
                if pending:
                    lines.append(indent + " | ".join(pending))
                    pending = []
                lines.append(indent + _element_line(el, by_id))
                walk(el.hierarchy.children_ids, depth + 1)
            if pending:
                lines.append(indent + " | ".join(pending))

    walk([node.id for node in report.tree], 0)
    return "\n".join(lines)


def describe_element(report, element_id) -> str:
    """Everything the report knows about one element, minus coordinates (for `inspect_element`)."""
    el = _find(report, element_id)
    by_id = {e.id: e for e in report.elements}

    def ref(eid: int) -> str:
        # Same wording as the outline: a container's title, the element's own OCR text, or a borrowed label
        other = by_id[eid]
        out = f"[{eid}] {other.type.value}"
        if other.type.value == "container":
            title = other.attributes.get("title", "")
            return out + (f' "{_clip(title, 30)}"' if title else "")
        if other.text_fragments:
            return out + f' "{_clip(other.text, 30)}"'
        return out + (f' label="{_clip(other.text, 30)}"' if other.text else "")

    shape, style, hierarchy = el.shape, el.style, el.hierarchy
    colors = [f"background {style.background_color}"]
    colors.append(f"border {style.border_color}" if style.border_color else "no visible border")
    if style.text_color:
        colors.append(f"text {style.text_color} (contrast {style.contrast_ratio}:1)")

    lines = [
        _element_line(el, by_id),
        f"  shape: {shape.kind.replace('_', ' ')}, {el.geometry.width}x{el.geometry.height} px, "
        f"{el.layout.region} of the window" + (", cut off at the window edge" if el.geometry.touches_edge else ""),
        f"  colors: {', '.join(colors)}; brightness {style.brightness:.2f}",
    ]
    if el.text_fragments:
        lines.append("  text read: " + "; ".join(
            f'"{f.text}" ({f.confidence:.0%} confident)' for f in el.text_fragments
        ))
    location = f"inside {ref(hierarchy.parent_id)}" if hierarchy.parent_id is not None else "top level"
    lines.append(f"  position: {location}, row {el.layout.row}, column {el.layout.column}")
    if hierarchy.children_ids:
        shown = ", ".join(ref(c) for c in hierarchy.children_ids[:20])
        more = f" and {len(hierarchy.children_ids) - 20} more" if len(hierarchy.children_ids) > 20 else ""
        lines.append(f"  contains: {shown}{more}")
    neighbors = [
        f"{side} {ref(eid)}" for side, eid in el.relations.neighbors.model_dump().items() if eid is not None
    ]
    if neighbors:
        lines.append("  neighbors: " + ", ".join(neighbors))
    if el.relations.labelled_by is not None:
        lines.append(f"  labelled by: {ref(el.relations.labelled_by)}")
    if el.relations.label_for:
        lines.append("  labels: " + ", ".join(ref(i) for i in el.relations.label_for))
    lines.append(f"  detected by: {' + '.join(el.provenance.sources)} ({el.confidence:.0%} confidence)")
    return "\n".join(lines)


@dataclass
class RecognizerObservation:
    report: object           # screen_recognizer ScreenReport (elements carry absolute screen positions)
    text: str                # coordinate-free outline for the LLM
    screenshot: bytes | None  # Set-of-Marks annotated PNG (for the report only)
    window_title: str


class RecognizerPerceiver:
    def __init__(self, window, ocr_backend: str = "tesseract", annotate: bool = True,
                 include_containers: bool = False):
        self.window = window
        self.annotate = annotate
        self.recognizer = _load_recognizer(ocr_backend, include_containers)

    def _focus(self) -> None:
        for name in ("SetActive", "SetFocus"):
            fn = getattr(self.window, name, None)
            if fn is None:
                continue
            try:
                fn()
                return
            except Exception:
                continue

    def observe(self) -> RecognizerObservation:
        self._focus()
        time.sleep(0.2)
        rect = self.window.BoundingRectangle
        img = ImageGrab.grab(
            bbox=(rect.left, rect.top, rect.right, rect.bottom), all_screens=True
        ).convert("RGB")

        title = ""
        try:
            title = self.window.Name or ""
        except Exception:
            pass
        return self.observe_image(img, (rect.left, rect.top), title)

    def observe_image(self, img, offset: tuple, window_title: str = "") -> RecognizerObservation:
        """Recognize an already-captured window image whose top-left sits at screen `offset`."""
        from screen_recognizer import SourceInfo
        from screen_recognizer.report import Point

        recognition = self.recognizer.analyze(img)
        report = self.recognizer.build_report(
            recognition,
            source=SourceInfo(kind="window", window_title=window_title,
                              offset=Point(x=offset[0], y=offset[1])),
        )

        shot = None
        if self.annotate:
            try:
                ann = self.recognizer.annotate(report.to_screen_state(), image=img, only_interactive=False)
                buf = io.BytesIO()
                ann.convert("RGB").save(buf, format="PNG")
                shot = buf.getvalue()
            except Exception:
                shot = None

        return RecognizerObservation(report, format_report(report), shot, window_title)


class RecognizerExecutor:
    def __init__(self, window, default_wait: float = 0.5):
        self.window = window
        self.default_wait = default_wait

    def _focus(self) -> None:
        for name in ("SetActive", "SetFocus"):
            fn = getattr(self.window, name, None)
            if fn is None:
                continue
            try:
                fn()
                return
            except Exception:
                continue

    def _resolve(self, report, element_id):
        el = _find(report, element_id)
        if el.type.value == "container":
            inside = ", ".join(str(i) for i in el.hierarchy.children_ids[:20]) or "(none detected)"
            raise ActionError(
                f"[{el.id}] is a container that groups other elements; act on an element inside it "
                f"instead. Ids inside: {inside}"
            )
        return el, el.screen_center.x, el.screen_center.y

    def click(self, report, element_id, button: str = "left", double: bool = False) -> str:
        el, ax, ay = self._resolve(report, element_id)
        self._focus()
        _click_screen(ax, ay, button, double)
        time.sleep(self.default_wait)
        verb = "Right-clicked" if button == "right" else ("Double-clicked" if double else "Clicked")
        return f'{verb} [{element_id}] {el.type.value} "{el.text}" at screen ({ax},{ay}).'

    def type_text(self, report, element_id, text: str, clear_first: bool = True) -> str:
        el, ax, ay = self._resolve(report, element_id)
        self._focus()
        _click_screen(ax, ay)  # focus the field
        time.sleep(0.2)
        if clear_first:
            auto.SendKeys("{Ctrl}a{Delete}", waitTime=0)
        auto.SendKeys(text, waitTime=self.default_wait)
        return f'Typed into [{element_id}] {el.type.value}: "{text}".'

    def press_keys(self, keys: str) -> str:
        self._focus()
        auto.SendKeys(keys, waitTime=self.default_wait)
        return f"Pressed keys: {keys}"

    def scroll(self, direction: str = "down", amount: int = 3) -> str:
        self._focus()
        amount = max(1, min(int(amount), 15))
        up = str(direction).lower() == "up"
        fn = getattr(self.window, "WheelUp" if up else "WheelDown", None)
        if fn is None:
            raise ActionError("This window does not support wheel scrolling.")
        try:
            fn(wheelTimes=amount, waitTime=self.default_wait)
        except Exception as e:
            raise ActionError(f"Scroll failed: {e}")
        return f"Scrolled {direction} {amount}."

    def wait(self, seconds: float = 1.0) -> str:
        seconds = min(max(float(seconds), 0.0), 15.0)
        time.sleep(seconds)
        return f"Waited {seconds:g}s."

    def dispatch(self, name: str, inp: dict, report) -> str:
        inp = dict(inp or {})
        inp.pop("reason", None)
        try:
            if name == "click":
                return self.click(report, inp["element_id"])
            if name == "double_click":
                return self.click(report, inp["element_id"], double=True)
            if name == "right_click":
                return self.click(report, inp["element_id"], button="right")
            if name == "type_text":
                return self.type_text(
                    report, inp["element_id"], inp.get("text", ""),
                    inp.get("clear_first", True),
                )
            if name == "press_keys":
                return self.press_keys(inp["keys"])
            if name == "scroll":
                return self.scroll(inp.get("direction", "down"), inp.get("amount", 3))
            if name == "wait":
                return self.wait(inp.get("seconds", 1))
        except KeyError as e:
            raise missing_arg_error(name, str(e), inp, None)
        raise ActionError(f"Unknown action '{name}'.")


# --- tools + system prompt for the recognizer decision engine ----------------
_REASON = {"type": "string", "description": "One short sentence: why this action moves the task forward."}
_EID = {"type": "integer", "description": "Element id from the current on-screen outline."}

RECOGNIZER_TOOLS = [
    {"name": "click", "description": "Left-click an element by its id.",
     "input_schema": {"type": "object", "properties": {"element_id": _EID, "reason": _REASON},
                      "required": ["element_id", "reason"]}},
    {"name": "double_click", "description": "Double-click an element by its id.",
     "input_schema": {"type": "object", "properties": {"element_id": _EID, "reason": _REASON},
                      "required": ["element_id", "reason"]}},
    {"name": "right_click", "description": "Right-click an element by its id (context menu).",
     "input_schema": {"type": "object", "properties": {"element_id": _EID, "reason": _REASON},
                      "required": ["element_id", "reason"]}},
    {"name": "type_text", "description": "Click an input element by id, then type text into it.",
     "input_schema": {"type": "object", "properties": {
         "element_id": _EID, "text": {"type": "string", "description": "Text to enter."},
         "clear_first": {"type": "boolean", "description": "Clear the field first (default true)."},
         "reason": _REASON}, "required": ["element_id", "text", "reason"]}},
    {"name": "press_keys", "description": "Send keys to the focused control, e.g. '{Enter}', '{Tab}', '{Ctrl}s'.",
     "input_schema": {"type": "object", "properties": {
         "keys": {"type": "string"}, "reason": _REASON}, "required": ["keys", "reason"]}},
    {"name": "scroll", "description": "Scroll the window to reveal elements not currently detected.",
     "input_schema": {"type": "object", "properties": {
         "direction": {"type": "string", "enum": ["down", "up"]},
         "amount": {"type": "integer", "description": "Wheel notches (default 3)."},
         "reason": _REASON}, "required": ["direction", "reason"]}},
    {"name": "wait", "description": "Pause for the UI to settle (loading/animation).",
     "input_schema": {"type": "object", "properties": {
         "seconds": {"type": "number"}, "reason": _REASON}, "required": ["seconds", "reason"]}},
    {"name": "inspect_element",
     "description": "Show what the outline leaves out for one element: shape, size, colors, border, "
                    "OCR confidence, container contents, neighbors and labels. Does not act on or re-read "
                    "the screen.",
     "input_schema": {"type": "object", "properties": {"element_id": _EID, "reason": _REASON},
                      "required": ["element_id", "reason"]}},
    {"name": "note", "description": "Record a verification/observation in the report (no UI action).",
     "input_schema": {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]}},
    {"name": "finish", "description": "End the task with a verdict. Call exactly once at the end.",
     "input_schema": {"type": "object", "properties": {
         "status": {"type": "string", "enum": ["passed", "failed", "blocked"]},
         "summary": {"type": "string"}}, "required": ["status", "summary"]}},
]

RECOGNIZER_SYSTEM = """\
You are an experienced manual QA tester driving a native Windows desktop application.

Each turn you are given the current screen as an outline of detected ELEMENTS:
  [id] type "visible text" label="nearby label"
  - id is an integer; type is one of button / input / text / icon / checkbox / container / unknown.
  - Indented lines are inside the container above them (a card, panel, toolbar or bar); the text
    after a container is its title. Elements joined by " | " sit side by side in one row.
  - label="..." is nearby text describing an input, checkbox or icon. An input marked
    (no text detected) looks empty.
  - `unknown` is a drawn shape whose text could not be read (often an icon or an unread label).

You do NOT see a screenshot and you never work with pixel coordinates. You choose an
element by its id and the tool clicks its exact location for you — there is no coordinate
guessing.

Rules:
  - Call EXACTLY ONE tool per turn, with a short `reason` where the tool asks for one.
  - Reference elements by their id from the MOST RECENT outline (ids are re-assigned each turn).
  - Act on elements, not containers: a container only groups what is inside it.
  - The local detector is imperfect: text can be split across lines, misread, or an element
    may be missing. If what you need isn't listed, `scroll` to reveal more, `press_keys` to
    navigate, or pick the closest sensible element.
  - To enter text, use `type_text` on an input's id (it clicks the field first, then types).
  - Use `inspect_element` when the outline isn't enough: to check visual state (an error shown
    in red, a highlighted or selected item, a faint disabled-looking button) or to tell similar
    elements apart. It does not change or re-read the screen, so ids stay valid.
  - After acting, check the new outline against what you expected. Record each
    verification with `note` before moving on.
  - Do NOT perform destructive or irreversible actions unless the task requires them.
  - Keep going until the task is done or you are blocked, then `finish` with:
      passed — task done and checks held, failed — the app misbehaved (describe it),
      blocked — you could not proceed (say why).
"""
