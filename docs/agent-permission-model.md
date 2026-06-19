# Agent Permission Model

**Status:** Current — post ADR-012 (0.7.0+)
**Author:** Mark Galpin

For the pre-ADR-012 state, see [adr/pre-ADR-012-agent-permission-model.md](adr/pre-ADR-012-agent-permission-model.md). For the change decision, see [adr/ADR-012-vendor-sdk-security-cleanup.md](adr/ADR-012-vendor-sdk-security-cleanup.md).

---

## Mental model: three orthogonal layers

Agent permission is not a single system — it is three independent layers that apply simultaneously:

| Layer | What it controls | Where enforced |
| --- | --- | --- |
| **Capability authorization** | Which tools/operations the agent may invoke | Claude Code settings, `--allowedTools`, connector policy |
| **Execution environment** | What credentials and filesystem paths exist | Process environment, IAM role, container image |
| **Network/infrastructure** | Which endpoints are reachable | Network policy, IAM resource policies |

A complete permission model requires all three. Profiles and connector policy address the first layer. The second and third layers require environmental controls (injected credentials, IAM scoping, Kubernetes NetworkPolicy) that operate outside Gateway's configuration surface.

The critical implication: **a container boundary provides process and filesystem isolation but does not replace capability authorization**. An agent running in a container can still invoke MCP tools, connector tools, and shell commands that make outbound network calls. Profiles and connector policy define which of those calls are permitted — the container boundary does not.

---

## Two separate "approval" systems

Gateway has two distinct systems that use the word "approval." They are unrelated.

### 1. Gateway registration/binding approvals

Managed in the gateway registry (`approval_state: pending | approved | rejected`). This governs whether an agent's device binding — the fingerprint of the origin machine and process that registered the agent — has been verified by the operator. Surfaced in the gateway daemon UI at `http://127.0.0.1:8765`.

This is an identity governance mechanism, not a tool permission mechanism.

### 2. Claude Code tool approval dialogs

When Claude Code runs interactively, it presents a per-tool approval dialog to the human at the terminal before executing certain operations — a human-in-the-loop mechanism for supervised sessions. Claude Code can also route those decisions programmatically: declarative `allow`/`deny`/`ask` rules, permission modes, the Agent SDK `canUseTool` callback, or an MCP `--permission-prompt-tool`.

**Fulcrum does not wire any of these up as an interactive runtime-approval channel today.** The reason is **separation of authority**, not the absence of a human: an agent usually has a human partner, but that partner is not necessarily authorized to grant what the agent might ask for. Capability authorization is a governance decision made by whoever constructs and deploys the agent; forwarding prompts to whoever happens to be collaborating with the agent at runtime would let an unauthorized party escalate its scope. (Concretely, there is also no path today: the stream-json event format Gateway reads — `assistant`, `content_block_delta`, `result` — carries no `permission_request` event, and `sentinel_cli` runs headless `-p` with no terminal at all.)

The current model is therefore **pre-authorization**: declare the permitted tools upfront (`allow`) and the capabilities the agent must never request (`deny`), and the agent operates within that fixed boundary. This is what `settings.local.json` permissions and connector policy provide. Forwarding *selected* prompts to an appropriately-authorized human may be added later — but only once the surface such prompts can arrive on is narrowed and the blast radius of any single approval is bounded. See [GATEWAY-AGENT-PROFILES-001](../specs/GATEWAY-AGENT-PROFILES-001/spec.md) "Why this exists."

---

## Per-runtime permission model

### `claude_code_channel` — attached session

The operator's Claude Code terminal provides interactive approval for any tool use. This is the only runtime where per-call human approval is practical.

Additional pre-authorization is provided by `settings.local.json` in the agent workdir:

```json
{
  "permissions": {
    "allow": ["mcp__ax-channel__*", "mcp__composio__GITHUB_*"],
    "deny":  ["mcp__composio__GITHUB_*DELETE*"]
  }
}
```

- **`allow`** — tools in this list are pre-authorized; no approval dialog for them
- **`deny`** — tools in this list are blocked even if otherwise allowed; deny takes precedence
- Patterns use fnmatch glob syntax; MCP tools are namespaced `mcp__<server>__<tool>`
- Multiple MCP servers can be addressed independently in the same file
- `settings.local.json` is per-agent-workdir — one file per agent identity

The `profiles` system (`ax agents profiles apply`) manages this file. A profile is a named JSON fragment that deep-merges into `settings.local.json`. The `base` profile for `claude_cli` grants `mcp__ax-channel__*` — the minimum needed for the agent to receive and reply to messages via the platform.

### `sentinel_cli` (Claude) — headless Claude CLI subprocess

Gateway spawns `claude -p --output-format stream-json` as a subprocess per message, optionally resuming a saved session.

No permission bypass flag is injected. The agent's capability authorization is determined entirely by `settings.local.json` in the agent workdir. An agent with no `settings.local.json` (or an empty allow list) will have text-only capability — it can generate and return text responses but cannot invoke tools.

To grant tool access, apply a profile before running the agent:

```bash
ax agents profiles apply <agent-name> --profile base
```

The profiles system is defined in [ADR-011](adr/ADR-011-channel-settings-profiles.md). Today setup runs as individual steps — gateway registration, client-layer setup (`ax channel setup`), and profile application (`ax agents profiles apply`); a one-command orchestrator that bundles them is possible future work.

`--allowedTools` may also be set in the registry entry's `allowed_tools` field. It restricts which tools Claude is offered at the CLI level, independent of `settings.local.json`. Both apply simultaneously — a tool must pass both filters to be usable.

`settings.local.json` in the agent workdir also applies (Claude reads it from `cwd`). The `deny` list takes precedence over `--allowedTools`.

### `hermes_plugin` — long-lived Hermes process

Gateway scaffolds `<workdir>/.hermes/` and spawns `hermes gateway run` as a long-lived process. The agent communicates with the Fulcrum platform via the bundled ax-platform plugin.

**Hermes plugin agents have no Bash tool by default.** All tools available to the agent come through the Gateway connector system. An agent without a `connector_ref` binding has no outbound tools.

Tool access is controlled by connector policy — see the Connector Model section below.

### `sentinel_inference_sdk` — direct vendor API runtimes

Gateway spawns the `sentinel.py` supervisor, which dispatches to a direct vendor API backend set via `sentinel_sdk_runtime` (required, no default): `openai_sdk`, `groq_sdk`, `mistral_sdk`, `gemini_sdk`, `leapfrog_sdk`, or `xai_sdk`.

These runtimes call vendor LLM APIs directly — no Claude CLI subprocess, no permission bypass flags. Tool access is controlled by the same Gateway connector system as `hermes_plugin`. The model is identical: `connector_search` and `connector_call` are meta-tools the agent uses to discover and invoke connector-backed tools, with connector policy enforced at both search time and call time.

`sentinel_inference_sdk` replaced `hermes_sentinel` in 0.7.0. See [ADR-012](adr/ADR-012-vendor-sdk-security-cleanup.md).

### `sentinel_hermes_sdk` — in-process Hermes AIAgent loop

Gateway spawns the same `sentinel.py` supervisor with a hardcoded `--runtime hermes_sdk` (daemon-set for this runtime type, not operator-configurable), running the full in-process Hermes AIAgent loop (90-turn, parallel tool execution, context compression). Supports Bedrock IAM auth, OpenRouter, Anthropic API, and Codex backends via the `model` field (e.g. `bedrock:claude-sonnet-4-6`, `anthropic:claude-sonnet-4-6`).

Tool authorization uses `_secure_hermes_tools` (a security shim on the Hermes tool registry) in addition to the standard connector policy. This is the preferred runtime for coding sentinels that need session continuity and rich tool use.

`sentinel_hermes_sdk` was promoted from `hermes_sdk` within `sentinel_inference_sdk` in 0.7.0. See [ADR-012](adr/ADR-012-vendor-sdk-security-cleanup.md).

### `exec` and `bedrock_agentcore` — command bridge runtimes

`exec` runs an arbitrary script as a subprocess. `bedrock_agentcore` runs a bridge that invokes Amazon Bedrock AgentCore Runtime via boto3.

Neither has a Gateway-managed tool permission layer. Permission is determined by:

- What the bridge script is written to do
- The AWS IAM role or credentials the process runs with (`bedrock_agentcore` uses `AWS_PROFILE` from the registry entry)

---

## The connector model

Connectors are Gateway-managed outbound tool adapters. They are independent resources (`~/.ax/gateway/connectors.json`) that agents reference by name.

### Provider types

Two provider types are supported:

| Provider | What it connects to |
| --- | --- |
| `composio` | Composio HTTP API — 500+ SaaS integrations; tool names are Composio slugs (`GITHUB_CREATE_PULL_REQUEST`, etc.) |
| `http_mcp` | Any MCP-compliant server via JSON-RPC (`tools/list`, `tools/call`); tool names are whatever the server defines |

An agent bound to either provider type uses `connector_search` and `connector_call` identically — the provider is an implementation detail transparent to the runtime.

### Tool policy

Each connector has four independent policy fields:

| Field | Type | Behavior |
| --- | --- | --- |
| `allowed_tools` | fnmatch patterns | Only tools matching at least one pattern are available |
| `denied_tools` | fnmatch patterns | Tools matching any pattern are blocked; checked before allow |
| `allowed_toolkits` | fnmatch patterns | Only tools whose app/toolkit field matches are available |
| `denied_toolkits` | fnmatch patterns | Tools from matching apps are blocked |

Policy is evaluated at two points:

- **Search time** (`connector_search`) — filtered tools are returned; blocked tools are invisible to the model
- **Call time** (`connector_call`) — `assert_tool_allowed()` raises `ConnectorPolicyError` if the tool doesn't pass policy; this is a hard enforcement, not just filtering

Deny is checked before allow. Allow is only applied when set — an empty allow list means "all tools permitted" (subject to deny).

### Granularity

For Composio: granularity is at the individual operation level. Examples:

- `allowed_tools: ["GITHUB_GET_*", "GITHUB_LIST_*"]` — read-only GitHub access
- `denied_tools: ["*DELETE*", "*MERGE*", "*CLOSE*"]` — block destructive operations across all apps
- Combined: `allowed_tools: ["GITHUB_*"]` + `denied_tools: ["GITHUB_DELETE*"]` — all GitHub except delete

For `http_mcp`: granularity is whatever tool names the MCP server exposes. If the server names its tools `read_record`, `write_record`, `delete_record`, you can use `allowed_tools: ["read_*"]` or `denied_tools: ["delete_*"]`.

### Per-agent isolation

Connectors are global resources by default — multiple agents can share one connector and its policy. Per-agent tool isolation requires a dedicated connector per agent:

```bash
ax gateway connectors add orion-tools --provider composio
ax gateway connectors set orion-tools allowed_tools '["GITHUB_GET_*", "JIRA_GET_*"]'
ax gateway agents add orion --template hermes --connector-ref orion-tools
```

There is no limit on connector count. The `connector_ref` field on an agent entry binds it to a specific connector.

---

## MCP servers and Claude Code

For `claude_code_channel` and `sentinel_cli` (Claude), MCP servers are configured in `.mcp.json` in the agent workdir. Each server's tools appear in Claude Code's permission namespace as `mcp__<server-name>__<tool-name>`.

`settings.local.json` allow/deny patterns address these names directly. This gives individual tool-level control across multiple MCP servers simultaneously, with server namespacing preventing conflicts between servers that expose tools with the same name.

Enforcement is client-side — Claude Code will not invoke a tool that fails the allow/deny check, regardless of what the MCP server exposes. The MCP server can additionally enforce its own authentication layer, providing two independent enforcement points.

---

## Capability authorization vs environment credentials

`--allowedTools` and `settings.local.json` prevent the agent from *invoking* unauthorized tools. They do not prevent an authorized tool (Bash) from using credentials that happen to be present in the execution environment.

Example: an agent with `Bash` in its tool allowlist and `AWS_SECRET_ACCESS_KEY` in its environment can run `aws ec2 terminate-instances` even if you intended Bash only for local file operations. The tool is permitted; what the tool does with ambient credentials is not controlled by the permission layer.

The implication: for agents that have Bash in their tool allowlist, environmental credential scoping is as important as the tool allowlist. The deployment model determines what credentials the process inherits:

- **Desktop model** — agent inherits the operator's shell environment; avoid sourcing production credentials in that environment
- **Containerized model** — pod-scoped IAM role via Kubernetes service account; the process identity only has the permissions its role grants, regardless of what commands it runs

Connectors are not subject to this concern: connector tools are invoked through Gateway, which holds the connector credentials, not the agent process. The agent calls `connector_call("my-connector", "GITHUB_CREATE_PR", args)` — it never sees the API key.

---

## Current gaps

| Gap | Affected runtimes | Severity |
| --- | --- | --- |
| Connector policy is per-connector, not per-agent by default | All `vendor_sdk` and `hermes_plugin` runtimes | Medium — shared connectors share policy; use dedicated connectors for isolation |
| No platform-level ACL on which profiles/connectors a user may apply | All runtimes | Gap in the agent-creation product model |
| `profiles` system only manages `permissions.allow`; `deny` list unsupported | `claude_code_channel`, `sentinel_cli` | Low — deny list adds defense-in-depth but must be written manually for now |
| `exec` and `bedrock_agentcore` have no Gateway-managed capability authorization | `exec`, `bedrock_agentcore` | By design — operator-owned bridge scripts |

---

## Direction: profiles and agent archetypes

**Profiles** are the management surface for capability authorization on the
**MCP-host / coding-agent client namespace** (per ADR-014) — clients whose capability
surface is a `settings.local.json`-style additive permission document (`claude_cli`
today, `cursor`/`windsurf` plausible future members). A profile is a named JSON fragment
organized by `client` under `ax_cli/agent_profiles/{client}/`.
Profiles are flat (no inheritance) and compose by ordered union. Applied via `ax agents
profiles apply`, which writes `settings.local.json` into the agent workdir. The profiles
system design is in [ADR-011](adr/ADR-011-channel-settings-profiles.md). A one-command
orchestration that would bundle profile application with the other setup layers is
possible future work.

For `vendor_sdk` and `hermes_plugin` agents, the capability authorization equivalent is a connector instance with a specific tool policy — there is no `settings.local.json` for SDK runtimes. Connector policy is managed separately. This is the boundary of the profiles model: it serves the MCP-host (file-based-permission) namespace, not the connector-policy namespaces.

**Agent archetypes** extend this model one level up. An archetype is a bundle that specifies a complete capability envelope: runtime type, profile(s) to apply, connector to bind and its tool policy, system prompt, and model. It is what the platform UI would offer to users as a named agent archetype — a code-reviewer, an agent-maker, a PR responder. A future one-command setup orchestrator would be the materialization step; an archetype is the declarative spec that drives it.

This concept emerged from the permission model analysis in ADR-012. The permission-surface distinction (file-based profiles vs. connector policy) has been back-ported into the ADR-010/ADR-011 reconciliation; the archetype-driven materialization remains future work.

Platform-level ACLs on which agent archetypes a given user may instantiate are planned but not yet specified.
