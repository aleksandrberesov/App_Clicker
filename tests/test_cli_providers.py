"""Provider selection messaging and the --check-providers pre-flight (offline: no network, no keys)."""

from types import SimpleNamespace as NS

import pytest

from app_clicker import cli
from app_clicker.cli import EXIT_CONFIG, SkipProvider, main

KEY_VARS = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "OPENROUTER_API_KEY", "GROQ_API_KEY",
            "GEMINI_API_KEY", "GOOGLE_API_KEY", "GITHUB_TOKEN", "OPENAI_API_KEY")


@pytest.fixture(autouse=True)
def no_real_keys(monkeypatch):
    import dotenv
    monkeypatch.setattr(dotenv, "load_dotenv", lambda *a, **k: False)  # never read the developer's .env
    for var in KEY_VARS:
        monkeypatch.delenv(var, raising=False)


def _run(capsys, *argv):
    rc = main(list(argv))
    out, err = capsys.readouterr()
    return rc, out, err


def _error_lines(out):
    return [ln for ln in out.splitlines() if ln.startswith("Error:")]


# -- no usable provider: one clear line, exit 2, nothing on stderr --------------
def test_paid_without_key_is_one_line_about_the_paid_key(capsys):
    rc, out, err = _run(capsys, "--paid", "--task", "x")
    assert rc == EXIT_CONFIG and err == ""  # stderr would surface as NativeCommandError in PowerShell
    (line,) = _error_lines(out)
    assert "--paid" in line and "ANTHROPIC_API_KEY" in line
    assert "OPENROUTER" not in out and "free" not in line  # not the free-tier advice
    assert "Building model chain (paid only)" in out


def test_free_without_keys_lists_free_providers_and_no_paid_key(capsys):
    rc, out, err = _run(capsys, "--free", "--task", "x")
    (line,) = _error_lines(out)
    assert rc == EXIT_CONFIG and err == ""
    assert "set one of OPENROUTER_API_KEY / GROQ_API_KEY / GEMINI_API_KEY / GITHUB_TOKEN" in line
    assert "ANTHROPIC" not in out


def test_auto_without_keys_covers_every_provider(capsys):
    rc, out, _ = _run(capsys, "--task", "x")
    (line,) = _error_lines(out)
    assert rc == EXIT_CONFIG
    for prov in ("openrouter", "groq", "gemini", "github", "anthropic"):
        assert f"{prov}: no " in line
    assert "--check-providers" in line


def test_named_provider_message(capsys):
    rc, out, _ = _run(capsys, "--provider", "groq", "--no-screenshots", "--task", "x")
    (line,) = _error_lines(out)
    assert rc == EXIT_CONFIG and "--provider groq" in line and "set GROQ_API_KEY" in line


def test_other_provider_problems_get_their_own_fix(capsys, monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "test")
    rc, out, _ = _run(capsys, "--provider", "groq", "--task", "x")  # text-only model, screenshots on
    (line,) = _error_lines(out)
    assert rc == EXIT_CONFIG and "text-only" in line and "--no-screenshots" in line
    assert "set GROQ_API_KEY" not in line  # the key is there; don't tell the user to set it


def test_other_config_errors_share_the_exit_code(capsys):
    rc, out, err = _run(capsys, "--engine", "web", "--task", "x")
    assert rc == EXIT_CONFIG and err == "" and "--engine web needs --url" in _error_lines(out)[0]


def test_missing_task_is_a_config_error(capsys, monkeypatch):
    monkeypatch.setattr(cli, "_build_provider", lambda *a, **k: (NS(), "m"))
    rc, out, _ = _run(capsys, "--paid")
    assert rc == EXIT_CONFIG and "Provide --task" in _error_lines(out)[0]


# -- --check-providers ---------------------------------------------------------
class HttpError(Exception):
    def __init__(self, code):
        super().__init__(f"HTTP {code}")
        self.status_code = code


class FakeClient:
    def __init__(self, error=None):
        self.error = error
        self.models = NS(list=self._list)

    def _list(self, **_):
        if self.error:
            raise self.error
        return []


def _providers(monkeypatch, available):
    """``available``: {provider: (model, client)}; every other provider is 'not configured'."""
    def build(prov, args, need_vision, allow_model_override, client_opts=None):
        if prov not in available:
            raise SkipProvider(f"no {prov.upper()}_KEY", env=f"{prov.upper()}_KEY")
        model, client = available[prov]
        return NS(client=client, fallback_models=["a", "b"] if prov == "openrouter" else []), model
    monkeypatch.setattr(cli, "_build_provider", build)


def test_check_reports_each_provider_and_mode(capsys, monkeypatch):
    _providers(monkeypatch, {"openrouter": ("nvidia/x:free", FakeClient())})
    rc, out, _ = _run(capsys, "--check-providers")
    assert rc == 0  # the default mode works through the free tier
    assert "openrouter  free  OK    nvidia/x:free (+2 fallback models) - live model probed" in out
    assert "anthropic   paid  SKIP  no ANTHROPIC_KEY" in out
    assert "--free           usable (openrouter)" in out
    assert "--paid           NOT usable" in out
    assert "default (auto)   usable (openrouter)" in out


def test_exit_code_follows_the_selected_mode(capsys, monkeypatch):
    _providers(monkeypatch, {"openrouter": ("m", FakeClient())})
    assert _run(capsys, "--check-providers", "--paid")[0] == EXIT_CONFIG
    assert _run(capsys, "--check-providers", "--free")[0] == 0
    _providers(monkeypatch, {"anthropic": ("claude-opus-5", FakeClient())})
    rc, out, _ = _run(capsys, "--check-providers", "--paid")
    assert rc == 0 and "claude-opus-5 - key accepted" in out
    assert "openrouter" not in out  # a --paid check does not probe the free tier


def test_rejected_key_makes_the_provider_unusable(capsys, monkeypatch):
    _providers(monkeypatch, {"anthropic": ("claude-opus-5", FakeClient(HttpError(401))),
                             "openrouter": ("m", FakeClient())})
    rc, out, _ = _run(capsys, "--check-providers", "--paid")
    assert rc == EXIT_CONFIG and "FAIL  claude-opus-5 - key rejected (HTTP 401)" in out
    rc, out, _ = _run(capsys, "--check-providers")
    assert rc == 0 and "--paid           NOT usable" in out and "default (auto)   usable (openrouter)" in out


def test_unverifiable_key_warns_but_stays_usable(capsys, monkeypatch):
    _providers(monkeypatch, {"groq": ("llama", FakeClient(HttpError(404)))})
    rc, out, _ = _run(capsys, "--check-providers", "--provider", "groq")
    assert rc == 0 and "WARN" in out and "could not verify the key" in out


def test_verify_key_classification():
    def verdict(prov, error):
        return cli._verify_key(prov, NS(client=FakeClient(error)))[0]

    class ConnectionFailure(Exception):
        pass

    assert verdict("anthropic", None) == "ok"
    assert verdict("groq", HttpError(401)) == "fail"
    assert verdict("anthropic", HttpError(403)) == "fail"
    assert verdict("groq", HttpError(403)) == "warn"      # some hosts refuse a models listing for good keys
    assert verdict("groq", HttpError(500)) == "warn"
    assert verdict("ollama", ConnectionFailure("refused")) == "fail"


def test_check_builds_real_clients_with_short_timeouts_offline(capsys, monkeypatch):
    # No _build_provider fake: the real SDK clients must accept the check's timeout/retry options
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test")
    monkeypatch.setenv("GROQ_API_KEY", "test")
    monkeypatch.setattr(cli, "_verify_key", lambda prov, backend: ("ok", "key accepted"))
    rc, out, _ = _run(capsys, "--check-providers", "--paid")
    assert rc == 0 and "claude-opus-5 - key accepted" in out
    rc, out, _ = _run(capsys, "--check-providers", "--provider", "groq")  # screenshots on: text-only
    assert rc == EXIT_CONFIG and "SKIP  text-only" in out
    rc, out, _ = _run(capsys, "--check-providers", "--provider", "groq", "--no-screenshots")
    assert rc == 0 and "OK" in out
