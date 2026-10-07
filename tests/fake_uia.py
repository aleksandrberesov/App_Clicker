"""A fake UI Automation tree the real ActionExecutor / Resolver can drive without a desktop."""

from types import SimpleNamespace as NS


class Rect:
    def __init__(self, left, top, right, bottom):
        self.left, self.top, self.right, self.bottom = left, top, right, bottom

    def width(self):
        return self.right - self.left

    def height(self):
        return self.bottom - self.top


class FakeControl:
    """One control. Optional patterns exist only when asked for, like the real per-type getters."""

    def __init__(self, name="", kind="Button", aid="", cls="", children=(), offscreen=False,
                 enabled=True, rect=(0, 0, 100, 30), value=None, selected=None, toggle=None,
                 expand=None, focused=False, on_click=None):
        self.Name = name
        self.ControlTypeName = kind + "Control"
        self.AutomationId = aid
        self.ClassName = cls
        self.IsOffscreen = offscreen
        self.IsEnabled = enabled
        self.HasKeyboardFocus = focused
        self.BoundingRectangle = Rect(*rect)
        self.children = list(children)
        self.clicks = 0
        self._on_click = on_click
        if value is not None:
            self.value = value
            self.GetValuePattern = lambda: NS(Value=self.value, SetValue=self._set_value)
        if selected is not None:
            self.selected = selected
            self.GetSelectionItemPattern = lambda: NS(IsSelected=self.selected, Select=self._select)
        if toggle is not None:
            self.toggle = toggle
            self.GetTogglePattern = lambda: NS(ToggleState=self.toggle, Toggle=self._toggle)
        if expand is not None:
            self.expand = expand
            self.GetExpandCollapsePattern = lambda: NS(ExpandCollapseState=self.expand)

    def GetChildren(self):
        return list(self.children)

    def SetFocus(self):
        return True

    def Click(self, waitTime=0):
        self.clicks += 1
        if self._on_click:
            self._on_click(self)

    def _set_value(self, text):
        self.value = text
        return True

    def _select(self):
        self.selected = True
        return True

    def _toggle(self):
        self.toggle = 0 if self.toggle == 1 else 1
        return True


def window(*children, title="Demo App", rect=(0, 0, 1000, 600)):
    return FakeControl(title, "Window", children=children, rect=rect)


def demo_app():
    """Apply button -> status label changes. Returns (window, apply, status, name, show)."""
    status = FakeControl("Статус: ожидание", "Text", aid="Status")
    apply = FakeControl("Применить", aid="Apply",
                        on_click=lambda _: setattr(status, "Name", "Статус: применено"))
    name = FakeControl("Имя", "Edit", aid="Name", value="")
    show = FakeControl("Показать отведение", "CheckBox", toggle=0)
    return window(apply, status, name, show), apply, status, name, show
