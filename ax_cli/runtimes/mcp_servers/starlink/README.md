# starlink MCP server

SpaceX **Starlink Enterprise** connector. Inspect connectivity assets
(accounts, service lines) and pull device telemetry through the Starlink
Enterprise REST API. Zero runtime deps beyond Python stdlib (`urllib`).

## Why this is a connector, not a model option

Starlink is the **comms / transport layer** of the kill chain, not an LLM.
A real model drives the agent and *calls* Starlink as a tool — same side of
the Gateway trust boundary as the Lattice connector. (The military
**Starshield** surface is separate and classified; this connector targets
the commercial Enterprise API.)

## Tools

- `starlink_get_service_lines(account_number)` — list service lines on an account.
- `starlink_get_account(account_number)` — account summary.
- `starlink_query_telemetry(query)` — query device telemetry; `query` is passed
  through to the telemetry endpoint (e.g. `{"accountNumber": "...", "batchSize": 100}`).

## Configuration

Auth is OAuth2 **client-credentials**. An account admin (or someone with
"Service Account Management" permission) creates a service account to get a
client id + secret; the connector exchanges them for a short-lived bearer
token. Credentials and the minted token are **never logged or written to the
workspace**.

| Env var | Required | Purpose |
|---|---|---|
| `STARLINK_CLIENT_ID` | yes | Service-account client id |
| `STARLINK_CLIENT_SECRET` | yes | Service-account client secret |
| `STARLINK_BASE_URL` | no | Enterprise API base (default `https://web-api.starlink.com/enterprise`) |
| `STARLINK_TOKEN_URL` | no | OAuth2 token endpoint (default `https://www.starlink.com/api/auth/connect/token`) |

Run standalone for a smoke test:

```bash
python -m ax_cli.runtimes.mcp_servers.starlink
```

## Status: scaffold — confirm before production use

The OAuth2 client-credentials flow is the established Starlink Enterprise
auth model, but Starlink versions its resource paths and the telemetry
request schema is deployment-specific. Before relying on this in production,
confirm against the operator's Enterprise API Swagger
([web-api.starlink.com/enterprise/swagger](https://web-api.starlink.com/enterprise/swagger/index.html)):

- the exact token URL and resource paths (`/v1/account/{n}/service-lines`, etc.),
- the telemetry request/response schema,
- rate limits and pagination.

See Starlink's [telemetry API guide](https://www.starlink.com/support/article/90109cc2-c7ec-31ff-d160-0a87f16ef759)
and the [Enterprise API reference](https://web-api.starlink.com/enterprise/swagger/index.html).
