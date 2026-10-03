"""Tests for the web engine: page snapshot, actions, dialogs, tabs and the agent loop.

Runs a real headless Chromium against a local page (tests/fixtures/web_demo.html),
so nothing touches the network and no model is called — a scripted chain stands
in for the LLM. Needs Playwright and its Chromium build:

    pip install playwright==1.62.0
    playwright install chromium
"""

import re
import sys
import tempfile
from pathlib import Path

# Add project root to sys.path
sys.path.insert(0, str(Path(__file__).parent.parent))

from app_clicker.actions import ActionError
from app_clicker.agent import TesterAgent
from app_clicker.llm import AssistantTurn, ToolCall
from app_clicker.reporter import Reporter
from app_clicker.web import WEB_SYSTEM, WEB_TOOLS, WebExecutor, WebPerceiver, WebSession

FIXTURE = (Path(__file__).parent / "fixtures" / "web_demo.html").as_uri()

_session = None


def _open():
    """A fresh browser context on the demo page, with its perceiver and executor."""
    global _session
    if _session is None:
        _session = WebSession(headless=True, action_timeout=1.5).start()
    _session.open_case(FIXTURE, fresh=True)
    return WebPerceiver(_session), WebExecutor(_session, default_wait=0)


def teardown_module():
    global _session
    if _session is not None:
        _session.close()
        _session = None


def _id(obs, fragment):
    """Id of the first listed element whose line contains ``fragment``."""
    for line in obs.tree_text.splitlines():
        m = re.match(r"\s*\[(e\d+)\] (.*)", line)
        if m and fragment in m.group(2):
            return m.group(1)
    raise AssertionError(f"No element containing {fragment!r} in:\n{obs.tree_text}")


def _sign_in(perceiver, executor, user="demo", password="secret"):
    obs = perceiver.observe()
    executor.dispatch("type_text", {"element_id": _id(obs, 'textbox "Username"'), "text": user}, obs.elements)
    executor.dispatch("type_text", {"element_id": _id(obs, '"Password"'), "text": password}, obs.elements)
    executor.dispatch("click", {"element_id": _id(obs, 'button "Sign in"')}, obs.elements)
    return perceiver.observe()


class ScriptedChain:
    """Stands in for the model: returns pre-written tool calls and records what it was shown."""

    def __init__(self, calls):
        self.calls = list(calls)
        self.shown = []

    def complete(self, system, transcript, tools, log=None):
        self.shown.append(transcript[-1]["text"])
        name, inp = self.calls.pop(0)
        call = ToolCall(id=f"call{len(self.shown)}", name=name, input=inp)
        return AssistantTurn(text="", tool_call=call, stop_reason="tool_use", usage={})


def test_snapshot_lists_form_controls_with_states():
    perceiver, _ = _open()
    obs = perceiver.observe()
    text = obs.tree_text

    assert obs.window_title == f"Orders Demo — {FIXTURE}", obs.window_title
    assert text.splitlines()[0] == 'heading "Sign in"', text
    assert re.search(r'^\[e\d+\] textbox "Username" \(required\)$', text, re.M), text
    assert re.search(r'^\[e\d+\] combobox "Role" value="Viewer" options=\[Viewer, Editor, Admin\]$', text, re.M), text
    assert 'checkbox "Remember me" (unchecked)' in text, text
    # Labels name their fields instead of repeating as text; the hidden app section is not listed
    assert 'text "Username"' not in text and "Welcome" not in text, text
    assert obs.screenshot and obs.screenshot.startswith(b"\x89PNG"), "expected a PNG screenshot"
    assert all(loc.count() == 1 for loc in obs.elements.values())


def test_executor_fills_form_and_signs_in():
    perceiver, executor = _open()

    obs = _sign_in(perceiver, executor, user="", password="")
    assert 'alert "Username is required"' in obs.tree_text, obs.tree_text

    obs = _sign_in(perceiver, executor, password="wrong")
    assert 'alert "Wrong password"' in obs.tree_text, obs.tree_text
    assert 'value="•••••"' in obs.tree_text and "wrong" not in obs.tree_text, obs.tree_text

    role, remember = _id(obs, 'combobox "Role"'), _id(obs, 'checkbox "Remember me"')
    assert executor.dispatch("select_option", {"element_id": role, "option": "editor"}, obs.elements) == \
        f'Selected "Editor" in {role}.'
    assert executor.dispatch("set_toggle", {"element_id": remember, "state": "on"}, obs.elements) == \
        f"{remember} is now checked."
    assert executor.dispatch("set_toggle", {"element_id": remember, "state": "on"}, obs.elements) == \
        f"{remember} is now checked."
    for name, inp in (("select_option", {"element_id": remember, "option": "x"}),
                      ("select_option", {"element_id": role, "option": "Owner"}),
                      ("click", {"element_id": "e999"})):
        try:
            executor.dispatch(name, inp, obs.elements)
        except ActionError:
            pass
        else:
            raise AssertionError(f"{name} {inp} should raise ActionError")

    obs = _sign_in(perceiver, executor)
    assert 'heading "Welcome, demo (Editor)"' in obs.tree_text, obs.tree_text
    assert 'row "#1042 | Pending | Delete"' in obs.tree_text, obs.tree_text
    for text, verdict in (("welcome, DEMO", "PASS"), ("#1042 Pending", "PASS"), ("Goodbye", "FAIL")):
        outcome = executor.dispatch("assert_text", {"text": text}, obs.elements)
        assert outcome.startswith(f"assert_text {verdict}"), outcome


def test_confirm_dialog_is_dismissed_unless_armed():
    perceiver, executor = _open()
    obs = _sign_in(perceiver, executor)

    executor.dispatch("click", {"element_id": _id(obs, 'button "Delete"')}, obs.elements)
    obs = perceiver.observe()
    assert 'A confirm dialog appeared: "Delete order #1042?". It was dismissed.' in obs.tree_text, obs.tree_text
    assert 'status "Delete cancelled"' in obs.tree_text, obs.tree_text

    executor.dispatch("set_dialog_response", {"action": "accept"}, obs.elements)
    executor.dispatch("click", {"element_id": _id(obs, 'button "Delete"')}, obs.elements)
    obs = perceiver.observe()
    assert "It was accepted." in obs.tree_text, obs.tree_text
    assert 'status "Order #1042 deleted"' in obs.tree_text and "#1042 | Pending" not in obs.tree_text, obs.tree_text


def test_modal_covers_the_page_and_blocks_clicks():
    perceiver, executor = _open()
    obs = _sign_in(perceiver, executor)
    executor.dispatch("click", {"element_id": _id(obs, 'button "Terms"')}, obs.elements)
    obs = perceiver.observe()

    assert re.search(r'button "Delete" \(covered by div#overlay\)', obs.tree_text), obs.tree_text
    assert re.search(r'^dialog "Terms"\n  text "Please accept the terms."\n  \[e\d+\] button "Close"$',
                     obs.tree_text, re.M), obs.tree_text
    try:
        executor.dispatch("click", {"element_id": _id(obs, 'button "Delete"')}, obs.elements)
    except ActionError as e:
        assert "intercepts pointer events" in str(e), e
    else:
        raise AssertionError("Clicking a covered button should raise ActionError")

    executor.dispatch("click", {"element_id": _id(obs, 'button "Close"')}, obs.elements)
    obs = perceiver.observe()
    assert "covered by" not in obs.tree_text and 'dialog "Terms"' not in obs.tree_text, obs.tree_text


def test_frames_shadow_dom_scroll_areas_and_offscreen_elements():
    perceiver, executor = _open()
    obs = _sign_in(perceiver, executor)
    assert re.search(r'^frame "Framed widget":\n  \[e\d+\] button "Framed button"$', obs.tree_text, re.M), obs.tree_text
    assert re.search(r'button "Bottom button" \(offscreen\)', obs.tree_text), obs.tree_text
    assert "... 24 more lines inside; scroll this area to see them" in obs.tree_text, obs.tree_text

    executor.dispatch("click", {"element_id": _id(obs, "Framed button")}, obs.elements)
    executor.dispatch("click", {"element_id": _id(obs, "Shadow button")}, obs.elements)
    obs = perceiver.observe()
    assert 'button "Framed clicked"' in obs.tree_text and 'status "Shadow clicked"' in obs.tree_text, obs.tree_text

    area = _id(obs, "scroll-area")
    outcome = executor.dispatch("scroll", {"element_id": area, "direction": "down", "amount": 3}, obs.elements)
    assert outcome == "Scrolled down 3 in div#list.", outcome
    obs = perceiver.observe()
    assert 'text "List item 1"' not in obs.tree_text and 'text "List item 20"' in obs.tree_text, obs.tree_text
    area = _id(obs, "scroll-area")
    executor.dispatch("scroll", {"element_id": area, "direction": "up", "amount": 20}, obs.elements)
    outcome = executor.dispatch("scroll", {"element_id": area, "direction": "up"}, obs.elements)
    assert outcome == "Could not scroll up: div#list is already at the start.", outcome

    executor.dispatch("click", {"element_id": _id(obs, "Bottom button")}, obs.elements)
    assert executor.dispatch("assert_text", {"text": "Bottom reached"}, {}).startswith("assert_text PASS")


def test_new_tabs_and_page_errors_are_reported():
    perceiver, executor = _open()
    obs = _sign_in(perceiver, executor)

    executor.dispatch("click", {"element_id": _id(obs, 'link "Help center"')}, obs.elements)
    obs = perceiver.observe()
    assert "A new tab opened and is now active" in obs.tree_text, obs.tree_text
    assert 'Open tabs: [0] "Orders Demo" | [1] "Help" (active)' in obs.tree_text, obs.tree_text
    assert obs.window_title.endswith("?help") and 'heading "Help center"' in obs.tree_text, obs.tree_text
    assert "Uncaught" not in obs.tree_text, obs.tree_text

    executor.dispatch("switch_tab", {"index": 0}, obs.elements)
    obs = perceiver.observe()
    assert "Welcome, demo" in obs.tree_text, obs.tree_text

    executor.dispatch("click", {"element_id": _id(obs, 'button "Broken button"')}, obs.elements)
    obs = perceiver.observe()
    assert "Uncaught JavaScript error: orders service unavailable" in obs.tree_text, obs.tree_text


def test_every_web_tool_is_dispatched():
    _, executor = _open()
    names = [t["name"] for t in WEB_TOOLS]
    assert len(names) == len(set(names)), names
    for name in names:
        if name in ("note", "finish"):
            continue
        try:
            executor.dispatch(name, {"seconds": 0}, {})
        except ActionError as e:
            assert "Unknown action" not in str(e), f"{name}: {e}"


def test_agent_loop_drives_the_page_and_writes_report():
    perceiver, executor = _open()
    chain = ScriptedChain([
        ("type_text", {"element_id": "e1", "text": "demo", "reason": "enter the user"}),
        ("type_text", {"element_id": "e2", "text": "secret", "reason": "enter the password"}),
        ("click", {"element_id": "e5", "reason": "sign in"}),
        ("assert_text", {"text": "Welcome, demo", "reason": "verify the greeting"}),
        ("note", {"text": "Greeting shown after sign-in."}),
        ("finish", {"status": "passed", "summary": "Signed in and saw the greeting."}),
    ])

    with tempfile.TemporaryDirectory() as out:
        reporter = Reporter(out, "web-sign-in", "Sign in as demo")
        agent = TesterAgent(chain, perceiver=perceiver, executor=executor, tools=WEB_TOOLS,
                            system=WEB_SYSTEM, screen_label="page", reporter=reporter, verbose=False)
        result = agent.run("Sign in as demo")
        report_md = Path(reporter.finalize(result)).read_text(encoding="utf-8")
        shots = sorted(p.name for p in Path(reporter.dir, "screenshots").glob("*.png"))

    assert (result.status, result.steps) == ("passed", 6), result
    initial, _, _, after_click, after_assert, _ = chain.shown
    assert initial.startswith("TASK:\nSign in as demo\n\nCurrent page: \"Orders Demo — file:"), initial
    assert '[e1] textbox "Username"' in initial, initial
    assert 'Updated page: "Orders Demo' in after_click and 'heading "Welcome, demo (Viewer)"' in after_click, after_click
    assert after_assert.startswith("assert_text PASS"), after_assert
    assert shots == ["step001.png", "step002.png", "step003.png", "step004.png"], shots
    assert "![step 3](screenshots/step003.png)" in report_md


if __name__ == "__main__":
    print("Running web engine tests...")
    try:
        for name, fn in list(globals().items()):
            if name.startswith("test_") and callable(fn):
                fn()
                print(f"[OK] {name}")
    finally:
        teardown_module()
    print("\nAll web engine tests passed successfully!")
