"""Launch or attach to the application window under test."""

from __future__ import annotations

import subprocess
import time

import uiautomation as auto

# Top-level containers we treat as candidate application windows.
_WINDOW_TYPES = {"WindowControl", "PaneControl"}


def launch_app(exe_path: str, args: list[str] | None = None, cwd: str | None = None):
    """Start the executable and return the Popen handle."""
    return subprocess.Popen([exe_path, *(args or [])], cwd=cwd)


def find_window(
    pid: int | None = None,
    title: str | None = None,
    class_name: str | None = None,
    timeout: float = 30.0,
    poll: float = 0.5,
):
    """Find a top-level window by pid, title substring, or class name.

    Note: some apps (installers/launchers, UWP hosts) show their real window
    under a different process than the one you launched — in that case pass a
    ``title`` as well so matching can fall back to it.
    """
    if pid is None and not title and not class_name:
        raise ValueError("Provide at least one of pid, title, or class_name.")

    deadline = time.time() + timeout
    while time.time() < deadline:
        root = auto.GetRootControl()
        for w in root.GetChildren():
            try:
                if w.ControlTypeName not in _WINDOW_TYPES:
                    continue
                name = w.Name or ""
                if pid is not None and w.ProcessId == pid:
                    return w
                if title and title.lower() in name.lower():
                    return w
                if class_name and (w.ClassName or "") == class_name:
                    return w
            except Exception:
                continue
        time.sleep(poll)

    raise RuntimeError(
        f"Could not find an application window (pid={pid!r}, title={title!r}, "
        f"class={class_name!r}) within {timeout:g}s. Is the window open and visible?"
    )
