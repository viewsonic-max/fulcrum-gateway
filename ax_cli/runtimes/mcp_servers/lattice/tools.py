"""Lattice MCP tool definitions and dispatchers.

Two tools wrap Anduril's Lattice Entity-Manager REST API so an aX agent can
read and publish Common Operational Picture entities (tracks, assets, points
of interest) as a *tool*, not as a model backend — Anduril does not expose an
LLM, so Lattice belongs on the connector side of the trust boundary.

- `lattice_get_entity(entity_id)` — GET one entity. Returns the entity JSON.
- `lattice_publish_entity(entity_id, entity)` — create-or-update one entity.

Credentials are read from the environment at call time:

    LATTICE_BASE_URL       — environment base URL (required)
    LATTICE_BEARER_TOKEN   — bearer token (required)
    LATTICE_SANDBOX_TOKEN  — sandbox authorization header value (optional;
                             required only for Lattice developer sandboxes)

The token is never logged and never written to the workspace. Missing
credentials fail closed with an actionable message naming the missing var.
"""

from __future__ import annotations

import json
import os
from typing import Any

from ..stdio_server import ToolSpec
from .client import LatticeClient, LatticeError

GET_ENTITY_INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "entity_id": {
            "type": "string",
            "description": "The Lattice entity ID to fetch.",
        },
    },
    "required": ["entity_id"],
}

PUBLISH_ENTITY_INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "entity_id": {
            "type": "string",
            "description": "The Lattice entity ID to create or update.",
        },
        "entity": {
            "type": "object",
            "description": (
                "The entity body following the Lattice Entity schema. The server "
                "validates the entity and rejects invalid bodies. Entities published "
                "this way are owned by this originator and cannot be edited by the UI."
            ),
        },
    },
    "required": ["entity_id", "entity"],
}


class _MissingCredentials(Exception):
    """Raised when required Lattice env vars are absent."""


def _build_client() -> LatticeClient:
    """Construct a LatticeClient from the environment, failing closed.

    Read at call time (not import time) so the server process can be launched
    before credentials are injected, matching how the sibling SDK runtimes
    resolve auth lazily.
    """
    base_url = os.environ.get("LATTICE_BASE_URL", "").strip()
    token = os.environ.get("LATTICE_BEARER_TOKEN", "").strip()
    sandbox_token = os.environ.get("LATTICE_SANDBOX_TOKEN", "").strip() or None
    if not base_url:
        raise _MissingCredentials("LATTICE_BASE_URL not set")
    if not token:
        raise _MissingCredentials("LATTICE_BEARER_TOKEN not set")
    return LatticeClient(base_url, token, sandbox_token=sandbox_token)


def _wrap_json(payload: Any) -> dict[str, Any]:
    """Return an MCP tool-call result wrapping a value as JSON text."""
    return {"content": [{"type": "text", "text": json.dumps(payload)}]}


def _handle_get_entity(arguments: dict[str, Any]) -> dict[str, Any]:
    entity_id = str(arguments.get("entity_id") or "").strip()
    if not entity_id:
        raise ValueError("lattice_get_entity.entity_id is required")
    try:
        client = _build_client()
    except _MissingCredentials as exc:
        raise ValueError(f"Lattice connector not configured: {exc}") from exc
    try:
        entity = client.get_entity(entity_id)
    except LatticeError as exc:
        raise ValueError(f"Lattice get_entity failed: {exc.message}") from exc
    return _wrap_json(entity)


def _handle_publish_entity(arguments: dict[str, Any]) -> dict[str, Any]:
    entity_id = str(arguments.get("entity_id") or "").strip()
    entity = arguments.get("entity")
    if not entity_id:
        raise ValueError("lattice_publish_entity.entity_id is required")
    if not isinstance(entity, dict):
        raise ValueError("lattice_publish_entity.entity must be an object")
    try:
        client = _build_client()
    except _MissingCredentials as exc:
        raise ValueError(f"Lattice connector not configured: {exc}") from exc
    try:
        result = client.publish_entity(entity_id, entity)
    except LatticeError as exc:
        raise ValueError(f"Lattice publish_entity failed: {exc.message}") from exc
    return _wrap_json(result)


def build_tools() -> list[ToolSpec]:
    return [
        ToolSpec(
            name="lattice_get_entity",
            description=(
                "Fetch a single entity from Anduril Lattice by ID via the Entities "
                "REST API. Returns the entity as JSON. Requires LATTICE_BASE_URL and "
                "LATTICE_BEARER_TOKEN in the environment."
            ),
            input_schema=GET_ENTITY_INPUT_SCHEMA,
            handler=_handle_get_entity,
        ),
        ToolSpec(
            name="lattice_publish_entity",
            description=(
                "Create or update a single entity in Anduril Lattice by ID via the "
                "Entities REST API (PUT). The entity body must follow the Lattice "
                "Entity schema; the server validates it. Returns the API response as "
                "JSON. Requires LATTICE_BASE_URL and LATTICE_BEARER_TOKEN."
            ),
            input_schema=PUBLISH_ENTITY_INPUT_SCHEMA,
            handler=_handle_publish_entity,
        ),
    ]
