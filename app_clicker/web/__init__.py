"""Web engine: drive a web app in a real browser through Playwright.

Plugs into the same agent loop as the desktop engine: :class:`WebPerceiver`
produces an element list plus a screenshot, :class:`WebExecutor` carries out the
model's action, and :class:`WebSession` owns the browser, tabs and page events.
"""

from .actions import WebExecutor
from .perception import WebPerceiver
from .session import BROWSERS, WebSession
from .tools import WEB_SYSTEM, WEB_TOOLS

__all__ = ["BROWSERS", "WEB_SYSTEM", "WEB_TOOLS", "WebExecutor", "WebPerceiver", "WebSession"]
