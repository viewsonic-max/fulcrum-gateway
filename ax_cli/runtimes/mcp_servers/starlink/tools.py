"""Starlink Enterprise MCP tool definitions and dispatchers.

Three tools wrap SpaceX's Starlink Enterprise REST API so an aX agent can
inspect connectivity assets and pull device telemetry as a *tool*. Starlink
is the transport/comms layer of the kill chain, not a model — so like
Lattice it sits on the connector side of the trust boundary.

- `starlink_get_service_lines(account_number)` — list service lines.
- `starlink_get_account(account_number)` — account summary.
- `starlink_query_telemetry(query)` — query device telemetry (passthrough body).

Credentials are read from the environment at call time:

    STARLINK_CLIENT_ID      — service-account client id (required)
    STARLINK_CLIENT_SECRET  — service-account client secret (required)
    STARLINK_BASE_URL       — Enterprise API base (optional; defaults documented)
    STARLINK_TOKEN_URL      — OAuth2 token endpoint (optional; default documented)

The credentials and minted token are never logged or written to the
workspace. Missing credentials fail closed with an actionable message.
"""

from __future__ import annotations

import json
import os
from typing import Any

from ..stdio_server import ToolSpec
from .client import DEFAULT_BASE_URL, DEFAULT_TOKEN_URL, StarlinkClient, StarlinkError

GET_SERVICE_LINES_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "account_number": {"type": "string", "description": "Enterprise account number."},
    },
    "required": ["account_number"],
}

GET_ACCOUNT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "account_number": {"type": "string", "description": "Enterprise account number."},
    },
    "required": ["account_number"],
}

QUERY_TELEMETRY_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "query": {
            "type": "object",
            "description": (
                "Telemetry query body passed through to the Enterprise telemetry "
                "endpoint, e.g. {\"accountNumber\": \"ACC-123\", \"batchSize\": 100}. "
                "The exact schema is version/deployment specific — see the README."
            ),
        },
    },
    "required": ["query"],
}


class _MissingCredentials(Exception):
    """Raised when required Starlink env vars are absent."""


def _build_client() -> StarlinkClient:
    """Construct a StarlinkClient from the environment, failing closed."""
    client_id = os.environ.get("STARLINK_CLIENT_ID", "").strip()
    client_secret = os.environ.get("STARLINK_CLIENT_SECRET", "").strip()
    base_url = os.environ.get("STARLINK_BASE_URL", "").strip() or DEFAULT_BASE_URL
    token_url = os.environ.get("STARLINK_TOKEN_URL", "").strip() or DEFAULT_TOKEN_URL
    if not client_id:
        raise _MissingCredentials("STARLINK_CLIENT_ID not set")
    if not client_secret:
        raise _MissingCredentials("STARLINK_CLIENT_SECRET not set")
    return StarlinkClient(client_id, client_secret, base_url=base_url, token_url=token_url)


def _wrap_json(payload: Any) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": json.dumps(payload)}]}


def _client_or_raise() -> StarlinkClient:
    try:
        return _build_client()
    except _MissingCredentials as exc:
        raise ValueError(f"Starlink connector not configured: {exc}") from exc


def _handle_get_service_lines(arguments: dict[str, Any]) -> dict[str, Any]:
    account = str(arguments.get("account_number") or "").strip()
    if not account:
        raise ValueError("starlink_get_service_lines.account_number is required")
    client = _client_or_raise()
    try:
        return _wrap_json(client.get_service_lines(account))
    except StarlinkError as exc:
        raise ValueError(f"Starlink get_service_lines failed: {exc.message}") from exc


def _handle_get_account(arguments: dict[str, Any]) -> dict[str, Any]:
    account = str(arguments.get("account_number") or "").strip()
    if not account:
        raise ValueError("starlink_get_account.account_number is required")
    client = _client_or_raise()
    try:
        return _wrap_json(client.get_account(account))
    except StarlinkError as exc:
        raise ValueError(f"Starlink get_account failed: {exc.message}") from exc


def _handle_query_telemetry(arguments: dict[str, Any]) -> dict[str, Any]:
    query = arguments.get("query")
    if not isinstance(query, dict):
        raise ValueError("starlink_query_telemetry.query must be an object")
    client = _client_or_raise()
    try:
        return _wrap_json(client.query_telemetry(query))
    except StarlinkError as exc:
        raise ValueError(f"Starlink query_telemetry failed: {exc.message}") from exc


def build_tools() -> list[ToolSpec]:
    return [
        ToolSpec(
            name="starlink_get_service_lines",
            description=(
                "List the Starlink service lines on an Enterprise account via the "
                "Starlink Enterprise REST API. Requires STARLINK_CLIENT_ID and "
                "STARLINK_CLIENT_SECRET (service-account credentials)."
            ),
            input_schema=GET_SERVICE_LINES_SCHEMA,
            handler=_handle_get_service_lines,
        ),
        ToolSpec(
            name="starlink_get_account",
            description=(
                "Fetch a Starlink Enterprise account summary by account number. "
                "Requires STARLINK_CLIENT_ID and STARLINK_CLIENT_SECRET."
            ),
            input_schema=GET_ACCOUNT_SCHEMA,
            handler=_handle_get_account,
        ),
        ToolSpec(
            name="starlink_query_telemetry",
            description=(
                "Query Starlink device telemetry via the Enterprise telemetry "
                "endpoint. Pass a `query` object (e.g. {accountNumber, batchSize}); "
                "the exact schema is deployment specific. Returns the telemetry "
                "payload as JSON."
            ),
            input_schema=QUERY_TELEMETRY_SCHEMA,
            handler=_handle_query_telemetry,
        ),
    ]
