# Vendor Integrations

This directory documents the third-party vendors ax-cli can drive, and how
each one is wired into Gateway. Several of these vendors are prime or
software contractors on the US **Golden Dome** missile-defense program, so
the set is organized around the kill chain: models that reason, and
platforms the models call as tools.

## The one rule: model vs. connector

ax-cli has exactly two integration shapes, and a vendor only fits if it
exposes a developer-accessible API:

| Shape | When | Where it lives | Example |
| --- | --- | --- | --- |
| **Model runtime** (`*_sdk`) | Vendor exposes an LLM / chat-completions API | `ax_cli/runtimes/hermes/runtimes/<name>_sdk.py` | Palantir AIP, Scale GP |
| **Tool / connector** (MCP server) | Vendor exposes a platform REST/gRPC API | `ax_cli/runtimes/mcp_servers/<name>/` | Anduril Lattice, Starlink, Aalyria |

A real model drives the agent; connectors are tools that model calls. Vendors
that sell hardware, sensors, interceptors, or system-integration labor —
without a developer API — are **not integration targets** (there's nothing
to wrap). On the Golden Dome roster that excludes Lockheed Martin, Northrop
Grumman, RTX/Raytheon, Boeing, General Dynamics, Leidos, HII, and Booz Allen.

## Vendor pages

| Vendor | Shape | Status | Page |
| --- | --- | --- | --- |
| Palantir AIP | Model runtime (`palantir_sdk`) | Functional (OpenAI-compatible) | [palantir-aip.md](palantir-aip.md) |
| Scale GenAI Platform | Model runtime (`scale_sdk`) | Functional, single-turn | [scale-genai-platform.md](scale-genai-platform.md) |
| Anduril Lattice | Connector (MCP) | Functional, confirm paths | [anduril-lattice.md](anduril-lattice.md) |
| SpaceX Starlink | Connector (MCP) | Functional, confirm paths | [starlink-enterprise.md](starlink-enterprise.md) |
| Aalyria Spacetime | Connector (MCP) | Scaffold (gRPC binding pending) | [aalyria-spacetime.md](aalyria-spacetime.md) |

## Trust boundary (applies to all of them)

Every vendor here is a **private, per-deployment endpoint** — a classified
enclave, a customer Foundry stack, a Lattice environment, a service account.
So they all follow the same rules from
[ADR-005](../adr/ADR-005-credentials-never-in-workspace.md):

- the operator supplies the base URL/host (no public default for private endpoints),
- credentials are Gateway-brokered and injected at launch, **never** written
  to `.ax/config.toml`, logs, messages, or generated docs,
- config resolution fails **closed** on missing endpoint/credentials/model
  rather than guessing a default.

## SDK runtime selection & wiring note (incl. xai_sdk)

Model runtimes are not picked through an `agent_template_catalog()` tile.
They are driven by the **vendored Hermes sentinel** via `--runtime <name>
--model <id>`, and selected on a managed-agent entry through the
`sentinel_sdk_runtime` knob (aliases: `hermes_runtime`, `sdk_runtime`),
resolved by `_hermes_sentinel_sdk_runtime()` in `ax_cli/gateway.py`.

For a runtime to be selectable end-to-end it must appear in **two** places —
they have to agree:

1. the `_HERMES_SENTINEL_SDK_RUNTIMES` allowlist in `ax_cli/gateway.py`
   (what the launcher will pass), and
2. the `--runtime` argparse `choices` in
   `ax_cli/runtimes/hermes/runtimes/.../sentinel.py` (what the sentinel will
   accept).

**xai_sdk wiring fix:** `xai_sdk` was present in the allowlist but missing
from the sentinel `choices`, so launching it would have passed `--runtime
xai_sdk` to a sentinel that rejected it. Adding `palantir_sdk` /`scale_sdk`
surfaced the gap, so both `xai_sdk` and the two new runtimes were added to the
sentinel `choices` to bring the two lists back into agreement.

### Why no template tile yet (follow-up)

A first-class CLI/UI **template tile** for SDK-backed models would be nice,
but today the only launch route for these runtimes is the
`hermes_sentinel` runtime type, which is **deprecated** (successor:
`hermes_plugin`, which runs `hermes gateway run` and does *not* take an SDK
`--runtime`). Shipping a Palantir/Scale tile on `hermes_sentinel` would build
new operator surface on a deprecated path — product debt we chose not to
incur. The clean fix is to add a non-deprecated sentinel launch route for SDK
runtimes first, then add tiles on top. Tracked as a follow-up.
