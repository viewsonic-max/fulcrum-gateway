"""Tests for the starlink MCP server: tool wiring + connector behavior.

The Starlink HTTP layer is stubbed (the client methods are patched) so these
tests run offline and never touch the real Starlink Enterprise API.
"""

from __future__ import annotations

import json

import pytest

from ax_cli.runtimes.mcp_servers.starlink.client import StarlinkClient, StarlinkError
from ax_cli.runtimes.mcp_servers.starlink.tools import (
    _handle_get_account,
    _handle_get_service_lines,
    _handle_query_telemetry,
    build_tools,
)


def _set_credentials(monkeypatch):
    monkeypatch.setenv("STARLINK_CLIENT_ID", "cid")
    monkeypatch.setenv("STARLINK_CLIENT_SECRET", "secret")
    monkeypatch.delenv("STARLINK_BASE_URL", raising=False)
    monkeypatch.delenv("STARLINK_TOKEN_URL", raising=False)


def test_build_tools_returns_three_tools():
    names = [t.name for t in build_tools()]
    assert names == [
        "starlink_get_service_lines",
        "starlink_get_account",
        "starlink_query_telemetry",
    ]


def test_get_service_lines_returns_json(monkeypatch):
    _set_credentials(monkeypatch)
    monkeypatch.setattr(
        StarlinkClient,
        "get_service_lines",
        lambda self, account: {"account": account, "serviceLines": [{"id": "sl-1"}]},
    )
    result = _handle_get_service_lines({"account_number": "ACC-1"})
    payload = json.loads(result["content"][0]["text"])
    assert payload["account"] == "ACC-1"
    assert payload["serviceLines"][0]["id"] == "sl-1"


def test_get_service_lines_requires_account(monkeypatch):
    _set_credentials(monkeypatch)
    with pytest.raises(ValueError, match="account_number is required"):
        _handle_get_service_lines({"account_number": "  "})


def test_fails_closed_without_client_id(monkeypatch):
    monkeypatch.delenv("STARLINK_CLIENT_ID", raising=False)
    monkeypatch.setenv("STARLINK_CLIENT_SECRET", "secret")
    with pytest.raises(ValueError, match="STARLINK_CLIENT_ID not set"):
        _handle_get_account({"account_number": "ACC-1"})


def test_fails_closed_without_client_secret(monkeypatch):
    monkeypatch.setenv("STARLINK_CLIENT_ID", "cid")
    monkeypatch.delenv("STARLINK_CLIENT_SECRET", raising=False)
    with pytest.raises(ValueError, match="STARLINK_CLIENT_SECRET not set"):
        _handle_get_account({"account_number": "ACC-1"})


def test_query_telemetry_passes_body_through(monkeypatch):
    _set_credentials(monkeypatch)
    captured = {}

    def fake_query(self, body):
        captured["body"] = body
        return {"data": []}

    monkeypatch.setattr(StarlinkClient, "query_telemetry", fake_query)
    result = _handle_query_telemetry({"query": {"accountNumber": "ACC-1", "batchSize": 10}})
    assert json.loads(result["content"][0]["text"]) == {"data": []}
    assert captured["body"]["batchSize"] == 10


def test_query_telemetry_rejects_non_object(monkeypatch):
    _set_credentials(monkeypatch)
    with pytest.raises(ValueError, match="query must be an object"):
        _handle_query_telemetry({"query": "nope"})


def test_get_account_surfaces_http_error(monkeypatch):
    _set_credentials(monkeypatch)

    def boom(self, account):
        raise StarlinkError("Starlink returned HTTP 404", status=404)

    monkeypatch.setattr(StarlinkClient, "get_account", boom)
    with pytest.raises(ValueError, match="HTTP 404"):
        _handle_get_account({"account_number": "missing"})


def test_client_caches_token(monkeypatch):
    import time

    client = StarlinkClient("cid", "secret")
    calls = {"n": 0}

    def fake_fetch():
        calls["n"] += 1
        client._token = "tok-123"
        client._token_expires_at = time.monotonic() + 300
        return "tok-123"

    monkeypatch.setattr(client, "_fetch_token", fake_fetch)
    assert client._auth_header() == "Bearer tok-123"
    # Second call should reuse the cached token, not re-fetch.
    assert client._auth_header() == "Bearer tok-123"
    assert calls["n"] == 1


class _FakeResp:
    """Minimal stand-in for the urlopen response context manager."""

    def __init__(self, payload: dict):
        self._raw = json.dumps(payload).encode("utf-8")

    def read(self):
        return self._raw

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def test_client_refreshes_token_after_expiry(monkeypatch):
    """Client-credentials tokens are short-lived; a long-running MCP server
    must re-fetch once the advertised expires_in window passes (#187 review)."""
    from ax_cli.runtimes.mcp_servers.starlink import client as client_mod

    clock = {"t": 1000.0}
    monkeypatch.setattr(client_mod.time, "monotonic", lambda: clock["t"])

    calls = {"n": 0}

    def fake_urlopen(req, timeout=None):
        calls["n"] += 1
        return _FakeResp({"access_token": f"tok-{calls['n']}", "expires_in": 120})

    monkeypatch.setattr(client_mod.urllib.request, "urlopen", fake_urlopen)

    client = StarlinkClient("cid", "secret")
    assert client._auth_header() == "Bearer tok-1"
    assert client._auth_header() == "Bearer tok-1"  # within the expiry window
    clock["t"] += 120  # past expires_in minus the refresh skew
    assert client._auth_header() == "Bearer tok-2"
    assert calls["n"] == 2


def test_request_retries_once_with_fresh_token_on_401(monkeypatch):
    """A token revoked before its advertised expiry surfaces as a 401; the
    client mints a fresh token and retries the request exactly once."""
    import urllib.error

    from ax_cli.runtimes.mcp_servers.starlink import client as client_mod

    seen = {"api_auth": [], "token_fetches": 0}

    def fake_urlopen(req, timeout=None):
        if "connect/token" in req.full_url:
            seen["token_fetches"] += 1
            return _FakeResp({"access_token": f"tok-{seen['token_fetches']}", "expires_in": 3600})
        auth = req.get_header("Authorization")
        seen["api_auth"].append(auth)
        if auth == "Bearer tok-1":
            raise urllib.error.HTTPError(req.full_url, 401, "unauthorized", None, None)
        return _FakeResp({"ok": True})

    monkeypatch.setattr(client_mod.urllib.request, "urlopen", fake_urlopen)

    client = StarlinkClient("cid", "secret")
    assert client.get_account("ACC-1") == {"ok": True}
    assert seen["api_auth"] == ["Bearer tok-1", "Bearer tok-2"]


def test_account_number_is_quoted_into_path(monkeypatch):
    """Model-supplied account numbers must not escape the /v1/account/<n>
    path segment (#187 review: path injection)."""
    captured = {}

    def fake_request(self, method, path, body=None, **kwargs):
        captured["path"] = path
        return {}

    monkeypatch.setattr(StarlinkClient, "_request", fake_request)
    client = StarlinkClient("cid", "secret")

    client.get_account("../v2/admin")
    assert captured["path"] == "/v1/account/..%2Fv2%2Fadmin"

    client.get_service_lines("ACC/1")
    assert captured["path"] == "/v1/account/ACC%2F1/service-lines"
