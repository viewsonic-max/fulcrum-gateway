"""Aalyria Spacetime MCP tool definitions and dispatchers.

Spacetime is a temporospatial SDN orchestration platform — the space/RF
network layer that routes data across satellites, ground stations, and
platforms. Like Lattice and Starlink it's a *tool/connector*, not a model.

Two tools:

- `spacetime_status()` — report whether the `spacetime-api` client is
  installed and whether SPACETIME_* connection/auth config is present. Pure
  diagnostics, makes no RPCs — safe to call before the gRPC bindings land.
- `spacetime_query_network_elements()` — the documented NBI query binding
  point. Lazy-loads the client; raises an actionable SpacetimeNotWired until
  the specific NBI RPC is pinned against the operator's Spacetime version
  (see README). We deliberately do not invent the generated stub method or
  message shape.

Config (read at call time, fails closed):

    SPACETIME_HOST              gRPC endpoint
    SPACETIME_AGENT_EMAIL       JWT subject (service account)
    SPACETIME_PRIVATE_KEY_ID    registered key id
    SPACETIME_PRIVATE_KEY_FILE  PEM private key path

The private key + minted JWT are used in-process only; never logged or
written to the workspace.
"""

from __future__ import annotations

import json
from typing import Any

from ..stdio_server import ToolSpec
from . import client as _client
from .client import (
    AalyriaConfig,
    SpacetimeNotConfigured,
    SpacetimeNotInstalled,
    SpacetimeNotWired,
    require_client,
)

STATUS_SCHEMA: dict[str, Any] = {"type": "object", "properties": {}}

QUERY_NETWORK_ELEMENTS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "entity_type": {
            "type": "string",
            "description": (
                "Optional NBI entity type filter (e.g. 'PLATFORM_DEFINITION', "
                "'NETWORK_NODE'). When omitted, returns all readable elements."
            ),
        },
    },
}


def _wrap_json(payload: Any) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": json.dumps(payload)}]}


def _handle_status(_arguments: dict[str, Any]) -> dict[str, Any]:
    """Diagnostics only — never makes an RPC."""
    client_installed = _client.spacetime_client_available()
    try:
        AalyriaConfig.from_env()
        configured = True
        config_error = None
    except SpacetimeNotConfigured as exc:
        configured = False
        config_error = str(exc)
    return _wrap_json(
        {
            "transport": "grpc",
            "client_package": "spacetime-api",
            "client_installed": client_installed,
            "configured": configured,
            "config_error": config_error,
            "rpc_bindings": "scaffold",  # see connector README
            "ready": bool(client_installed and configured),
        }
    )


def _handle_query_network_elements(arguments: dict[str, Any]) -> dict[str, Any]:
    # Fail closed on config + install before reaching the binding point so the
    # operator gets the most actionable error first.
    try:
        require_client()
        AalyriaConfig.from_env()
    except SpacetimeNotInstalled as exc:
        raise ValueError(str(exc)) from exc
    except SpacetimeNotConfigured as exc:
        raise ValueError(f"Spacetime connector not configured: {exc}") from exc

    # Documented binding point. The NBI query RPC (service + method + request
    # message) must be pinned against the operator's `spacetime-api` version
    # before it is called; guessing it in a defense-network context would be
    # worse than failing loudly. See ax_cli/runtimes/mcp_servers/aalyria/README.md.
    raise SpacetimeNotWired(
        "spacetime_query_network_elements is a scaffold: the NBI query RPC is not yet "
        "bound. Pin the generated stub method + request message against your installed "
        "spacetime-api version and wire it here (see connector README)."
    )


def build_tools() -> list[ToolSpec]:
    return [
        ToolSpec(
            name="spacetime_status",
            description=(
                "Report Aalyria Spacetime connector readiness: whether the "
                "spacetime-api gRPC client is installed and whether SPACETIME_* "
                "connection/auth config is present. Diagnostics only — makes no RPCs."
            ),
            input_schema=STATUS_SCHEMA,
            handler=_handle_status,
        ),
        ToolSpec(
            name="spacetime_query_network_elements",
            description=(
                "Query Spacetime NBI network elements (platforms, nodes, antennas). "
                "Requires the spacetime-api client and SPACETIME_* config. NOTE: the "
                "NBI RPC binding is a scaffold pending confirmation against your "
                "Spacetime version — see the connector README."
            ),
            input_schema=QUERY_NETWORK_ELEMENTS_SCHEMA,
            handler=_handle_query_network_elements,
        ),
    ]
