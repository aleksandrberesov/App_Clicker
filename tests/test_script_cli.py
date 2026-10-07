"""Scripted cases through the command line: no model, config errors first, --assert-ocr, --dump-tree."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from fake_uia import demo_app  # noqa: E402

from app_clicker import cli, visual  # noqa: E402
from app_clicker.cli import EXIT_CONFIG, ConfigError, main  # noqa: E402


@pytest.fixture(autouse=True)
def desktop_stand_in(monkeypatch):
    """A fake desktop window, no real waiting, and a guard that no model is built unless asked."""
    root, apply, status, *_ = demo_app()
    monkeypatch.setattr(cli, "find_window", lambda **kw: root)
    monkeypatch.setattr("app_clicker.actions.time.sleep", lambda s: None)
    monkeypatch.setattr(visual, "active_engine", lambda: "tesseract")
    monkeypatch.setattr(cli, "build_backend_chain",
                        lambda a: pytest.fail("a scripted run must not build a model chain"))
    return root


def _tasks(tmp_path, text):
    path = tmp_path / "checks.yaml"
    path.write_text(text, encoding="utf-8")
    return str(path)


def _run(capsys, tmp_path, *argv):
    rc = main(["--attach", "--window-title", "Demo", "--out", str(tmp_path / "reports"),
               "--no-screenshots", "--step-timeout", "0.2", *argv])
    return rc, capsys.readouterr().out


PASSING = """
tests:
  - name: Применение настроек
    steps:
      - click: "Применить"
      - assert_value: {id: Status, contains: "применено"}
      - assert_exists: {type: CheckBox, name: "Показать отведение"}
"""


def test_scripted_run_needs_no_model_and_writes_a_report(capsys, tmp_path):
    rc, out = _run(capsys, tmp_path, "--tasks", _tasks(tmp_path, PASSING))
    assert rc == 0
    assert "Verdict: PASSED" in out and "Scripted run: no model used" in out
    assert "PASSED     Применение настроек" in out
    report = next((tmp_path / "reports").glob("*/report.md")).read_text(encoding="utf-8")
    assert "assert_value" in report and "Применить" in report and "PASSED" in report


def test_failed_assertion_exits_1(capsys, tmp_path):
    rc, out = _run(capsys, tmp_path, "--tasks", _tasks(tmp_path, """
tests:
  - name: Wrong expectation
    steps:
      - assert_value: {id: Status, equals: "Статус: применено"}
"""))
    assert rc == 1 and "Verdict: FAILED" in out and "FAILED     Wrong expectation" in out


def test_blocked_step_exits_1(capsys, tmp_path):
    rc, out = _run(capsys, tmp_path, "--tasks", _tasks(tmp_path, """
tests:
  - name: Missing button
    steps:
      - click: "Отменить"
"""))
    assert rc == 1 and "Verdict: BLOCKED" in out and "Similar on screen" in out


def test_a_bad_script_is_a_config_error_before_the_app_is_touched(capsys, tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "find_window", lambda **kw: pytest.fail("the window must not be looked up"))
    rc, out = _run(capsys, tmp_path, "--tasks", _tasks(tmp_path, """
tests:
  - name: Bad
    steps:
      - click: "Применить"
      - clck: "Отмена"
"""))
    assert rc == EXIT_CONFIG
    assert out.strip().splitlines()[-1] == (
        "Error: case 'Bad', step 2 (clck): unknown action 'clck' (did you mean 'click'?)")


def test_a_case_is_either_a_task_or_steps(capsys, tmp_path):
    rc, out = _run(capsys, tmp_path, "--tasks", _tasks(tmp_path, """
tests:
  - name: Both
    task: do it
    steps: [{wait: 1}]
"""))
    assert rc == EXIT_CONFIG and "exactly one of 'task'" in out
    rc, out = _run(capsys, tmp_path, "--tasks", _tasks(tmp_path, "tests:\n  - name: Neither\n"))
    assert rc == EXIT_CONFIG and "exactly one of 'task'" in out


def test_a_suite_with_a_model_case_still_builds_the_chain(capsys, tmp_path, monkeypatch):
    def chain(args):
        raise ConfigError("chain needed")
    monkeypatch.setattr(cli, "build_backend_chain", chain)
    rc, out = _run(capsys, tmp_path, "--tasks", _tasks(tmp_path, PASSING + """
  - name: Explore
    task: Open the settings and look around.
"""))
    assert rc == EXIT_CONFIG and "chain needed" in out


def test_scripts_need_the_desktop_engine_and_an_ocr_backend(capsys, tmp_path, monkeypatch):
    path = _tasks(tmp_path, "tests:\n  - name: Canvas\n    steps:\n      - assert_ocr: II\n")
    rc, out = _run(capsys, tmp_path, "--tasks", path, "--engine", "recognizer")
    assert rc == EXIT_CONFIG and "need --engine uia" in out
    monkeypatch.setattr(visual, "active_engine", lambda: None)
    rc, out = _run(capsys, tmp_path, "--tasks", path)
    assert rc == EXIT_CONFIG and "need an OCR backend" in out
    # steps that never use OCR don't care
    assert _run(capsys, tmp_path, "--tasks", _tasks(tmp_path, PASSING))[0] == 0


# -- --assert-ocr -------------------------------------------------------------------
def test_assert_ocr_helper_reads_the_region_and_sets_the_exit_code(capsys, tmp_path, monkeypatch, desktop_stand_in):
    reads = []

    def read_text(self, bbox=None, **options):
        reads.append((bbox, options))
        return ["aVL", "II", "60"]

    monkeypatch.setattr(visual.VisualMatcher, "read_text", read_text)
    rc, out = _run(capsys, tmp_path, "--assert-ocr", "II", "--assert-ocr", "60",
                   "--ocr-region", "0.1,0.5,0.5,1")
    assert rc == 0 and "assert-ocr" in out
    assert reads[0][0] == (100, 300, 500, 600)           # fractions of the 1000x600 window
    assert [r[1]["scale"] for r in reads[:3]] == [2, 3, 4]   # small regions are enlarged, at several sizes
    assert reads[0][1]["psm"] == 11                          # sparse text, for labels on a canvas
    rc, out = _run(capsys, tmp_path, "--assert-ocr", "III")
    assert rc == 1 and "'III' not found" in out and "OCR read: aVL II 60" in out


def test_ocr_lenient_flag_tolerates_look_alike_glyphs(capsys, tmp_path, monkeypatch):
    monkeypatch.setattr(visual.VisualMatcher, "read_text", lambda self, bbox=None, **o: ["Il", "60"])
    assert _run(capsys, tmp_path, "--assert-ocr", "II", "--step-timeout", "0.1")[0] == 1   # strict: Il is not II
    assert _run(capsys, tmp_path, "--assert-ocr", "II", "--ocr-lenient")[0] == 0
    assert _run(capsys, tmp_path, "--assert-ocr", "III", "--ocr-lenient", "--step-timeout", "0.1")[0] == 1


@pytest.mark.parametrize("argv, fragment", [
    (["--ocr-region", "0,0,1,1"], "only apply together with --assert-ocr"),
    (["--ocr-lenient"], "only apply together with --assert-ocr"),
    (["--assert-ocr", "II", "--task", "x"], "runs on its own"),
    (["--assert-ocr", "II", "--ocr-region", "a,b"], "'left,top,right,bottom'"),
    (["--assert-ocr", "II", "--ocr-region", "0.9,0,0.1,1"], "left < right"),
])
def test_assert_ocr_option_errors(capsys, tmp_path, argv, fragment):
    rc, out = _run(capsys, tmp_path, *argv)
    assert rc == EXIT_CONFIG and fragment in out


# -- --dump-tree --------------------------------------------------------------------
def test_dump_tree_shows_what_a_script_can_name(capsys):
    rc = main(["--dump-tree", "--window-title", "Demo"])
    out = capsys.readouterr().out
    assert rc == 0
    assert 'Window: "Demo App"' in out
    assert 'Button "Применить" #Apply' in out and 'CheckBox "Показать отведение"' in out
    assert "do not use them" in out


def test_dump_tree_needs_a_way_to_pick_the_window(capsys):
    assert main(["--dump-tree"]) == EXIT_CONFIG
    assert "--window-title, --window-class or --pid" in capsys.readouterr().out
