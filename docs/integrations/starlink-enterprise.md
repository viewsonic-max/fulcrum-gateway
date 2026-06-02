# SpaceX Starlink (Enterprise API)

**Shape:** Tool / connector — MCP server
**Code:** `ax_cli/runtimes/mcp_servers/starlink/`
**Status:** Functional — confirm resource paths/telemetry schema before production

## Overview

SpaceX is a frontrunner on Golden Dome (space-based interceptors and the
transport layer). **Starlink Enterprise** is its REST API for managing
accounts, service lines, user terminals, and device telemetry. Starlink is
the **comms / transport layer** of the kill chain, not an LLM, so it's a
tool/connector. (The military **Starshield** surface is separate and
classified; this connector targets the commercial Enterprise API.)

## Tools

- `starlink_get_service_lines(account_number)` — list service lines on an account.
- `starlink_get_account(account_number)` — account summary.
- `starlink_query_telemetry(query)` — query device telemetry; `query` is passed
  through (e.g. `{"accountNumber": "...", "batchSize": 100}`).

Stdlib-only (`urllib`). Auth is OAuth2 **client-credentials**: a service
account yields a client id + secret, exchanged for a short-lived bearer
token, then sent as `Authorization: Bearer <token>` (cached in-process).

## Configuration

| Env var | Required | Purpose |
| --- | --- | --- |
| `STARLINK_CLIENT_ID` | yes | Service-account client id |
| `STARLINK_CLIENT_SECRET` | yes | Service-account client secret |
| `STARLINK_BASE_URL` | no | Enterprise base; default `https://web-api.starlink.com/enterprise` |
| `STARLINK_TOKEN_URL` | no | OAuth2 token endpoint; default `https://www.starlink.com/api/auth/connect/token` |

Create the service account from an account with "Admin" or "Service Account
Management" permission. Smoke test:
`python -m ax_cli.runtimes.mcp_servers.starlink`.

## Status / caveats

The OAuth2 client-credentials flow is the established Starlink Enterprise auth
model, but Starlink versions its resource paths and the telemetry request
schema is deployment-specific. Confirm the token URL, resource paths, and
telemetry schema against the operator's Enterprise API Swagger before
production.

## Security

The client id/secret and the minted token are Gateway-brokered, used
in-process only, and never logged or written to the workspace.

## Sources

- [Starlink Enterprise API (Swagger)](https://web-api.starlink.com/enterprise/swagger/index.html)
- [Telemetry API — getting started](https://www.starlink.com/support/article/90109cc2-c7ec-31ff-d160-0a87f16ef759)
- [Starlink API docs](https://starlink.readme.io/docs/getting-started)
