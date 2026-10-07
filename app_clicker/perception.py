"""Perception layer: turn the live app window into something the model can read.

Produces an :class:`Observation` containing
  * a compact text dump of the UI Automation tree, with each control tagged by a
    stable-per-observation id (``e1``, ``e2``, ...),
  * a mapping ``id -> uiautomation control`` used by the action executor, and
  * a PNG screenshot of the window (native apps often have incomplete trees, so
    the model gets pixels too).

The tree is rebuilt from scratch on every observation, so ids always refer to
the current screen and never go stale across steps.
"""

from __future__ import annotations

import io
from dataclasses import dataclass
from typing import Any, Optional

import uiautomation as auto
from PIL import ImageGrab

# Control types that usually carry a text value worth surfacing to the model.
_VALUE_TYPES = {
    "EditControl",
    "ComboBoxControl",
    "DocumentControl",
    "SpinnerControl",
    "HyperlinkControl",
}

# Control types whose selected/checked state is meaningful.
_STATEFUL_TYPES = {
    "ListItemControl",
    "TabItemControl",
    "TreeItemControl",
    "MenuItemControl",
    "RadioButtonControl",
    "CheckBoxControl",
}


def _safe(getter, default=None):
    """Call ``getter`` and swallow the COM errors UIA loves to throw."""
    try:
        return getter()
    except Exception:
        return default


def _pattern(ctrl, getter_name: str):
    """Fetch a UIA pattern, tolerating control types that lack its getter."""
    getter = getattr(ctrl, getter_name, None)
    if getter is None:
        return None
    try:
        return getter()
    except Exception:
        return None


@dataclass
class Observation:
    window_title: str
    tree_text: str
    elements: dict[str, Any]          # id -> uiautomation control
    screenshot: Optional[bytes] = None  # PNG bytes, or None if disabled/failed
    bbox: Optional[tuple] = None        # (left, top, right, bottom) captured
    report_json: Optional[str] = None   # full Screen_Recognizer report (recognizer engine only)


class Perceiver:
    def __init__(
        self,
        window,
        max_nodes: int = 250,
        max_depth: int = 30,
        screenshots: bool = True,
        max_image_edge: int = 1568,
    ):
        self.window = window
        self.max_nodes = max_nodes
        self.max_depth = max_depth
        self.screenshots = screenshots
        self.max_image_edge = max_image_edge

    # -- public API --------------------------------------------------------
    def screenshot(self) -> Optional[bytes]:
        """Just the window screenshot, without walking the UI tree (scripted runs don't need it)."""
        return self._capture()[0]

    def observe(self) -> Observation:
        elements: dict[str, Any] = {}
        lines: list[str] = []
        counter = [0]

        def walk(ctrl, depth: int) -> None:
            if counter[0] >= self.max_nodes or depth > self.max_depth:
                return
            for child in _safe(ctrl.GetChildren, []) or []:
                if counter[0] >= self.max_nodes:
                    lines.append("  " * depth + "... (node limit reached)")
                    break
                counter[0] += 1
                eid = f"e{counter[0]}"
                elements[eid] = child
                lines.append(self._describe(eid, child, depth))
                walk(child, depth + 1)

        # Include the window node itself as e1.
        counter[0] += 1
        wid = f"e{counter[0]}"
        elements[wid] = self.window
        lines.append(self._describe(wid, self.window, 0))
        walk(self.window, 1)

        shot, bbox = (None, None)
        if self.screenshots:
            shot, bbox = self._capture()

        return Observation(
            window_title=_safe(lambda: self.window.Name, "") or "",
            tree_text="\n".join(lines),
            elements=elements,
            screenshot=shot,
            bbox=bbox,
        )

    # -- helpers -----------------------------------------------------------
    def _describe(self, eid: str, ctrl, depth: int) -> str:
        indent = "  " * depth
        ctype_full = _safe(lambda: ctrl.ControlTypeName, "") or ""
        ctype = ctype_full.replace("Control", "") or "?"
        name = (_safe(lambda: ctrl.Name, "") or "").strip().replace("\n", " ")
        if len(name) > 60:
            name = name[:57] + "..."
        aid = _safe(lambda: ctrl.AutomationId, "") or ""

        parts = [f"[{eid}] {ctype}"]
        if name:
            parts.append(f'"{name}"')
        if aid:
            parts.append(f"#{aid}")

        if ctype_full in _VALUE_TYPES:
            vp = _pattern(ctrl, "GetValuePattern")
            val = _safe(lambda: vp.Value) if vp else None
            if val:
                v = val.replace("\n", " ")
                if len(v) > 40:
                    v = v[:37] + "..."
                parts.append(f'value="{v}"')

        states: list[str] = []
        if _safe(lambda: ctrl.IsEnabled, True) is False:
            states.append("disabled")
        if _safe(lambda: ctrl.IsOffscreen, False):
            states.append("offscreen")
        if ctype_full in _STATEFUL_TYPES:
            sp = _pattern(ctrl, "GetSelectionItemPattern")
            if sp and _safe(lambda: sp.IsSelected):
                states.append("selected")
            tp = _pattern(ctrl, "GetTogglePattern")
            if tp:
                ts = _safe(lambda: tp.ToggleState)
                if ts == 1:
                    states.append("checked")
                elif ts == 0:
                    states.append("unchecked")
        if states:
            parts.append("(" + ", ".join(states) + ")")

        return indent + " ".join(parts)

    def _capture(self):
        bbox = None
        try:
            rect = self.window.BoundingRectangle
            bbox = (rect.left, rect.top, rect.right, rect.bottom)
            img = ImageGrab.grab(bbox=bbox, all_screens=True)
        except Exception:
            try:
                img = ImageGrab.grab(all_screens=True)
            except Exception:
                return None, None
        w, h = img.size
        longest = max(w, h)
        if longest > self.max_image_edge:
            scale = self.max_image_edge / longest
            img = img.resize((max(1, int(w * scale)), max(1, int(h * scale))))
        buf = io.BytesIO()
        img.convert("RGB").save(buf, format="PNG")
        return buf.getvalue(), bbox
