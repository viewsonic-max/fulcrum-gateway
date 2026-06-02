# Aalyria Spacetime

**Shape:** Tool / connector — MCP server
**Code:** `ax_cli/runtimes/mcp_servers/aalyria/`
**Status:** Scaffold — gRPC NBI binding pending (config + diagnostics functional)

## Overview

Aalyria is named among the networking vendors on Golden Dome. **Spacetime** is
its temporospatial software-defined networking platform — the routing fabric
across satellites, ground stations, and platforms. Spacetime is the
**space/RF network orchestration layer**, not an LLM, so it's a
tool/connector.

## Why this connector is a scaffold (and Lattice/Starlink aren't)

Lattice and Starlink expose REST APIs, so their connectors make real calls
over stdlib `urllib`. **Spacetime is gRPC-first**:

- clients are generated from [github.com/aalyria/api](https://github.com/aalyria/api)
  and shipped as the `spacetime-api` Python metapackage on Aalyria's *private*
  package index;
- auth is a self-signed **JWT** (private/public keypair) sent as a bearer
  token in gRPC `authorization` metadata.

That can't be hand-rolled over `urllib`, and the generated NBI/SBI stub method
+ message shapes must be pinned against a specific `spacetime-api` version on
the operator's environment. Rather than invent those bindings in a
defense-network context, the connector ships like the repo's
langgraph/strands bridges: real config + diagnostics now, the RPC binding as a
clearly-marked follow-up.

## Tools

- `spacetime_status()` — report whether `spacetime-api` is installed and
  whether `SPACETIME_*` config is present. **Diagnostics only; makes no RPCs.**
- `spacetime_query_network_elements(entity_type?)` — NBI query. Fails closed
  on missing client/config, then raises a clear `SpacetimeNotWired` until the
  NBI RPC is bound (the scaffold point).

## Configuration

| Env var | Required | Purpose |
| --- | --- | --- |
| `SPACETIME_HOST` | yes | gRPC endpoint (e.g. `dns:///<env>:443`) |
| `SPACETIME_AGENT_EMAIL` | yes | Service-account email (JWT subject) |
| `SPACETIME_PRIVATE_KEY_ID` | yes | Key id registered with Spacetime |
| `SPACETIME_PRIVATE_KEY_FILE` | yes | Path to the PEM private key |

```bash
pip install spacetime-api --extra-index-url \
  https://us-central1-python.pkg.dev/a5a-spacetime-artifacts/py-packages/simple
python -m ax_cli.runtimes.mcp_servers.aalyria
```

## To finish wiring (follow-up)

1. Pin a `spacetime-api` version; identify the NBI query RPC + request/response
   messages from the generated stubs.
2. Build the self-signed-JWT auth and a secure gRPC channel with the token in
   `authorization` metadata.
3. Implement `_handle_query_network_elements` against that stub; add a
   round-trip test that skips when `spacetime-api` is absent.

## Security

The private key path + minted JWT are used in-process only — never logged or
written to the workspace.

## Sources

- [Spacetime API repo (GitHub)](https://github.com/aalyria/api)
- [Client libraries — Spacetime](https://docs.spacetime.aalyria.com/api/client-libraries/)
- [Authentication — Spacetime](https://docs.spacetime.aalyria.com/api/authentication/)
