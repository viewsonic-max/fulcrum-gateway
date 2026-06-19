# GATEWAY-AGENT-PROFILES-001: Agent Settings Profiles

**Status:** Accepted / shipped — implemented in #232/#242 as `ax_cli/agent_settings_profiles.py` and the `ax agents profiles` command tree. This spec documents the shipped system; the decision record is ADR-011.
**Owner:** @markgalpin
**Date:** 2026-06-05 (reconciled to shipped reality 2026-06-18)

**Related:**

- [ADR-011](../../docs/adr/ADR-011-channel-settings-profiles.md) — the profiles composition-model decision this spec implements
- [ADR-014](../../docs/adr/ADR-014-client-field-unification.md) — `client` field semantics; profiles are keyed by `client`
- [docs/agent-permission-model.md](../../docs/agent-permission-model.md) — authoritative permission model (post-ADR-012); owns the file-based-profiles vs. connector-policy distinction that bounds this spec
- [GATEWAY-MANAGED-AGENT-CONFIG-001](../GATEWAY-MANAGED-AGENT-CONFIG-001/spec.md) — operational-config tier (registry fields incl. `model`)

## Why this exists

A Fulcrum agent is constructed for a specific job, so its capability envelope is largely
known when the agent is built. The right model is therefore **pre-authorization**:
declare upfront what the agent may do — and, where it matters, what it must never do — so
it operates within a fixed boundary rather than negotiating permissions at runtime.

Claude Code *can* gate or forward tool decisions several ways (declarative
`allow`/`deny`/`ask` rules, permission modes, the Agent SDK `canUseTool` callback, or an
MCP `--permission-prompt-tool`). Fulcrum deliberately does **not** wire any of these up as
an interactive runtime-approval channel. The reason is **not** that there is no human — an
agent usually has a human partner — but that **the partner is not necessarily authorized
to grant what the agent might ask for.** Capability authorization is a governance decision
made by whoever constructs and deploys the agent (an operator with the authority to grant
it), not something the person collaborating with the agent at runtime can expand by
clicking "approve." Forwarding prompts to whoever happens to be partnering with the agent
would let an unauthorized party escalate its scope.

That shapes the model:

- **Authorization stays with the operator.** The allow/deny envelope is set when the agent
  is built, by someone with authority to grant it. The runtime partner works *within* that
  envelope; they cannot widen it.
- **Pre-emptive deny, not just allow.** Even where a prompt *could* be forwarded, there are
  capabilities we never want the agent to use or even request. `deny` rules (which take
  precedence over allow) fix that in advance, independent of whether a prompt could
  otherwise reach a human.
- **No prompt-spam.** We already know the tools an agent needs for its job. Interactively
  re-approving each one every run is noise; declaring them upfront removes it.
- **Headless has no session anyway.** `sentinel_cli` runs `claude -p` with no interactive
  session, so pre-authorization is the only option there regardless.

This is the current model, not a permanent stance. Forwarding *selected* authorization
prompts to an appropriately-authorized human may make sense later — but only once we have
(a) **narrowed the surface** on which such prompts can arrive (which tools, which contexts
can ask) and (b) **bounded the blast radius** of any single approval (what one "yes" can
enable). Until both are controlled, pre-authorization with explicit allow/deny is the safe
default, and a forwarding path would be an addition layered on top of it, not a
replacement.

For Claude Code the boundary is `.claude/settings.local.json` (`permissions.allow` /
`permissions.deny`). Profiles are the management surface for composing that file from
named, reusable fragments (a `base` role, an `agent-maker` role, a `code-reviewer` role)
rather than hand-editing JSON per agent.

## Scope

> **This spec governs the MCP-host / coding-agent client namespace only.** Profiles
> compose a `settings.local.json`-style additive permission document, so they apply to
> clients whose capability surface *is* such a file — the MCP-host namespace (per
> ADR-014): `claude_cli` today, with `cursor`/`windsurf` plausible future members. The
> system is keyed by `client` precisely so it generalizes within this namespace. Profiles
> do **not** apply to the connector-policy namespaces (hermes, sentinel-SDK), whose
> capability surface is connector tool policy with no settings file. **Profiles are not a
> general capability-authorization solution** — they do not solve, or even touch,
> connector-policy authorization; that is a separate, independently-managed mechanism.
> That boundary is owned by `docs/agent-permission-model.md` (from the ADR-012 analysis).

**In:**

- `ax_cli/agent_profiles/<client>/` — profile file directory convention (e.g. `claude_cli/`)
- `agent_settings_profiles` module — resolve, merge, apply, diff logic
- `ax agents profiles` subcommand tree — profile management CLI (`list`, `show`, `diff`, `apply`)

**Out:**

- Capability authorization for connector-policy namespaces (hermes, sentinel-SDK) — a separate mechanism (connector instance + tool policy); profiles do not address it. See `docs/agent-permission-model.md`
- The one-command deployment orchestrator — future work, out of scope here
- Profile file contents for specific roles — those are config, not spec
- The `model` key — registry-authored, not a profiles concern (see "`model` is not a profiles concern")

## Directory layout

Profiles are keyed by **`client`** (per ADR-014):

```text
ax_cli/agent_profiles/
  claude_cli/
    base.json          # enabledMcpjsonServers + permissions.allow mcp__ax-channel__* — every claude_cli agent
    agent-maker.json   # Bash permissions for ax gateway agent management commands
    code-reviewer.json # Read permissions for broad workspace read access
    ...
  echo/                # other clients add a sibling directory, same convention
    ...
```

Profile files are flat JSON fragments — they define only the keys they add. They do
not reference or extend other profiles. Adding a new role = adding a new file.

## Merge semantics

Profiles are merged in order: `base` → named profiles in specified order →
operator's existing settings file.

- **List fields** (`permissions.allow`, `enabledMcpjsonServers`): union — all entries
  combined, duplicates removed, base-first then profile order.
- **Scalar fields**: last-wins — later profile overrides earlier.
- **Operator's existing settings file**: merged last, always wins. A re-run never
  removes operator additions.

> **Open limitation — `apply` can only widen, never narrow (#243).** Because list
> fields union and the existing file always wins, `apply` *without* `--reset` is
> structurally incapable of restricting an agent below what its settings already allow.
> The trap: applying a "narrower" profile (e.g. one scoping an agent down to
> `mcp__ax-channel__reply`) onto settings that already contain a broader
> `mcp__ax-channel__*` produces *both* entries — the wildcard still matches everything
> and the narrowing is silently ineffective, even though the operator was told the
> restricted profile was applied.
>
> This is a **known gap, not a settled design.** Today `--reset` is the only narrowing
> path, but it is blunt: it discards *all* operator additions and unrelated profile
> state, not just the subsuming wildcard, so it cannot express "narrow this one family,
> keep everything else." A merge-time subsumption check alone cannot fix it either —
> the merge cannot infer whether a more-specific entry means "also allow this" (keep the
> wildcard) or "restrict to this" (drop the wildcard); identical inputs, opposite
> intent. See Open questions for the likely direction (an explicit, composable narrowing
> semantic on profiles). #243 tracks an interim operator warning.

## `agent_settings_profiles` module

Profile management logic lives in `ax_cli/agent_settings_profiles.py`. No profile
logic lives in `channel.py` or the gateway command modules. Public surface as shipped
(note the argument order — `profiles, client, workdir` — and that `client` replaced
the originally-proposed `runtime`):

```python
agent_settings_profiles.apply(profiles, client, workdir, *, reset=False) -> Path
# Merges the named profiles for `client` in order into the client's settings file
# (`.claude/settings.local.json` for claude_cli) and writes it. With reset=False,
# merges into existing settings (list union, scalar last-wins). With reset=True,
# starts from a clean slate (only profile content). Raises ValueError for
# unsupported clients.

agent_settings_profiles.diff(profiles, client, workdir, *, reset=False) -> dict
# Previews what applying `profiles` would change against the current settings file.
# Without reset, the merge is widen-only so the "remove" set is empty; with reset=True
# it previews the apply --reset result (which can remove keys). NOTE: this is a
# "preview this apply" diff, not a matched/unmatched promotion-candidate report.

agent_settings_profiles.resolve(profiles, client) -> dict
# Returns the merged profile dict without writing. Used by diff and dry-run display.

agent_settings_profiles.list_available(client) -> list[str]
# Returns profile names (filenames without .json) available for `client`.

agent_settings_profiles.list_all() -> dict[str, list[str]]
# Returns {client: [profile names]} across all client directories.

agent_settings_profiles.current_profile_list(workdir, client) -> list[str]
# Returns the profile list recorded in the workspace (the _axProfiles list, written by apply).

agent_settings_profiles.agent_info_from_registry(agent_name) -> dict | None
# Resolves an agent's workdir/client from the gateway registry for the CLI commands.
```

## `ax agents profiles` subcommand tree

Profile management commands live under `ax agents profiles`, separate from
`ax channel`, because they are client-agnostic within the namespace — they apply to any
gateway-managed agent whose client is a profiles client, not only channel agents. The
shipped tree is `list`, `show`, `diff`, `apply`. Agent name resolves workdir + client
from the registry; `--client`/`--workdir` override.

### `ax agents profiles list`

```bash
ax agents profiles list [--client <name>]
```

Lists available profiles. With `--client` (e.g. `claude_cli`), filters to that
client's directory; without it, lists all clients. Output is a table of client and
profile name.

### `ax agents profiles show`

```bash
ax agents profiles show <agent-name> [--client <name>] [--workdir <path>]
```

Shows the profiles currently recorded for the agent (the `_axProfiles` list written
by `apply`) and the resulting settings.

### `ax agents profiles diff`

```bash
ax agents profiles diff <agent-name> --profile <name>... [--reset] [--client <name>] [--workdir <path>]
```

Previews what applying the given `--profile`(s) would change in the agent's
`settings.local.json`. Without `--reset` the merge is widen-only (nothing is removed);
`--reset` previews the `apply --reset` result (replace rather than merge, which can
remove keys). At least one `--profile` is required.

### `ax agents profiles apply`

```bash
ax agents profiles apply <agent-name> \
    --profile <name>... \
    [--reset] \
    [--client <name>] \
    [--workdir <path>]
```

Writes the settings file for the agent without re-running channel mechanics. At least
one `--profile` is required.

- `--profile` — the profile list to apply (repeatable). Merged in order.
- `--reset` — rewrites settings.local.json to exactly the resolved profile set,
  discarding operator additions; use when the workspace has drifted and should be
  brought back to the declared profile state. Without `--reset`, the existing file is
  merged (list union, scalar last-wins) and operator additions are preserved.

### Not shipped (originally proposed)

The original draft also proposed the following, which **did not ship** and should not
be treated as available:

- `--add-profile` / `--remove-profile` incremental modes on `apply` — the shipped
  `apply` takes an explicit `--profile` list only. Incremental add/remove against the
  recorded list is not exposed on the CLI (the module retains `current_profile_list`
  but no command consumes it for add/remove).
- `ax agents profiles promote` — capturing unmatched entries into a new profile file.
  Deferred; the shipped `diff` does not produce the promotion-candidate report it
  depended on.

## Permission removal

**Reset to declared profiles** (`--reset` on `ax agents profiles apply`) — rewrites
settings.local.json to exactly the resolved result of the supplied `--profile` list,
discarding operator additions. This is the inverse of "operator always wins" — it
enforces the declared profile state. Use when a workspace has accumulated
hand-configured permissions that should be cleared, or when auditing that an agent's
effective permissions match its declared role. **`--reset` is also the only way to
*narrow* an agent's permissions** — see the widen-only caveat under Merge semantics
(#243); a narrowing profile applied without `--reset` has no restrictive effect.

> **Divergence from the original draft:** the draft also described per-profile removal
> via `--remove-profile`, computing the union minus one named profile using the
> workspace's recorded list. That mode **did not ship** — the shipped `apply` takes an
> explicit `--profile` list and `--reset` only. To drop a profile today, re-`apply`
> the desired list with `--reset`.

## `model` is not a profiles concern

The agent's `model` is a **registry operational-config field**, not something profiles
author. The authoritative source is the gateway registry (`model`, set via `--model` or a
manifest — see [GATEWAY-MANAGED-AGENT-CONFIG-001](../GATEWAY-MANAGED-AGENT-CONFIG-001/spec.md)).
*How* that registry value reaches each runtime is out of scope for this spec — see
GATEWAY-MANAGED-AGENT-CONFIG-001 for the field and #361/#369 for the per-runtime
application path.

The one rule the profiles layer must respect: **a profile fragment must not author
`model`.** If it did, the profile and the registry would be two competing writers,
leaving `settings.local.json` internally inconsistent (a profile-authored model with the
registry's value, or vice versa). This is convention today; single-writer enforcement
(profiles refuse/strip `model`) is tracked in **#378**.

## Open questions

- **How should profiles express *narrowing*? (#243 — unresolved)** Union merge is
  widen-only and `--reset` is all-or-nothing (see the Merge semantics caveat). The
  likely direction is a first-class, composable narrowing semantic on profile fragments
  rather than relying on `--reset`: e.g. a profile can mark its `permissions.allow` for a
  given prefix family as *authoritative* (replace-not-union for that family), or carry an
  explicit `remove`/`deny` list. This preserves additive ergonomics by default while
  making scope-down a declarative operation that composes with other profiles and does
  not discard unrelated operator state. A merge-time wildcard-subsumption check alone is
  insufficient — the merge cannot infer add-vs-restrict intent from identical inputs, so
  intent must be carried in the profile format. Decide the format before building beyond
  the interim warning #243 proposes.

## TODOs

- **Profile-discovery workflow + `promote`** — the original draft assumed a `diff` that
  reports matched/unmatched promotion candidates and a `promote` that captures them.
  Neither shipped (the shipped `diff` previews a specific apply). Revisit whether the
  discovery workflow is still wanted before building `promote`.
- **Incremental `apply --add-profile/--remove-profile`** — not shipped; `apply` takes an
  explicit `--profile` list. File an issue if incremental editing against the recorded
  `_axProfiles` list is wanted.
- **Additional profiles clients** — only `claude_cli` ships a profile directory today.
  Adding `cursor`/`windsurf` is an entry in `_CLIENT_CONFIG` (settings path) plus a
  profile directory; no merge-engine changes. Profiles for a non-JSON settings format
  would need a format dispatcher in `apply()`.

## Cross-references

- [ADR-011](../../docs/adr/ADR-011-channel-settings-profiles.md) — profiles composition-model decision
- [ADR-014](../../docs/adr/ADR-014-client-field-unification.md) — `client` field semantics; profiles keyed by `client` (`claude_cli`)
- [docs/agent-permission-model.md](../../docs/agent-permission-model.md) — file-based profiles vs. connector-policy boundary
- [GATEWAY-MANAGED-AGENT-CONFIG-001](../GATEWAY-MANAGED-AGENT-CONFIG-001/spec.md) — operational-config tier (registry `model`); the source the daemon projects from
- [GATEWAY-AGENT-TOOLBELT-001](../GATEWAY-AGENT-TOOLBELT-001/spec.md) — toolbelt routing; `base.json` grants the MCP tools toolbelt routing exposes
