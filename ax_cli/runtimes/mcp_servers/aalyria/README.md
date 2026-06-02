# aalyria MCP server (Spacetime)

Aalyria **Spacetime** connector. Orchestrate and inspect the temporospatial
software-defined network layer — the routing fabric across satellites,
ground stations, and platforms — through the Spacetime API.

## Why this is a connector, not a model option

Spacetime is the **space/RF network orchestration layer**, not an LLM. A real
model drives the agent and *calls* Spacetime as a tool, same side of the
Gateway trust boundary as Lattice and Starlink.

## Why this one is a scaffold (and the others aren't)

Lattice and Starlink expose REST APIs, so their connectors make real calls
with stdlib `urllib`. **Spacetime is gRPC-first**: clients are generated from
[github.com/aalyria/api](https://github.com/aalyria/api) and shipped as the
`spacetime-api` Python metapackage on Aalyria's *private* package index, and
auth is a self-signed JWT (keypair) in gRPC `authorization` metadata. That
can't be hand-rolled over `urllib`, and the generated NBI/SBI stub method and
message shapes must be pinned against a specific `spacetime-api` version on
the operator's environment. Rather than invent those bindings in a
defense-network context, this connector ships like the repo's
langgraph/strands bridges: real config + diagnostics now, the RPC binding as
a clearly-marked follow-up.

## Tools

- `spacetime_status()` — report whether the `spacetime-api` client is
  installed and whether `SPACETIME_*` config is present. **Diagnostics only;
  makes no RPCs** — safe to call today.
- `spacetime_query_network_elements(entity_type?)` — NBI network-element query.
  Fails closed if the client/config are missing, then raises a clear
  `SpacetimeNotWired` until the NBI RPC is bound (the scaffold point).

## Configuration

Auth is a self-signed JWT from a registered keypair. Credentials are used
in-process only and are **never logged or written to the workspace**.

| Env var | Required | Purpose |
|---|---|---|
| `SPACETIME_HOST` | yes | gRPC endpoint (e.g. `dns:///<env>:443`) |
| `SPACETIME_AGENT_EMAIL` | yes | Service-account email (JWT subject) |
| `SPACETIME_PRIVATE_KEY_ID` | yes | Key id registered with Spacetime |
| `SPACETIME_PRIVATE_KEY_FILE` | yes | Path to the PEM private key |

Install the client (private index) and smoke-test the diagnostics tool:

```bash
pip install spacetime-api --extra-index-url \
  https://us-central1-python.pkg.dev/a5a-spacetime-artifacts/py-packages/simple
python -m ax_cli.runtimes.mcp_servers.aalyria
```

## To finish wiring (the follow-up)

1. Pin a `spacetime-api` version; identify the NBI service + query RPC and its
   request/response messages from the generated stubs.
2. Build the JWT auth (self-signed, keypair) and a secure gRPC channel with
   the token in `authorization` metadata.
3. Implement `_handle_query_network_elements` against that stub and add a
   round-trip test behind a marker that skips when `spacetime-api` is absent.

Sources: [Spacetime API repo](https://github.com/aalyria/api),
[client libraries](https://docs.spacetime.aalyria.com/api/client-libraries/),
[authentication](https://docs.spacetime.aalyria.com/api/authentication/).
