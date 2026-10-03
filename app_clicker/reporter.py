"""Write a Markdown test report with per-step screenshots (and screen reports for the recognizer engine)."""

from __future__ import annotations

import datetime as _dt
import os
import re


def _slug(text: str) -> str:
    s = re.sub(r"[^A-Za-z0-9]+", "-", text).strip("-").lower()
    return s[:50] or "case"


class Reporter:
    def __init__(self, base_dir: str, case_name: str, task: str):
        ts = _dt.datetime.now().strftime("%Y%m%d-%H%M%S")
        self.dir = os.path.join(base_dir, f"{ts}_{_slug(case_name)}")
        self.shots_dir = os.path.join(self.dir, "screenshots")
        os.makedirs(self.shots_dir, exist_ok=True)
        self.case_name = case_name
        self.task = task
        self.started = _dt.datetime.now()
        self._entries: list[str] = []

    # -- screenshot helper -------------------------------------------------
    def _save_shot(self, step: int, obs) -> str | None:
        if not getattr(obs, "screenshot", None):
            return None
        fname = f"step{step:03d}.png"
        path = os.path.join(self.shots_dir, fname)
        with open(path, "wb") as f:
            f.write(obs.screenshot)
        return f"screenshots/{fname}"

    def _save_screen_report(self, step: int, obs) -> str | None:
        report_json = getattr(obs, "report_json", None)
        if not report_json:
            return None
        fname = f"step{step:03d}.json"
        reports_dir = os.path.join(self.dir, "screen_reports")
        os.makedirs(reports_dir, exist_ok=True)
        with open(os.path.join(reports_dir, fname), "w", encoding="utf-8") as f:
            f.write(report_json)
        return f"screen_reports/{fname}"

    # -- logging API -------------------------------------------------------
    def log_step(self, step, name, inp, reason, outcome, ok, obs) -> None:
        rel = self._save_shot(step, obs)
        report_rel = self._save_screen_report(step, obs)
        args = {k: v for k, v in (inp or {}).items() if k != "reason"}
        status = "✅" if ok else "⚠️"
        block = [f"### Step {step} — `{name}` {status}"]
        if reason:
            block.append(f"*{reason}*")
        block.append("")
        block.append(f"- **Action:** `{name}` {args}")
        block.append(f"- **Result:** {outcome}")
        if report_rel:
            block.append(f"- **Screen report:** [{os.path.basename(report_rel)}]({report_rel})")
        if rel:
            block.append("")
            block.append(f"![step {step}]({rel})")
        self._entries.append("\n".join(block))

    def log_note(self, step, text) -> None:
        self._entries.append(f"### Step {step} — 📝 Note\n\n> {text}")

    def log_finish(self, step, status, summary) -> None:
        self._entries.append(f"### Step {step} — 🏁 Finish: **{status.upper()}**\n\n{summary}")

    # -- finalize ----------------------------------------------------------
    def finalize(self, result) -> str:
        ended = _dt.datetime.now()
        emoji = {"passed": "✅", "failed": "❌", "blocked": "⛔", "incomplete": "⏱️"}
        head = [
            f"# Test report: {self.case_name}",
            "",
            f"- **Verdict:** {emoji.get(result.status, '❔')} **{result.status.upper()}**",
            f"- **Steps:** {result.steps}",
            f"- **Started:** {self.started:%Y-%m-%d %H:%M:%S}",
            f"- **Duration:** {(ended - self.started).total_seconds():.0f}s",
            "",
            "## Task",
            "",
            self.task,
            "",
            "## Summary",
            "",
            result.summary or "_(none)_",
            "",
            "## Steps",
            "",
        ]
        report = "\n".join(head) + "\n\n".join(self._entries) + "\n"
        path = os.path.join(self.dir, "report.md")
        with open(path, "w", encoding="utf-8") as f:
            f.write(report)
        return path
