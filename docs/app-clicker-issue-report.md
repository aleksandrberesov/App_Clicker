# App Clicker — issue report (2026-10-07)

Context: verifying a one-line UI fix in CardioSimulator Win (ECG Constructor → Tips → «Вся область отведения»
must highlight the edited lead, not aVL). Two attempts through App Clicker, both ended `BLOCKED`
without reaching the assertion. Reports are under `E:\Automation_projects\App_Clicker\reports\`.

Environment: Windows 11, `--exe …\antiAI-ECG-Simulator.exe` (WinUI 3, Release/x64, Full edition), `--keep-open`.

## Observed failures

### 1. `--paid` fails hard with no key (20261007, first run)
- Output: `No usable model provider was available` / `anthropic: skipped (no ANTHROPIC_API_KEY)`.
- Expected: this is correct behaviour, but the message lists free-tier options first and the exit is a
  PowerShell `NativeCommandError` wall of text. Suggest a single clear line + non-zero exit code, and
  a pre-flight check (`--check-providers`) that reports which modes are usable before a run starts.

### 2. `--free`: upstream 429 aborts the run (report `20261007-110113_task`)
- Model `google/gemma-4-31b-it:free` returned 429 ("temporarily rate-limited upstream").
- Backoff retries (3s, 6s, 12s) were all spent on the same model; run ended `BLOCKED` at step 2 having done only a `wait`.
- Suggest: on repeated 429, fail over to the next live free slug (the `--list-free-models` set) instead of
  retrying the same one; only give up when the whole chain is exhausted.

### 3. `--free`: action schema violations, then crash on empty reply (report `20261007-110552_task`)
- Model `nvidia/nemotron-3-nano-omni-30b-a3b-reasoning:free`.
- Steps 4 and 6: model issued `click {x: 0.4701, y: 0.2821}` (normalised coordinates); the tool requires
  `element_id` → `ERROR: Missing required argument 'element_id'`. The model never recovered from this form.
- Step 7: `model call failed: 'NoneType' object is not subscriptable` — an unhandled `None` in the
  response parsing path (likely an empty/`None` `choices`/`message` from the provider, e.g. when the reasoning
  model spends the budget on reasoning tokens: out=4,013 tokens). The run is declared
  "could not recover" and terminates. This is a bug in the client, not a model verdict.
- Suggest:
  - Guard the response parse (`None` content/choices) and treat it as a retryable model failure, not fatal.
  - Either accept `x`/`y` clicks (mapped to screenshot size) or return an error that spells out the
    allowed forms and valid element ids, so weak models can correct themselves.
  - Do not count a tool-schema error as a consumed step toward the step budget.

### 4. Repeated clicking on the same control (same run)
- Step 3 clicked `e175` ("Tips"), step 5 clicked `e180` ("Tips button in the bottom toolbar") — the model
  apparently could not tell whether the first click had worked. The post-action result is only
  "Clicked e175", with no description of what changed.
- Suggest: after each action return a short diff of the UIA tree / visible text (new panel, new dialog,
  selection changes), so the model gets feedback beyond a screenshot.

## Gaps for this kind of check (WinUI 3 / Russian UI)
- Task text mixed English instructions with Russian control labels (`'Вся область отведения'`); the free
  models did not get past the first screen, so we could not tell whether label matching works for Cyrillic.
  Worth a smoke test of Cyrillic `name` matching against the UIA tree.
- The pass/fail criterion ("highlighted lead label is II, not aVL") is a rendered Win2D label drawn on a
  canvas — not in the UIA tree. App Clicker has OCR tools (tesseract on); a documented recipe for
  "assert text drawn on a canvas via OCR of a region" would help, plus a `--assert-ocr "II"` style helper.
- A free-text `--task` has no deterministic fallback. For regression gates we would like scripted steps
  (list of `click name=… / assert text=…`) executed without a model, with the model only used for
  exploration.

## Requests, in priority order
1. Fix the `NoneType` crash in model-call handling; make it retryable.
2. Fail over across free models on 429 and on repeated invalid actions.
3. Accept or clearly document coordinate clicks; improve the error text for invalid args.
4. Return a post-action UI diff to the model.
5. Add a `--check-providers` pre-flight and clearer no-key messaging.
6. Scripted (model-free) step mode with UIA + OCR assertions for gating checks.

## Repro
```powershell
cd E:\Automation_projects\App_Clicker; .\.venv\Scripts\Activate.ps1
python -m app_clicker --free `
  --exe "E:\VLN_Project\CardioSimulator\Win\artifacts\publish\antiAI-ECG-Simulator.exe" `
  --keep-open --task "Open the ECG Constructor. Select rhythm 35. Click Tips. Choose 'Вся область отведения'. Leave the lead dropdown empty. Click on the rhythm view. Assert highlighted lead label is II, not aVL."
```
Result varies by model; the two runs above are the evidence. Cause of item 3's `NoneType` error is a
hypothesis from the symptom — the App Clicker source was not inspected.
