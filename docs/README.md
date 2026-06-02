# ax-gateway Documentation

## For new operators

Start here and follow in order:

1. [Quickstart](quickstart.md) — install, run Gateway, send your first message
2. [Training Index](devrel-teaching-operators-contributors.md) — glossary,
   concepts, scenarios, and a suggested reading order

## Reference

| Doc | Audience | Covers |
| --- | --- | --- |
| [Agent Authentication](agent-authentication.md) | Operators, contributors | Auth model, trust boundary, credential flows, proxy allowlist |
| [Gateway Agent Runtimes](gateway-agent-runtimes.md) | Operators, contributors | Runtime patterns (Hermes, Claude Code, exec), space resolution, agent lifecycle, inbox semantics |
| [Vendor Integrations](integrations/README.md) | Operators, contributors | Model runtimes vs. tool connectors; Palantir, Scale, Anduril Lattice, Starlink, Aalyria; SDK runtime selection + xai_sdk wiring note |
| [Credential Security](credential-security.md) | Operators | Fingerprinting, honeypot keys, PAT rotation, detection signals |
| [Module Guide: gateway.py](module-guide-gateway.md) | Contributors | Section-by-section code map with line ranges and key functions |
| [Release Process](release-process.md) | Maintainers | Versioning, PyPI publishing, changelog generation |
| [Operator QA Runbook](operator-qa-runbook.md) | Operators | Manual and automated QA checks for Gateway |
| [Offline Development](offline-development.md) | Contributors | Developing and smoke-testing agents without platform access |

## Scenarios (step-by-step task guides)

| Scenario | Learning goal |
| --- | --- |
| [Move agent to new space](scenarios/move-agent-to-new-space.md) | Space resolution cascade |
| [Debug a stuck agent](scenarios/debug-stuck-agent.md) | Agent lifecycle states |
| [Send a file through Gateway](scenarios/send-file-through-gateway.md) | Trust boundary, proxy |
| [Second agent in same workspace](scenarios/setup-second-agent-same-workspace.md) | Workspace identity |
| [Investigate a 429 storm](scenarios/investigate-429-storm.md) | Rate limiting |
| [Recover corrupted registry](scenarios/recover-corrupted-registry.md) | State files |
| [Rotate an agent PAT](scenarios/rotate-agent-pat.md) | Credential lifecycle |
| [Store user PAT in an encrypted secret store](scenarios/encrypted-pat-at-rest.md) | User PAT at rest with dotenvx, sops, or pass |

## Architecture Decision Records

| ADR | Decision | Status |
| --- | --- | --- |
| [ADR-001](adr/ADR-001-gateway-localhost-only.md) | Gateway binds to 127.0.0.1 only | Accepted |
| [ADR-002](adr/ADR-002-flat-proxy-allowlist.md) | Flat proxy allowlist, not per-agent ACLs | Accepted |
| [ADR-003](adr/ADR-003-session-tokens-per-connect.md) | Session tokens are short-lived, per-connect | Accepted |
| [ADR-004](adr/ADR-004-space-state-in-session.md) | Space state in session.json, not registry | Accepted |
| [ADR-005](adr/ADR-005-credentials-never-in-workspace.md) | Credentials brokered, never in workspace | Accepted |
| [ADR-006](adr/ADR-006-use-admin-proxy-tiers.md) | use/admin tier model for proxy | Proposed |

## Other docs

| Doc | Purpose |
| --- | --- |
| [Gateway Demo Script](gateway-demo-script.md) | Demo walkthrough for presentations |
| [Login E2E Runbook](login-e2e-runbook.md) | End-to-end login testing |
| [MCP App Signal Adapter](mcp-app-signal-adapter.md) | MCP integration for processing signals |
| [MCP Headless PAT](mcp-headless-pat.md) | Headless PAT authentication for MCP |
| [MCP Remote OAuth](mcp-remote-oauth.md) | Remote OAuth flow for MCP |
| [Reminder Lifecycle](reminder-lifecycle.md) | How agent reminders work |
