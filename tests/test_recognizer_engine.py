"""Offline tests for the recognizer engine: outline, inspect, clicks and the agent loop.

Nothing touches the real desktop: the window is faked, the screen image is drawn
in memory, and clicks are captured instead of sent.
"""

import contextlib
import re
import sys
import tempfile
from pathlib import Path

# Add project root to sys.path
sys.path.insert(0, str(Path(__file__).parent.parent))

from PIL import Image, ImageDraw
from screen_recognizer import ScreenReport

from app_clicker import recognizer_engine
from app_clicker.actions import ActionError
from app_clicker.llm import AssistantTurn, ToolCall
from app_clicker.recognizer_agent import RecognizerAgent
from app_clicker.recognizer_engine import RecognizerExecutor, RecognizerPerceiver, describe_element
from app_clicker.reporter import Reporter

OFFSET = (100, 200)  # pretend the window's top-left corner is here on screen
COORDINATES = re.compile(r"\d+\s*,\s*\d+")

_cache = {}


class FakeWindow:
    Name = "Login Demo"

    def SetActive(self):
        pass


def draw_login_form() -> Image.Image:
    img = Image.new("RGB", (520, 300), (245, 247, 250))
    draw = ImageDraw.Draw(img)
    draw.rectangle([20, 20, 500, 280], fill=(255, 255, 255), outline=(226, 232, 240))
    draw.text((40, 36), "Account Login", fill=(15, 23, 42))
    draw.text((40, 76), "Username:", fill=(71, 85, 105))
    draw.rectangle([40, 100, 400, 135], fill=(255, 255, 255), outline=(203, 213, 225), width=2)
    draw.rectangle([40, 200, 160, 235], fill=(37, 99, 235), outline=(29, 78, 216))
    draw.text((75, 210), "Sign In", fill=(255, 255, 255))
    draw.rectangle([180, 200, 300, 235], fill=(254, 242, 242), outline=(254, 202, 202))
    draw.text((215, 210), "Cancel", fill=(220, 38, 38))
    return img


class ImagePerceiver:
    """Serves the drawn form instead of capturing a real window."""

    def __init__(self, perceiver: RecognizerPerceiver):
        self.perceiver = perceiver

    def observe(self):
        return self.perceiver.observe_image(draw_login_form(), OFFSET, FakeWindow.Name)


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


@contextlib.contextmanager
def captured_clicks():
    clicks = []
    original = recognizer_engine._click_screen
    recognizer_engine._click_screen = lambda x, y, button="left", double=False: clicks.append((x, y, button, double))
    try:
        yield clicks
    finally:
        recognizer_engine._click_screen = original


def _perceiver() -> RecognizerPerceiver:
    if "perceiver" not in _cache:
        _cache["perceiver"] = RecognizerPerceiver(FakeWindow(), ocr_backend="auto")
    return _cache["perceiver"]


def _observation():
    if "obs" not in _cache:
        _cache["obs"] = ImagePerceiver(_perceiver()).observe()
    return _cache["obs"]


def _find(report, text):
    return next(el for el in report.elements if text in el.text)


def _container(report):
    return next(el for el in report.elements if el.type.value == "container")


def test_outline_is_structured_and_coordinate_free():
    obs = _observation()
    report = obs.report
    lines = obs.text.splitlines()
    card = _container(report)
    sign_in, cancel = _find(report, "Sign In"), _find(report, "Cancel")
    username = next(el for el in report.elements if el.type.value == "input")

    assert any(line.startswith(f"[{card.id}] container") for line in lines), obs.text
    # Children are indented under the card; Sign In and Cancel share a row
    assert f'  [{sign_in.id}] button "{sign_in.text}" | [{cancel.id}] button "{cancel.text}"' in lines, obs.text
    # The empty input shows its label instead of pretending the label is its content
    assert "Username" in username.text
    assert f'  [{username.id}] input (no text detected) label="{username.text}"' in lines, obs.text
    assert not COORDINATES.search(obs.text), obs.text


def test_inspect_element_gives_details_without_coordinates():
    report = _observation().report
    sign_in = _find(report, "Sign In")
    details = describe_element(report, sign_in.id)

    assert "shape: rectangle" in details, details
    assert f"background {sign_in.style.background_color}" in details, details
    assert f"inside [{_container(report).id}] container" in details, details
    assert f"right [{_find(report, 'Cancel').id}] button" in details, details
    assert not COORDINATES.search(details), details

    # References to an empty input show its label, not the label as if it were typed in
    username = next(el for el in report.elements if el.type.value == "input")
    card_details = describe_element(report, _container(report).id)
    assert f'[{username.id}] input label="{username.text}"' in card_details, card_details

    with contextlib.suppress(ActionError):
        describe_element(report, 999)
        raise AssertionError("Unknown id should raise ActionError")


def test_executor_clicks_report_screen_position():
    report = _observation().report
    sign_in = _find(report, "Sign In")
    executor = RecognizerExecutor(FakeWindow(), default_wait=0)

    with captured_clicks() as clicks:
        executor.dispatch("click", {"element_id": sign_in.id, "reason": "test"}, report)
        assert clicks == [(sign_in.center.x + OFFSET[0], sign_in.center.y + OFFSET[1], "left", False)]

        try:
            executor.dispatch("click", {"element_id": _container(report).id}, report)
        except ActionError as e:
            assert "container" in str(e)
        else:
            raise AssertionError("Clicking a container should raise ActionError")
        assert len(clicks) == 1


def test_agent_loop_with_inspect_and_click():
    report = _observation().report
    sign_in = _find(report, "Sign In")
    chain = ScriptedChain([
        ("inspect_element", {"element_id": sign_in.id, "reason": "check the button"}),
        ("click", {"element_id": sign_in.id, "reason": "sign in"}),
        ("finish", {"status": "passed", "summary": "done"}),
    ])

    with tempfile.TemporaryDirectory() as out, captured_clicks() as clicks:
        reporter = Reporter(out, "recognizer-demo", "Sign in")
        agent = RecognizerAgent(chain, FakeWindow(), ocr_backend="auto", reporter=reporter, verbose=False)
        agent.perceiver = ImagePerceiver(_perceiver())
        agent.executor.default_wait = 0
        result = agent.run("Sign in")
        report_md = Path(reporter.finalize(result)).read_text(encoding="utf-8")
        saved = list(Path(reporter.dir, "screen_reports").glob("*.json"))
        saved_report = ScreenReport.model_validate_json(saved[0].read_text(encoding="utf-8")) if saved else None

    assert (result.status, result.steps) == ("passed", 3)
    assert clicks == [(sign_in.screen_center.x, sign_in.screen_center.y, "left", False)]

    initial, after_inspect, after_click = chain.shown
    assert "TASK:\nSign in" in initial and _observation().text in initial
    assert "shape: rectangle" in after_inspect and "not re-read" in after_inspect
    assert f"Clicked [{sign_in.id}]" in after_click

    assert len(saved) == 1 and saved_report.find_by_id(sign_in.id).text == sign_in.text
    assert "screen_reports/step002.json" in report_md


if __name__ == "__main__":
    print("Running recognizer engine tests...")
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"[OK] {name}")
    print("\nAll recognizer engine tests passed successfully!")
