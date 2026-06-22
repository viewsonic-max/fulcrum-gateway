# GATEWAY-MANAGED-AGENT-CONFIG-001: Operational Configuration for Gateway-Managed Agents

**Status:** v1 draft  
**Owner:** @markgalpin  
**Date:** 2026-06-08  
**Related:**
- [GATEWAY-AGENT-REGISTRY-001](../GATEWAY-AGENT-REGISTRY-001/spec.md) — identity and binding (stable fields, fingerprint approval)
- [GATEWAY-RUNTIME-PERSISTENCE-001](../GATEWAY-RUNTIME-PERSISTENCE-001/spec.md) — runtime lifecycle (desired_state, effective_state, placement)
- [ADR-007](../../docs/adr/ADR-007-agent-classes-and-signals.md) — agent classes and signaling contracts
- [ADR-012](../../docs/adr/ADR-012-vendor-sdk-security-cleanup.md) — sentinel_inference_sdk and sentinel_hermes_sdk separation
- [ADR-014](../../docs/adr/ADR-014-client-field-unification.md) — `client` field semantics
- [GATEWAY-AGENT-PROFILES-001](../GATEWAY-AGENT-PROFILES-001/spec.md) — the profiles system that consumes the `client` field (writes `settings.local.json` for MCP-host clients)
- [docs/agent-manifests.md](../../docs/agent-manifests.md) — declarative manifest format (PR #235)

---

## Why this exists

The Gateway registry entry for a managed agent contains three distinct tiers of
fields, each with different owners, lifecycles, and update semantics:

| Tier | Spec | Who writes | Lifecycle |
|------|------|------------|-----------|
| Identity & binding | GATEWAY-AGENT-REGISTRY-001 | Operator at register time; approval for changes | Stable; changes require a trust event |
| Operational config | **this spec** | Operator via `agents add` / `agents update` / manifest apply | Mutable; no approval required |
| Runtime state | GATEWAY-AGENT-REGISTRY-001 §Runtime State; GATEWAY-RUNTIME-PERSISTENCE-001 | Daemon and agent process | Ephemeral; overwritten by daemon |

Operational config is the operator's declaration of *what the agent does and
how it runs*. It is set at registration time and may be updated at any time
without a fingerprint or approval event. The daemon reads it at each agent start
to configure the supervised process. Manifests (PR #235) are the declarative
form of this tier — `ax gateway agents apply` is the mechanism; this spec is
the schema.

---

## Field glossary

All fields in this tier are optional at the schema level unless noted. Fields
absent from the registry entry use the runtime's built-in default (if any) or
are treated as unset. Fields explicitly set to an empty string are treated as
"cleared" by `agents update`.

### Core runtime selection

| Field | Type | CLI flag | Description |
|-------|------|----------|-------------|
| `template_id` | string | `--template` | Template that seeded this agent. Determines default `runtime_type`, workdir requirements, and launchability rules. Stored as the template's `id` string (e.g. `hermes`, `claude_code_channel`). |
| `runtime_type` | string | `--type` | How Gateway supervises this agent's process. Canonical values: `echo`, `exec`, `hermes_plugin`, `sentinel_inference_sdk`, `sentinel_hermes_sdk`, `sentinel_cli`, `claude_code_channel`, `inbox`. See ADR-012 for the inference sentinel family. |
| `client` | string | `--client` | Which inference client or MCP host the agent uses within its runtime. Semantics depend on `runtime_type` — see "The `client` field" section below. |

### Process and workspace

| Field | Type | CLI flag | Description |
|-------|------|----------|-------------|
| `workdir` | string (absolute path) | `--workdir` | Absolute path to the agent's runtime directory. Required for `hermes_plugin`, `sentinel_cli`, `claude_code_channel`. Must be absolute — a relative path is stored literally and resolved against the daemon's cwd, not the registration cwd. |
| `exec_command` | string | `--exec` | Shell command for `exec`-type agents. Passed to the OS via `shlex.split`. |

### Model and inference

| Field | Type | CLI flag | Description |
|-------|------|----------|-------------|
| `model` | string | `--model` | Model identifier passed to the runtime. Format is runtime-specific: inference API model names (e.g. `gemini-2.0-flash`, `gpt-4o`) for `sentinel_inference_sdk`; local Ollama model names (e.g. `gemma4:latest`) for the `ollama` template. Previously `ollama_model` for Ollama agents — that field is removed as a breaking change. |

### Access control

| Field | Type | CLI flag | Description |
|-------|------|----------|-------------|
| `allow_all_users` | bool | `--allow-all-users` | When true, any space member may interact with this agent. When false (default), access is restricted to `allowed_users` if set, or to the registering operator only. For `hermes_plugin` agents, this also writes `AX_ALLOW_ALL_USERS=1` into the scaffolded `.env`. |
| `allowed_users` | string | `--allowed-users` | Comma-separated list of user IDs permitted to interact with this agent. Ignored when `allow_all_users` is true. |

### Agent identity presentation

| Field | Type | CLI flag | Description |
|-------|------|----------|-------------|
| `description` | string | `--description` | Human-readable description of what this agent does. Shown in `agents list` and the Gateway UI. |
| `system_prompt` | string | `--system-prompt` | Operator-supplied role instructions injected into the runtime at start. For Claude Code agents, passed via `--append-system-prompt`. For Hermes/sentinel agents, written into the scaffolded config. |

### Connectors

| Field | Type | CLI flag | Description |
|-------|------|----------|-------------|
| `connector_ref` | string | `--connector-ref` | Name of the outbound connector (registered via `ax gateway connectors add`) that this agent may use for tool calls. Required for `langgraph_composio` template. At apply time, validated against the connector registry — error is actionable if the connector is missing. |

### Timing

| Field | Type | CLI flag | Description |
|-------|------|----------|-------------|
| `timeout_seconds` | int | `--timeout` | Per-message processing timeout in seconds. The daemon kills and restarts the supervised process if it exceeds this value. |

### Hermes-specific

| Field | Type | CLI flag | Description |
|-------|------|----------|-------------|
| `provider` | string | `--provider` | LLM provider name for `hermes_plugin` agents (e.g. `openai-codex`, `anthropic`). Validated against `~/.hermes/auth.json` at registration time. |

---

## The `client` field

`client` is the canonical field for selecting which inference client or MCP host
an agent uses within its `runtime_type`. It answers the question: *"given how
Gateway supervises this agent, which underlying tool does the reasoning?"*

This is distinct from `runtime_type` (supervision model) and `model` (which
model the client calls). The three form orthogonal axes:

```
runtime_type   → how Gateway supervises the agent process
client         → which tool/SDK handles reasoning within that process
model          → which LLM model the client calls
```

### Two namespaces within `client`

The valid values of `client` fall into two non-overlapping namespaces depending
on `runtime_type`:

**MCP host / coding-agent tool** (for `sentinel_cli`, `claude_code_channel`):

Values name the MCP host that runs the agent's reasoning loop. The host
determines which settings file the profiles system writes to and what permission
model applies.

| Value | Host | Settings file |
|-------|------|--------------|
| `claude` | Claude Code CLI | `.claude/settings.local.json` |

Future values: `cursor`, `windsurf`, and other MCP-capable coding agents as
support is added. Default: `claude`.

**Inference SDK** (for `sentinel_inference_sdk`):

Values name the vendor inference SDK that the sentinel dispatches to. These are
always SDK names, never product names — if Anthropic's inference SDK is added,
its value is `anthropic_sdk`, not `claude`, to avoid ambiguity with the Claude
Code CLI host.

| Value | Vendor |
|-------|--------|
| `openai_sdk` | OpenAI |
| `gemini_sdk` | Google Gemini |
| `groq_sdk` | Groq |
| `mistral_sdk` | Mistral |
| `leapfrog_sdk` | Leapfrog |
| `xai_sdk` | xAI |

`client` is required for `sentinel_inference_sdk` — an agent missing it records
a setup error at start time rather than defaulting silently.

`client` is not applicable to `hermes_plugin`, `echo`, `exec`, `inbox`, or
`sentinel_hermes_sdk` (whose backend is always the in-process Hermes AIAgent
loop).

### Registry storage and backwards compatibility

The field is stored as `client` in the registry entry. The daemon reads it with
the following fallback chain for backwards compatibility with pre-ADR-014 entries:

```python
entry.get("client")
  or entry.get("sentinel_sdk_runtime")   # ADR-012 name, deprecated
  or entry.get("hermes_runtime")          # pre-ADR-012 name, deprecated
  or entry.get("sdk_runtime")             # earliest name, deprecated
```

Deprecated aliases are removed from the fallback chain once all known deployments
have been migrated (tracked as a follow-on to ADR-014).

---

## Update semantics

Operational config fields may be updated at any time via `ax gateway agents update`
or `ax gateway agents apply` (manifest). No approval event is required. The
daemon re-reads the config at the next agent start — a running agent is not
interrupted mid-session; restart is required for changes to take effect.

### Fields requiring re-registration

The following fields cannot be changed via `agents update` and require
re-registering the agent (archive + add):

- `runtime_type` — changing the supervision model changes the process shape and
  may require different credential scopes or workspace structure.
- `template_id` — templates set up workspace structure at registration time that
  `update` does not redo.

### Fields not in this tier

The following fields look like config but belong to other tiers:

- `agent_id`, `name`, `install_id`, `space_id`, `base_url` — identity fields;
  changes require an approval event (GATEWAY-AGENT-REGISTRY-001).
- `desired_state`, `effective_state`, `placement_state` — runtime lifecycle
  fields written by the daemon (GATEWAY-RUNTIME-PERSISTENCE-001).
- `token_file`, `pat_source`, `credential_source` — credential provenance;
  written at registration, not operator-mutable.

---

## Manifest relationship

Declarative agent manifests (PR #235, `ax gateway agents apply`) are the
primary interface for managing operational config as code. The manifest schema
(`ManifestDict` in `ax_cli/agent_manifests.py`) is a direct projection of this
field glossary — every manifest field maps to one operational config field, and
every operational config field that is operator-settable has a manifest
equivalent.

Fields absent from a manifest are left unchanged on update (`_UNSET` semantics).
A manifest declares the fields the operator cares about; the rest are preserved
as-is. This is the core declarative semantic.

Example manifest for a `sentinel_inference_sdk` Slack output agent:

```toml
name = "slack-output"
type = "sentinel_inference_sdk"
client = "gemini_sdk"
model = "gemini-2.0-flash"
connector_ref = "composio-main"
allow_all_users = true
description = "Sends Slack output via Composio connector"
```

---

## Open questions

- Should `runtime_type` changes be supported via `agents update` with an
  explicit `--force` flag, or always require re-registration? Current behavior
  is re-registration required.
- Should the `client` field for `sentinel_cli` / `claude_code_channel` be
  inferred from `runtime_type` when absent (defaulting to `claude`) or stored
  explicitly at registration time? Storing explicitly makes entries
  self-describing; inferring keeps the registry smaller.
