# ADR-010: Agent Workspace Layer Separation and Client Generalization

**Status:** Accepted for the separation-of-concerns baseline — the three-layer workspace ownership model and `--client` generalization shipped (#232, #242) and are the foundation ADR-011 builds on. **Decision 4 (the `ax agents deploy` one-command orchestrator) is explicitly *speculative* and deferred to future ADR/architecture work** — it was written before ADR-012/ADR-014 clarified the `client` namespaces and is not a committed design. The draft GATEWAY-AGENT-DEPLOY-001 holds that exploratory work.

**See also:**

- [ADR-005](ADR-005-credentials-never-in-workspace.md) — credentials brokered by Gateway, never copied to workspace
- [ADR-007](ADR-007-agent-classes-and-signals.md) — agent classes; defines the `attached_session` class and `claude_code_channel` template
- [ADR-011](ADR-011-channel-settings-profiles.md) — profiles system design: flat files, composition model, `agent_settings_profiles` module, command tree
- [GATEWAY-AGENT-PROFILES-001](../../specs/GATEWAY-AGENT-PROFILES-001/spec.md) — shipped profiles spec (directory, module API, command tree); the implementation of the permission-profiles layer
- [ADR-013](ADR-013-hermes-plugin-platform-adapter.md) — hermes plugin/platform adapter; relevant to the Toolbelt Option D path the profiles layer feeds
- [ADR-014](ADR-014-client-field-unification.md) — `client` field semantics; the canonical keying this ADR's "client generalization" resolves to (`claude_cli`, not `claude`)
- [GATEWAY-AGENT-DEPLOY-001](../../specs/GATEWAY-AGENT-DEPLOY-001/spec.md) — **draft / speculative**: the future `ax agents deploy` orchestrator (Decision 4), not yet a committed design
- [GATEWAY-RUNTIME-AUTOSETUP-001](../../specs/GATEWAY-RUNTIME-AUTOSETUP-001/spec.md) — zero-touch runtime setup philosophy
- [GATEWAY-AGENT-TOOLBELT-001](../../specs/GATEWAY-AGENT-TOOLBELT-001/spec.md) — toolbelt routing; the profiles layer grants the MCP tool permissions that toolbelt routing exposes

## Context

The `claude_code_channel` template requires two CLI commands to produce a
launch-ready workspace:

```bash
ax gateway agents add <name> --template claude_code_channel --workdir <path>
ax channel setup <name> --workdir <path>
```

`agents add` writes the gateway adapter layer: `.ax/config.toml` and `CLAUDE.md`.
`channel setup` writes the runtime client layer: `.mcp.json` and the channel env file.

During implementation, three gaps were identified:

1. **`.claude/settings.local.json` is not written by either command.** Without it, operators
   face interactive Claude Code approval prompts on first launch that look like a
   broken channel. There is no obvious owner in the current two-command structure.

2. **The two-step sequence is always required** but operators must know to run both.
   There is no single command that produces a launch-ready workspace.

3. **`channel setup` has Claude Code-specific hardcodings** — env file path and launch
   command are specific to the Claude Code CLI. Other MCP clients would require
   forking the command rather than extending it.

The key structural insight: `_write_agent_workspace_config()` in
`ax_cli/commands/gateway_agents.py` is called by both `agents add` and `agents
update`. It must remain generic across all template types — writing Claude
Code-specific files there would corrupt every `agents update`.

Examining where `settings.local.json` belongs revealed that it is neither a gateway-layer
file nor a runtime-client-mechanics file — it governs which permissions the client
grants to its tools and servers, which is a distinct third concern driven by agent
role, not by channel mechanics.

## Decision

### 1. Three distinct workspace layers with clear ownership

Every agent workspace file belongs to exactly one of three layers:

**Gateway adapter layer** — owned by `agents add` and `agents update` via
`_write_agent_workspace_config()`. Files are generic, derived from the gateway
registry entry, and re-written on `agents update` to stay in sync with agent state:

- `.ax/config.toml` — agent identity
- `CLAUDE.md` / `AGENT_CONTEXT.md` — workspace context

**Runtime client mechanics layer** — owned by `channel setup`. Files are
client-specific, written once on setup, and not disturbed by `agents update`:

- `.mcp.json` — MCP server configuration
- Per-agent channel env file — channel identity variables

**Permission profiles layer** — owned by `agent_settings_profiles.apply()` (see ADR-011). Files
are role-specific, written on deploy or explicit profile update:

- `.claude/settings.local.json` — client permission pre-approvals

> **Caveat — the `model` key is not a profiles concern.** The profiles layer owns the
> *permission* content of `settings.local.json`, but the top-level `model` key is
> authored from the gateway registry, not by a profile: the daemon projects the
> registered `model` into this same file on start for `claude_code_channel` (#361/#369).
> Profiles must not author `model` (single-writer — #378). See
> [GATEWAY-AGENT-PROFILES-001](../../specs/GATEWAY-AGENT-PROFILES-001/spec.md) "`model`
> is not a profiles concern."

These three layers are independently re-runnable for repair. A future one-command
orchestrator could invoke all three in sequence (see the speculative Decision 4), but
the layer separation stands on its own regardless of whether that orchestrator is built.

> **Scope — the profiles layer serves one client *namespace*, not all agents.** The
> permission-profiles layer applies to clients whose capability surface is a
> **file-based, additive permission document** — the MCP-host / coding-agent namespace
> (per ADR-014; `claude_cli` today, with `cursor`/`windsurf` plausible future members).
> The system is keyed by `client` precisely so it generalizes within this namespace. It
> does **not** apply to the connector-policy namespaces ADR-014 defines (hermes,
> sentinel-SDK), which have no settings file and configure tool access by other means
> (connectors, SDK/`config.yaml`). So "layer 3" generalizes within the MCP-host namespace
> but is not a universal layer across all agents. (Distinction owned by
> `docs/agent-permission-model.md`, from the ADR-012 analysis.)

### 2. `settings.local.json` belongs to the profiles layer, not `channel setup`

`settings.local.json` content is determined by the agent's role (which tools and servers
it is permitted to use), not by the channel mechanics (how the MCP connection is
configured). Placing it in `channel setup` would conflate role with mechanics —
a different role profile would require re-running channel setup, which is wrong.

`channel setup` owns `.mcp.json` and the env file only. `agent_settings_profiles.apply()` owns
the permission content of `settings.local.json` (the `model` key excepted — see the caveat
above). Each is independently re-runnable (and a future deploy orchestrator would invoke
them). The profiles system design is in ADR-011.

### 3. `channel setup` should be client-parameterized

`channel setup` implements the MCP channel mechanics, which are protocol-level and
not specific to Claude Code. The command accepts a `--client` parameter (default:
`claude_cli`) that drives the env file location and generated launch command. Adding a
new MCP client is adding a client variant, not modifying shared mechanics. **(Shipped
in #242.)**

### 4. `ax agents deploy` one-command orchestrator *(speculative — deferred to future ADR/architecture work)*

> This decision is **not committed.** It records the original direction that motivated
> the separation of concerns above, but the `deploy` orchestrator itself is unbuilt and
> predates the ADR-012/ADR-014 `client`-namespace clarification. Treat it as a sketch of
> intent, not an accepted design. The exploratory detail lives in the draft
> GATEWAY-AGENT-DEPLOY-001; a future ADR should ratify (or revise) the orchestrator —
> in particular, `deploy` is intended to be **namespace-independent**, with the
> three-layer sequence below being only its `claude_cli` implementation. Other client
> namespaces would have different per-namespace implementations.

The sketch: `agents add` stays a pure gateway primitive — it registers identity and
writes gateway-layer files only, no side effects, no escape-hatch flags (CI that wants
gateway-only registration uses it directly). A future `ax agents deploy` would, for a
`claude_cli` agent, invoke the three layers in sequence:

1. Gateway registration (`agents add` equivalent)
2. Runtime client mechanics (`channel setup`)
3. Permission profiles (`agent_settings_profiles.apply()`) — `claude_cli` only

making the layer boundaries explicit in the orchestration, each step failing and
repairing independently. Whether this is the right shape — and how non-`claude_cli`
namespaces deploy — is the open work.

## Consequences

- **Positive:** Every workspace file has a single owning layer. Future contributors
  know where new files belong without case-by-case judgment.
- **Positive:** `agents update` re-writes only gateway-layer files. Runtime client
  and profiles files are not disturbed by platform config changes.
- **Positive:** New workspaces launch silently — no interactive approval prompts.
- **Positive:** `channel setup` generalization supports future MCP clients without
  command proliferation.
- **Positive:** `agents add` stays a clean gateway primitive with no hidden side effects.
- **Positive (speculative):** should the `deploy` orchestrator (Decision 4) be ratified,
  it is the natural home for manifest-driven deployment as agent roles become more
  specific and automation becomes the primary use case.
- **Negative:** `--client` parameterization adds surface area to `channel setup`.
  Non-default client paths must be clearly gated until implemented.
- **Mixed:** the permission-profiles layer generalizes *within* the MCP-host
  (file-based-permission) namespace (keyed by `client`; `claude_cli` today,
  `cursor`/`windsurf` plausible) but not *across* namespaces — it has no meaning for the
  connector-policy clients (hermes, sentinel-SDK). Any future `deploy` must dispatch the
  profiles step by namespace rather than treat it as universal (this is part of why
  Decision 4 is deferred).
