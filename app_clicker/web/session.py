"""Browser session for the web engine: launch or attach, track tabs, collect page events.

One session spans a whole run. Each test case gets a fresh browser context (clean
cookies and storage) unless ``--keep-open`` is set, mirroring how the desktop
engine relaunches the app between cases. With ``--cdp`` the session attaches to a
browser you already run and reuses its logged-in profile instead.

Playwright's sync API only delivers events (dialogs, new tabs, page errors) while
a Playwright call is in progress, so the web engine waits with
``page.wait_for_timeout`` rather than ``time.sleep``.
"""

from __future__ import annotations

from playwright.sync_api import sync_playwright

BROWSERS = ("chromium", "chrome", "msedge", "firefox", "webkit")
_MAX_EVENTS = 10


def _clip(text: str, limit: int = 150) -> str:
    t = " ".join((text or "").split())
    return t[: limit - 1] + "…" if len(t) > limit else t


class WebSession:
    def __init__(
        self,
        browser: str = "chromium",
        headless: bool = False,
        cdp: str | None = None,
        storage_state: str | None = None,
        viewport: tuple[int, int] = (1280, 800),
        action_timeout: float = 5.0,
        nav_timeout: float = 30.0,
    ):
        if browser not in BROWSERS:
            raise ValueError(f"Unknown browser {browser!r}; choose one of {', '.join(BROWSERS)}.")
        self.browser = browser
        self.headless = headless
        self.cdp = cdp
        self.storage_state = storage_state
        self.viewport = viewport
        self.action_timeout = action_timeout
        self.nav_timeout = nav_timeout

        self._pw = None
        self._browser = None
        self.context = None
        self.page = None
        self._events: list[str] = []
        self._dialog_response: tuple[bool, str | None] | None = None
        self._opening = False

    # -- lifecycle ---------------------------------------------------------
    def start(self) -> "WebSession":
        self._pw = sync_playwright().start()
        try:
            if self.cdp:
                self._browser = self._pw.chromium.connect_over_cdp(self.cdp)
            elif self.browser in ("firefox", "webkit"):
                self._browser = getattr(self._pw, self.browser).launch(headless=self.headless)
            else:
                channel = None if self.browser == "chromium" else self.browser
                self._browser = self._pw.chromium.launch(headless=self.headless, channel=channel)
        except Exception:
            self._pw.stop()
            self._pw = None
            raise
        return self

    def open_case(self, url: str | None = None, fresh: bool = True) -> None:
        """Prepare the tab a test case starts in, and load ``url`` into it."""
        first = self.context is None
        if self.cdp:
            if first:
                if not self._browser.contexts:
                    raise RuntimeError("The browser at --cdp has no open window to attach to.")
                self._adopt(self._browser.contexts[0])
                # With a URL, open our own tab rather than taking over the one you are using
                self.page = None if url else self._visible_page()
        elif first or fresh:
            old, self.page = self.context, None
            if old is not None:
                old.close()
            options = {"viewport": {"width": self.viewport[0], "height": self.viewport[1]}}
            if self.storage_state:
                options["storage_state"] = self.storage_state
            self._adopt(self._browser.new_context(**options))

        page = self.ensure_page()
        if url and (first or fresh):
            page.goto(url, wait_until="domcontentloaded")
        self._events.clear()  # setup noise is not something the model did
        self._dialog_response = None

    def close(self) -> None:
        try:
            # Attached over CDP: leave your browser running; stopping the driver just disconnects
            if self._browser is not None and not self.cdp:
                self._browser.close()
        finally:
            if self._pw is not None:
                self._pw.stop()
            self._pw = self._browser = self.context = self.page = None

    # -- tabs --------------------------------------------------------------
    def tabs(self) -> list:
        if self.context is None:
            return []
        return [p for p in self.context.pages if not p.is_closed()]

    def ensure_page(self):
        if self.page is None or self.page.is_closed():
            self._opening = True
            try:
                self.page = self.context.new_page()
            finally:
                self._opening = False
        return self.page

    def switch_to(self, index: int):
        tabs = self.tabs()
        if not 0 <= index < len(tabs):
            raise IndexError(f"No tab {index}; open tabs are 0..{len(tabs) - 1}.")
        self.page = tabs[index]
        self.page.bring_to_front()
        return self.page

    def _visible_page(self):
        tabs = self.tabs()
        for p in tabs:
            try:
                if p.evaluate("document.visibilityState") == "visible":
                    return p
            except Exception:
                continue
        return tabs[-1] if tabs else None

    # -- dialogs and events ------------------------------------------------
    def arm_dialog(self, accept: bool, prompt_text: str | None = None) -> None:
        """Decide how the NEXT JavaScript dialog is answered (one-shot)."""
        self._dialog_response = (accept, prompt_text)

    def drain_events(self) -> list[str]:
        events, self._events = self._events, []
        return events

    def _event(self, text: str) -> None:
        if len(self._events) < _MAX_EVENTS:
            self._events.append(text)
        elif len(self._events) == _MAX_EVENTS:
            self._events.append("(more events were dropped)")

    def _adopt(self, context) -> None:
        self.context = context
        context.set_default_timeout(self.action_timeout * 1000)
        context.set_default_navigation_timeout(self.nav_timeout * 1000)
        context.on("page", self._on_new_page)
        for p in context.pages:
            self._watch(p)

    def _watch(self, page) -> None:
        page.on("dialog", self._on_dialog)
        page.on("pageerror", lambda err: self._event(
            f"Uncaught JavaScript error: {_clip(getattr(err, 'message', None) or str(err))}"))
        page.on("console", lambda msg: msg.type == "error" and self._event(
            f"Console error: {_clip(msg.text)}"))
        page.on("close", self._on_close)

    def _on_new_page(self, page) -> None:
        self._watch(page)
        self.page = page
        if not self._opening:
            self._event(f"A new tab opened and is now active: {page.url or 'about:blank'}")

    def _on_close(self, page) -> None:
        if page is not self.page:
            return
        remaining = [p for p in page.context.pages if not p.is_closed() and p is not page]
        self.page = remaining[-1] if remaining else None
        self._event("The active tab closed" + (f"; switched to {self.page.url}" if self.page else "; no tabs are left"))

    def _on_dialog(self, dialog) -> None:
        if self._dialog_response is not None:
            accept, text = self._dialog_response
            self._dialog_response = None
        else:
            # Alerts only have OK; a beforeunload left unanswered would block navigation
            accept, text = dialog.type in ("alert", "beforeunload"), None
        try:
            if accept:
                dialog.accept(text) if text is not None else dialog.accept()
            else:
                dialog.dismiss()
        except Exception:
            pass
        verdict = "accepted" + (f' with "{_clip(text, 60)}"' if text is not None else "") if accept else "dismissed"
        self._event(f'A {dialog.type} dialog appeared: "{_clip(dialog.message)}". It was {verdict}.')
