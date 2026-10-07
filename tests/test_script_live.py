"""Scripted steps against a REAL window: Cyrillic names through UI Automation, text on a canvas through OCR.

Opt-in, because it drives the actual desktop (a small topmost window appears, the mouse moves and
clicks, the screen is captured) and needs Tesseract:

    set APP_CLICKER_DESKTOP_TESTS=1
    python -m pytest tests/test_script_live.py

The window is tests/fixtures/winforms_demo.ps1: a WinForms form with Cyrillic control names and a
custom-painted canvas that shows "aVL" until Apply is clicked and "II" afterwards - the same shape
as the lead-highlight check this feature was built for.
"""

import os
import subprocess
import time
from pathlib import Path

import pytest

from app_clicker import visual
from app_clicker.app_target import find_window
from app_clicker.cli import _set_dpi_aware, main

pytestmark = [
    pytest.mark.skipif(os.environ.get("APP_CLICKER_DESKTOP_TESTS") != "1",
                       reason="drives the real desktop; set APP_CLICKER_DESKTOP_TESTS=1 to run"),
    pytest.mark.skipif(visual.active_engine() is None, reason="needs an OCR backend (Tesseract)"),
]

FIXTURE = Path(__file__).parent / "fixtures" / "winforms_demo.ps1"
TITLE = "AppClicker Demo"


@pytest.fixture
def demo_window():
    _set_dpi_aware()
    proc = subprocess.Popen(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(FIXTURE)])
    try:
        find_window(title=TITLE, timeout=40)
        time.sleep(1.0)  # let the canvas paint
        yield proc
    finally:
        proc.kill()


def _run(capsys, tmp_path, *argv):
    rc = main(["--attach", "--window-title", TITLE, "--out", str(tmp_path / "reports"),
               "--step-timeout", "6", *argv])
    return rc, capsys.readouterr().out


def test_cyrillic_names_and_canvas_text_end_to_end(demo_window, capsys, tmp_path):
    tasks = tmp_path / "checks.yaml"
    tasks.write_text("""
tests:
  - name: Применить подсвечивает отведение II
    steps:
      - assert_ocr: {text: aVL, in: {name: "Ритм"}}                  # before: the wrong lead
      - assert_no_ocr: {text: II, lenient: true, in: {name: "Ритм"}}
      - type_text: {name: "Имя пациента", text: "Иванов"}
      - set_toggle: {name: "Показать отведение", state: "on"}
      - assert_state: {name: "Показать отведение", checked: true}
      - assert_value: {id: PatientName, equals: "Иванов"}
      - click: {type: Button, name: "Применить"}
      - assert_value: {id: StatusLabel, equals: "Статус: применено"}
      - assert_ocr: {text: II, lenient: true, in: {name: "Ритм"}}    # after: the edited lead
      - assert_no_ocr: {text: aVL, in: {name: "Ритм"}}
""", encoding="utf-8")
    rc, out = _run(capsys, tmp_path, "--tasks", str(tasks))
    assert rc == 0, out
    assert "Verdict: PASSED" in out and "Scripted run: no model used" in out
    shots = list((tmp_path / "reports").glob("*/screenshots/step*.png"))
    assert len(shots) == 10 and all(p.stat().st_size > 1000 for p in shots)


def test_a_check_that_does_not_hold_fails_with_what_ocr_read(demo_window, capsys, tmp_path):
    tasks = tmp_path / "checks.yaml"
    tasks.write_text("tests:\n  - name: Too early\n    steps:\n"
                     "      - assert_ocr: {text: II, lenient: true, in: {name: \"Ритм\"}, timeout: 2}\n",
                     encoding="utf-8")
    rc, out = _run(capsys, tmp_path, "--tasks", str(tasks), "--no-screenshots")
    assert rc == 1 and "Verdict: FAILED" in out
    assert "'II' not found in \"Ритм\". OCR read: aVL" in out          # what the canvas really showed


def test_assert_ocr_flag_checks_a_running_window(demo_window, capsys, tmp_path):
    rc, out = _run(capsys, tmp_path, "--no-screenshots", "--assert-ocr", "aVL", "--ocr-region", "0,0.2,1,1")
    assert rc == 0, out
    rc, _ = _run(capsys, tmp_path, "--no-screenshots", "--assert-ocr", "II", "--step-timeout", "2")
    assert rc == 1


def test_dump_tree_lists_the_names_a_script_can_use(demo_window, capsys):
    rc = main(["--dump-tree", "--window-title", TITLE])
    out = capsys.readouterr().out
    assert rc == 0
    assert 'Button "Применить" #ApplyButton' in out
    assert 'CheckBox "Показать отведение" #ShowLead' in out
    assert 'Pane "Ритм" #RhythmCanvas' in out
