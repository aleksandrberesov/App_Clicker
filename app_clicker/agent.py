"""The tester agent: the perceive -> decide -> act -> observe loop.

Backend-agnostic: it maintains a neutral transcript and delegates each model
call to an LLM backend (Anthropic or any OpenAI-compatible provider). A fresh
observation — UI tree + screenshot — is fed back as each action's result.

Target-agnostic too: by default it drives a Windows window through UIA, but a
different perceiver / executor / toolset can be injected (the web engine passes
Playwright-backed ones). The loop only needs ``observe()`` and ``dispatch()``.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .actions import ActionExecutor, ArgumentError
from .perception import Observation, Perceiver
from .tools import build_system, build_tools


# Malformed tool calls (missing element_id, ...) change nothing on screen, so up to this
# many per run are refunded from the step budget; after N in a row the chain rotates models.
MAX_FREE_ARG_ERRORS = 5
ARG_ERRORS_BEFORE_ROTATE = 2


@dataclass
class RunResult:
    status: str = "incomplete"       # passed | failed | blocked | incomplete
    summary: str = ""
    steps: int = 0
    notes: list[str] = field(default_factory=list)
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read: int = 0
    cache_write: int = 0


class TesterAgent:
    def __init__(
        self,
        chain,
        window=None,
        max_steps: int = 40,
        screenshots: bool = True,
        reporter=None,
        verbose: bool = True,
        visual: bool = False,
        *,
        perceiver=None,
        executor=None,
        tools: list | None = None,
        system: str | None = None,
        screen_label: str = "window",
    ):
        if window is None and (perceiver is None or executor is None):
            raise ValueError("Pass a window, or both a perceiver and an executor.")
        self.chain = chain
        self.max_steps = max_steps
        self.reporter = reporter
        self.verbose = verbose
        self.perceiver = perceiver or Perceiver(window, screenshots=screenshots)
        self.executor = executor or ActionExecutor(window)
        self.tools = tools if tools is not None else build_tools(visual)
        self.system = system if system is not None else build_system(visual)
        self.screen_label = screen_label  # "window" for desktop apps, "page" for web

    # -- text builders -----------------------------------------------------
    def _obs_text(self, obs: Observation, task: str | None = None) -> str:
        header = f"TASK:\n{task}\n\n" if task else ""
        return (
            f"{header}Current {self.screen_label}: \"{obs.window_title}\"\n\n"
            f"UI elements:\n{obs.tree_text}\n\n"
            "Decide the next single action, referencing elements by id."
        )

    def _result_text(self, outcome: str, obs: Observation) -> str:
        return (
            f"{outcome}\n\n"
            f"Updated {self.screen_label}: \"{obs.window_title}\"\n\n"
            f"UI elements:\n{obs.tree_text}"
        )

    def _log(self, msg: str) -> None:
        if self.verbose:
            print(msg, flush=True)

    def _complete(self, transcript):
        """Delegate to the backend chain (handles retries + free->paid switching)."""
        return self.chain.complete(self.system, transcript, self.tools, log=self._log)

    def _account(self, result: RunResult, usage: dict) -> None:
        result.input_tokens += usage.get("input", 0)
        result.output_tokens += usage.get("output", 0)
        result.cache_read += usage.get("cache_read", 0)
        result.cache_write += usage.get("cache_write", 0)

    # -- main loop ---------------------------------------------------------
    def run(self, task: str) -> RunResult:
        result = RunResult()
        obs = self.perceiver.observe()
        transcript = [{"role": "user", "text": self._obs_text(obs, task), "image": obs.screenshot}]
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
                    "text": (
                        "You did not call an action tool. Call exactly one action to "
                        "proceed, or call `finish` if the task is complete or blocked."
                    ),
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
                    "text": self._result_text("Noted.", obs), "image": obs.screenshot,
                    "is_error": False,
                })
                continue

            # A real UI action.
            self._log(f"[{step}] {name} {inp}")
            try:
                outcome = self.executor.dispatch(name, inp, obs.elements)
                ok = True
                arg_streak = 0
            except ArgumentError as e:
                outcome = f"ERROR: {e}"
                ok = False
                arg_streak += 1
                if free_left > 0:  # nothing happened on screen; don't burn a step
                    free_left -= 1
                    step -= 1
                if arg_streak >= ARG_ERRORS_BEFORE_ROTATE:
                    arg_streak = 0
                    rotate = getattr(self.chain, "rotate_model", None)
                    if rotate and rotate(self._log, "repeated invalid tool calls"):
                        outcome += " (switched to a different model after repeated invalid calls)"
            except Exception as e:  # ActionError or unexpected COM error
                outcome = f"ERROR: {e}"
                ok = False
            self._log(f"      -> {outcome}")

            obs = self.perceiver.observe()
            if self.reporter:
                self.reporter.log_step(step, name, inp, reason, outcome, ok, obs)

            transcript.append({
                "role": "tool", "tool_call_id": tc.id, "name": name,
                "text": self._result_text(outcome, obs), "image": obs.screenshot,
                "is_error": not ok,
            })
        else:
            result.status = "incomplete"
            result.summary = f"Reached the {self.max_steps}-step limit before finishing."
            result.steps = self.max_steps

        return result
