# App Clicker

An AI-driven **manual tester for native Windows desktop apps and web apps**. You
give it a plain-English task (or a list of test cases); it launches the app (or
opens the page in a browser), reads the UI, and clicks / types / selects its way
through — verifying expected results and writing a screenshot-backed report.

It is essentially a *computer-use agent scoped to a single Windows application*:

```
launch/attach ─► perceive (UIA tree + screenshot) ─► Claude picks ONE action
      ▲                                                        │
      └──────────────  observe new state  ◄──────  execute action (UIA)
```

The loop repeats until Claude calls `finish` (passed / failed / blocked) or the
step cap is reached. Every step's reasoning, action, outcome and screenshot land
in a Markdown report under `reports/`.

## Perception engines

App Clicker has three ways to perceive the app, chosen with `--engine`:

| `--engine` | How it perceives | What the LLM sees | LLM role |
| --- | --- | --- | --- |
| `uia` (default) | Windows UI Automation tree + a screenshot | tagged element tree **and the screenshot** | reads pixels + tree, picks an element id |
| `recognizer` | [Screen_Recognizer](../Screen_Recognizer)'s full report — local OCR + OpenCV | a coordinate-free outline: elements nested by container, side-by-side rows, input labels; details on request via `inspect_element` — **no screenshot** | pure **decision engine**: picks an integer id; the tool maps it to exact pixels |
| `web` | the page's rendered DOM in a real browser, via Playwright + a screenshot | tagged element list with page text, states and events **and the screenshot** | picks an element id; Playwright acts on it — see [Web apps](#web-apps---engine-web) |

The **recognizer** engine keeps the model out of the pixels entirely — it never
sees a screenshot and never guesses coordinates, which eliminates coordinate
hallucination and cuts token cost sharply (no images in the prompt). It also
works on custom-drawn / web-view UIs the UIA tree can't expose. Trade-offs:
detection quality depends on the app's visual style (cleaner on standard forms,
noisier on dense dark IDEs), and local OCR is a few-to-~15s per step on a weak
CPU. The Set-of-Marks annotated screenshot (numbered badges) is saved in the
report so you can see what was detected.

Each step also saves Screen_Recognizer's full JSON report
(`screen_reports/stepNNN.json`: every element's container, row, label, shape,
colors and screen position) next to its screenshot. The model sees only a
compact outline built from it:

```
[20] container "Quick Actions"
  [8] text "Quick Actions"
  [10] button "Run Test Suite" | [11] button "Deploy Build"
```

When the outline isn't enough, such as checking that an error shows in red or
telling two similar buttons apart, the model calls `inspect_element` for that
element's shape, colors, border, OCR confidence and neighbors. This answers
from the current report without re-reading the screen.

Run the offline tests (no model calls, no real clicks):

```bash
python tests/test_recognizer_engine.py
```

Setup for the recognizer engine (one-time):

```bash
E:\Automation_projects\App_Clicker\.venv\Scripts\python.exe -m pip install -e E:\Automation_projects\Screen_Recognizer --no-deps
```

Run it:

```bash
python -m app_clicker --engine recognizer --paid --exe "...\app.exe" --task "..."
```

`--ocr-backend tesseract` (default) is fastest on CPU; `rapidocr` and `auto` are
also available. Everything else (`--free`/`--paid`, prompt caching, reports,
multi-case suites) works the same across all engines.

## Web apps (`--engine web`)

The web engine drives a web app in a real browser through
[Playwright](https://playwright.dev/python/). It runs the same loop, reports and
model chain as the desktop engines; only perception and actions differ.

Setup (one-time; the version matches the Chromium build already downloaded on
this machine, so no browser download is needed):

```bash
E:\Automation_projects\App_Clicker\.venv\Scripts\python.exe -m pip install playwright==1.62.0
```

On a machine without Playwright's browsers, also run `playwright install chromium`,
or pass `--browser msedge` to use the installed Edge.

Run it:

```bash
python -m app_clicker --engine web --url https://app.example.com/login --task "Sign in as demo and verify the dashboard greets the user"
```

A suite against the bundled demo page:

```bash
python -m app_clicker --engine web --headless --url file:///E:/Automation_projects/App_Clicker/tests/fixtures/web_demo.html --tasks tasks/web_demo_tasks.yaml
```

| Option | Meaning |
| --- | --- |
| `--url URL` | Page opened at the start of each case. |
| `--browser` | `chromium` (default, Playwright's build), `msedge` / `chrome` (installed browser), `firefox`, `webkit`. |
| `--headless` | No browser window. |
| `--storage-state PATH` | Start each case logged in, from a Playwright storage-state JSON (cookies + local storage). |
| `--cdp URL` | Attach to a browser you already run, and use its profile: start Edge/Chrome with `--remote-debugging-port=9222 --user-data-dir=<separate dir>` (Chrome 136+ refuses remote debugging on the default profile), then pass `--cdp http://localhost:9222`. With `--url` it opens its own tab; otherwise it uses the visible one. |
| `--keep-open` | Keep the same tab and its state across cases. Without it, each case starts in a fresh browser context (clean cookies and storage). |

**What the model sees.** A script walks the rendered DOM of every visible frame
(open shadow roots included) and lists interactive elements with ids, plus the
page text that gives them context — headings, paragraphs, table rows, alerts,
dialogs. The page title and URL, events since the last step, open tabs and scroll
position come first:

```
Events since the last step:
  - A confirm dialog appeared: "Delete order #1042?". It was dismissed.
Page scroll: 0% (the page is 2.8 screens tall)

heading "Welcome, demo (Editor)"
row "#1042 | Pending | Delete"
[e1] button "Delete" (covered by div#overlay)
[e6] scroll-area (scrolled 0%)
  text "List item 1"
  ... 24 more lines inside; scroll this area to see them
[e8] button "Bottom button" (offscreen)
dialog "Terms"
  [e9] button "Close"
frame "Framed widget":
  [e10] button "Framed button"
```

Fields show `value="..."` (passwords masked) and native drop-downs show
`options=[...]`. The states are `checked`, `selected`, `expanded`, `disabled`,
`required`, `invalid`, `focused`, `offscreen`, and `covered by X` (a modal or
overlay sits on top, so a click would fail). Ids are written to a `data-ac-id`
attribute and reassigned every step, so each action is a Playwright locator
with its usual waiting and actionability checks.

**Tools:** `click`, `double_click`, `right_click`, `hover`, `type_text`,
`select_option` (native drop-downs), `set_toggle`, `scroll` (a scroll area or the
page), `scroll_into_view`, `press_keys` (Playwright key names: `Enter`,
`Control+A`, ...), `navigate`, `go_back`, `switch_tab`, `set_dialog_response`,
`assert_text` (checks the DOM, no OCR), `click_at` (canvas fallback), `wait`,
`note`, `finish`.

**Browser behaviour to know:**
- JavaScript `confirm` / `prompt` dialogs are dismissed unless the model calls
  `set_dialog_response` first. Alerts are accepted. Every dialog is reported to
  the model.
- A link that opens a new tab makes that tab active, and the model is told.
- Uncaught script errors and console errors are reported as events, so the
  model can flag them as defects.
- Action timeout is 5s, so a click on a covered or disabled element fails fast
  with the reason.

Run the browser tests (a headless Chromium against
`tests/fixtures/web_demo.html`; no network, no model calls):

```bash
python tests/test_web_engine.py
```

App_Tester can't record web runs yet: its replay selectors are built from the
UIA tree.

## Requirements

- **Windows** (uses Microsoft UI Automation via `uiautomation`).
- **Python 3.10+**.
- An **Anthropic API key**, or a free provider key (see below).
- For `--engine web`: **Playwright** (`pip install playwright==1.62.0`).

## Install

```bash
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
copy .env.example .env   # then put your key in .env
```

## Usage

Single task, launching an app:

```bash
python -m app_clicker --exe "C:\Windows\System32\calc.exe" --window-title "Calculator" --task "Compute 7 x 8 and verify the result is 56."
```

Attach to an already-open window instead of launching:

```bash
python -m app_clicker --attach --window-title "Notepad" --task "Type 'hello world', then verify it appears in the document."
```

Run a suite of test cases from YAML:

```bash
python -m app_clicker --exe "C:\Windows\System32\calc.exe" --window-title "Calculator" --tasks tasks/example_tasks.yaml
```

### Key options

| Option | Meaning |
| --- | --- |
| `--exe PATH` | Executable to launch. |
| `--attach` | Attach to a running window (with `--window-title` / `--pid`). |
| `--window-title TEXT` | Match the window by title substring (recommended — a launched app's real window may run under a different pid). |
| `--pid N` | Attach to a specific process id. |
| `--task "..."` / `--tasks file.yaml` | One task, or a suite. |
| `--max-steps N` | Action budget per case (default 40). |
| `--no-screenshots` | Send only the UI tree (cheaper; **required** for text-only models). |
| `--provider` | `anthropic` (default) or an OpenAI-compatible provider (see below). |
| `--model` | Model id. For `openrouter`, `auto` (default) discovers a live free model. |
| `--list-free-models` | Print the free tool-capable OpenRouter models available right now, then exit. |
| `--base-url` | Override the OpenAI-compatible endpoint (for a custom/self-hosted one). |
| `--keep-open` | Don't close a launched app between/after cases. |

At the end of each case it prints a **token/cost meter** (dollar estimate for
Anthropic models; "free / unknown" otherwise), including cached-token counts.

**Prompt caching** is on by default for the Anthropic backend: the stable
`tools`+`system` prefix is cached, and a rolling cache breakpoint on the newest
message each turn lets every later turn read the whole prior transcript from
cache (~0.1×) instead of re-paying for every accumulated screenshot. In practice
this cuts a multi-step paid run by ~70–80% (e.g. a ~1M-token run: ~$5.20 → ~$1.20
on Opus). Disable with `--no-cache`. OpenAI-compatible providers cache
automatically where they support it.

## Free by default, paid fallback

The default is `--provider auto`: it builds a **free-first, paid-last chain** and
uses the first provider that works. If a free provider is unavailable for any
reason — daily cap hit, no live model, missing key — it automatically moves to
the next, ending at paid. It can even switch **mid-run** if a free provider dies
partway through (the transcript is provider-neutral, so switching is seamless).

Priority order:

1. **Free** — `openrouter` (auto-discovers a live free model), then `groq`,
   `gemini`, `github` (each used only if its key is set).
2. **Paid** — `anthropic` (`--paid-model`, default `claude-opus-5`; caching on).

```bash
python -m app_clicker --exe "...\app.exe" --task "..."                    # auto: free, else paid
python -m app_clicker --free --exe "...\app.exe" --task "..."             # free tier only
python -m app_clicker --paid --exe "...\app.exe" --task "..."             # paid only
python -m app_clicker --paid --paid-model claude-sonnet-5 --exe "..." --task "..."  # cheaper paid
```

Put whichever keys you have in `.env` (see `.env.example`): `OPENROUTER_API_KEY`
for the widest free option, plus optionally `GROQ_API_KEY` / `GEMINI_API_KEY` /
`GITHUB_TOKEN`, and `ANTHROPIC_API_KEY` for the paid fallback. The run prints the
chain it built and which provider it ended up using. To pin one provider/model,
use `--provider <name> --model <id>`.

## Providers (incl. free options)

The tool speaks two backends: **Anthropic** and any **OpenAI-compatible**
endpoint. The latter unlocks free tiers behind one flag:

| `--provider` | Default model | Vision | Free? | Key env var |
| --- | --- | --- | --- | --- |
| `anthropic` | `claude-opus-5` | yes | no (paid) | `ANTHROPIC_API_KEY` |
| `openrouter` | `auto` (discovers a live free model) | yes | free tiers, rate-limited | `OPENROUTER_API_KEY` |
| `groq` | `llama-3.3-70b-versatile` | **no → use `--no-screenshots`** | free, fast | `GROQ_API_KEY` |
| `gemini` | `gemini-2.0-flash` | yes | free tier* | `GEMINI_API_KEY` |
| `github` | `gpt-4o-mini` | yes | free tier, rate-limited | `GITHUB_TOKEN` |
| `ollama` | `qwen2.5vl` | yes | free/local | *(none)* |
| `openai` | *(set `--model`)* | — | no | `OPENAI_API_KEY` |

\* Hosted free tiers generally **use your inputs for training**, and this tool
screenshots the app. Prefer test apps/clean screens, or the tree-only path.

**Free models change constantly.** OpenRouter's `:free` slugs get retired or turned
paid without notice, so don't hardcode one. With `--provider openrouter` and no
`--model`, the tool **auto-selects a live free model**: it queries OpenRouter's model
list, filters to free + tool-capable (+ vision unless `--no-screenshots`), and probes
candidates so it picks one that responds *right now* (skipping 404/403/rate-limited
ones). List today's options with `--list-free-models`; pin one with `--model <id>`.

Examples:

```bash
# OpenRouter, auto-select a live free model
python -m app_clicker --provider openrouter --exe "C:\Windows\System32\notepad.exe" --window-title "Notepad" --task "Type 'hi' and verify it appears."
```

```bash
# See which free models work right now
python -m app_clicker --provider openrouter --list-free-models
```

```bash
# Groq (text-only, very fast) — tree-only mode
python -m app_clicker --provider groq --no-screenshots --attach --window-title "Notepad" --task "Type 'hi' and verify it appears."
```

**Caveats for small/free models:** not every free model supports tool use or
vision; agentic reliability is lower than Opus (more malformed/missing tool
calls, which the loop retries). If a model ignores tools, pick a
tool-use-capable one, or a bigger model.

## What the model can do each step

UIA-based: `click`, `double_click`, `right_click`, `type_text`, `select_item`,
`set_toggle`, `expand_collapse`, `scroll`, `scroll_into_view`, `press_keys`,
`wait`, `note` (record a verification), and `finish` (verdict + summary).

Optional OCR-based (see below): `click_text`, `assert_text`, `click_at`.

After every action the model gets the new screen **plus a summary of what changed**
since the previous one — elements that appeared (with their new ids), disappeared, or
changed state/value, and how many scrolled in or out of view — so it can tell whether a
click did anything instead of repeating it:

```
Changes since the previous screen (+3 added):
  + [e3] Pane "Tips"
  +   [e4] RadioButton "Вся область отведения"
```

`UI tree unchanged after this action` means the element tree is identical; the action may
have had no effect, or it only changed drawn content (a canvas, say) that has no element
of its own. The one-line headline (`UI changes: +3 added`) is also printed to the console
and added to each step in the report. (`uidiff.py`.)

## Visual (OCR) tools — for content UIA can't see

Web views, embedded browsers, and custom-drawn canvases render as pixels with no
accessibility ids. When enabled, the agent gets three extra tools:

- **`click_text`** — click on-screen text located by OCR,
- **`assert_text`** — deterministically check that expected text is visible,
- **`click_at`** — click a normalized 0-1000 window position (last resort).

They auto-enable when an OCR backend is installed (disable with `--no-visual`).
Two backends, auto-selected:

| Backend | Install | Speed on CPU |
| --- | --- | --- |
| **Tesseract** (preferred) | `pip install pytesseract` + `winget install UB-Mannheim.TesseractOCR` | fast (~a few sec on CPU; groups words into lines so multi-word labels match) |
| **RapidOCR** (fallback) | `pip install rapidocr-onnxruntime==1.2.3` (no binary) | slow on weak CPUs (tens of seconds on text-dense screens) |

On a low-end CPU, install **Tesseract** — RapidOCR's deep-learning OCR is too
slow there for comfortable interactive use. The startup line prints which engine
is active.

## Limitations & notes

- **Accessibility-tree quality varies.** WinForms/WPF/Win32 apps expose rich
  trees; some custom-drawn or game-engine UIs expose almost nothing — the
  screenshot carries those, but element-id targeting is weaker there.
- **Elevated apps:** to drive an app running as admin, run this tool elevated too.
- **UWP / Store apps:** the pid you launch is often `ApplicationFrameHost`; pass
  `--window-title` so window matching works.
- **Non-determinism:** it's an LLM agent — same task can take different paths.
  For strict regression testing, keep tasks specific and assert concrete results.
- **Safety:** the system prompt tells it to avoid destructive/irreversible
  actions unless the task requires them, but you are driving a real app — point
  it at test data, not production.

## Layout

```
app_clicker/
  perception.py   UIA tree + screenshot  -> Observation
  actions.py      execute an action on a control (UIA patterns + click fallback)
  tools.py        Anthropic tool schemas + system prompt
  agent.py        the perceive/decide/act/observe loop (perceiver/executor injectable)
  uidiff.py       what changed between two snapshots (fed back to the model after each action)
  app_target.py   launch / find the window
  web/            --engine web (Playwright)
    session.py      launch / attach to the browser, tabs, dialogs, page events
    perception.py   rendered DOM + screenshot -> Observation
    actions.py      execute an action through a Playwright locator
    tools.py        web tool schemas + system prompt
  reporter.py     Markdown report + screenshots
  cli.py          command-line entry point
tasks/            example test-case suites
tests/            offline tests (fixtures/web_demo.html for the web engine)
reports/          generated reports (gitignored)
```
