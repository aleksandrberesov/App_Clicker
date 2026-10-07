"""Web action executor: carry out the model's chosen action through Playwright.

Element ids resolve to locators on the ``data-ac-id`` attribute the perceiver
wrote, so actions get Playwright's built-in waiting and actionability checks
(visible, enabled, not covered) instead of blind clicks. Playwright errors are
shortened to the one line that says why an action failed, and raised as
:class:`ActionError` so the model can see it and try something else.
"""

from __future__ import annotations

from playwright.sync_api import Error as PlaywrightError

from ..actions import ActionError, missing_arg_error

_WHY = ("intercepts pointer events", "not enabled", "not visible", "not editable", "not stable",
        "not attached", "outside of the viewport", "Unknown key", "net::", "Cannot type")

# Scroll the nearest scrollable ancestor of `el` (the page itself if there is none)
_SCROLL_JS = """
(el, [dx, dy]) => {
  const scrollable = (n) => {
    const s = getComputedStyle(n);
    return (dy && /(auto|scroll|overlay)/.test(s.overflowY) && n.scrollHeight > n.clientHeight + 1) ||
           (dx && /(auto|scroll|overlay)/.test(s.overflowX) && n.scrollWidth > n.clientWidth + 1);
  };
  let n = el;
  while (n && n.nodeType === 1 && n !== document.documentElement && !scrollable(n)) {
    n = n.parentElement || (n.getRootNode() instanceof ShadowRoot ? n.getRootNode().host : null);
  }
  const root = document.scrollingElement || document.documentElement;
  const target = n && n.nodeType === 1 && n !== document.documentElement ? n : root;
  const before = [target.scrollLeft, target.scrollTop];
  target.scrollBy({ left: dx, top: dy, behavior: 'instant' });
  const page = target === root || target === document.body;
  const vertical = dy !== 0;
  const pos = vertical ? target.scrollTop : target.scrollLeft;
  const max = vertical ? target.scrollHeight - target.clientHeight : target.scrollWidth - target.clientWidth;
  return {
    moved: target.scrollLeft !== before[0] || target.scrollTop !== before[1],
    where: page ? 'the page' : target.tagName.toLowerCase() + (target.id ? '#' + target.id : ''),
    atEnd: pos >= max - 2, atStart: pos <= 0,
  };
}
"""

# Text that spans elements ("#1042 Pending" across table cells), then form field values
_PAGE_TEXT_JS = """
(q) => {
  const norm = (s) => (s || '').replace(/\\s+/g, ' ').trim().toLowerCase();
  const want = norm(q);
  if (document.body && norm(document.body.innerText).includes(want)) return { kind: 'text' };
  for (const f of document.querySelectorAll('input:not([type=password]):not([type=hidden]), textarea, select')) {
    const v = f.tagName === 'SELECT' ? [...f.selectedOptions].map((o) => o.label).join(' ') : f.value;
    if (norm(v).includes(want)) return { kind: 'field', value: v };
  }
  return null;
}
"""


def _short(e: Exception) -> str:
    """Playwright errors carry a long call log; keep the headline and the reason."""
    lines = [ln.strip() for ln in str(e).splitlines() if ln.strip()]
    if not lines:
        return type(e).__name__
    head = lines[0]
    reasons = [ln.lstrip("-×0123456789 ").strip() for ln in lines[1:] if any(k in ln for k in _WHY)]
    if reasons and reasons[-1] not in head:
        head += f" ({reasons[-1]})"
    return head[:400]


class WebExecutor:
    def __init__(self, session, default_wait: float = 0.4, settle_timeout: float = 3.0):
        self.session = session
        self.default_wait = default_wait
        self.settle_timeout = settle_timeout

    # -- internals ---------------------------------------------------------
    @property
    def page(self):
        page = self.session.page
        if page is None or page.is_closed():
            raise ActionError("No tab is open. Use `navigate` to open a page.")
        return page

    def _get(self, elements: dict, element_id: str):
        loc = elements.get(element_id)
        if loc is None:
            raise ActionError(
                f"Unknown element_id {element_id!r}. Use an id shown in the current screen."
            )
        try:
            count = loc.count()
        except PlaywrightError:
            count = 0
        if count == 0:
            raise ActionError(
                f"{element_id} is no longer on the page (it changed since the last read). "
                "Use ids from the latest element list."
            )
        return loc.first

    def _settle(self) -> None:
        """Give the page a moment to react: short pause, then DOM ready and (briefly) network idle."""
        page = self.session.page
        if page is None or page.is_closed():
            return
        page.wait_for_timeout(self.default_wait * 1000)
        page = self.session.page  # a click may have opened and activated a new tab
        if page is None or page.is_closed():
            return
        for state, seconds in (("domcontentloaded", self.settle_timeout), ("networkidle", 1.5)):
            try:
                page.wait_for_load_state(state, timeout=seconds * 1000)
            except PlaywrightError:
                pass

    # -- element actions ---------------------------------------------------
    def click(self, elements, element_id, button: str = "left", double: bool = False) -> str:
        loc = self._get(elements, element_id)
        if double:
            loc.dblclick()
        else:
            loc.click(button=button)
        self._settle()
        verb = "Right-clicked" if button == "right" else ("Double-clicked" if double else "Clicked")
        return f"{verb} {element_id}."

    def hover(self, elements, element_id) -> str:
        self._get(elements, element_id).hover()
        self._settle()
        return f"Hovering over {element_id}."

    def type_text(self, elements, element_id, text: str, clear_first: bool = True) -> str:
        loc = self._get(elements, element_id)
        if clear_first:
            try:
                loc.fill(text)
            except PlaywrightError as e:
                if "not an <input>" not in str(e) and "editable" not in str(e):
                    raise
                # Not a real field (e.g. a custom widget that listens for keys): type into it
                loc.click()
                self.page.keyboard.press("Control+A")
                self.page.keyboard.type(text)
        else:
            loc.focus()
            self.page.keyboard.press("Control+End")
            self.page.keyboard.type(text)
        self._settle()
        return f'Typed into {element_id}: "{text}"'

    def select_option(self, elements, element_id, option: str) -> str:
        loc = self._get(elements, element_id)
        if loc.evaluate("el => el.tagName") != "SELECT":
            raise ActionError(
                f"{element_id} is not a native drop-down. Click it to open its list, then click the option."
            )
        options = loc.evaluate("el => [...el.options].map(o => ({label: o.label.trim(), value: o.value}))")
        want = option.strip().lower()
        index = next(
            (i for rule in (
                lambda o: o["label"].lower() == want,
                lambda o: o["value"].lower() == want,
                lambda o: want in o["label"].lower(),
            ) for i, o in enumerate(options) if rule(o)),
            None,
        )
        if index is None:
            labels = ", ".join(o["label"] for o in options[:30])
            raise ActionError(f"{element_id} has no option matching {option!r}. Options: {labels}")
        loc.select_option(index=index)
        self._settle()
        return f'Selected "{options[index]["label"]}" in {element_id}.'

    def set_toggle(self, elements, element_id, state: str = "toggle") -> str:
        loc = self._get(elements, element_id)
        try:
            current = loc.is_checked()
        except PlaywrightError:
            raise ActionError(f"{element_id} is not a checkbox, radio or switch; use `click` instead.")
        want = (not current) if state == "toggle" else (state == "on")
        if want != current:
            try:
                loc.set_checked(want)
            except PlaywrightError as e:
                if "intercepts pointer events" not in str(e):
                    raise
                # A styled control whose real input sits under its label: skip the hit-target check
                loc.set_checked(want, force=True)
            self._settle()
        now = loc.is_checked()
        if now != want:
            raise ActionError(f"{element_id} is still {'checked' if now else 'unchecked'} after toggling it.")
        return f"{element_id} is now {'checked' if now else 'unchecked'}."

    def scroll(self, elements, element_id=None, direction: str = "down", amount: int = 3) -> str:
        direction = str(direction).lower()
        steps = {"down": (0, 1), "up": (0, -1), "right": (1, 0), "left": (-1, 0)}
        if direction not in steps:
            raise ActionError(f"Unknown scroll direction {direction!r} (use up/down/left/right).")
        amount = max(1, min(int(amount), 20))
        dx, dy = (s * amount * 100 for s in steps[direction])

        if element_id:
            result = self._get(elements, element_id).evaluate(_SCROLL_JS, [dx, dy])
        else:
            result = self.page.evaluate(
                f"([dx, dy]) => ({_SCROLL_JS})"
                "(document.elementFromPoint(innerWidth / 2, innerHeight / 2) || document.body, [dx, dy])",
                [dx, dy],
            )
        self._settle()
        edge = "end" if direction in ("down", "right") else "start"
        at_edge = result["atEnd"] if edge == "end" else result["atStart"]
        if not result["moved"]:
            return f"Could not scroll {direction}: {result['where']} is already at the {edge}."
        return f"Scrolled {direction} {amount} in {result['where']}" + (f" (now at the {edge})." if at_edge else ".")

    def scroll_into_view(self, elements, element_id) -> str:
        self._get(elements, element_id).scroll_into_view_if_needed()
        self._settle()
        return f"Scrolled {element_id} into view."

    # -- page actions ------------------------------------------------------
    def press_keys(self, keys: str) -> str:
        combos = str(keys).split()
        if not combos:
            raise ActionError("No keys given.")
        try:
            for combo in combos:
                self.page.keyboard.press(combo)
        except PlaywrightError as e:
            raise ActionError(
                f"{_short(e)}. Use Playwright key names, e.g. 'Enter', 'Tab', 'Escape', "
                "'ArrowDown', 'Control+A', 'Shift+Tab'."
            )
        self._settle()
        return f"Pressed keys: {' '.join(combos)}"

    def navigate(self, url: str) -> str:
        url = str(url).strip()
        if "://" not in url and not url.startswith(("about:", "data:")):
            url = "https://" + url
        page = self.session.ensure_page()
        response = page.goto(url, wait_until="domcontentloaded")
        self._settle()
        status = f" (HTTP {response.status})" if response is not None else ""
        return f"Navigated to {page.url}{status}."

    def go_back(self) -> str:
        page = self.page
        before = page.url
        page.go_back(wait_until="domcontentloaded")
        self._settle()
        if page.url == before:
            return "There is no earlier page in this tab's history; still on the same page."
        return f"Went back to {page.url}."

    def switch_tab(self, index) -> str:
        try:
            page = self.session.switch_to(int(index))
        except (IndexError, ValueError) as e:
            raise ActionError(str(e))
        self._settle()
        return f"Switched to tab {int(index)}: {page.url}"

    def set_dialog_response(self, action: str = "accept", prompt_text: str | None = None) -> str:
        accept = str(action).lower() == "accept"
        self.session.arm_dialog(accept, prompt_text if accept else None)
        how = "accepted" + (f' with "{prompt_text}"' if accept and prompt_text is not None else "") if accept else "dismissed"
        return f"The next JavaScript dialog will be {how}."

    def assert_text(self, text: str) -> str:
        page = self.page
        for frame in page.frames:
            try:
                matches = frame.get_by_text(text).filter(visible=True)
                if matches.count():
                    found = _clip_text(matches.first.inner_text(timeout=2000))
                    return f"assert_text PASS: {text!r} is shown on the page (in \"{found}\")."
                hit = frame.evaluate(_PAGE_TEXT_JS, text)
                if hit and hit["kind"] == "text":
                    return f"assert_text PASS: {text!r} is shown on the page."
                if hit:
                    return f"assert_text PASS: {text!r} is the value of a form field (\"{_clip_text(hit['value'])}\")."
            except PlaywrightError:
                continue
        try:
            visible = _clip_text(page.evaluate("document.body ? document.body.innerText : ''"), 300)
        except PlaywrightError:
            visible = ""
        return f"assert_text FAIL: {text!r} is not shown on the page. Page text begins: \"{visible}\""

    def click_at(self, x, y, button: str = "left", double: bool = False) -> str:
        page = self.page
        width, height = page.evaluate("[innerWidth, innerHeight]")
        px, py = float(x) / 1000.0 * width, float(y) / 1000.0 * height
        if double:
            page.mouse.dblclick(px, py, button=button)
        else:
            page.mouse.click(px, py, button=button)
        self._settle()
        return f"Clicked at ({x},{y})/1000 -> page ({int(px)},{int(py)})."

    def wait(self, seconds: float = 1.0) -> str:
        seconds = min(max(float(seconds), 0.0), 15.0)
        self.page.wait_for_timeout(seconds * 1000)
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
            if name == "hover":
                return self.hover(elements, inp["element_id"])
            if name == "type_text":
                return self.type_text(elements, inp["element_id"], inp.get("text", ""),
                                      inp.get("clear_first", True))
            if name == "select_option":
                return self.select_option(elements, inp["element_id"], inp["option"])
            if name == "set_toggle":
                return self.set_toggle(elements, inp["element_id"], inp.get("state", "toggle"))
            if name == "scroll":
                return self.scroll(elements, inp.get("element_id"), inp.get("direction", "down"),
                                   inp.get("amount", 3))
            if name == "scroll_into_view":
                return self.scroll_into_view(elements, inp["element_id"])
            if name == "press_keys":
                return self.press_keys(inp["keys"])
            if name == "navigate":
                return self.navigate(inp["url"])
            if name == "go_back":
                return self.go_back()
            if name == "switch_tab":
                return self.switch_tab(inp["index"])
            if name == "set_dialog_response":
                return self.set_dialog_response(inp.get("action", "accept"), inp.get("prompt_text"))
            if name == "assert_text":
                return self.assert_text(inp["text"])
            if name == "click_at":
                return self.click_at(inp["x"], inp["y"], inp.get("button", "left"), inp.get("double", False))
            if name == "wait":
                return self.wait(inp.get("seconds", 1))
        except KeyError as e:
            raise missing_arg_error(name, str(e), inp, elements)
        except PlaywrightError as e:
            raise ActionError(_short(e))
        raise ActionError(f"Unknown action '{name}'.")


def _clip_text(text: str, limit: int = 80) -> str:
    t = " ".join((text or "").split())
    return t[: limit - 1] + "…" if len(t) > limit else t
