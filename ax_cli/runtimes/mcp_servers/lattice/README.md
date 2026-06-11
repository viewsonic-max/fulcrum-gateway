# lattice MCP server

Anduril **Lattice** connector. Read and publish Common Operational Picture
entities (tracks, assets, points of interest) through the Lattice Entities
REST API. Zero runtime deps beyond Python stdlib (`urllib`).

## Why this is a connector, not a model option

Anduril does not publish a general-purpose LLM. Lattice is a C2 /
autonomy / sensor-fusion platform, so it belongs on the **tool/connector**
side of the Gateway trust boundary: a real model (Claude, a Palantir AIP
model, an `*_sdk` runtime, …) drives the agent and *calls* Lattice as a
tool. If you were looking to add Lattice "as a model," that's the mismatch —
there is no chat-completions endpoint to point a runtime at.

## Tools

### `lattice_get_entity(entity_id)`

```json
{"entity_id": "entity-abc-123"}
```

Returns the entity as JSON:

```json
{"entityId": "entity-abc-123", "...": "..."}
```

### `lattice_publish_entity(entity_id, entity)`

Create-or-update one entity (REST `PUT`). The `entity` body must follow the
Lattice Entity schema; the server validates it and rejects invalid bodies.
Entities published this way are *owned by this originator* — the Lattice UI
and other sources cannot edit or delete them.

```json
{
  "entity_id": "entity-abc-123",
  "entity": {"entityId": "entity-abc-123", "...": "..."}
}
```

## Configuration

Credentials are read from the environment at call time and are **never
logged or written to the workspace** — the connector is a Gateway-brokered
passthrough, not a credential store.

| Env var | Required | Purpose |
|---|---|---|
| `LATTICE_BASE_URL` | yes | Lattice environment base URL (per-deployment; no public default) |
| `LATTICE_BEARER_TOKEN` | yes | Bearer token, sent as `Authorization: Bearer <token>` |
| `LATTICE_SANDBOX_TOKEN` | no | Value for the `anduril-sandbox-authorization` header; required only for Lattice developer sandboxes |

Run standalone for a smoke test:

```bash
python -m ax_cli.runtimes.mcp_servers.lattice
```

## Status: functional — confirm before production use

This connector wraps the two documented Entity-Manager REST operations
(`GET` / `PUT /api/v1/entities/{id}`) from Anduril's public developer docs.
Lattice environments are versioned per deployment, so before relying on this
in production, confirm against the operator's Lattice environment (or the
pinned `anduril-lattice-sdk` version):

- the exact entity REST path and API version,
- the Entity JSON schema for `publish_entity`,
- whether OAuth client-credentials (vs. a pre-issued bearer token) is the
  required auth flow for that environment.

For the full official clients see `anduril-lattice-sdk` (PyPI) /
`@anduril-industries/lattice-sdk` (npm) and
[developer.anduril.com](https://developer.anduril.com/).
