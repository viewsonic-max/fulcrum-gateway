# ADR-012 — sentinel_inference_sdk rename and CLI bypass removal

**Status:** Accepted
**Date:** 2026-06-05
**Author:** Mark Galpin

---

## Context

The Gateway supervisor manages several agent runtime types. Two of them — `hermes_sentinel` and `sentinel_cli` — supported CLI subprocess backends (Claude Code and Codex) in addition to SDK-based LLM calls. All CLI subprocess paths hardcoded permission bypass flags that gave agents unrestricted tool access with no per-agent authorization.

The profiles system that enables safe pre-authorization for these agents is defined in [ADR-011](ADR-011-channel-settings-profiles.md) and specified in [GATEWAY-AGENT-PROFILES-001](../../specs/GATEWAY-AGENT-PROFILES-001/spec.md). ADR-012 is a prerequisite to that system being meaningful: there is no point managing `settings.local.json` permission grants while a hardcoded bypass flag makes them optional.

- `--dangerously-skip-permissions` — hardcoded in every Claude CLI invocation across three code paths: `gateway.py:_build_sentinel_claude_cmd`, `sentinel.py:_build_claude_cmd`, and `runtimes/claude_cli.py`
- `--dangerously-bypass-approvals-and-sandbox` — hardcoded in every Codex CLI invocation: `gateway.py:_build_sentinel_codex_cmd`, `sentinel.py:_build_codex_cmd`, and `runtimes/codex_cli.py`

The full pre-change permission model is documented in [ADR-012A1-previous-agent-permission-model.md](ADR-012A1-previous-agent-permission-model.md).

### The bypass problem

These flags are not operator-configurable escape hatches — they are unconditional. Every agent running the Claude or Codex CLI path through Gateway has unrestricted access to all tools the CLI supports, including Bash. There is no Gateway-managed constraint that limits what those agents can do with ambient credentials.

The `--allowedTools` flag was available for `sentinel_cli` Claude agents as an opt-in restriction, but it was not required and defaulted to unrestricted. An agent with no `allowed_tools` registry entry had full Bash access with no audit trail beyond what the CLI itself captured.

### Codex has no safe headless model

Codex's permission model is sandbox-based (filesystem write scope), not tool-name-based. There is no `--allowedTools` equivalent. `--sandbox workspace-write` restricts filesystem writes but still allows reading credentials anywhere on the filesystem, inheriting environment variable credentials, and making arbitrary network calls. The only alternative to `--dangerously-bypass-approvals-and-sandbox` is interactive mode — which requires a human at a terminal and cannot be wired to the platform approval flow.

### hermes_sentinel is a misleading name

The `hermes_sentinel` runtime was originally a Claude Code sentinel implemented in the Hermes agent framework. Over time it acquired support for multiple backends: Claude CLI subprocess, Codex CLI subprocess, and SDK-based calls to vendor LLMs (OpenAI, Groq, Mistral, Gemini, etc.). The CLI subprocess variants were always a poor fit — `sentinel_cli` already covers that use case directly via Gateway. What `hermes_sentinel` actually hosts today is a set of SDK adapters for third-party LLM vendors, wrapped in the Hermes supervisor model. The name does not reflect this.

---

## Decision

### 1. Remove `--dangerously-skip-permissions` from all Claude CLI paths

The flag is removed from:
- `gateway.py:_build_sentinel_claude_cmd` (sentinel_cli Claude)
- `sentinel.py:_build_claude_cmd` (sentinel_inference_sdk Claude — dead code, removed with the legacy path)
- `runtimes/claude_cli.py` (sentinel_inference_sdk Claude CLI plugin)

Consequence: `sentinel_cli` Claude agents without a `settings.local.json` permissions configuration will have text-only capability after this change. The agent can respond to messages but cannot invoke tools. Operators must apply a profile to grant tool access — see [ADR-011](ADR-011-channel-settings-profiles.md) and [GATEWAY-AGENT-PROFILES-001](../../specs/GATEWAY-AGENT-PROFILES-001/spec.md) for the profiles system and `ax agents profiles apply`. This is the intended behavior — tool access should be explicitly authorized, not implicitly granted.

### 2. Remove the Codex CLI runtime entirely

`runtimes/codex_cli.py`, `_build_sentinel_codex_cmd`, and `_build_codex_cmd` are deleted. The `codex` and `codex_cli` choices are removed from the `--runtime` argparse in `sentinel.py`.

**Alternatives considered and rejected:**

*Operator-configurable bypass flag.* Rather than deleting the code, we considered keeping it gated behind an explicit opt-in — for example, a registry field like `allow_codex_bypass: true` that would conditionally pass `--dangerously-bypass-approvals-and-sandbox`. This was rejected because it would shift responsibility onto operators without giving them a safer path forward. Codex has no `--allowedTools` equivalent: even with a bypass flag properly gated, there is no mechanism to scope what the agent can do once running. The bypass is all-or-nothing. An operator enabling the flag would be getting the same unrestricted access that exists today, just with an extra click. The only honest description of the flag is "disable all agent safety boundaries," which should not be a valid operator choice in a Gateway-supervised runtime.

More fundamentally: configurable bypass flags assume a human operator who can read the flag name, understand the tradeoff, and make an informed decision. Gateway's purpose is to supervise AI agents, not human users. An AI agent running under the bypass flag cannot reason about whether that flag should have been set — it simply executes with whatever ambient permissions it was given. The safety boundary is Gateway's responsibility, not the agent's. A flag that removes Gateway's enforcement while placing the decision with the agent inverts the trust model entirely.

*Exception-guarded bypass.* We also considered wrapping the bypass invocation in try/except so that if Codex ever added a non-bypass headless mode, we could fall back gracefully. This was rejected because the exception would never fire — Codex silently rejects the flag rather than raising — and because keeping the dead-end code path alive would create maintenance surface without benefit.

More importantly, even if Codex added a safe headless mode, the existing code would not become safe by simply un-guarding it. Any viable reimplementation of Codex CLI support would require supervision infrastructure we do not currently have: injected system prompts that scope the agent's behavior to its declared use case, use-case filtering to constrain what tasks Codex can be handed, and audit tooling that goes beyond what the CLI itself captures. Codex's sandbox model does not compose with connector policy the way the SDK runtimes do. A future implementation would be a new design effort, not an un-deletion of the present code. Leaving the code in place as a placeholder would suggest a shallower effort than is actually required.

The core issue is not implementation complexity but authorization model incompatibility: Claude's permission model is tool-name-based (settings.local.json lists allowed tools by name), which the profiles system in ADR-011 can populate. Codex's model is sandbox-based (filesystem write scope), with no named-tool filtering. There is no safe authorization model we can build for Codex headless execution that is comparable to what Claude offers. Removal forces operators toward `openai_sdk`, which has proper connector policy enforcement.

There is no safe headless Codex model available. Operators who need OpenAI model access should use `openai_sdk` under `sentinel_inference_sdk`.

### 3. Remove CLI subprocess paths from the sentinel_inference_sdk runtime

The `sentinel_inference_sdk` runtime (previously `hermes_sentinel`) is re-scoped to SDK-only. The `_build_claude_cmd` and `_build_codex_cmd` functions in `sentinel.py` are deleted along with the entire legacy subprocess fallback path in `run_cli`. Claude CLI subprocess access is available via `sentinel_cli` directly. The `claude_cli.py` plugin survives as the implementation backing `sentinel_cli` for operators who need the Hermes supervisor model to manage a Claude subprocess, but it is no longer reachable from `sentinel_inference_sdk`.

### 4. Rename `hermes_sentinel` → `sentinel_inference_sdk`

The runtime type ID, all dispatch checks, internal function names, and UI strings are updated throughout `gateway.py`, `gateway_runtime_types.py`, and `commands/gateway.py`. The runtime catalog entry is updated with a new label, description, and `successor_runtime_type` pointer from the old `hermes_sentinel` ID.

**Why keep it at all — why not remove it entirely?**

This requires reading [ADR-013](ADR-013-hermes-plugin-platform-adapter.md) alongside the current state. ADR-013 records the decision to replace the "per-mention sentinel-subprocess pattern" with `hermes_plugin` — a long-lived `hermes gateway run` process. On its face, that looks like an argument for removing `hermes_sentinel` rather than renaming it.

The distinction is what `hermes_sentinel` / `sentinel_inference_sdk` *actually does* after this ADR. Before this change, its `run_cli` dispatch had two populations of runtime:

1. CLI subprocess backends (Claude CLI, Codex CLI) — these spawned a new process *per mention*. This is exactly the pattern ADR-013 deprecated. We removed these in decisions 2 and 3 above.

2. SDK runtime backends (openai_sdk, groq_sdk, mistral_sdk, gemini_sdk, leapfrog_sdk, hermes_sdk) — these run within a long-lived daemon process. No per-mention subprocess spawning. No CLI bypass flags. Tool authorization is handled by connector policy.

After this ADR, `sentinel_inference_sdk` is exclusively the second population. The behavior ADR-013 deprecated is gone. What remains is a Gateway-supervised persistent daemon that dispatches to vendor SDK runtimes — a different shape that does not conflict with `hermes_plugin`'s design goals.

Note that not all SDK backends in this population are equivalent. `openai_sdk`, `groq_sdk`, `mistral_sdk`, `gemini_sdk`, and `leapfrog_sdk` are lightweight direct vendor API calls. `hermes_sdk` is architecturally distinct: it runs a full in-process Hermes AIAgent loop with Bedrock IAM, OpenRouter, and Anthropic API backends — a different shape that does not belong under the same runtime type. Decision 5 below addresses this.

The runtimes that remain serve different operator needs:

- `hermes_plugin`: operators running Hermes agents with its full agentic loop, tool system, and platform plugin architecture. Hermes manages the agent's reasoning and tool calls natively.
- `sentinel_inference_sdk`: operators running lightweight direct vendor API calls — a simpler shape where the vendor API is the agent. No local agentic framework dependency. No equivalent `hermes_plugin` path exists for openai_sdk, groq_sdk, etc.
- `sentinel_hermes_sdk`: operators running the in-process Hermes AIAgent loop via sentinel infrastructure — see decision 5.

Removing `sentinel_inference_sdk` would break all existing deployments using non-Hermes vendor SDKs with no migration path. The rename retains those agents while stripping the parts ADR-013 rightly criticized.

### 5. Promote `hermes_sdk` to its own runtime type: `sentinel_hermes_sdk`

`hermes_sdk` is not a lightweight vendor API call — it runs a full in-process Hermes AIAgent loop (90-turn agentic loop, parallel tool execution, context compression, subagent delegation) with its own tool security layer (`_secure_hermes_tools`) and connector tool registration. It also carries the only Bedrock IAM support in the sentinel stack. Leaving it as an undifferentiated member of `sentinel_inference_sdk` misrepresents its shape and its capabilities.

`sentinel_hermes_sdk` is a peer runtime type alongside `sentinel_inference_sdk`, not a choice within it. Both are sentinels — both spawn `sentinel.py` and use the same SSE listener, session store, history store, and connector policy infrastructure. The difference is the backend: `sentinel_inference_sdk` dispatches to a direct vendor API; `sentinel_hermes_sdk` dispatches to the in-process Hermes AIAgent loop via `--runtime hermes_sdk`.

Consequences:

- `hermes_sdk` is removed from `_HERMES_SENTINEL_SDK_RUNTIMES` and is no longer a valid `client` choice within `sentinel_inference_sdk`.
- The `client` field (see ADR-014) is required for `sentinel_inference_sdk` agents — there is no default. An agent missing it will record a setup error rather than silently picking a backend. The old field name `sentinel_sdk_runtime` is removed as a breaking change; operators must use `client` (via `ax gateway agents update <name> --client <value>`).
- `sentinel_hermes_sdk` gets its own catalog entry, dispatch predicate, and resolved runtime in the dispatch layer (`gateway.py`). Both runtime types share the same command and environment builder; the resolved runtime is passed as a parameter.
- Bedrock IAM auth (`bedrock:claude-*` model prefix, `AWS_REGION`, instance profile) is supported exclusively via `sentinel_hermes_sdk`.

---

## Breaking changes

| Change | Impact | Migration |
|---|---|---|
| `runtime_type: hermes_sentinel` no longer valid | All existing `hermes_sentinel` agents | Update registry entry: `ax gateway agents update <name> --type sentinel_inference_sdk` |
| `sentinel_cli` Claude agents lose unrestricted tool access | Agents without `settings.local.json` become text-only | Apply a profile: `ax agents profiles apply <name> --profile base` |
| Codex CLI runtime removed | Any agent using `runtime_type: sentinel_cli` with `sentinel_runtime: codex` | No path within Gateway; use `openai_sdk` under `sentinel_inference_sdk` for OpenAI models |
| `--runtime codex/codex_cli` removed from `sentinel_inference_sdk` argparse | Agent configs passing `--runtime codex` to `sentinel.py` | Switch to `--runtime openai_sdk` |
| `client: hermes_sdk` no longer valid within `sentinel_inference_sdk` | Any `sentinel_inference_sdk` agent relying on the `hermes_sdk` default or explicit setting | Change `runtime_type` to `sentinel_hermes_sdk`; remove `client` / `sentinel_sdk_runtime` field |
| `client` field is now required for `sentinel_inference_sdk` (was `sentinel_sdk_runtime`) | Any `sentinel_inference_sdk` agent without a client configured | Set via CLI: `ax gateway agents update <name> --client openai_sdk` (or `groq_sdk`, `gemini_sdk`, etc.) |

---

## Non-breaking

- `sentinel_inference_sdk` SDK runtime agents (`openai_sdk`, `groq_sdk`, `mistral_sdk`, `gemini_sdk`, `leapfrog_sdk`) are unaffected. The supervisor and plugin dispatch logic are unchanged.
- `sentinel_hermes_sdk` is a new runtime type introduced in this PR alongside the rename; there are no existing `sentinel_inference_sdk` agents in production to migrate.
- `hermes_plugin` agents are unaffected.
- `claude_code_channel` agents are unaffected.
- Connector policy enforcement is unaffected.

---

## Post-change permission model and forward direction

The updated permission model is in [agent-permission-model.md](../agent-permission-model.md).

The permission analysis that drove this ADR also produced a concept now called **agent archetypes** — declarative bundles specifying runtime type, profiles, connector policy, system prompt, and model. (Deliberately *not* "agent class": ADR-007 owns that term for the five signaling-contract classes.) An archetype is the spec a future one-command setup orchestrator would materialize; it is the unit the platform UI would expose as a named role. The permission-surface distinction (file-based profiles vs. connector policy) has been back-ported into [ADR-011](ADR-011-channel-settings-profiles.md) and `docs/agent-permission-model.md`; the archetype-driven orchestrator remains future work (see ADR-010 Decision 4).
