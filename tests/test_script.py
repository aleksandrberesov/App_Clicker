"""Scripted (model-free) steps: parsing, element lookup, assertions, OCR checks (no desktop needed)."""

import sys
import unicodedata
from pathlib import Path
from types import SimpleNamespace as NS

import pytest

sys.path.insert(0, str(Path(__file__).parent))  # fake_uia lives next to the tests

from fake_uia import FakeControl, demo_app as _app, window  # noqa: E402

from app_clicker.actions import ActionExecutor  # noqa: E402
from app_clicker.locate import ElementNotFound, Resolver, build_locator, LocatorError  # noqa: E402
from app_clicker.script import ScriptError, ScriptRunner, ocr_matches, parse_steps  # noqa: E402


# -- parsing ------------------------------------------------------------------------
def test_bare_values_are_shorthands():
    steps = parse_steps([{"click": "Tips"}, {"wait": 1.5}, {"assert_ocr": "II"}, {"assert_title": "Calc"},
                         {"press_keys": "{Ctrl}a"}, {"scroll": "down"}])
    assert steps[0].locator.name == "Tips"
    assert steps[1].args == {"seconds": 1.5}
    assert steps[2].args == {"text": "II"}
    assert steps[3].args == {"contains": "Calc"}
    assert steps[4].args == {"keys": "{Ctrl}a"}
    assert steps[5].locator is None and steps[5].args == {"direction": "down"}


def test_full_mapping_with_locator_options_and_nesting():
    (step,) = parse_steps([{"type_text": {"type": "Edit", "id": "Name", "index": 2, "text": 56,
                                          "within": {"name": "Form", "type": "Pane"}, "timeout": 9,
                                          "reason": "enter the value"}}])
    assert step.args == {"text": "56"}          # a YAML number is typed as text
    assert step.locator.type == "Edit" and step.locator.id == "Name" and step.locator.index == 2
    assert step.locator.within.name == "Form" and step.timeout == 9 and step.reason == "enter the value"


@pytest.mark.parametrize("raw, fragment", [
    ([{"clck": "Tips"}], "unknown action 'clck' (did you mean 'click'?)"),
    ([{"click": "Tips", "timeout": 5}], "inside it"),
    ([{"click": {}}], "needs an element"),
    ([{"click": {"nam": "x"}}], "does not take 'nam' (did you mean 'name'?)"),
    ([{"click": {"name": "x", "colour": 1}}], "does not take 'colour'"),
    ([{"click": {"name": "x", "index": 0}}], "'index' must be a whole number >= 1"),
    ([{"wait": 99}], "'seconds' must be a number from 0 to 15"),
    ([{"assert_ocr": {"text": "II", "psm": 99}}], "'psm' must be a whole number from 0 to 13"),
    ([{"assert_ocr": {"text": "II", "region": [0.5, 0, 0.2, 1]}}], "left < right"),
    ([{"assert_ocr": {"text": "II", "region": [0, 0, 1, 1], "in": "Canvas"}}], "either 'region' or 'in'"),
    ([{"assert_ocr": {"text": "II", "regex": "I+"}}], "exactly one of: text, regex"),
    ([{"assert_ocr": {"regex": "(unclosed"}}], "not a valid regex"),
    ([{"assert_value": {"name": "x"}}], "exactly one of: equals, contains, regex"),
    ([{"assert_state": {"name": "x"}}], "at least one of: enabled"),
    ([{"assert_ocr": "  "}], "must not be empty"),
    ([{"click": {"name": "x", "name_regex": "("}}], "not a valid regex"),
    ([{"click_at": {"x": 5}}], "needs 'y'"),
    ([{"set_toggle": {"name": "x", "state": "maybe"}}], "must be one of: on, off, toggle"),
    (["click"], "one-key mapping"),
])
def test_invalid_steps_say_what_is_wrong(raw, fragment):
    with pytest.raises(ScriptError) as err:
        parse_steps(raw, "case 'Demo'")
    assert fragment in str(err.value) and "case 'Demo', step 1" in str(err.value)


def test_an_empty_steps_list_is_rejected():
    with pytest.raises(ScriptError, match="case 'Demo': 'steps' must be a non-empty list"):
        parse_steps([], "case 'Demo'")


def test_error_names_the_failing_step():
    with pytest.raises(ScriptError, match=r"step 3 \(wait\)"):
        parse_steps([{"click": "a"}, {"click": "b"}, {"wait": -1}])


# -- element lookup ----------------------------------------------------------------
def _find(root, spec):
    return Resolver(window(*root) if isinstance(root, list) else root).pick(build_locator(spec))


def test_cyrillic_names_match_despite_case_spacing_and_unicode_form():
    nbsp = chr(0xA0)
    button = FakeControl(f"Вся{nbsp}область  отведения")     # non-breaking + double space, as UIA text can be
    other = FakeControl("Выбранное отведение")
    assert _find([other, button], "вся область отведения") is button
    assert _find([other, button], {"name": "ВСЯ ОБЛАСТЬ ОТВЕДЕНИЯ"}) is button
    # й stored precomposed in the app but typed in the script as и + a combining breve
    decomposed = unicodedata.normalize("NFD", "Район")
    assert decomposed != "Район" and len(decomposed) == len("Район") + 1
    district = FakeControl("Район")
    assert _find([other, district], decomposed) is district


def test_other_ways_to_select():
    ok, cancel = FakeControl("OK", aid="okBtn"), FakeControl("Cancel", aid="cancelBtn", cls="Special")
    edit = FakeControl("Search", "Edit", aid="query")
    root = [ok, cancel, edit]
    assert _find(root, {"id": "cancelBtn"}) is cancel
    assert _find(root, {"type": "Edit"}) is edit
    assert _find(root, {"type": "EditControl"}) is edit        # either spelling of the type
    assert _find(root, {"class": "Special"}) is cancel
    assert _find(root, {"name_contains": "canc"}) is cancel
    assert _find(root, {"name_regex": "^o.$"}) is ok
    assert _find(root, {"type": "Button", "name": "OK"}) is ok
    assert _find(root, {"type": "Edit", "name": "OK"}) is None  # every key must match


def test_visible_matches_beat_offscreen_twins_and_index_counts_among_them():
    hidden, first, second = FakeControl("Tips", offscreen=True), FakeControl("Tips"), FakeControl("Tips")
    resolver = Resolver(window(hidden, first, second))
    assert resolver.pick(build_locator("Tips")) is first
    assert resolver.pick(build_locator({"name": "Tips", "index": 2})) is second
    assert resolver.pick(build_locator({"name": "Tips", "index": 3})) is None
    only_hidden = Resolver(window(FakeControl("Tips", offscreen=True)))
    assert only_hidden.pick(build_locator("Tips"), "prefer") is not None   # actions may scroll to it
    assert only_hidden.pick(build_locator("Tips"), "only") is None         # assertions are about the screen


def test_within_scopes_the_search():
    in_a = FakeControl("Close")
    in_b = FakeControl("Close")
    panel_a = FakeControl("Panel A", "Pane", children=[in_a])
    panel_b = FakeControl("Panel B", "Pane", children=[in_b])
    resolver = Resolver(window(panel_a, panel_b))
    assert resolver.pick(build_locator({"name": "Close", "within": {"name": "Panel B"}})) is in_b
    assert resolver.pick(build_locator({"name": "Close", "within": "Nope"})) is None


def test_wait_polls_until_the_element_appears():
    root = window()
    clock = {"t": 0.0}

    def sleep(seconds):
        clock["t"] += seconds
        if clock["t"] >= 1.0 and not root.children:
            root.children.append(FakeControl("Готово"))   # the panel shows up a second later

    resolver = Resolver(root, sleep=sleep, clock=lambda: clock["t"])
    assert resolver.wait(build_locator("готово"), timeout=5).Name == "Готово"
    assert 1.0 <= clock["t"] < 2.0


def test_not_found_lists_similar_names_on_screen():
    root = window(FakeControl("Применено"), FakeControl("Выбранное отведение"), FakeControl("OK"))
    clock = {"t": 0.0}
    resolver = Resolver(root, sleep=lambda s: clock.update(t=clock["t"] + s), clock=lambda: clock["t"])
    with pytest.raises(ElementNotFound) as err:
        resolver.wait(build_locator({"name": "Применить", "type": "Button"}), timeout=1)
    text = str(err.value)
    assert "Button \"Применить\"" in text and "after 1s" in text
    assert 'Similar on screen: Button "Применено"' in text


def test_bad_locators():
    with pytest.raises(LocatorError, match="at least one of"):
        build_locator({"index": 2})
    with pytest.raises(LocatorError, match="unknown key"):
        build_locator({"label": "x"})


# -- running steps -----------------------------------------------------------------
class Recorder:
    def __init__(self):
        self.steps = []

    def log_step(self, n, action, args, reason, outcome, ok, obs):
        self.steps.append((n, action, outcome, ok))


class FakeVisual:
    """Stands in for OCR: returns the next scripted reading and remembers how it was asked."""

    def __init__(self, *readings):
        self.readings, self.calls = list(readings), []

    def read_text(self, bbox=None, **options):
        self.calls.append((bbox, options))
        reading = self.readings.pop(0) if len(self.readings) > 1 else self.readings[0]
        return reading.split()


def _runner(root, visual=None, **kw):
    clock = {"t": 0.0}
    resolver = Resolver(root, poll=0.5, sleep=lambda s: clock.update(t=clock["t"] + s), clock=lambda: clock["t"])
    executor = ActionExecutor(root, default_wait=0)
    if visual is not None:
        executor.visual = visual
    return ScriptRunner(root, executor=executor, resolver=resolver, screenshots=False, verbose=False,
                        sleep=resolver.sleep, clock=resolver.clock, step_timeout=2, **kw)


def test_a_scripted_case_runs_and_passes():
    root, apply, status, name, show = _app()
    recorder = Recorder()
    steps = parse_steps([
        {"click": "Применить"},
        {"assert_value": {"id": "Status", "contains": "ПРИМЕНЕНО", "ignore_case": True}},
        {"type_text": {"name": "Имя", "text": "Иванов"}},
        {"set_toggle": {"name": "Показать отведение", "state": "on"}},
        {"assert_state": {"name": "Показать отведение", "checked": True, "enabled": True}},
        {"assert_exists": {"type": "Text", "name_contains": "Статус"}},
        {"assert_title": "Demo"},
    ])
    result = _runner(root, reporter=recorder).run(steps)
    assert result.status == "passed", result.summary
    assert apply.clicks == 1 and name.value == "Иванов" and show.toggle == 1
    assert [s[1] for s in recorder.steps] == [s.action for s in steps] and all(s[3] for s in recorder.steps)
    assert recorder.steps[0][2] == 'Clicked "Применить".'      # the outcome names the element, not 'e1'


def test_a_failed_assertion_stops_the_run_with_verdict_failed():
    root, *_ = _app()
    recorder = Recorder()
    steps = parse_steps([{"assert_value": {"id": "Status", "equals": "Статус: применено"}},
                         {"click": "Применить"}])
    result = _runner(root, reporter=recorder).run(steps)
    assert result.status == "failed" and result.steps == 1 and len(recorder.steps) == 1
    assert "reads 'Статус: ожидание'" in result.summary and "equals 'Статус: применено'" in result.summary


def test_a_missing_element_blocks_a_click_but_fails_an_assertion():
    root, *_ = _app()
    click = _runner(root).run(parse_steps([{"click": "Отменить"}]))
    assert click.status == "blocked" and "no element matching" in click.summary
    assert 'Similar on screen' in click.summary
    assert _runner(root).run(parse_steps([{"assert_state": {"name": "Отменить", "enabled": True}}])).status == "failed"
    assert _runner(root).run(parse_steps([{"assert_exists": "Отменить"}])).status == "failed"


def test_assert_missing_waits_for_an_element_to_go():
    root, apply, *_ = _app()
    apply._on_click = lambda _: root.children.remove(apply)
    result = _runner(root).run(parse_steps([{"click": "Применить"}, {"assert_missing": "Применить"}]))
    assert result.status == "passed"
    assert _runner(window(FakeControl("Stuck"))).run(parse_steps([{"assert_missing": "Stuck"}])).status == "failed"


def test_state_assertions_report_what_was_found():
    root, *_ = _app()
    wrong = _runner(root).run(parse_steps([{"assert_state": {"name": "Показать отведение", "checked": True}}]))
    assert wrong.status == "failed" and "checked is False, expected True" in wrong.summary
    unsupported = _runner(root).run(parse_steps([{"assert_state": {"name": "Применить", "selected": True}}]))
    assert unsupported.status == "failed" and "does not expose a selected state" in unsupported.summary


def test_assertions_wait_for_the_screen_to_catch_up():
    root, apply, status, *_ = _app()
    ticks = {"n": 0}
    runner = _runner(root)
    real_sleep = runner.sleep

    def slow(seconds):
        real_sleep(seconds)
        ticks["n"] += 1
        if ticks["n"] == 2:
            status.Name = "Статус: применено"          # the UI updates 2 polls later

    runner.sleep = slow
    result = runner.run(parse_steps([{"assert_value": {"id": "Status", "contains": "применено"}}]))
    assert result.status == "passed" and ticks["n"] == 2


# -- OCR assertions ----------------------------------------------------------------
@pytest.mark.parametrize("blob, args, expected", [
    ("II aVL", {"text": "II"}, True),
    ("III", {"text": "II"}, False),                      # whole words: II is not inside III
    ("II", {"text": "ii"}, False),                       # case matters unless asked
    ("II", {"text": "ii", "ignore_case": True}, True),
    ("lead (II)", {"text": "II"}, True),
    ("Вся область отведения", {"text": "область отведения"}, True),
    ("Вся областьотведения", {"text": "область отведения"}, False),
    ("aVLx", {"text": "aVL"}, False),
    ("aVLx", {"text": "aVL", "match": "contains"}, True),
    ("II 35 bpm", {"regex": r"\d+ bpm"}, True),
])
def test_ocr_matching(blob, args, expected):
    assert ocr_matches(blob, args) is expected


def test_ocr_assertions_read_the_given_area_with_ocr_friendly_defaults():
    canvas = FakeControl("Ритм", "Pane", rect=(100, 200, 500, 400))
    root = window(canvas, rect=(0, 0, 1000, 600))
    visual = FakeVisual("aVL II 60")
    result = _runner(root, visual).run(parse_steps([
        {"assert_ocr": {"text": "II", "in": {"name": "Ритм"}}},
        {"assert_no_ocr": {"text": "III", "region": [0.1, 0.5, 0.5, 1.0]}},
        {"assert_ocr": {"text": "II"}},
    ]))
    assert result.status == "passed", result.summary
    in_canvas, in_region, whole = visual.calls[0:3], visual.calls[3:6], visual.calls[6:]
    assert [c[0] for c in in_canvas] == [(100, 200, 500, 400)] * 3        # the element's rectangle
    assert [c[1]["scale"] for c in in_canvas] == [2, 3, 4]                # a small label is read at several sizes
    assert all(c[1]["psm"] == 11 for c in in_canvas)                      # sparse text: a label on a canvas
    assert [c[0] for c in in_region] == [(100, 300, 500, 600)] * 3        # fractions of the 1000x600 window
    assert len(whole) == 1 and whole[0][0] == (0, 0, 1000, 600)           # no area: one read of the window
    assert whole[0][1]["scale"] == 1 and whole[0][1]["psm"] is None


def test_ocr_reads_at_several_scales_and_combines_the_words():
    # a lone II is read as Il, i or II depending on the size; any one right reading is enough
    visual = FakeVisual("Il", "i", "II")
    root = window(FakeControl("Ритм", "Pane"))
    result = _runner(root, visual).run(parse_steps([{"assert_ocr": {"text": "II", "in": "Ритм"}}]))
    assert result.status == "passed" and len(visual.calls) == 3


@pytest.mark.parametrize("blob, text, lenient, expected", [
    ("Il", "II", True, True),            # I and l look alike
    ("1l", "II", True, True),
    ("||", "II", True, True),
    ("III", "II", True, False),          # but II is still not III
    ("Ill", "III", True, True),
    ("Il", "II", False, False),          # strict by default
    ("aVL", "II", True, False),
    ("6O bpm", "60 bpm", True, True),    # O / 0
    ("S0", "50", True, True),
])
def test_lenient_matching_folds_look_alike_glyphs(blob, text, lenient, expected):
    assert ocr_matches(blob, {"text": text, "lenient": lenient}) is expected


def test_lenient_and_scale_validation():
    with pytest.raises(ScriptError, match="'lenient' applies to 'text'"):
        parse_steps([{"assert_ocr": {"regex": "I+", "lenient": True}}])
    assert parse_steps([{"assert_ocr": {"text": "II", "scale": [2, 4]}}])[0].args["scale"] == [2, 4]
    assert parse_steps([{"assert_ocr": {"text": "II", "scale": 3}}])[0].args["scale"] == [3]
    for bad in (9, [], [2] * 6, "big"):
        with pytest.raises(ScriptError, match="'scale' must be a number from 0.5 to 8"):
            parse_steps([{"assert_ocr": {"text": "II", "scale": bad}}])


def test_ocr_failures_show_what_was_read():
    root = window(FakeControl("Ритм", "Pane"))
    result = _runner(root, FakeVisual("aVL 60 bpm")).run(parse_steps([{"assert_ocr": {"text": "II"}}]))
    assert result.status == "failed" and "'II' not found" in result.summary and "OCR read: aVL 60 bpm" in result.summary
    result = _runner(root, FakeVisual("II aVL")).run(parse_steps([{"assert_no_ocr": "aVL"}]))
    assert result.status == "failed" and "'aVL' still present" in result.summary


def test_ocr_assertion_retries_until_the_canvas_redraws():
    visual = FakeVisual("aVL", "aVL", "II")                       # the canvas repaints on the 3rd look
    result = _runner(window(), visual).run(parse_steps([{"assert_ocr": "II"}]))
    assert result.status == "passed" and len(visual.calls) == 3


def test_ocr_options_are_passed_through():
    visual = FakeVisual("II")
    parse = parse_steps([{"assert_ocr": {"text": "II", "region": [0, 0, 1, 1], "scale": 2, "psm": 11,
                                          "min_conf": 10, "invert": True}}])
    _runner(window(), visual).run(parse)
    assert visual.calls[0][1] == {"scale": 2, "psm": 11, "min_conf": 10, "invert": True}


def test_ocr_area_that_is_not_there_fails_the_assertion():
    result = _runner(window(), FakeVisual("II")).run(parse_steps([{"assert_ocr": {"text": "II", "in": "Canvas"}}]))
    assert result.status == "failed" and "no element matching" in result.summary


def test_missing_ocr_engine_blocks_instead_of_crashing():
    class NoOcr:
        def read_text(self, *a, **k):
            raise RuntimeError("No OCR backend available.")

    result = _runner(window(), NoOcr()).run(parse_steps([{"assert_ocr": "II"}]))
    assert result.status == "blocked" and "No OCR backend" in result.summary


def test_screenshots_are_taken_per_step_and_a_failing_shot_is_harmless():
    root, *_ = _app()
    recorder = Recorder()
    shots = []

    def shooter():
        shots.append(1)
        raise OSError("no display")

    runner = _runner(root, reporter=recorder)
    runner.screenshots, runner._shooter = True, shooter
    assert runner.run(parse_steps([{"click": "Применить"}, {"assert_exists": "Применить"}])).status == "passed"
    assert len(shots) == 2
