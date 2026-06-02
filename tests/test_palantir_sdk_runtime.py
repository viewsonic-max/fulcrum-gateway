"""Tests for the Palantir AIP SDK runtime adapter.

The `openai` Python SDK is mocked via sys.modules so these tests run
offline and do not need a live Foundry stack. Coverage spans registration
discovery, missing-credential paths (both PALANTIR_AIP_TOKEN and
PALANTIR_AIP_BASE_URL), the fail-closed missing-model path, the happy
streaming path (callback fan-out, RuntimeResult shape, history
accumulation), system-prompt threading, the missing-package path, and
operator-supplied base_url propagation.

Mirrors the test conventions in tests/test_leapfrog_sdk_runtime.py so the
two private-endpoint provider suites read the same to reviewers.
"""

from __future__ import annotations

import os
import sys
import types
from unittest.mock import MagicMock

import pytest  # noqa: F401  (pytest is the test runner; import keeps tooling happy)

# The Hermes sentinel prepends ax_cli/runtimes/hermes to sys.path in production
# so vendored runtimes can do `from tools import ...` as an absolute import.
# Replicate that here so the same import path resolves under pytest.
_HERMES_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "ax_cli",
    "runtimes",
    "hermes",
)
if _HERMES_DIR not in sys.path:
    sys.path.insert(0, _HERMES_DIR)

# Importing the module triggers `@register("palantir_sdk")` at module load time,
# so the runtime is in REGISTRY regardless of which other tests in the suite
# may have already populated it (get_runtime's auto-discovery only fires when
# REGISTRY is fully empty).
from ax_cli.runtimes.hermes.runtimes import palantir_sdk  # noqa: F401, E402

# ── Helpers ────────────────────────────────────────────────────────────────

# A valid-looking Foundry model resource identifier (Model Catalog form).
_MODEL_RI = "ri.language-model-service..language-model.gpt-5-2"


def _text_delta(text: str | None):
    """Build a duck-typed chat-completions stream chunk carrying text content
    in choices[0].delta.content."""
    delta = types.SimpleNamespace(content=text, tool_calls=None)
    choice = types.SimpleNamespace(delta=delta)
    return types.SimpleNamespace(choices=[choice])


# Stand-in typed exception classes for the openai SDK. These mirror the
# names + minimal shape (status_code, message) the runtime's typed `except`
# blocks expect. We define them locally instead of `import openai` so the
# test suite can run in environments where the openai package isn't installed.
class _FakeAPIStatusError(Exception):
    def __init__(self, message="", *, status_code=400):
        super().__init__(message)
        self.message = message
        self.status_code = status_code


class _FakeAPITimeoutError(_FakeAPIStatusError):
    def __init__(self, message="timeout"):
        super().__init__(message, status_code=408)


class _FakeRateLimitError(_FakeAPIStatusError):
    def __init__(self, message="rate limit exceeded"):
        super().__init__(message, status_code=429)


class _FakeAuthenticationError(_FakeAPIStatusError):
    def __init__(self, message="invalid token"):
        super().__init__(message, status_code=401)


class _FakePermissionDeniedError(_FakeAPIStatusError):
    def __init__(self, message="permission denied"):
        super().__init__(message, status_code=403)


class _FakeInternalServerError(_FakeAPIStatusError):
    def __init__(self, message="server error"):
        super().__init__(message, status_code=500)


def _install_fake_openai(monkeypatch, fake_client):
    """Swap the `openai` module in sys.modules so the runtime's
    `from openai import OpenAI, ...` returns our mock constructor and
    stand-in typed exception classes."""
    fake_openai = types.ModuleType("openai")
    fake_openai.OpenAI = MagicMock(return_value=fake_client)
    fake_openai.APIStatusError = _FakeAPIStatusError
    fake_openai.APITimeoutError = _FakeAPITimeoutError
    fake_openai.AuthenticationError = _FakeAuthenticationError
    fake_openai.InternalServerError = _FakeInternalServerError
    fake_openai.PermissionDeniedError = _FakePermissionDeniedError
    fake_openai.RateLimitError = _FakeRateLimitError
    monkeypatch.setitem(sys.modules, "openai", fake_openai)
    return fake_openai


def _set_credentials(monkeypatch):
    monkeypatch.setenv("PALANTIR_AIP_TOKEN", "test_token")
    monkeypatch.setenv(
        "PALANTIR_AIP_BASE_URL",
        "https://acme.palantirfoundry.com/api/v2/llm/proxy/openai/v1",
    )


def _fake_client_streaming(chunks):
    """A fake OpenAI client whose chat.completions.create returns `chunks`."""
    fake_client = MagicMock()
    fake_client.chat.completions.create.return_value = iter(chunks)
    return fake_client


class _RecordingCallback:
    """Minimal StreamCallback implementation that records what it sees."""

    def __init__(self):
        self.deltas: list[str] = []
        self.complete: str | None = None
        self.statuses: list[str] = []
        self.tool_starts: list[tuple[str, str]] = []

    def on_text_delta(self, text: str) -> None:
        self.deltas.append(text)

    def on_text_complete(self, text: str) -> None:
        self.complete = text

    def on_tool_start(self, name: str, summary: str) -> None:
        self.tool_starts.append((name, summary))

    def on_tool_end(self, *_args, **_kwargs) -> None:
        pass

    def on_status(self, status: str) -> None:
        self.statuses.append(status)


# ── Tests ──────────────────────────────────────────────────────────────────


def test_palantir_sdk_registers_under_expected_name():
    from ax_cli.runtimes.hermes.runtimes import get_runtime

    runtime = get_runtime("palantir_sdk")
    assert runtime.name == "palantir_sdk"


def test_palantir_sdk_returns_crashed_when_token_missing(monkeypatch):
    """No PALANTIR_AIP_TOKEN in env should short-circuit before any openai import."""
    from ax_cli.runtimes.hermes.runtimes import get_runtime

    monkeypatch.delenv("PALANTIR_AIP_TOKEN", raising=False)
    monkeypatch.setenv(
        "PALANTIR_AIP_BASE_URL",
        "https://acme.palantirfoundry.com/api/v2/llm/proxy/openai/v1",
    )
    result = get_runtime("palantir_sdk").execute("hi", workdir=".", model=_MODEL_RI)
    assert result.exit_reason == "crashed"
    assert "PALANTIR_AIP_TOKEN" in result.text


def test_palantir_sdk_returns_crashed_when_base_url_missing(monkeypatch):
    from ax_cli.runtimes.hermes.runtimes import get_runtime

    monkeypatch.setenv("PALANTIR_AIP_TOKEN", "test_token")
    monkeypatch.delenv("PALANTIR_AIP_BASE_URL", raising=False)
    result = get_runtime("palantir_sdk").execute("hi", workdir=".", model=_MODEL_RI)
    assert result.exit_reason == "crashed"
    assert "PALANTIR_AIP_BASE_URL" in result.text


def test_palantir_sdk_fails_closed_when_model_missing(monkeypatch):
    """A missing model must fail closed with an actionable message rather than
    sending an empty/guessed model name to the proxy."""
    from ax_cli.runtimes.hermes.runtimes import get_runtime

    _set_credentials(monkeypatch)
    result = get_runtime("palantir_sdk").execute("hi", workdir=".", model=None)
    assert result.exit_reason == "crashed"
    assert "model resource identifier" in result.text
    assert "ri.language-model-service" in result.text


def test_palantir_sdk_uses_operator_supplied_base_url(monkeypatch):
    """The operator-supplied PALANTIR_AIP_BASE_URL must be passed to OpenAI()."""
    from ax_cli.runtimes.hermes.runtimes import get_runtime

    monkeypatch.setenv("PALANTIR_AIP_TOKEN", "operator_token_value")
    base = "https://acme.palantirfoundry.com/api/v2/llm/proxy/openai/v1"
    monkeypatch.setenv("PALANTIR_AIP_BASE_URL", base)

    fake_client = _fake_client_streaming([_text_delta("done")])
    fake_openai = _install_fake_openai(monkeypatch, fake_client)

    get_runtime("palantir_sdk").execute("hi", workdir=".", model=_MODEL_RI)

    _, kwargs = fake_openai.OpenAI.call_args
    assert kwargs["base_url"] == base
    assert kwargs["api_key"] == "operator_token_value"


def test_palantir_sdk_streams_text_and_accumulates_history(monkeypatch):
    from ax_cli.runtimes.hermes.runtimes import get_runtime

    _set_credentials(monkeypatch)
    fake_client = _fake_client_streaming([_text_delta("Hello "), _text_delta("world")])
    _install_fake_openai(monkeypatch, fake_client)

    cb = _RecordingCallback()
    result = get_runtime("palantir_sdk").execute(
        "say hi", workdir=".", model=_MODEL_RI, stream_cb=cb
    )

    assert result.exit_reason == "done"
    assert result.text == "Hello world"
    assert cb.complete == "Hello world"
    # The model used should be threaded through to the proxy call unchanged.
    _, kwargs = fake_client.chat.completions.create.call_args
    assert kwargs["model"] == _MODEL_RI
    # History should carry the user turn and the final assistant turn.
    roles = [m["role"] for m in result.history]
    assert roles == ["user", "assistant"]


def test_palantir_sdk_threads_system_prompt_into_messages(monkeypatch):
    from ax_cli.runtimes.hermes.runtimes import get_runtime

    _set_credentials(monkeypatch)
    fake_client = _fake_client_streaming([_text_delta("ok")])
    _install_fake_openai(monkeypatch, fake_client)

    get_runtime("palantir_sdk").execute(
        "hi", workdir=".", model=_MODEL_RI, system_prompt="You are Palantir-bot."
    )

    _, kwargs = fake_client.chat.completions.create.call_args
    messages = kwargs["messages"]
    assert messages[0] == {"role": "system", "content": "You are Palantir-bot."}


def test_palantir_sdk_returns_crashed_when_openai_missing(monkeypatch):
    """When the openai package can't be imported, fail with an actionable message."""
    from ax_cli.runtimes.hermes.runtimes import get_runtime

    _set_credentials(monkeypatch)

    # Force `from openai import ...` to raise ImportError.
    monkeypatch.setitem(sys.modules, "openai", None)
    result = get_runtime("palantir_sdk").execute("hi", workdir=".", model=_MODEL_RI)
    assert result.exit_reason == "crashed"
    assert "openai" in result.text.lower()
