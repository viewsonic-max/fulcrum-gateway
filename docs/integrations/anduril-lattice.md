# Anduril Lattice

**Shape:** Tool / connector — MCP server
**Code:** `ax_cli/runtimes/mcp_servers/lattice/`
**Status:** Functional — confirm entity path/schema before production

## Overview

Anduril is a software prime on Golden Dome, building AI-powered
command-and-control. **Lattice** is its C2 / autonomy / sensor-fusion
platform with a REST **Entities** API for the Common Operational Picture
(tracks, assets, points of interest).

Anduril does **not** publish a general-purpose LLM, so Lattice is a
tool/connector — a real model drives the agent and *calls* Lattice — not a
model option. (This is the mismatch if someone asks to add Anduril "as a
model.")

## Tools

- `lattice_get_entity(entity_id)` — fetch one entity (`GET`).
- `lattice_publish_entity(entity_id, entity)` — create/update one entity
  (`PUT`). Entities published this way are owned by the originator; the
  Lattice UI cannot edit or delete them. The server validates the body.

Stdlib-only (`urllib`), matching the svg_viz "no deps beyond stdlib" property.

## Configuration

| Env var | Required | Purpose |
| --- | --- | --- |
| `LATTICE_BASE_URL` | yes | Lattice environment base URL (per-deployment; no default) |
| `LATTICE_BEARER_TOKEN` | yes | Bearer token (`Authorization: Bearer <token>`) |
| `LATTICE_SANDBOX_TOKEN` | no | `anduril-sandbox-authorization` header value (developer sandboxes only) |

Smoke test: `python -m ax_cli.runtimes.mcp_servers.lattice`.

## Status / caveats

The connector wraps `GET` / `PUT /api/v1/entities/{id}` from Anduril's public
Entity-Manager REST reference. Lattice environments are versioned per
deployment, so before production confirm the exact entity path + API version,
the Entity JSON schema, and whether OAuth client-credentials (vs. a
pre-issued bearer) is required for that environment. The official SDKs
(`anduril-lattice-sdk`, `@anduril-industries/lattice-sdk`) are heavier
OAuth+gRPC clients; this connector deliberately wraps just the REST
operations an agent needs.

## Security

The bearer token is Gateway-brokered and used in-process only — never logged
or written to the workspace.

## Sources

- [Lattice API overview — Anduril](https://developer.anduril.com/reference/overview/overview)
- [Publish entity (REST) — Anduril](https://developer.anduril.com/reference/rest/entities/publish-entity)
- [anduril-lattice-sdk (PyPI)](https://pypi.org/project/anduril-lattice-sdk/)
- [lattice-sdk-python (GitHub)](https://github.com/anduril/lattice-sdk-python)
