"""Agent loop for the 'recognizer' engine (Screen_Recognizer as perception).

Same perceive -> decide -> act -> observe loop as the UIA agent, but the model
gets a coordinate-free outline of Screen_Recognizer's full report (never a
screenshot), and actions target integer element ids that the executor maps to
exact screen pixels.
"""

from __future__ import annotations

from .actions import ActionError, ArgumentError
from .agent import ARG_ERRORS_BEFORE_ROTATE, MAX_FREE_ARG_ERRORS, RunResult
from .perception import Observation
from .recognizer_engine import (
    RECOGNIZER_SYSTEM,
    RECOGNIZER_TOOLS,
    RecognizerExecutor,
    RecognizerPerceiver,
    describe_element,
)
from .uidiff import UIChange, diff_snapshots


class RecognizerAgent:
    def __init__(self, chain, window, ocr_backend: str = "tesseract",
                 max_steps: int = 40, reporter=None, verbose: bool = True):
        self.chain = chain
        self.max_steps = max_steps
        self.reporter = reporter
        self.verbose = verbose
        self.perceiver = RecognizerPerceiver(window, ocr_backend=ocr_backend)
        self.executor = RecognizerExecutor(window)
        self.tools = RECOGNIZER_TOOLS
        self.system = RECOGNIZER_SYSTEM

    def _log(self, msg: str) -> None:
        if self.verbose:
            print(msg, flush=True)

    def _account(self, result: RunResult, usage: dict) -> None:
        result.input_tokens += usage.get("input", 0)
        result.output_tokens += usage.get("output", 0)
        result.cache_read += usage.get("cache_read", 0)
        result.cache_write += usage.get("cache_write", 0)

    def _obs_text(self, obs, task: str | None = None) -> str:
        header = f"TASK:\n{task}\n\n" if task else ""
        return (
            f"{header}Current window: \"{obs.window_title}\"\n\n"
            f"{obs.text}\n\nChoose the next action by element id."
        )

    def _result_text(self, outcome: str, obs, change: UIChange | None = None) -> str:
        shown = f"{change.render()}\n\n" if change is not None else ""
        return f"{outcome}\n\n{shown}Updated window: \"{obs.window_title}\"\n\n{obs.text}"

    def _report_obs(self, obs) -> Observation:
        # Reuse the reporter's screenshot handling: the annotated Set-of-Marks
        # image is the report's visual (not sent to the model). The full screen
        # report is saved alongside it for debugging and other tools.
        return Observation(window_title=obs.window_title, tree_text=obs.text,
                           elements={}, screenshot=obs.screenshot,
                           report_json=obs.report.to_json())

    def _complete(self, transcript):
        """Delegate to the backend chain (handles retries + free->paid switching)."""
        return self.chain.complete(self.system, transcript, self.tools, log=self._log)

    def run(self, task: str) -> RunResult:
        result = RunResult()
        obs = self.perceiver.observe()
        # No image ever goes to the model — this is the decision-engine contract.
        transcript = [{"role": "user", "text": self._obs_text(obs, task=task), "image": None}]
        nudges = 0

        step = 0
        free_left = MAX_FREE_ARG_ERRORS   # malformed calls we refund from the step budget
        arg_streak = 0
        while step < self.max_steps:
            step += 1
            try:
                turn = self._complete(transcript)
            except Exception as e:
                result.status = "blocked"
                result.summary = f"Model call failed and could not recover: {e}"
                result.steps = step - 1
                self._log(f"[{step}] model call failed: {e}")
                break
            self._account(result, turn.usage)

            tc = turn.tool_call
            transcript.append({
                "role": "assistant",
                "text": turn.text,
                "tool_call": ({"id": tc.id, "name": tc.name, "input": tc.input} if tc else None),
                "raw": turn.raw,
            })

            if tc is None:
                nudges += 1
                if nudges > 3:
                    result.status = "blocked"
                    result.summary = turn.text or "Model stopped without calling an action."
                    result.steps = step
                    break
                transcript.append({
                    "role": "user",
                    "text": ("You did not call an action tool. Call exactly one action to "
                             "proceed, or call `finish` if the task is complete or blocked."),
                    "image": None,
                })
                continue

            name, inp = tc.name, (tc.input or {})
            reason = inp.get("reason")

            if name == "finish":
                status = inp.get("status", "passed")
                summary = inp.get("summary", "")
                self._log(f"[{step}] FINISH ({status}): {summary}")
                if self.reporter:
                    self.reporter.log_finish(step, status, summary)
                result.status, result.summary, result.steps = status, summary, step
                break

            if name == "note":
                text = inp.get("text", "")
                self._log(f"[{step}] NOTE: {text}")
                result.notes.append(text)
                if self.reporter:
                    self.reporter.log_note(step, text)
                obs = self.perceiver.observe()
                transcript.append({
                    "role": "tool", "tool_call_id": tc.id, "name": name,
                    "text": self._result_text("Noted.", obs), "image": None, "is_error": False,
                })
                continue

            if name == "inspect_element":
                # Answered from the current report: no UI action and no re-read, so ids stay valid
                self._log(f"[{step}] INSPECT {inp.get('element_id')}")
                try:
                    details = describe_element(obs.report, inp.get("element_id"))
                    ok = True
                except ActionError as e:
                    details = f"ERROR: {e}"
                    ok = False
                transcript.append({
                    "role": "tool", "tool_call_id": tc.id, "name": name,
                    "text": f"{details}\n\n(The screen was not re-read; ids from the last outline are still valid.)",
                    "image": None, "is_error": not ok,
                })
                continue

            self._log(f"[{step}] {name} {inp}")
            before, ran = obs, True
            try:
                outcome = self.executor.dispatch(name, inp, obs.report)
                ok = True
                arg_streak = 0
            except ArgumentError as e:
                outcome = f"ERROR: {e}"
                ok = False
                ran = False  # malformed call: nothing happened, so there is no change to report
                arg_streak += 1
                if free_left > 0:  # nothing happened on screen; don't burn a step
                    free_left -= 1
                    step -= 1
                if arg_streak >= ARG_ERRORS_BEFORE_ROTATE:
                    arg_streak = 0
                    rotate = getattr(self.chain, "rotate_model", None)
                    if rotate and rotate(self._log, "repeated invalid tool calls"):
                        outcome += " (switched to a different model after repeated invalid calls)"
            except Exception as e:
                outcome = f"ERROR: {e}"
                ok = False
            self._log(f"      -> {outcome}")

            obs = self.perceiver.observe()
            change = (diff_snapshots(before.text, obs.text, before.window_title, obs.window_title)
                      if ran else None)
            if change is not None:
                self._log(f"      {change.headline()}")
            if self.reporter:
                self.reporter.log_step(step, name, inp, reason,
                                       f"{outcome} ({change.headline()})" if change else outcome,
                                       ok, self._report_obs(obs))

            transcript.append({
                "role": "tool", "tool_call_id": tc.id, "name": name,
                "text": self._result_text(outcome, obs, change), "image": None, "is_error": not ok,
            })
        else:
            result.status = "incomplete"
            result.summary = f"Reached the {self.max_steps}-step limit before finishing."
            result.steps = self.max_steps

        return result
