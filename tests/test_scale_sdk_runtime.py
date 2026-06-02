"""Tests for the Scale GenAI Platform (SGP) SDK runtime adapter.

`httpx.post` is monkeypatched so these tests run offline and never touch a
live Scale GenAI Platform / Donovan enclave. Coverage: registration, missing
API key, fail-closed missing model, the happy single-turn path (header +
url + body + text extraction across response envelopes), and the auth-error
path.

Scale GP is intentionally NOT OpenAI-wire-compatible (path /egp/v1/
chat-completions, `x-api-key` header), so unlike leapfrog/palantir this
runtime drives httpx directly.
"""

from __future__ import annotations

import os
import sys

import httpx
import pytest  # noqa: F401

_HERMES_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "ax_cli",
    "runtimes",
    "hermes",
)
if _HERMES_DIR not in sys.path:
    sys.path.insert(0, _HERMES_DIR)

from ax_cli.runtimes.hermes.runtimes import scale_sdk  # noqa: F401, E402

_MODEL = "defense-llama-3-70b"


class _FakeResponse:
    def __init__(self, status_code=200, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload if payload is not None else {}
        self.text = text or ""

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


def _set_credentials(monkeypatch):
    monkeypatch.setenv("SCALE_API_KEY", "sk-test")
    monkeypatch.delenv("SCALE_BASE_URL", raising=False)


class _RecordingCallback:
    def __init__(self):
        self.complete = None
        self.statuses = []

    def on_text_delta(self, text):  # noqa: D401
        pass

    def on_text_complete(self, text):
        self.complete = text

    def on_tool_start(self, *a):
        pass

    def on_tool_end(self, *a, **k):
        pass

    def on_status(self, status):
        self.statuses.append(status)


def test_scale_sdk_registers_under_expected_name():
    from ax_cli.runtimes.hermes.runtimes import get_runtime

    assert get_runtime("scale_sdk").name == "scale_sdk"


def test_scale_sdk_crashes_when_api_key_missing(monkeypatch):
    from ax_cli.runtimes.hermes.runtimes import get_runtime

    monkeypatch.delenv("SCALE_API_KEY", raising=False)
    result = get_runtime("scale_sdk").execute("hi", workdir=".", model=_MODEL)
    assert result.exit_reason == "crashed"
    assert "SCALE_API_KEY" in result.text


def test_scale_sdk_fails_closed_when_model_missing(monkeypatch):
    from ax_cli.runtimes.hermes.runtimes import get_runtime

    _set_credentials(monkeypatch)
    result = get_runtime("scale_sdk").execute("hi", workdir=".", model=None)
    assert result.exit_reason == "crashed"
    assert "model id" in result.text


def test_scale_sdk_happy_path_sends_x_api_key_and_extracts_text(monkeypatch):
    from ax_cli.runtimes.hermes.runtimes import get_runtime

    _set_credentials(monkeypatch)
    captured = {}

    def fake_post(url, headers=None, json=None, timeout=None):
        captured["url"] = url
        captured["headers"] = headers
        captured["json"] = json
        return _FakeResponse(
            200, {"choices": [{"message": {"role": "assistant", "content": "Defense Llama here."}}]}
        )

    monkeypatch.setattr(httpx, "post", fake_post)
    cb = _RecordingCallback()
    result = get_runtime("scale_sdk").execute(
        "status?", workdir=".", model=_MODEL, system_prompt="You are mission-bot.", stream_cb=cb
    )

    assert result.exit_reason == "done"
    assert result.text == "Defense Llama here."
    assert cb.complete == "Defense Llama here."
    # Verified wire contract: hyphenated path, x-api-key auth, default base.
    assert captured["url"] == "https://api.spellbook.scale.com/egp/v1/chat-completions"
    assert captured["headers"]["x-api-key"] == "sk-test"
    assert captured["json"]["model"] == _MODEL
    assert captured["json"]["messages"][0] == {"role": "system", "content": "You are mission-bot."}
    # History should carry the user + assistant turns.
    assert [m["role"] for m in result.history] == ["user", "assistant"]


def test_scale_sdk_respects_enclave_base_url_override(monkeypatch):
    from ax_cli.runtimes.hermes.runtimes import get_runtime

    monkeypatch.setenv("SCALE_API_KEY", "sk-test")
    monkeypatch.setenv("SCALE_BASE_URL", "https://donovan.enclave.mil/egp/v1")
    captured = {}

    def fake_post(url, headers=None, json=None, timeout=None):
        captured["url"] = url
        return _FakeResponse(200, {"message": {"content": "ack"}})

    monkeypatch.setattr(httpx, "post", fake_post)
    result = get_runtime("scale_sdk").execute("hi", workdir=".", model=_MODEL)
    assert result.exit_reason == "done"
    assert captured["url"] == "https://donovan.enclave.mil/egp/v1/chat-completions"


def test_scale_sdk_surfaces_auth_error(monkeypatch):
    from ax_cli.runtimes.hermes.runtimes import get_runtime

    _set_credentials(monkeypatch)
    monkeypatch.setattr(httpx, "post", lambda *a, **k: _FakeResponse(401, {}, text="unauthorized"))
    result = get_runtime("scale_sdk").execute("hi", workdir=".", model=_MODEL)
    assert result.exit_reason == "auth_error"
    assert "401" in result.text
