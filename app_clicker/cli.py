"""Command-line entry point for App Clicker."""

from __future__ import annotations

import argparse
import ctypes
import os
import sys

# The app under test may be non-English: model reasons, element names and window
# titles all carry through to stdout. On Windows that defaults to cp1252 when
# stdout is a pipe, which raises UnicodeEncodeError mid-run and loses the whole
# case. Force UTF-8, and fall back to replacement chars on a console that can't
# render the glyphs rather than crashing.
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, OSError):  # non-reconfigurable stream
    pass

from .agent import TesterAgent
from .app_target import find_window, launch_app
from .llm import AnthropicBackend, BackendChain, OpenAICompatBackend, estimate_cost
from .reporter import Reporter

# Provider presets for the OpenAI-compatible backend.
# Each: base_url, env var(s) holding the key (first found wins), default model,
# and whether the default model is vision-capable (informational).
PRESETS = {
    "openrouter": {
        "base_url": "https://openrouter.ai/api/v1",
        "key_env": ["OPENROUTER_API_KEY"],
        "model": "auto",  # discovered at runtime — free slugs change constantly
        "vision": True,
    },
    "groq": {
        "base_url": "https://api.groq.com/openai/v1",
        "key_env": ["GROQ_API_KEY"],
        "model": "llama-3.3-70b-versatile",  # text-only: use --no-screenshots
        "vision": False,
    },
    "gemini": {
        "base_url": "https://generativelanguage.googleapis.com/v1beta/openai/",
        "key_env": ["GEMINI_API_KEY", "GOOGLE_API_KEY"],
        "model": "gemini-2.0-flash",
        "vision": True,
    },
    "github": {
        "base_url": "https://models.inference.ai.azure.com",
        "key_env": ["GITHUB_TOKEN"],
        "model": "gpt-4o-mini",
        "vision": True,
    },
    "ollama": {
        "base_url": "http://localhost:11434/v1",
        "key_env": [],  # local; no key needed
        "model": "qwen2.5vl",
        "vision": True,
    },
    "openai": {  # generic escape hatch (defaults to api.openai.com; paid)
        "base_url": None,
        "key_env": ["OPENAI_API_KEY"],
        "model": None,
        "vision": True,
    },
}


def _set_dpi_aware() -> None:
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)  # PER_MONITOR_DPI_AWARE
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass


def _load_cases(args) -> list[dict]:
    if args.tasks:
        import yaml

        with open(args.tasks, encoding="utf-8") as f:
            data = yaml.safe_load(f)
        if isinstance(data, dict) and "tests" in data:
            data = data["tests"]
        if not isinstance(data, list):
            raise ConfigError("Tasks file must be a YAML list of {name, task} entries.")
        cases = []
        for i, item in enumerate(data, 1):
            if isinstance(item, str):
                cases.append({"name": f"case-{i}", "task": item})
            else:
                cases.append({"name": item.get("name", f"case-{i}"), "task": item["task"]})
        return cases
    if args.task:
        return [{"name": args.name or "task", "task": args.task}]
    raise ConfigError('Provide --task "..." or --tasks tasks.yaml')


# Kept here rather than imported from .web so the desktop engines don't need Playwright installed.
WEB_BROWSERS = ("chromium", "chrome", "msedge", "firefox", "webkit")


# Provider priority: free first, paid last (used by --provider auto / free / paid).
FREE_PROVIDERS = ["openrouter", "groq", "gemini", "github"]
PAID_PROVIDERS = ["anthropic"]

# Exit codes: 0 = every case passed, 1 = some case did not pass, 2 = the run could not start as
# configured (no usable provider, bad arguments) — nothing was run.
EXIT_CONFIG = 2

# --check-providers should answer quickly even when a provider is down: no retries, short timeout.
_CHECK_CLIENT_OPTS = {"timeout": 20.0, "max_retries": 0}


class ConfigError(Exception):
    """The run can't start as configured; main() prints it as one line and exits with EXIT_CONFIG."""


class SkipProvider(Exception):
    """A provider can't be used right now (no key, no live model, wrong modality).

    ``env`` names the API-key variable whose absence caused it; ``fix`` is a short imperative
    remedy for any other cause. Both feed the one-line summary when nothing is usable.
    """

    def __init__(self, reason: str, env: str | None = None, fix: str | None = None):
        super().__init__(reason)
        self.env = env
        self.fix = fix


def _load_env() -> None:
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except Exception:
        pass


def _provider_key(envs):
    for env in envs:
        if os.environ.get(env):
            return os.environ[env]
    return None


def _build_provider(prov, args, need_vision, allow_model_override, client_opts=None):
    """Build one (backend, model) for a provider, or raise SkipProvider.

    ``client_opts`` are extra SDK client options (e.g. a short timeout for --check-providers).
    """
    opts = client_opts or {}
    if prov == "anthropic":
        if not (os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN")):
            raise SkipProvider("no ANTHROPIC_API_KEY", env="ANTHROPIC_API_KEY")
        try:
            import anthropic
        except ImportError:
            raise SkipProvider("the 'anthropic' package is not installed", fix="pip install anthropic")
        model = (args.model if allow_model_override and args.model else None) or args.paid_model or "claude-opus-5"
        return AnthropicBackend(anthropic.Anthropic(**opts), model=model, cache=not args.no_cache), model

    preset = PRESETS[prov]
    key = _provider_key(preset["key_env"])
    if preset["key_env"] and not key:
        raise SkipProvider(f"no {preset['key_env'][0]}", env=preset["key_env"][0])
    if need_vision and not preset.get("vision", True):
        raise SkipProvider("text-only but the uia engine sends screenshots (use --engine recognizer or --no-screenshots)")

    try:
        from openai import OpenAI
    except ImportError:
        raise SkipProvider("the 'openai' package is not installed", fix="pip install openai")

    headers = {"HTTP-Referer": "https://localhost", "X-Title": "App Clicker"} if prov == "openrouter" else None
    client = OpenAI(base_url=(args.base_url or preset["base_url"]),
                    api_key=key or "not-needed", default_headers=headers, **opts)

    model = (args.model if allow_model_override and args.model else None) or preset["model"]
    fallbacks = []
    if model == "auto":
        if prov != "openrouter":
            raise SkipProvider("model 'auto' is only supported for openrouter")
        from .openrouter import list_free_tool_models, pick_free_model
        try:
            cands = list_free_tool_models(require_vision=need_vision)
            model = pick_free_model(client, require_vision=need_vision, candidates=cands)
        except Exception as e:
            raise SkipProvider(f"no live free model ({str(e)[:60]})",
                               fix="the free tier may be busy or capped, retry later")
        fallbacks = [m for m in cands if m != model]
    elif not model:
        raise SkipProvider("needs an explicit --model")

    return OpenAICompatBackend(client, model=model, fallback_models=fallbacks), model


def _needs_vision(args) -> bool:
    return args.engine != "recognizer" and not args.no_screenshots


def _tier(prov: str) -> str:
    return "paid" if prov in PAID_PROVIDERS else "free"


def _chain_order(mode: str) -> tuple[list[str], bool]:
    """(providers in priority order, whether --model applies) for a --provider value."""
    if mode == "auto":
        return FREE_PROVIDERS + PAID_PROVIDERS, False
    if mode == "free":
        return FREE_PROVIDERS, False
    if mode == "paid":
        return PAID_PROVIDERS, False
    return [mode], True


def _no_provider_message(mode: str, skipped: list) -> str:
    """One line saying why nothing is usable for this mode, and what would fix it."""
    scope = {"auto": "", "free": " for --free", "paid": " for --paid"}.get(mode, f" for --provider {mode}")
    reasons = "; ".join(f"{prov}: {why}" for prov, why in skipped)
    keys = list(dict.fromkeys(why.env for _, why in skipped if why.env))
    hints = []
    if keys:
        hints.append("set " + ("one of " if len(keys) > 1 else "") + " / ".join(keys)
                     + " in .env or the environment")
    hints.extend(dict.fromkeys(why.fix for _, why in skipped if why.fix))
    message = f"no usable model provider{scope} ({reasons})."
    if hints:
        message += " Fix: " + "; ".join(hints) + "."
    return message + " Run --check-providers for details."


def build_backend_chain(args):
    """Build a free-first, paid-last backend chain according to --provider."""
    _load_env()
    mode = args.provider
    order, single = _chain_order(mode)
    need_vision = _needs_vision(args)
    label = {"auto": "free first, paid fallback", "free": "free only",
             "paid": "paid only"}.get(mode, f"provider: {mode}")
    print(f"  Building model chain ({label}):")
    entries, skipped = [], []
    for prov in order:
        try:
            backend, model = _build_provider(prov, args, need_vision, allow_model_override=single)
        except SkipProvider as e:
            skipped.append((prov, e))
            print(f"    - {prov}: skipped ({e})")
            continue
        print(f"    + {prov} [{_tier(prov)}] -> {model}")
        entries.append((backend, model, prov))

    if not entries:
        raise ConfigError(_no_provider_message(mode, skipped))
    return BackendChain(entries)


# -- --check-providers ----------------------------------------------------------
def _verify_key(prov: str, backend) -> tuple[str, str]:
    """Ask the provider a cheap authenticated question (list models: no tokens spent).

    Returns (status, note): ok = key accepted, warn = couldn't tell, fail = rejected/unreachable.
    """
    try:
        if prov == "anthropic":
            backend.client.models.list(limit=1)
        else:
            backend.client.models.list()
    except Exception as e:
        code = getattr(e, "status_code", None)
        # Only 401 is conclusive on OpenAI-compatible hosts: some answer 403/404 to a models
        # listing even for a good key.
        if code == 401 or (code == 403 and prov == "anthropic"):
            return "fail", f"key rejected (HTTP {code})"
        name = type(e).__name__
        if "connection" in name.lower() or "timeout" in name.lower():
            return "fail", f"cannot reach the provider ({name})"
        return "warn", f"could not verify the key ({name}: {str(e)[:60]})"
    return "ok", "key accepted"


def _check_one(prov: str, args, need_vision: bool, single: bool) -> tuple[str, str]:
    """(status, detail) for one provider; status is ok / warn / skip / fail."""
    try:
        backend, model = _build_provider(prov, args, need_vision, allow_model_override=single,
                                         client_opts=_CHECK_CLIENT_OPTS)
    except SkipProvider as e:
        return "skip", str(e)
    except Exception as e:
        return "fail", f"unexpected error ({type(e).__name__}: {str(e)[:80]})"
    extra = len(getattr(backend, "fallback_models", None) or [])
    where = model + (f" (+{extra} fallback models)" if extra else "")
    if prov == "openrouter":
        # Free slugs churn, so building the backend already probed for a live model
        pinned = bool(single and args.model and args.model != "auto")
        return "ok", where + (" - pinned model, not probed" if pinned else " - live model probed")
    status, note = _verify_key(prov, backend)
    return status, f"{where} - {note}"


def _mode_orders(mode: str) -> list[tuple[str, list[str]]]:
    """The (label, providers) pairs a provider check reports; the last is the selected mode."""
    if mode == "auto":
        return [("--free", FREE_PROVIDERS), ("--paid", PAID_PROVIDERS),
                ("default (auto)", FREE_PROVIDERS + PAID_PROVIDERS)]
    order, _ = _chain_order(mode)
    return [({"free": "--free", "paid": "--paid"}.get(mode, f"--provider {mode}"), order)]


def check_providers(args) -> int:
    """--check-providers: which providers and modes can run with these keys and engine flags."""
    _load_env()
    mode = args.provider
    need_vision = _needs_vision(args)
    single = _chain_order(mode)[1]
    modes = _mode_orders(mode)
    providers = list(dict.fromkeys(prov for _, order in modes for prov in order))

    shots = "screenshots on" if need_vision else "no screenshots"
    print(f"Provider check (engine {args.engine}, {shots}):")
    results = {}
    for prov in providers:
        results[prov] = _check_one(prov, args, need_vision, single)
        status, detail = results[prov]
        print(f"  {prov:<11} {_tier(prov):<5} {status.upper():<5} {detail}")

    print()
    print("Modes:")
    selected_usable = False
    for label, order in modes:
        usable = [prov for prov in order if results[prov][0] in ("ok", "warn")]
        print(f"  {label:<16} " + (f"usable ({' -> '.join(usable)})" if usable else "NOT usable"))
        selected_usable = bool(usable)  # the last entry is the mode this run would use
    return 0 if selected_usable else EXIT_CONFIG


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="app_clicker",
        description="AI-driven manual tester for native Windows desktop apps and web apps.",
    )
    target = p.add_argument_group("target application")
    target.add_argument("--exe", help="Path to the .exe to launch.")
    target.add_argument("--exe-args", nargs=argparse.REMAINDER, default=[],
                        help="Arguments passed to the launched exe (put last).")
    target.add_argument("--attach", action="store_true",
                        help="Attach to an already-running window instead of launching.")
    target.add_argument("--window-title", help="Match the window by title substring.")
    target.add_argument("--window-class", help="Match the window by class name.")
    target.add_argument("--pid", type=int, help="Attach to a specific process id.")
    target.add_argument("--launch-timeout", type=float, default=30.0,
                        help="Seconds to wait for the window to appear (default 30).")

    web = p.add_argument_group("web application (--engine web)")
    web.add_argument("--url", help="Page to open at the start of each case.")
    web.add_argument("--browser", default="chromium", choices=WEB_BROWSERS,
                     help="Browser to launch (default chromium, Playwright's build). 'msedge' / 'chrome' "
                          "use the installed browser, so no browser download is needed.")
    web.add_argument("--headless", action="store_true", help="Run the browser without a window.")
    web.add_argument("--storage-state", metavar="PATH",
                     help="Playwright storage-state JSON (cookies + local storage) to start each case "
                          "logged in.")
    web.add_argument("--cdp", metavar="URL",
                     help="Attach to a running Chromium/Edge/Chrome started with --remote-debugging-port, "
                          "e.g. http://localhost:9222, and use its profile instead of launching a browser.")

    task = p.add_argument_group("task")
    task.add_argument("--task", help="A single plain-English task/test case.")
    task.add_argument("--name", help="Name for the single task (used in the report).")
    task.add_argument("--tasks", help="YAML file with a list of {name, task} test cases.")

    model = p.add_argument_group("model / provider")
    model.add_argument("--provider", default="auto",
                       choices=["auto", "free", "paid", "anthropic", *PRESETS.keys()],
                       help="'auto' (default): try free providers first, fall back to paid. "
                            "'free' / 'paid': that tier only. Or name one provider directly.")
    model.add_argument("--model",
                       help="Model id for a single named provider. For openrouter, 'auto' "
                            "discovers a live free model. Ignored for the multi-provider auto/free/paid chains.")
    model.add_argument("--paid-model", default="claude-opus-5",
                       help="Anthropic model used for the paid fallback (default claude-opus-5; "
                            "e.g. claude-sonnet-5 or claude-haiku-4-5 for cheaper).")
    model.add_argument("--base-url", help="Override the OpenAI-compatible base URL.")
    model.add_argument("--list-free-models", action="store_true",
                       help="List free tool-capable OpenRouter models available now, then exit.")
    model.add_argument("--check-providers", action="store_true",
                       help="Pre-flight, then exit: report which providers and modes (--free / --paid / "
                            "default) are usable with your keys and the engine flags given. Verifies keys "
                            "with a free models-list call and probes OpenRouter for a live free model "
                            "(a few tiny requests). Exit code 0 if the selected mode is usable, 2 if not.")
    tier = model.add_mutually_exclusive_group()
    tier.add_argument("--free", action="store_true",
                      help="Free tier only (no paid fallback). Shorthand for --provider free.")
    tier.add_argument("--paid", action="store_true",
                      help="Paid tier only. Shorthand for --provider paid.")

    run = p.add_argument_group("run")
    run.add_argument("--max-steps", type=int, default=40, help="Max actions per case.")
    run.add_argument("--no-screenshots", action="store_true",
                    help="Send only the UI tree (required for text-only models like Groq Llama).")
    run.add_argument("--no-cache", action="store_true",
                    help="Disable Anthropic prompt caching (on by default; cuts paid-run cost sharply).")
    run.add_argument("--no-visual", action="store_true",
                    help="Disable OCR/vision tools (click_text/assert_text/click_at). On by default when "
                         "rapidocr-onnxruntime + opencv are installed.")
    run.add_argument("--out", default="reports", help="Directory for reports.")
    run.add_argument("--keep-open", action="store_true",
                    help="Do not close a launched app between/after cases. Web: keep the same tab and "
                         "its state between cases instead of starting each in a fresh browser context.")
    run.add_argument("--quiet", action="store_true", help="Reduce console output.")
    run.add_argument("--engine", default="uia", choices=["uia", "recognizer", "web"],
                    help="Perception engine. 'uia' = accessibility tree + screenshot to the LLM; "
                         "'recognizer' = Screen_Recognizer (local OCR/CV) with the LLM as a pure "
                         "decision engine (no screenshot, id-based clicks); 'web' = a web app in a "
                         "browser via Playwright (DOM element list + screenshot).")
    run.add_argument("--ocr-backend", default="tesseract", choices=["auto", "tesseract", "rapidocr"],
                    help="OCR backend for the recognizer engine (default tesseract — fast on CPU).")
    return p


def _print_meter(result, model: str) -> None:
    cost = estimate_cost(model, result.input_tokens, result.output_tokens,
                         result.cache_read, result.cache_write)
    line = f"  Tokens: in={result.input_tokens:,} out={result.output_tokens:,}"
    if result.cache_read or result.cache_write:
        line += f" (cache read={result.cache_read:,}, write={result.cache_write:,})"
    line += f"  |  cost: ${cost:.4f}" if cost is not None else "  |  cost: free / unknown pricing"
    print(line)


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return _main(args)
    except ConfigError as e:
        # A single line on stdout, not an exception on stderr: Windows PowerShell 5.1 renders a
        # native command's stderr as a NativeCommandError block, burying the one line that matters.
        print(f"Error: {e}")
        return EXIT_CONFIG


def _main(args) -> int:
    if args.list_free_models:
        from .openrouter import list_free_tool_models

        require_vision = not args.no_screenshots
        models = list_free_tool_models(require_vision=require_vision)
        kind = "vision " if require_vision else ""
        print(f"Free tool-capable {kind}models on OpenRouter ({len(models)}), best first:")
        for mid in models:
            print("  ", mid)
        print("\nUse one with:  --provider openrouter --model <id>"
              "   (or just --provider openrouter for auto-select)")
        return 0

    # --free / --paid are shorthands for the free-only / paid-only chains.
    if args.free:
        args.provider = "free"
    elif args.paid:
        args.provider = "paid"

    if args.check_providers:
        return check_providers(args)

    _set_dpi_aware()
    if args.engine == "web":
        _check_web_args(args)
    chain = build_backend_chain(args)
    cases = _load_cases(args)

    visual = False
    session = None
    if args.engine == "recognizer":
        print(f"  Engine: recognizer (Screen_Recognizer, ocr={args.ocr_backend}) — "
              "no screenshots to the LLM; it decides by element id.")
    elif args.engine == "web":
        session = _start_web_session(args)
    else:
        # OCR/vision tools: on by default when an OCR backend is available.
        from .visual import active_engine
        oeng = None if args.no_visual else active_engine()
        visual = oeng is not None
        if args.no_visual:
            pass
        elif oeng == "tesseract":
            print("  OCR visual tools: on (tesseract)")
        elif oeng == "rapidocr":
            print("  OCR visual tools: on (rapidocr — slow on CPU; for speed run "
                  "`winget install UB-Mannheim.TesseractOCR`)")
        else:
            print("  OCR visual tools: off — install Tesseract "
                  "(`winget install UB-Mannheim.TesseractOCR`) or `pip install rapidocr-onnxruntime==1.2.3`")

    try:
        results = _run_cases(args, chain, cases, visual, session)
    finally:
        if session is not None:
            session.close()

    print("\n=== Summary ===")
    for name, status in results:
        print(f"  {status.upper():10} {name}")
    return 0 if all(s == "passed" for _, s in results) else 1


def _check_web_args(args) -> None:
    if not (args.url or args.cdp):
        raise ConfigError("--engine web needs --url (or --cdp to attach to a running browser).")
    if args.storage_state and not os.path.isfile(args.storage_state):
        raise ConfigError(f"--storage-state file not found: {args.storage_state}")
    if args.storage_state and args.cdp:
        print("  Note: --storage-state is ignored with --cdp (the attached browser's profile is used).")


def _start_web_session(args):
    try:
        from .web import WebSession
    except ImportError as e:
        raise ConfigError(
            f"The web engine needs Playwright ({e}). Install it:\n"
            "  pip install playwright\n"
            "  playwright install chromium   (or skip this and pass --browser msedge)"
        )
    if args.cdp:
        where = f"attached to {args.cdp}"
    else:
        where = args.browser + (", headless" if args.headless else "")
    print(f"  Engine: web (Playwright, {where}) — page element list"
          + ("" if args.no_screenshots else " + screenshot") + " to the LLM.")
    try:
        return WebSession(browser=args.browser, headless=args.headless, cdp=args.cdp,
                          storage_state=args.storage_state).start()
    except Exception as e:
        detail = "\n  ".join(str(e).strip().splitlines()[:3])
        hint = ("Is the browser running with --remote-debugging-port?" if args.cdp
                else "Run `playwright install chromium`, or pass --browser msedge to use the installed Edge.")
        raise ConfigError(f"Could not start the browser:\n  {detail}\n{hint}")


def _run_cases(args, chain, cases, visual, session) -> list[tuple[str, str]]:
    results = []
    for idx, case in enumerate(cases, 1):
        print(f"\n=== Case {idx}/{len(cases)}: {case['name']} ===")
        proc = None
        try:
            if session is not None:
                session.open_case(args.url, fresh=not args.keep_open)
            elif args.exe and not args.attach:
                proc = launch_app(args.exe, args.exe_args)
                window = find_window(pid=proc.pid, title=args.window_title,
                                     class_name=args.window_class, timeout=args.launch_timeout)
            else:
                window = find_window(pid=args.pid, title=args.window_title,
                                     class_name=args.window_class, timeout=args.launch_timeout)
        except Exception as e:
            what = "open the page" if session is not None else "obtain window"
            print(f"  Could not {what}: {e}", file=sys.stderr)
            results.append((case["name"], "blocked"))
            if proc and not args.keep_open:
                proc.terminate()
            continue

        reporter = Reporter(args.out, case["name"], case["task"])
        if session is not None:
            from .web import WEB_SYSTEM, WEB_TOOLS, WebExecutor, WebPerceiver
            agent = TesterAgent(
                chain=chain,
                max_steps=args.max_steps,
                reporter=reporter,
                verbose=not args.quiet,
                perceiver=WebPerceiver(session, screenshots=not args.no_screenshots),
                executor=WebExecutor(session),
                tools=WEB_TOOLS,
                system=WEB_SYSTEM,
                screen_label="page",
            )
        elif args.engine == "recognizer":
            from .recognizer_agent import RecognizerAgent
            agent = RecognizerAgent(
                chain=chain,
                window=window,
                ocr_backend=args.ocr_backend,
                max_steps=args.max_steps,
                reporter=reporter,
                verbose=not args.quiet,
            )
        else:
            agent = TesterAgent(
                chain=chain,
                window=window,
                max_steps=args.max_steps,
                screenshots=not args.no_screenshots,
                reporter=reporter,
                verbose=not args.quiet,
                visual=visual,
            )
        result = agent.run(case["task"])
        report_path = reporter.finalize(result)
        print(f"  Verdict: {result.status.upper()}  |  report: {report_path}")
        print(f"  Provider used: {chain.provider} ({chain.model})"
              + ("  [switched from free to paid]" if chain.switched else ""))
        _print_meter(result, chain.model)
        results.append((case["name"], result.status))

        if proc and not args.keep_open:
            proc.terminate()
    return results


if __name__ == "__main__":
    raise SystemExit(main())
