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
    client = StarlinkClient("cid", "secret")
    calls = {"n": 0}

    def fake_fetch():
        calls["n"] += 1
        client._token = "tok-123"
        return "tok-123"

    monkeypatch.setattr(client, "_fetch_token", fake_fetch)
    assert client._auth_header() == "Bearer tok-123"
    # Second call should reuse the cached token, not re-fetch.
    assert client._auth_header() == "Bearer tok-123"
    assert calls["n"] == 1
