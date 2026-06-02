"""Tests for the aalyria (Spacetime) MCP server scaffold.

These exercise the diagnostics + fail-closed behavior. They do not make gRPC
calls (the NBI binding is an intentional scaffold), so they run offline with
no `spacetime-api` install.
"""

from __future__ import annotations

import json

import pytest

from ax_cli.runtimes.mcp_servers.aalyria import client as aalyria_client
from ax_cli.runtimes.mcp_servers.aalyria.client import (
    AalyriaConfig,
    SpacetimeNotConfigured,
)
from ax_cli.runtimes.mcp_servers.aalyria.tools import (
    _handle_query_network_elements,
    _handle_status,
    build_tools,
)


def _set_config(monkeypatch):
    monkeypatch.setenv("SPACETIME_HOST", "dns:///env.spacetime.test:443")
    monkeypatch.setenv("SPACETIME_AGENT_EMAIL", "agent@example.mil")
    monkeypatch.setenv("SPACETIME_PRIVATE_KEY_ID", "key-1")
    monkeypatch.setenv("SPACETIME_PRIVATE_KEY_FILE", "/tmp/key.pem")


def _clear_config(monkeypatch):
    for var in (
        "SPACETIME_HOST",
        "SPACETIME_AGENT_EMAIL",
        "SPACETIME_PRIVATE_KEY_ID",
        "SPACETIME_PRIVATE_KEY_FILE",
    ):
        monkeypatch.delenv(var, raising=False)


def test_build_tools_returns_status_and_query():
    names = [t.name for t in build_tools()]
    assert names == ["spacetime_status", "spacetime_query_network_elements"]


def test_config_from_env_fails_closed_when_missing(monkeypatch):
    _clear_config(monkeypatch)
    with pytest.raises(SpacetimeNotConfigured) as exc:
        AalyriaConfig.from_env()
    assert "SPACETIME_HOST" in str(exc.value)


def test_config_from_env_succeeds_when_present(monkeypatch):
    _set_config(monkeypatch)
    cfg = AalyriaConfig.from_env()
    assert cfg.host == "dns:///env.spacetime.test:443"
    assert cfg.agent_email == "agent@example.mil"


def test_status_reports_not_ready_when_unconfigured(monkeypatch):
    _clear_config(monkeypatch)
    monkeypatch.setattr(aalyria_client, "spacetime_client_available", lambda: False)
    result = _handle_status({})
    payload = json.loads(result["content"][0]["text"])
    assert payload["transport"] == "grpc"
    assert payload["client_installed"] is False
    assert payload["configured"] is False
    assert payload["ready"] is False
    assert payload["config_error"] is not None


def test_status_reports_ready_when_installed_and_configured(monkeypatch):
    _set_config(monkeypatch)
    monkeypatch.setattr(aalyria_client, "spacetime_client_available", lambda: True)
    result = _handle_status({})
    payload = json.loads(result["content"][0]["text"])
    assert payload["client_installed"] is True
    assert payload["configured"] is True
    assert payload["ready"] is True


def test_query_fails_closed_when_client_missing(monkeypatch):
    _set_config(monkeypatch)
    monkeypatch.setattr(aalyria_client, "spacetime_client_available", lambda: False)
    with pytest.raises(ValueError, match="spacetime-api"):
        _handle_query_network_elements({})


def test_query_raises_not_wired_when_ready(monkeypatch):
    """When client + config are present, the NBI binding is the documented
    scaffold point and must surface a clear not-wired error."""
    _set_config(monkeypatch)
    monkeypatch.setattr(aalyria_client, "spacetime_client_available", lambda: True)
    from ax_cli.runtimes.mcp_servers.aalyria.client import SpacetimeNotWired

    with pytest.raises(SpacetimeNotWired, match="scaffold"):
        _handle_query_network_elements({})
