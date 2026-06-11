"""Tests for the lattice MCP server: tool wiring + connector behavior.

The Lattice HTTP layer is stubbed (the client's `_request` is patched) so
these tests run offline and never touch a real Lattice environment.
"""

from __future__ import annotations

import json

import pytest

from ax_cli.runtimes.mcp_servers.lattice import tools as lattice_tools
from ax_cli.runtimes.mcp_servers.lattice.client import LatticeClient, LatticeError
from ax_cli.runtimes.mcp_servers.lattice.tools import (
    _handle_get_entity,
    _handle_publish_entity,
    build_tools,
)


def _set_credentials(monkeypatch):
    monkeypatch.setenv("LATTICE_BASE_URL", "https://env.lattice.test")
    monkeypatch.setenv("LATTICE_BEARER_TOKEN", "test_token")
    monkeypatch.delenv("LATTICE_SANDBOX_TOKEN", raising=False)


def test_build_tools_returns_get_and_publish():
    tools = build_tools()
    names = [t.name for t in tools]
    assert names == ["lattice_get_entity", "lattice_publish_entity"]
    for tool in tools:
        assert tool.input_schema["type"] == "object"
        assert "required" in tool.input_schema


def test_get_entity_returns_entity_json(monkeypatch):
    _set_credentials(monkeypatch)
    monkeypatch.setattr(
        LatticeClient,
        "get_entity",
        lambda self, entity_id: {"entityId": entity_id, "ok": True},
    )
    result = _handle_get_entity({"entity_id": "abc-123"})
    payload = json.loads(result["content"][0]["text"])
    assert payload == {"entityId": "abc-123", "ok": True}
    assert "isError" not in result


def test_get_entity_requires_entity_id(monkeypatch):
    _set_credentials(monkeypatch)
    with pytest.raises(ValueError, match="entity_id is required"):
        _handle_get_entity({"entity_id": "  "})


def test_get_entity_fails_closed_without_base_url(monkeypatch):
    monkeypatch.delenv("LATTICE_BASE_URL", raising=False)
    monkeypatch.setenv("LATTICE_BEARER_TOKEN", "test_token")
    with pytest.raises(ValueError, match="LATTICE_BASE_URL not set"):
        _handle_get_entity({"entity_id": "abc-123"})


def test_get_entity_fails_closed_without_token(monkeypatch):
    monkeypatch.setenv("LATTICE_BASE_URL", "https://env.lattice.test")
    monkeypatch.delenv("LATTICE_BEARER_TOKEN", raising=False)
    with pytest.raises(ValueError, match="LATTICE_BEARER_TOKEN not set"):
        _handle_get_entity({"entity_id": "abc-123"})


def test_publish_entity_passes_body_through(monkeypatch):
    _set_credentials(monkeypatch)
    captured = {}

    def fake_publish(self, entity_id, entity):
        captured["entity_id"] = entity_id
        captured["entity"] = entity
        return {"accepted": True}

    monkeypatch.setattr(LatticeClient, "publish_entity", fake_publish)
    result = _handle_publish_entity(
        {"entity_id": "abc-123", "entity": {"entityId": "abc-123", "kind": "track"}}
    )
    payload = json.loads(result["content"][0]["text"])
    assert payload == {"accepted": True}
    assert captured["entity_id"] == "abc-123"
    assert captured["entity"]["kind"] == "track"


def test_publish_entity_rejects_non_object_entity(monkeypatch):
    _set_credentials(monkeypatch)
    with pytest.raises(ValueError, match="entity must be an object"):
        _handle_publish_entity({"entity_id": "abc-123", "entity": "not-an-object"})


def test_get_entity_surfaces_http_error(monkeypatch):
    _set_credentials(monkeypatch)

    def boom(self, entity_id):
        raise LatticeError("Lattice returned HTTP 404", status=404)

    monkeypatch.setattr(LatticeClient, "get_entity", boom)
    with pytest.raises(ValueError, match="HTTP 404"):
        _handle_get_entity({"entity_id": "missing"})


def test_client_sends_sandbox_header_when_configured():
    client = LatticeClient(
        "https://env.lattice.test/",
        "tok",
        sandbox_token="sandbox-abc",
    )
    headers = client._headers()
    assert headers["Authorization"] == "Bearer tok"
    assert headers["anduril-sandbox-authorization"] == "sandbox-abc"


def test_client_omits_sandbox_header_when_absent():
    client = LatticeClient("https://env.lattice.test", "tok")
    headers = client._headers()
    assert "anduril-sandbox-authorization" not in headers


def test_module_exposes_missing_credentials_guard():
    # _build_client is the single fail-closed gate both handlers route through.
    assert hasattr(lattice_tools, "_build_client")


def test_entity_id_is_quoted_into_path(monkeypatch):
    """Model-supplied entity ids must not escape the /api/v1/entities/<id>
    path segment (#187 review: path injection)."""
    captured = {}

    def fake_request(self, method, path, body=None, **kwargs):
        captured["method"] = method
        captured["path"] = path
        return {}

    monkeypatch.setattr(LatticeClient, "_request", fake_request)
    client = LatticeClient("https://env.lattice.test", "tok")

    client.get_entity("../../admin?x=1")
    assert captured["path"] == "/api/v1/entities/..%2F..%2Fadmin%3Fx%3D1"

    client.publish_entity("a/b", {"kind": "track"})
    assert captured["method"] == "PUT"
    assert captured["path"] == "/api/v1/entities/a%2Fb"
