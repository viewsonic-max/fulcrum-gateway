# ADR-011: Agent Settings Profiles — Permission Composition Model

**Status:** Accepted — implemented in #232/#242 as `ax_cli/agent_settings_profiles.py` and the `ax agents profiles` command tree, keyed by `client` per ADR-014 (`claude_cli`, not `runtime`). This ADR records the composition model; surface details below are reconciled to the shipped form.

**See also:**

- [ADR-010](ADR-010-channel-setup-separation-of-concerns.md) — establishes `settings.local.json` as the profiles layer's responsibility; this ADR decides how the profiles layer works internally
- [ADR-005](ADR-005-credentials-never-in-workspace.md) — credentials brokered by Gateway; profiles contain permission grants, not credentials
- [ADR-014](ADR-014-client-field-unification.md) — `client` field semantics; profiles are keyed by `client` (the directory and module argument), which this ADR's "by runtime" framing resolves to
- [docs/agent-permission-model.md](../agent-permission-model.md) — the authoritative permission model (post-ADR-012); owns the file-based-profiles vs. connector-policy distinction that bounds this ADR's scope
- [GATEWAY-AGENT-PROFILES-001](../../specs/GATEWAY-AGENT-PROFILES-001/spec.md) — implementation detail for the shipped system: profile directory layout, merge semantics, `agent_settings_profiles` module API, `ax agents profiles` command tree

## Context

> **Scope — this ADR governs the MCP-host / coding-agent client namespace only.**
> Profiles compose a `settings.local.json`-style additive permission document, so they
> apply to clients whose capability surface *is* such a file (`claude_cli` today, with
> `cursor`/`windsurf` plausible future members — the system is keyed by `client` so it
> generalizes within this namespace). They do **not** apply to the connector-policy
> namespaces (hermes, sentinel-SDK), whose capability surface is connector tool policy
> with no settings file. Profiles are **not** a general capability-authorization
> solution: they do not address connector-policy authorization, which is a separate,
> independently-managed mechanism. This boundary is owned by
> `docs/agent-permission-model.md` (from the ADR-012 analysis); see ADR-014 for the
> `client` namespaces.

ADR-010 established that `settings.local.json` belongs to a distinct profiles layer,
separate from both the gateway adapter layer and the runtime client mechanics layer.
What ADR-010 did not decide is how that profiles layer works internally.

Three requirements drove this design:

1. **Different agent roles need different permission sets.** A baseline agent needs
   only `reply` and `get_messages`. An agent-maker needs bash permissions for
   `ax gateway` commands. A code-reviewer needs broad read access. These must not
   be conflated.

2. **An agent may need permissions from multiple roles simultaneously** — an agent
   that is both an agent-creator and a code-reviewer needs permissions from both.

3. **Operators must be able to inspect the effective permission set** of a deployed
   agent and promote hand-configured permissions into named profiles without
   specifying them speculatively upfront.

Two structural approaches for composing profiles were rejected:

- **Hardcoding permission sets** — new roles require code changes; effective
  permissions are opaque to operators.

- **Inheritance chains (`extends:`)** — hard to reason about; an operator should
  understand the full effective permission set without traversing a hierarchy.

A scoping concern also emerged: profile management is reusable across setup paths
(initial deploy, repair, incremental update). It must live in a dedicated module,
not embedded in any single command.

## Decision

### 1. Flat profile files, no inheritance, organized by client

Profiles are flat JSON fragments. Each file is self-contained — it defines only
the additions it makes, with no `extends` or `include` references. There is no
hierarchy to traverse. The subdirectory is the `client` (the `--client` parameter
from ADR-010, e.g. `claude_cli`). Directory layout and file format are in
GATEWAY-AGENT-PROFILES-001.

### 2. `base` is a conventional starting point, not an enforced invariant

`base` is a well-known profile file that ships with the package and contains the
minimum permissions for a functional agent. Most manifests and deploys will include
it as their first profile by convention. It is not enforced by the system — the
manifest is the explicit specification, and an operator with a good reason to omit
it (e.g., a highly restricted agent) can do so. No special enforcement mechanism,
no `--no-base` escape hatch needed.

### 3. Profiles are composed by explicit list; merge is ordered union

The effective profile set is whatever the manifest or `--profile` flags specify,
resolved in order. Multiple profiles compose by union on list fields and last-wins
on scalars. The operator's existing settings file is always merged last and never
overwritten. There is no separate "composition profile" artifact — listing profiles
explicitly IS the composition. Full merge semantics are in GATEWAY-AGENT-PROFILES-001.

### 4. Dedicated profiles module; no profile logic in command files

Profile management logic lives in a dedicated `ax_cli/agent_settings_profiles.py`
module. No profile logic lives in `channel.py`, the gateway command modules, or any
command file. The setup path calls `agent_settings_profiles.apply()` as an independent
layer (today via `ax agents profiles apply`; a future one-command setup orchestrator
would bundle it with the other layers). Module API is in GATEWAY-AGENT-PROFILES-001.

### 5. Profiles entry point; shared module

`ax agents profiles apply` triggers profiles only — re-merges and rewrites
`settings.local.json` without touching `.mcp.json`, the env file, or gateway state. It is
the command for updating an existing agent's permissions independently of the other setup
layers. A future one-command setup orchestrator would call the same
`agent_settings_profiles.apply()` from this shared module.

`ax agents profiles diff` is the inspection command — it shows which entries in the
workspace settings are covered by known profiles and which are unmatched (promotion
candidates). This is the workflow entry point for building new profiles from real
hand-configured workspaces.

> **Shipped behavior differs (reconciliation).** This describes the originally-intended
> `diff` (a no-argument matched/unmatched promotion report). The **shipped** `diff`
> instead previews a specific `--profile` apply; there is no promotion report and no
> `promote` command. See [GATEWAY-AGENT-PROFILES-001](../../specs/GATEWAY-AGENT-PROFILES-001/spec.md)
> for the as-built surface.

## Consequences

- **Positive:** Profile files are readable in isolation — an operator sees exactly
  what a profile grants without context about other profiles.
- **Positive:** New roles require only a new JSON file; no code changes.
- **Positive:** New clients add a subdirectory and a format handler in the module —
  no changes to existing profiles or command files.
- **Positive:** Template definitions encode the right profiles for each agent
  archetype, so setup produces a correctly-permissioned workspace without
  operator knowledge of profile names.
- **Positive:** `ax agents profiles apply` and `diff` give operators clean workflows
  for updating and inspecting permissions independently of other layers.
- **Negative:** Flat profiles allow duplicate entries across files. This is
  intentional — legibility of individual profiles takes priority over DRY.
- **Negative:** Ordered union merge can only *widen* effective permissions, never
  narrow them. A profile intended to scope an agent down has no restrictive effect
  when a broader wildcard is already present (it is merged alongside, and Claude
  Code's OR-based matching means the wildcard still wins). `--reset` is the only
  current narrowing path, and it is blunt — it discards all operator additions and
  unrelated profile state, not just the offending wildcard. A composable narrowing
  mechanism is unresolved (#243).
- **Negative:** Because `base` is a convention rather than an invariant, a manifest
  that omits it will produce an agent without the standard minimum permissions. This
  is intentional — the manifest is the specification — but operators authoring new
  manifests should be aware.
- **Open:** For clients with non-JSON settings formats, `agent_settings_profiles.apply()` needs a
  format dispatcher. Defer until a non-JSON runtime needs profiles.
