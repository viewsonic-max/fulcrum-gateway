"""ax gateway — managed-agent CRUD, workspace scaffolding, and the `agents` sub-app.

Extracted from ``ax_cli/commands/gateway.py`` (issue #28 Phase 1).
"""

from __future__ import annotations

import json
import os
import shutil
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

import typer

from .. import gateway as gateway_core
from ..client import AxClient
from ..commands.bootstrap import (
    _create_agent_in_space,
    _find_agent_in_space,
    _mint_agent_pat,
    _polish_metadata,
)
from ..config import resolve_space_id
from ..gateway import (
    _INFERENCE_SDK_CLIENTS,
    _MCP_HOST_CLIENT_BINARIES,
    _is_passive_runtime,
    active_gateway_pid,
    activity_log_path,
    agent_token_path,
    agent_token_relpath,
    annotate_runtime_health,
    apply_entry_current_space,
    archive_stale_gateway_approvals,
    deny_gateway_approval,
    ensure_gateway_identity_binding,
    ensure_local_asset_binding,
    evaluate_runtime_attestation,
    find_agent_entry,
    gateway_dir,
    get_gateway_approval,
    hermes_setup_status,
    inference_sdk_client_names,
    load_agent_pending_messages,
    load_gateway_managed_agent_token,
    load_gateway_registry,
    load_gateway_session,
    load_recent_gateway_activity,
    ollama_setup_status,
    record_gateway_activity,
    remove_agent_entry,
    resolve_agent_token_file,
    save_agent_pending_messages,
    save_gateway_registry,
    upsert_agent_entry,
)
from ..gateway_runtime_types import _bridge_python
from ..manifest_template_library import (
    agent_template_definition,
    agent_template_list,
    copy_template_manifest,
    template_manifest_path,
)
from ..output import JSON_OPTION, console, err_console, print_json, print_kv, print_table
from .gateway_app import _UNSET, agents_app

# Operator-facing help text for the --client option on `ax gateway agents
# add` and `ax gateway agents update`. Generated from the allowlist
# (via `inference_sdk_client_names()`) so adding a new SDK runtime to
# `_INFERENCE_SDK_CLIENTS` in `gateway_hermes.py` updates this text
# automatically. Closes the recurring help-text drift gap from #326.
_CLIENT_HELP = (
    "MCP host or inference SDK client "
    f"(claude_cli for sentinel_cli; {' | '.join(inference_sdk_client_names())} "
    "for sentinel_inference_sdk). Not accepted for claude_code_channel."
)

# Agents-list cache: serves last-good upstream response when paxai.app
# rate-limits us, mirroring the spaces cache pattern in PR #148. The cache
# is best-effort — write/read failures are swallowed; we never fail a
# request because we couldn't update cache.


def _agents_cache_path() -> Path:
    return gateway_dir() / "agents.cache.json"


def _load_agents_cache() -> list[dict]:
    try:
        raw = json.loads(_agents_cache_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    items = raw.get("agents") if isinstance(raw, dict) else raw
    return [item for item in (items or []) if isinstance(item, dict)]


def _save_agents_cache(agents: list[dict]) -> None:
    payload = {"agents": agents, "saved_at": datetime.now(timezone.utc).isoformat()}
    try:
        _agents_cache_path().write_text(json.dumps(payload), encoding="utf-8")
    except OSError:
        pass


def _save_agent_token(name: str, token: str) -> Path:
    token_path = agent_token_path(name)
    token_path.write_text(token.strip() + "\n")
    token_path.chmod(0o600)
    return token_path


def _load_managed_agent_or_exit(name: str) -> dict:
    registry = load_gateway_registry()
    entry = find_agent_entry(registry, name)
    if not entry:
        err_console.print(f"[red]Managed agent not found:[/red] {name}")
        raise typer.Exit(1)
    return entry


def _registry_ref_for_agent(registry: dict, target: dict) -> str | None:
    target_name = str(target.get("name") or "").lower()
    target_install_id = str(target.get("install_id") or "")
    for index, entry in enumerate(registry.get("agents", []), start=1):
        if (
            entry is target
            or (target_name and str(entry.get("name") or "").lower() == target_name)
            or (target_install_id and str(entry.get("install_id") or "") == target_install_id)
        ):
            return f"#{index}"
    return None


def _with_registry_refs(registry: dict, agent: dict) -> dict:
    annotated = dict(agent)
    ref = _registry_ref_for_agent(registry, agent)
    if ref:
        annotated["registry_ref"] = ref
        annotated["registry_index"] = int(ref.lstrip("#"))
    install_id = str(annotated.get("install_id") or "")
    if install_id:
        annotated["registry_code"] = install_id[:8]
    return annotated


def _load_managed_agent_client(entry: dict) -> AxClient:
    if os.environ.get("AX_OFFLINE"):
        from ax_cli.offline_client import OfflineAxClient

        return OfflineAxClient(
            agent_name=str(entry.get("name") or "") or None,
            agent_id=str(entry.get("agent_id") or "") or None,
        )
    try:
        token = load_gateway_managed_agent_token(entry)
    except ValueError as exc:
        err_console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1)
    return AxClient(
        base_url=str(entry.get("base_url") or ""),
        token=token,
        agent_name=str(entry.get("name") or ""),
        agent_id=str(entry.get("agent_id") or "") or None,
    )


def _resolve_system_prompt_input(
    *, system_prompt: str | None, system_prompt_file: str | None, current: str | None = None
) -> str | None:
    """Resolve the operator's system-prompt input from either a literal value
    or a file path. Mutual exclusion: only one of ``--system-prompt`` /
    ``--system-prompt-file`` may be set per call.

    Returns the resolved text, or ``current`` (the existing entry value) when
    neither flag was supplied. An empty string from either source is treated
    as "clear the prompt" and returns ``""``; ``None`` means "no change".
    """
    if system_prompt is not None and system_prompt_file is not None:
        raise ValueError("--system-prompt and --system-prompt-file are mutually exclusive.")
    if system_prompt_file is not None:
        path = Path(system_prompt_file).expanduser()
        if not path.is_file():
            raise ValueError(f"System prompt file not found: {path}")
        return path.read_text(encoding="utf-8").strip()
    if system_prompt is not None:
        return system_prompt.strip()
    return current


def _normalize_connector_ref(connector_ref: str) -> str:
    """Resolve and validate a connector registry reference (name or id)."""
    from ..connectors import ConnectorNotFoundError, find_connector

    ref = str(connector_ref or "").strip()
    if not ref:
        raise ValueError(
            "Template LangGraph + Composio requires --connector-ref <name>. "
            "Register a connector first: ax gateway connectors add <name> --provider composio --managed-auth"
        )
    try:
        row = find_connector(ref)
    except ConnectorNotFoundError as exc:
        raise ValueError(f"Connector not found: {ref!r}. Run: ax gateway connectors list") from exc
    if not row.enabled:
        raise ValueError(f"Connector {row.name!r} is disabled. Run: ax gateway connectors enable {row.name}")
    return row.name


def _scaffold_bridge_workdir(
    template: dict | None,
    *,
    explicit_workdir: str | None,
    explicit_exec: str | None,
) -> str | None:
    """Scaffold a bridge-template workdir so the registered agent runs without
    a manual copy step (#130).

    When the operator passes ``--workdir`` for a template that ships a bridge
    file (langgraph, autogen, strands), create the workdir, copy the bridge
    into it, and return a rewritten exec_command pointing at the workdir
    copy. Returns ``None`` if nothing was scaffolded, in which case the
    caller keeps its existing exec_command.

    Skipped when the operator supplied ``--exec`` (their command is the
    source of truth) or when the bridge source file isn't on disk (e.g. a
    PyPI install where ``examples/`` wasn't shipped — leaving the original
    exec_command in place at least surfaces a clear path in the subprocess
    error).
    """
    if not template or not explicit_workdir or explicit_exec:
        return None
    bridge_source_str = str((template.get("defaults") or {}).get("bridge_source") or "").strip()
    if not bridge_source_str:
        return None
    bridge_source = Path(bridge_source_str)
    if not bridge_source.is_file():
        return None
    workdir_path = Path(explicit_workdir).expanduser().resolve()
    workdir_path.mkdir(parents=True, exist_ok=True)
    target = workdir_path / bridge_source.name
    if not target.exists():
        shutil.copyfile(bridge_source, target)
    return f"{_bridge_python()} {target}"


def _register_managed_agent(
    *,
    name: str,
    runtime_type: str | None = None,
    template_id: str | None = None,
    exec_cmd: str | None = None,
    workdir: str | None = None,
    provider: str | None = None,
    space_id: str | None = None,
    audience: str = "both",
    description: str | None = None,
    model: str | None = None,
    system_prompt: str | None = None,
    timeout_seconds: int | None = None,
    allow_all_users: bool = False,
    allowed_users: str | None = None,
    connector_ref: str | None = None,
    agent_client: str | None = None,
    start: bool = True,
) -> dict:
    name = name.strip()
    if not name:
        raise ValueError("Managed agent name is required.")
    template = None
    explicit_workdir = str(workdir or "").strip() or None
    explicit_exec = str(exec_cmd or "").strip() or None
    if template_id:
        try:
            template = agent_template_definition(template_id)
        except KeyError as exc:
            raise ValueError(f"Unknown template: {template_id}") from exc
        if not bool(template.get("launchable", True)):
            raise ValueError(f"Template {template['label']} is not launchable yet.")
        defaults = template.get("defaults") or {}
        runtime_type = runtime_type or str(defaults.get("runtime_type") or "")
        exec_cmd = exec_cmd or (str(defaults.get("exec_command") or "").strip() or None)
        workdir = workdir or (str(defaults.get("workdir") or "").strip() or None)
        if "start" in defaults:
            start = bool(defaults.get("start"))
    runtime_type = runtime_type or "echo"
    runtime_type = _normalize_runtime_type(runtime_type)
    template_effective_id = str(template.get("id") if template else "").strip().lower()
    if template_effective_id == "ollama" and not str(model or "").strip():
        model = str(ollama_setup_status().get("recommended_model") or "").strip() or None
    if template_effective_id in {"hermes", "sentinel_cli", "claude_code_channel"} and not explicit_workdir:
        raise ValueError(
            f"Template {template['label']} requires --workdir so Gateway can bind the agent to its runtime folder."
        )
    normalized_connector_ref: str | None = None
    if connector_ref and str(connector_ref).strip():
        normalized_connector_ref = _normalize_connector_ref(connector_ref)
    elif template_effective_id == "langgraph_composio":
        raise ValueError(
            "Template LangGraph + Composio requires --connector-ref <name>. "
            "Register a connector first: ax gateway connectors add <name> --provider composio --managed-auth"
        )
    scaffolded_exec = _scaffold_bridge_workdir(template, explicit_workdir=explicit_workdir, explicit_exec=explicit_exec)
    if scaffolded_exec:
        exec_cmd = scaffolded_exec
        workdir = str(Path(explicit_workdir).expanduser().resolve())
    _validate_runtime_registration(runtime_type, exec_cmd)
    timeout_effective = _normalize_timeout_seconds(timeout_seconds)
    normalized_provider = str(provider or "").strip() or None
    if normalized_provider and runtime_type != "hermes_plugin":
        raise ValueError("--provider is only supported for hermes_plugin runtimes.")
    if normalized_provider:
        _validate_hermes_provider(normalized_provider)

    normalized_agent_client = str(agent_client or "").strip() or None
    if normalized_agent_client:
        if runtime_type == "claude_code_channel":
            raise ValueError("--client is not accepted for claude_code_channel; the client is always claude_cli.")
        elif runtime_type == "sentinel_inference_sdk":
            valid = sorted(_INFERENCE_SDK_CLIENTS)
            if normalized_agent_client not in _INFERENCE_SDK_CLIENTS:
                raise ValueError(
                    f"--client '{normalized_agent_client}' is not a recognised inference SDK client. "
                    f"Valid values: {', '.join(valid)}."
                )
        elif runtime_type == "sentinel_cli":
            valid_mcp = sorted(_MCP_HOST_CLIENT_BINARIES)
            if normalized_agent_client not in _MCP_HOST_CLIENT_BINARIES:
                raise ValueError(
                    f"--client '{normalized_agent_client}' is not a recognised MCP host client. "
                    f"Valid values: {', '.join(valid_mcp)}."
                )
    if runtime_type == "claude_code_channel":
        agent_client = "claude_cli"

    if not model and runtime_type == "hermes_plugin":
        model = _resolve_hermes_model(workdir or explicit_workdir)
    normalized_model = str(model or "").strip() or None

    client = _load_gateway_user_client()
    session = _load_gateway_session_or_exit()
    registry = load_gateway_registry()
    existing_home_space = _existing_agent_home_space(client, name) if not space_id else None
    selected_space = _resolve_gateway_agent_home_space(
        client=client,
        session=session,
        registry=registry,
        explicit_space_id=space_id or existing_home_space,
    )
    existing = _with_upstream_429_retry(
        lambda: _find_agent_in_space(client, name, selected_space),
        max_retries=INTERACTIVE_429_MAX_RETRIES,
        base_wait=INTERACTIVE_429_BASE_WAIT,
    )
    if existing:
        agent = existing
        if description or model:
            _with_upstream_429_retry(
                lambda: client.update_agent(
                    name, **{k: v for k, v in {"description": description, "model": model}.items() if v}
                ),
                max_retries=INTERACTIVE_429_MAX_RETRIES,
                base_wait=INTERACTIVE_429_BASE_WAIT,
            )
    else:
        agent = _with_upstream_429_retry(
            lambda: _create_agent_in_space(
                client,
                name=name,
                space_id=selected_space,
                description=description,
                model=model,
                gateway_id=session.get("gateway_id"),
            ),
            max_retries=INTERACTIVE_429_MAX_RETRIES,
            base_wait=INTERACTIVE_429_BASE_WAIT,
        )
    normalized_system_prompt = (system_prompt or "").strip() or None
    _polish_metadata(client, name=name, bio=None, specialization=None, system_prompt=normalized_system_prompt)

    agent_id = str(agent.get("id") or agent.get("agent_id") or "")
    token, pat_source = _with_upstream_429_retry(
        lambda: _mint_agent_pat(
            client,
            agent_id=agent_id,
            agent_name=name,
            audience=audience,
            expires_in_days=90,
            pat_name=f"gateway-{name}",
            space_id=selected_space,
        ),
        max_retries=INTERACTIVE_429_MAX_RETRIES,
        base_wait=INTERACTIVE_429_BASE_WAIT,
    )
    # Saves the token to the canonical <gateway_dir>/agents/<name>/token. The
    # return path is intentionally not captured: recovery derives it from the
    # agent name (agent_token_path / agent_token_relpath), so it is neither
    # stored in the registry as an absolute path nor recorded in the event.
    _save_agent_token(name, token)

    requires_approval = bool((template or {}).get("requires_approval", False))
    entry_payload = {
        "name": name,
        "template_id": template.get("id") if template else None,
        "template_label": template.get("label") if template else None,
        "agent_id": agent_id,
        "space_id": selected_space,
        "base_url": session["base_url"],
        "runtime_type": runtime_type,
        "exec_command": exec_cmd,
        "workdir": workdir,
        "model": normalized_model,
        "timeout_seconds": timeout_effective,
        # Stored relative to gateway_dir() so the registry stays portable across
        # hosts/containers; resolved via resolve_agent_token_file() at read (#89).
        "token_file": agent_token_relpath(name),
        "desired_state": "running" if start else "stopped",
        "effective_state": "stopped",
        "transport": "gateway",
        "credential_source": "gateway",
        "last_error": None,
        "backlog_depth": 0,
        "processed_count": 0,
        "dropped_count": 0,
        "pat_source": pat_source,
        "requires_approval": requires_approval,
        "added_at": datetime.now(timezone.utc).isoformat(),
    }
    if normalized_system_prompt:
        entry_payload["system_prompt"] = normalized_system_prompt
    if allow_all_users:
        entry_payload["allow_all_users"] = True
    if allowed_users and str(allowed_users).strip():
        entry_payload["allowed_users"] = str(allowed_users).strip()
    if normalized_connector_ref:
        entry_payload["connector_ref"] = normalized_connector_ref
    if normalized_provider:
        entry_payload["provider"] = normalized_provider
    if agent_client and str(agent_client).strip():
        entry_payload["client"] = str(agent_client).strip()
    if requires_approval:
        entry_payload["install_id"] = str(uuid.uuid4())
    entry = upsert_agent_entry(registry, entry_payload)
    if not requires_approval:
        ensure_local_asset_binding(registry, entry, created_via="cli", auto_approve=True)
    ensure_gateway_identity_binding(registry, entry, session=session, created_via="cli")
    entry.update(evaluate_runtime_attestation(registry, entry))
    _write_agent_workspace_config(entry)
    hermes_status = hermes_setup_status(entry)
    if not hermes_status.get("ready", True):
        entry["effective_state"] = "error"
        entry["last_error"] = str(
            hermes_status.get("detail") or hermes_status.get("summary") or "Hermes setup is incomplete."
        )
        entry["current_activity"] = str(hermes_status.get("summary") or "Hermes setup is incomplete.")
    elif hermes_status.get("resolved_path"):
        entry["hermes_repo_path"] = str(hermes_status["resolved_path"])
    save_gateway_registry(registry)
    record_gateway_activity(
        "managed_agent_added",
        entry=entry,
        space_id=selected_space,
    )
    return annotate_runtime_health(entry, registry=registry)


def _agent_workspace_context_text(entry: dict, *, workdir: str) -> str:
    name = str(entry.get("name") or "agent").strip()
    template = str(entry.get("template_id") or entry.get("runtime_type") or "gateway").strip()
    runtime = str(entry.get("runtime_type") or "gateway").strip()
    operator_prompt = str(entry.get("system_prompt") or "").strip()
    persona_section = (
        f"""## Operator-supplied role instructions

The operator registered this agent with the following system prompt. These
take precedence over the generic guidance below. They were passed to the
runtime via `--system-prompt` (Hermes / OpenAI-compatible) or
`--append-system-prompt` (Claude Code).

```
{operator_prompt}
```

"""
        if operator_prompt
        else """## Operator-supplied role instructions

No operator-supplied system prompt is configured for this agent. To set one,
run from your control workspace:

```bash
ax gateway agents update {name} --system-prompt "Your role instructions..."
# or, from a file:
ax gateway agents update {name} --system-prompt-file ./role.md
```

""".replace("{name}", name)
    )
    return f"""# aX Agent Context

You are `@{name}`, an agent connected to the aX multi-user, multi-agent network through the local Gateway.

Identity and runtime:

- Agent name: `@{name}`
- Agent type: `{template}`
- Runtime: `{runtime}`
- Runtime folder: `{workdir}`
- Gateway URL: `http://127.0.0.1:8765`

{persona_section}## How to use aX from this folder

```bash
ax gateway local connect --workdir .
ax gateway local inbox --workdir .
ax gateway local send --workdir . "@agent_name message"
```

## Guidelines

- Use the Gateway CLI from this folder for aX messages, inbox checks, tasks, and context.
- Do not ask the user for a PAT and do not store user tokens in this folder.
- If Gateway says approval is required, tell the user to open `http://127.0.0.1:8765` and approve the pending binding.
- Treat aX as your shared agent network: messages may come from users, service accounts, or other agents.
- Keep replies concise unless the task needs detail, and surface useful progress through the runtime when possible.
- Keep self-description updates, preferences, avatar metadata, and capability notes aligned with Gateway-backed agent settings as those commands become available.
"""


def _agent_workspace_readme_text(entry: dict, *, workdir: str) -> str:
    name = str(entry.get("name") or "agent").strip()
    template = str(entry.get("template_id") or entry.get("runtime_type") or "gateway").strip()
    return f"""# aX Gateway Agent

This folder is registered with the local aX Gateway as `@{name}`.

- Agent type: `{template}`
- Runtime folder: `{workdir}`
- Gateway URL: `http://127.0.0.1:8765`

Read `.ax/AGENT_CONTEXT.md` first. It explains your aX identity and the Gateway CLI path.

Use the Gateway CLI from this folder when you need platform context:

```bash
ax gateway local connect --workdir .
ax gateway local inbox --workdir .
ax gateway local send --workdir . "@agent_name message"
```

Do not add a user PAT here. Gateway owns credential minting and the local
fingerprint binding for this agent. Keep self-description updates, preferences,
avatar metadata, and capability notes in Gateway-backed agent settings as those
commands become available.
"""


def _write_agent_context_hint(path: Path, *, agent_name: str, context_path: Path) -> None:
    if path.exists():
        return
    path.write_text(
        "\n".join(
            [
                f"# {agent_name} on aX",
                "",
                "This workspace is connected to aX through the local Gateway.",
                f"Read `{context_path}` before using aX tools.",
                "",
            ]
        ),
        encoding="utf-8",
    )


_AGENT_CONTEXT_MARKER_BEGIN = "<!-- BEGIN ax-gateway-agent-context (auto-generated; do not edit by hand) -->"
_AGENT_CONTEXT_MARKER_END = "<!-- END ax-gateway-agent-context -->"


def _render_agent_persona_markdown(entry: dict, *, workdir: str) -> str:
    """Body of the auto-generated section that's written into the runtime's
    native context file (CLAUDE.md for Claude Code, AGENTS.md for Hermes).

    Layout: operator-supplied role first (the agent's identity), then the
    generic aX network/CLI guidance the agent needs to collaborate. Mirrors
    `_compose_agent_system_prompt` in ax_cli/gateway.py — same ordering, so
    what the runtime gets via `--system-prompt` matches what the human sees
    in the workdir doc.
    """
    name = str(entry.get("name") or "agent").strip()
    operator_prompt = str(entry.get("system_prompt") or "").strip()
    persona_block = (
        f"## Role\n\n{operator_prompt}\n"
        if operator_prompt
        else (
            "## Role\n\n"
            "_No operator-supplied system prompt is configured for this agent._\n\n"
            "To set one, from your control workspace run:\n\n"
            "```bash\n"
            f'ax gateway agents update {name} --system-prompt "Your role instructions..."\n'
            "```\n"
        )
    )
    from ..connectors.guidance import connector_ref_for_agent, render_connector_block

    connector_block = render_connector_block(connector_ref_for_agent(entry) or "")

    return f"""# `@{name}` — aX agent context

You are `@{name}`, an agent on the aX multi-agent network. Other agents may
@-mention you. The Gateway daemon brokers your credentials; you don't manage
tokens directly.

- Workdir: `{workdir}`
- Gateway: http://127.0.0.1:8765

{persona_block}
## Collaboration model

- Reply on the same thread by passing the incoming message_id as parent_id.
- @-mention other agents by name to delegate or ask for help.
- See who is online, route work, and read your inbox via the CLI below.

## CLI

```bash
ax send "@target your message"           # send a new message
ax send -p <message_id> "..."             # reply on a thread
ax messages list                           # read your inbox
ax tasks create "title" --assign-to <agent>  # delegate work
ax tasks list                              # open tasks for you
ax agents list                             # see who is online
```
{connector_block}"""


def _write_marker_section(path: Path, *, body: str) -> None:
    """Idempotently install or refresh the auto-generated agent-context
    section in the given file.

    - File missing: write a new file containing only the section.
    - File exists with the markers: replace the section in place.
    - File exists without the markers: prepend the section so the LLM sees
      the persona before any user content. Preserves user content.
    """
    section = f"{_AGENT_CONTEXT_MARKER_BEGIN}\n\n{body.rstrip()}\n\n{_AGENT_CONTEXT_MARKER_END}\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        path.write_text(section, encoding="utf-8")
        return
    existing = path.read_text(encoding="utf-8")
    if _AGENT_CONTEXT_MARKER_BEGIN in existing and _AGENT_CONTEXT_MARKER_END in existing:
        head, _, rest = existing.partition(_AGENT_CONTEXT_MARKER_BEGIN)
        _, _, tail = rest.partition(_AGENT_CONTEXT_MARKER_END)
        # Strip the leftover newline immediately after the end marker so the
        # tail re-attaches cleanly. Preserve the rest of tail verbatim.
        if tail.startswith("\n"):
            tail = tail[1:]
        path.write_text(head + section + tail, encoding="utf-8")
        return
    # No markers — prepend so the persona is the first thing the LLM reads.
    path.write_text(section + "\n" + existing, encoding="utf-8")


def _agent_runtime_context_target(entry: dict, *, workdir: Path) -> Path | None:
    """Map a managed-agent entry to the runtime-native context file.

    Claude Code reads CLAUDE.md from the workdir; Hermes' sentinel reads
    AGENTS.md (with CLAUDE.md fallback). Returns None for templates that
    don't have a workdir-based runtime convention.
    """
    template = str(entry.get("template_id") or "").strip().lower()
    runtime = str(entry.get("runtime_type") or "").strip().lower()
    if template == "claude_code_channel" or runtime == "claude_code_channel":
        return workdir / "CLAUDE.md"
    if template in {"hermes", "sentinel_cli"} or runtime in {"sentinel_inference_sdk", "sentinel_cli"}:
        return workdir / "AGENTS.md"
    return None


def _write_agent_workspace_config(entry: dict) -> None:
    template = str(entry.get("template_id") or "").strip().lower()
    runtime = str(entry.get("runtime_type") or "").strip().lower()
    if template not in {"hermes", "sentinel_cli", "claude_code_channel"} and runtime not in {
        "sentinel_inference_sdk",
        "sentinel_cli",
        "claude_code_channel",
    }:
        return
    workdir = str(entry.get("workdir") or "").strip()
    name = str(entry.get("name") or "").strip()
    if not workdir or not name:
        return
    root = Path(workdir).expanduser().resolve()
    config_dir = root / ".ax"
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / "config.toml").write_text(
        _gateway_local_config_text(agent_name=name, gateway_url="http://127.0.0.1:8765", workdir=str(root))
    )
    (config_dir / "config.toml").chmod(0o600)
    (config_dir / "README.md").write_text(_agent_workspace_readme_text(entry, workdir=str(root)))
    context_path = config_dir / "AGENT_CONTEXT.md"
    context_path.write_text(_agent_workspace_context_text(entry, workdir=str(root)), encoding="utf-8")

    # Also write the persona into the file the runtime reads natively
    # (CLAUDE.md for Claude Code, AGENTS.md for Hermes). Use a marker-bounded
    # section so user-authored content in those files is preserved on re-write.
    target = _agent_runtime_context_target(entry, workdir=root)
    if target is not None:
        _write_marker_section(target, body=_render_agent_persona_markdown(entry, workdir=str(root)))


def _update_managed_agent(
    *,
    name: str,
    template_id: str | object = None,
    runtime_type: str | object = None,
    exec_cmd: str | object = _UNSET,
    workdir: str | object = _UNSET,
    provider: str | object = _UNSET,
    description: str | object = _UNSET,
    model: str | object = _UNSET,
    system_prompt: str | object = _UNSET,
    timeout_seconds: int | object = _UNSET,
    allow_all_users: bool | object = _UNSET,
    allowed_users: str | object = _UNSET,
    connector_ref: str | object = _UNSET,
    agent_client: str | object = _UNSET,
    python_path: str | object = _UNSET,
    desired_state: str | None = None,
) -> dict:
    name = name.strip()
    if not name:
        raise ValueError("Managed agent name is required.")

    registry = load_gateway_registry()
    entry = find_agent_entry(registry, name)
    if not entry:
        raise LookupError(f"Managed agent not found: {name}")

    # _UNSET defaults let callers (manifest apply) omit these without clearing
    # them; downstream treats None as "leave unchanged", so normalize here.
    if template_id is _UNSET:
        template_id = None
    if runtime_type is _UNSET:
        runtime_type = None
    if provider is _UNSET:
        provider = None
    if description is _UNSET:
        description = None
    template = None
    if template_id:
        try:
            template = agent_template_definition(template_id)
        except KeyError as exc:
            raise ValueError(f"Unknown template: {template_id}") from exc
        if not bool(template.get("launchable", True)):
            raise ValueError(f"Template {template['label']} is not launchable yet.")

    runtime_candidate = (
        runtime_type or (template.get("defaults") or {}).get("runtime_type") if template else runtime_type
    )
    runtime_effective = str(runtime_candidate or entry.get("runtime_type") or "echo")
    runtime_effective = _normalize_runtime_type(runtime_effective)
    template_effective_id = str(template.get("id") if template else entry.get("template_id") or "").strip().lower()

    if template:
        defaults = template.get("defaults") or {}
        exec_effective = (
            str(exec_cmd).strip() or None
            if exec_cmd is not _UNSET
            else (str(defaults.get("exec_command") or "").strip() or None)
        )
        workdir_effective = (
            str(workdir).strip() or None
            if workdir is not _UNSET
            else (str(defaults.get("workdir") or "").strip() or None)
        )
    else:
        exec_effective = (
            str(entry.get("exec_command") or "").strip() or None
            if exec_cmd is _UNSET
            else (str(exec_cmd).strip() or None)
        )
        workdir_effective = (
            str(entry.get("workdir") or "").strip() or None if workdir is _UNSET else (str(workdir).strip() or None)
        )

    if model is _UNSET:
        model_effective = str(entry.get("model") or "").strip() or None
    else:
        model_effective = str(model).strip() or None
    if template_effective_id == "ollama" and model is _UNSET and not model_effective:
        model_effective = str(ollama_setup_status().get("recommended_model") or "").strip() or None

    if connector_ref is not _UNSET:
        connector_clean = str(connector_ref or "").strip()
        if connector_clean:
            entry["connector_ref"] = _normalize_connector_ref(connector_clean)
        else:
            entry.pop("connector_ref", None)

    if agent_client is not _UNSET:
        sdk_clean = str(agent_client or "").strip()
        if sdk_clean:
            if runtime_effective == "claude_code_channel":
                raise ValueError("--client is not accepted for claude_code_channel; the client is always claude_cli.")
            elif runtime_effective == "sentinel_inference_sdk":
                valid = sorted(_INFERENCE_SDK_CLIENTS)
                if sdk_clean not in _INFERENCE_SDK_CLIENTS:
                    raise ValueError(
                        f"--client '{sdk_clean}' is not a recognised inference SDK client. "
                        f"Valid values: {', '.join(valid)}."
                    )
            elif runtime_effective == "sentinel_cli":
                valid_mcp = sorted(_MCP_HOST_CLIENT_BINARIES)
                if sdk_clean not in _MCP_HOST_CLIENT_BINARIES:
                    raise ValueError(
                        f"--client '{sdk_clean}' is not a recognised MCP host client. "
                        f"Valid values: {', '.join(valid_mcp)}."
                    )
            entry["client"] = sdk_clean
        else:
            entry.pop("client", None)

    if python_path is not _UNSET:
        py_clean = str(python_path or "").strip()
        if py_clean:
            entry["python"] = py_clean
        else:
            entry.pop("python", None)

    if template_effective_id == "langgraph_composio" and not str(entry.get("connector_ref") or "").strip():
        raise ValueError(
            "Template LangGraph + Composio requires --connector-ref <name>. "
            "Register a connector first: ax gateway connectors add <name> --provider composio --managed-auth"
        )

    _validate_runtime_registration(runtime_effective, exec_effective)
    normalized_provider = str(provider or "").strip() or None
    if normalized_provider and runtime_effective != "hermes_plugin":
        raise ValueError("--provider is only supported for hermes_plugin runtimes.")
    if normalized_provider:
        _validate_hermes_provider(normalized_provider)
        entry["provider"] = normalized_provider

    if desired_state is not None:
        normalized_desired = desired_state.lower().strip()
        if normalized_desired not in {"running", "stopped"}:
            raise ValueError("Desired state must be running or stopped.")
        entry["desired_state"] = normalized_desired
    if timeout_seconds is not _UNSET:
        entry["timeout_seconds"] = _normalize_timeout_seconds(timeout_seconds)  # type: ignore[arg-type]

    if model is _UNSET and runtime_effective == "hermes_plugin":
        model_effective = _resolve_hermes_model(workdir_effective or str(entry.get("workdir") or "")) or model_effective

    session = _load_gateway_session_or_exit()
    upstream_fields: dict = {}
    if description:
        upstream_fields["description"] = description
    if model_effective:
        upstream_fields["model"] = model_effective
    if system_prompt is not _UNSET:
        sp_value = str(system_prompt).strip() if system_prompt else ""  # type: ignore[arg-type]
        upstream_fields["system_prompt"] = sp_value or None
    if upstream_fields:
        import httpx as _httpx

        client = _load_gateway_user_client()
        try:
            client.update_agent(name, **upstream_fields)
        except _httpx.HTTPStatusError as exc:
            if exc.response.status_code == 404:
                raise ValueError(
                    f"Agent '{name}' was not found on the platform (404). "
                    "The upstream registration may have been deleted or the gateway may be pointed at a different environment. "
                    "Use `ax gateway agents remove` then `ax gateway agents add` to re-register."
                ) from exc
            raise
    if system_prompt is not _UNSET:
        sp_value = str(system_prompt).strip() if system_prompt else ""  # type: ignore[arg-type]
        if sp_value:
            entry["system_prompt"] = sp_value
        else:
            entry.pop("system_prompt", None)

    if template:
        entry["template_id"] = template.get("id")
        entry["template_label"] = template.get("label")
    entry["runtime_type"] = runtime_effective
    entry["exec_command"] = exec_effective
    entry["workdir"] = workdir_effective
    if allow_all_users is not _UNSET:
        if allow_all_users:
            entry["allow_all_users"] = True
        else:
            entry.pop("allow_all_users", None)
    if allowed_users is not _UNSET:
        allowed_clean = str(allowed_users or "").strip()
        if allowed_clean:
            entry["allowed_users"] = allowed_clean
        else:
            entry.pop("allowed_users", None)
    entry.pop("ollama_model", None)  # hard cut: old field removed
    if model_effective:
        entry["model"] = model_effective
    elif model is not _UNSET:
        entry.pop("model", None)
    entry["updated_at"] = datetime.now(timezone.utc).isoformat()
    entry.setdefault("transport", "gateway")
    entry.setdefault("credential_source", "gateway")

    if template and template.get("id") != "hermes":
        entry.pop("hermes_repo_path", None)

    ensure_gateway_identity_binding(registry, entry, session=session)
    ensure_local_asset_binding(registry, entry, created_via="cli", auto_approve=True, replace_existing=True)
    entry.update(evaluate_runtime_attestation(registry, entry))
    _write_agent_workspace_config(entry)
    hermes_status = hermes_setup_status(entry)
    if not hermes_status.get("ready", True):
        entry["effective_state"] = "error"
        entry["last_error"] = str(
            hermes_status.get("detail") or hermes_status.get("summary") or "Hermes setup is incomplete."
        )
        entry["current_activity"] = str(hermes_status.get("summary") or "Hermes setup is incomplete.")
    elif hermes_status.get("resolved_path"):
        entry["hermes_repo_path"] = str(hermes_status["resolved_path"])

    save_gateway_registry(registry)
    record_gateway_activity(
        "managed_agent_updated",
        entry=entry,
        template_id=entry.get("template_id"),
        runtime_type=runtime_effective,
        workdir=workdir_effective,
        exec_command=exec_effective,
        desired_state=entry.get("desired_state"),
        timeout_seconds=entry.get("timeout_seconds"),
    )
    return annotate_runtime_health(entry, registry=registry)


def _hide_managed_agents(names: list[str], *, reason: str = "operator_cleanup") -> dict:
    normalized_names = []
    seen = set()
    for raw_name in names:
        name = str(raw_name or "").strip()
        key = name.lower()
        if not name or key in seen:
            continue
        normalized_names.append(name)
        seen.add(key)
    if not normalized_names:
        raise ValueError("Choose at least one managed agent to hide.")

    registry = load_gateway_registry()
    hidden: list[dict] = []
    missing: list[str] = []
    hidden_reason = str(reason or "").strip() or "operator_cleanup"
    hidden_at = gateway_core._now_iso()
    for name in normalized_names:
        entry = find_agent_entry(registry, name)
        if not entry:
            missing.append(name)
            continue
        if str(entry.get("desired_state") or "").strip().lower() != "stopped":
            entry["desired_state_before_hide"] = entry.get("desired_state") or "running"
        entry["desired_state"] = "stopped"
        entry["lifecycle_phase"] = "hidden"
        entry["hidden_at"] = hidden_at
        entry["hidden_reason"] = hidden_reason
        hidden.append(entry)

    save_gateway_registry(registry)
    for entry in hidden:
        record_gateway_activity(
            "managed_agent_hidden",
            entry=entry,
            hidden_reason=hidden_reason,
            operator_action=True,
        )
    return {
        "count": len(hidden),
        "missing": missing,
        "hidden": [annotate_runtime_health(entry, registry=registry) for entry in hidden],
    }


def _restore_hidden_managed_agents(names: list[str]) -> dict:
    """Symmetric inverse of _hide_managed_agents.

    Clears lifecycle_phase=hidden + hide bookkeeping, restores desired_state
    to whatever the operator-driven hide had captured (desired_state_before_hide).
    Refuses to restore agents that are not in the hidden phase — the
    archived phase has its own restore path (PR #147), and "active" agents
    don't need restoration.
    """
    normalized_names: list[str] = []
    seen: set[str] = set()
    for raw_name in names:
        name = str(raw_name or "").strip()
        key = name.lower()
        if not name or key in seen:
            continue
        normalized_names.append(name)
        seen.add(key)
    if not normalized_names:
        raise ValueError("Choose at least one managed agent to restore.")

    registry = load_gateway_registry()
    restored: list[dict] = []
    missing: list[str] = []
    not_hidden: list[str] = []
    for name in normalized_names:
        entry = find_agent_entry(registry, name)
        if not entry:
            missing.append(name)
            continue
        if str(entry.get("lifecycle_phase") or "") != "hidden":
            not_hidden.append(name)
            continue
        prior = str(entry.get("desired_state_before_hide") or "").strip() or "running"
        entry["lifecycle_phase"] = "active"
        entry["desired_state"] = prior
        entry.pop("desired_state_before_hide", None)
        entry.pop("hidden_at", None)
        entry.pop("hidden_reason", None)
        entry["last_runtime_error_at"] = None
        entry["consecutive_setup_errors"] = 0
        entry["last_setup_error_signature"] = None
        entry["setup_disabled"] = False
        entry["setup_disabled_at"] = None
        entry["setup_disabled_reason"] = None
        restored.append(entry)

    save_gateway_registry(registry)
    for entry in restored:
        record_gateway_activity(
            "managed_agent_unhidden",
            entry=entry,
            operator_action=True,
        )
    return {
        "count": len(restored),
        "missing": missing,
        "not_hidden": not_hidden,
        "restored": [annotate_runtime_health(entry, registry=registry) for entry in restored],
    }


def _read_recovery_evidence(name: str) -> dict | None:
    """Reconstruct a minimal registry row for an agent from local evidence.

    Used when a managed_agent_added activity event was recorded but the
    registry row was lost (pre-race-fix damage). Reads from three sources,
    all verifiable:

    - Activity log: most recent managed_agent_added for ``name`` →
      agent_id, asset_id, install_id, gateway_id, runtime_type,
      transport, space_id, credential_source, ts.
    - Token directory: ``~/.ax/gateway/agents/<name>/token`` must exist
      (we don't fabricate credentials). The token path is derived from
      ``name`` (agent_token_path / agent_token_relpath), never read from
      the event — older records may still carry a ``token_file`` value,
      but it is ignored.
    - Workdir ``.ax/AGENT_CONTEXT.md`` if present, for the workdir hint.

    Returns None if no managed_agent_added event is recorded or the
    token file is missing — both required for a safe recovery.
    """
    target_event: dict | None = None
    activity_path = activity_log_path()
    try:
        with activity_path.open(encoding="utf-8") as fh:
            for line in fh:
                try:
                    ev = json.loads(line)
                except (ValueError, json.JSONDecodeError):
                    continue
                if ev.get("agent_name") != name or ev.get("event") != "managed_agent_added":
                    continue
                target_event = ev  # later writes win — pick the most recent
    except OSError:
        return None
    if not isinstance(target_event, dict):
        return None
    # The token always lives at the canonical <gateway_dir>/agents/<name>/token,
    # so verify *that* location rather than the absolute path frozen into the
    # activity event, which may have been captured under a different host (#89).
    if not agent_token_path(name).is_file():
        return None
    return target_event


def _recover_managed_agents_from_evidence(names: list[str]) -> dict:
    """Recover registry rows for agents present locally (token + activity)
    but absent from registry.json (pre-race-fix row loss).

    Refuses to recover agents that are already in the registry — use
    archive/restore or hide/unhide for state changes on existing rows.
    The reconstructed row is minimal: enough fields for the daemon to
    pick it up on next reconcile and hydrate the rest from upstream.
    """
    normalized: list[str] = []
    seen: set[str] = set()
    for raw in names:
        n = str(raw or "").strip()
        if not n or n.lower() in seen:
            continue
        normalized.append(n)
        seen.add(n.lower())
    if not normalized:
        raise ValueError("Choose at least one agent to recover.")

    registry = load_gateway_registry()
    recovered: list[dict] = []
    already_present: list[str] = []
    no_evidence: list[str] = []

    for name in normalized:
        if find_agent_entry(registry, name) is not None:
            already_present.append(name)
            continue
        evidence = _read_recovery_evidence(name)
        if evidence is None:
            no_evidence.append(name)
            continue
        # Build minimal row — sourced fields only.
        entry: dict = {
            "name": name,
            "agent_id": str(evidence.get("agent_id") or "").strip(),
            "asset_id": str(evidence.get("asset_id") or evidence.get("agent_id") or "").strip(),
            "install_id": str(evidence.get("install_id") or "").strip(),
            "gateway_id": str(evidence.get("gateway_id") or "").strip(),
            "runtime_type": str(evidence.get("runtime_type") or "").strip(),
            "transport": str(evidence.get("transport") or "gateway").strip(),
            "credential_source": str(evidence.get("credential_source") or "gateway").strip(),
            # Reconstruct the portable relative form, not the (possibly foreign)
            # absolute path recorded in the activity event (#89).
            "token_file": agent_token_relpath(name),
            "space_id": str(evidence.get("space_id") or "").strip(),
            "added_at": str(evidence.get("ts") or "").strip(),
            "lifecycle_phase": "active",
            "desired_state": "stopped",  # safe default — operator restarts deliberately
            "drift_reason": "registry_row_recovered_from_evidence",
        }
        # Pick a sensible template_id from runtime_type; daemon hydrates from
        # upstream on reconcile.
        rt = entry["runtime_type"]
        if rt == "claude_code_channel":
            entry["template_id"] = "claude_code_channel"
            entry["template_label"] = "Claude Code Channel"
        elif rt == "sentinel_inference_sdk":
            entry["template_id"] = "hermes"
            entry["template_label"] = "Hermes"
        elif rt == "inbox":
            entry["template_id"] = "pass_through"
            entry["template_label"] = "Pass-through"
        registry.setdefault("agents", []).append(entry)
        recovered.append(entry)

    save_gateway_registry(registry)
    for entry in recovered:
        record_gateway_activity(
            "managed_agent_recovered",
            entry=entry,
            operator_action=True,
            recovery_source="local_evidence",
        )

    return {
        "count": len(recovered),
        "already_present": already_present,
        "no_evidence": no_evidence,
        "recovered": [annotate_runtime_health(entry, registry=registry) for entry in recovered],
    }


def _archive_managed_agent(name: str, *, reason: str | None = None, client_factory=None) -> dict:
    """Archive a managed agent. Sticky — sweep won't auto-restore.

    Sets `lifecycle_phase=archived` and `desired_state=stopped` so the daemon
    reconciler stops the runtime. Captures `desired_state_before_archive` so
    `restore` can put it back. Best-effort upstream signal `archived`. The
    local registry is authoritative; upstream failure is logged, never fatal.
    """
    registry = load_gateway_registry()
    entry = find_agent_entry(registry, name)
    if not entry:
        raise LookupError(f"Managed agent not found: {name}")
    if str(entry.get("lifecycle_phase") or "active") == "archived":
        return annotate_runtime_health(entry, registry=registry)
    prior_desired_state = str(entry.get("desired_state") or "running")
    entry["lifecycle_phase"] = "archived"
    entry["archived_at"] = _utc_now_iso()
    if reason and str(reason).strip():
        entry["archived_reason"] = str(reason).strip()[:240]
    else:
        entry.pop("archived_reason", None)
    entry["desired_state_before_archive"] = prior_desired_state
    entry["desired_state"] = "stopped"
    save_gateway_registry(registry, merge_archive=False)
    record_gateway_activity(
        "managed_agent_archived",
        entry=entry,
        reason=str(reason).strip() if reason else None,
    )
    return annotate_runtime_health(entry, registry=registry)


def _restore_managed_agent(name: str, *, client_factory=None) -> dict:
    """Restore an archived agent to active. Honors prior desired_state.

    If `desired_state_before_archive` was captured at archive time, the
    runtime restores to that state. Otherwise defaults to `stopped` (safer
    than auto-resuming a runtime the operator may have intentionally
    disabled). Best-effort upstream signal `connected`.
    """
    registry = load_gateway_registry()
    entry = find_agent_entry(registry, name)
    if not entry:
        raise LookupError(f"Managed agent not found: {name}")
    if str(entry.get("lifecycle_phase") or "active") != "archived":
        return annotate_runtime_health(entry, registry=registry)
    prior = str(entry.get("desired_state_before_archive") or "stopped")
    entry["lifecycle_phase"] = "active"
    entry.pop("archived_at", None)
    entry.pop("archived_reason", None)
    entry.pop("desired_state_before_archive", None)
    entry["desired_state"] = prior if prior in {"running", "stopped"} else "stopped"
    save_gateway_registry(registry, merge_archive=False)
    record_gateway_activity("managed_agent_restored", entry=entry)
    return annotate_runtime_health(entry, registry=registry)


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _remove_managed_agent(name: str, *, client_factory=None) -> dict:
    registry = load_gateway_registry()
    peek = find_agent_entry(registry, name)
    if not peek:
        raise LookupError(f"Managed agent not found: {name}")
    # Best-effort upstream delete BEFORE local removal so the platform-side
    # record can be retired in lockstep. Missing session, 404, or network
    # failure are recorded as audit events but never block the local
    # removal — the local registry is authoritative for the gateway.
    agent_id = str(peek.get("agent_id") or "").strip()
    if agent_id:
        user_client = client_factory() if client_factory is not None else _build_session_client_silent()
        if user_client is not None:
            try:
                user_client.delete_agent(agent_id)
            except Exception as exc:  # noqa: BLE001
                record_gateway_activity(
                    "managed_agent_remove_upstream_failed",
                    entry=peek,
                    error=str(exc)[:360],
                )
    entry = remove_agent_entry(registry, name)
    if not entry:
        # Should be unreachable since peek succeeded; defensive only.
        raise LookupError(f"Managed agent not found: {name}")
    save_gateway_registry(registry)
    archive_stale_gateway_approvals()
    token_file = resolve_agent_token_file(entry) if str(entry.get("token_file") or "").strip() else None
    if token_file and token_file.is_file():
        token_file.unlink()
    record_gateway_activity("managed_agent_removed", entry=entry)
    return entry


def _reject_managed_agent_approval(name: str) -> dict:
    detail = _agent_detail_payload(name, activity_limit=1)
    if detail is None:
        raise LookupError(f"Managed agent not found: {name}")
    agent = detail.get("agent") or {}
    approval_id = str(agent.get("approval_id") or "").strip()
    if not approval_id:
        raise ValueError(f"@{name} does not have a pending Gateway approval.")
    approval = get_gateway_approval(approval_id)
    rejected = deny_gateway_approval(approval_id)
    removed = None
    if (
        str(approval.get("status") or "").lower() == "pending"
        and str(approval.get("approval_kind") or "") == "new_binding"
    ):
        try:
            removed = _remove_managed_agent(name)
        except LookupError:
            removed = None
    return {
        "approval": rejected,
        "removed_agent": removed,
        "removed": removed is not None,
    }


def _move_managed_agent_space(name: str, new_space_id: str | None, *, revert: bool = False) -> dict:
    name = name.strip()
    if not name:
        raise ValueError("Managed agent name is required.")
    if revert:
        if new_space_id and new_space_id.strip():
            raise ValueError("Pass either --space or --revert, not both.")
        registry_for_revert = load_gateway_registry()
        revert_entry = find_agent_entry(registry_for_revert, name)
        if not revert_entry:
            raise LookupError(f"Managed agent not found: {name}")
        previous = str(revert_entry.get("previous_space_id") or "").strip()
        if not previous:
            raise ValueError(f"@{name} has no recorded previous space to revert to. Use --space <id> instead.")
        new_space_id = previous
    else:
        new_space_id = (new_space_id or "").strip()
        if not new_space_id:
            raise ValueError("Target space is required.")
    client = _load_gateway_user_client()
    new_space_id = resolve_space_id(client, explicit=new_space_id)
    registry = load_gateway_registry()
    entry = find_agent_entry(registry, name)
    if not entry:
        raise LookupError(f"Managed agent not found: {name}")
    if bool(entry.get("pinned")):
        raise ValueError(f"@{name} is pinned to its current space. Unlock it before moving.")
    if str(entry.get("space_id") or "").strip() == new_space_id:
        apply_entry_current_space(entry, new_space_id, space_name=_space_name_for_id(client, new_space_id))
        ensure_gateway_identity_binding(registry, entry, session=load_gateway_session())
        save_gateway_registry(registry)
        return annotate_runtime_health(entry, registry=registry)
    identifier = str(entry.get("agent_id") or name)
    try:
        client.set_agent_placement(identifier, space_id=new_space_id, pinned=bool(entry.get("pinned")))
    except AttributeError:
        try:
            client.update_agent(identifier, space_id=new_space_id)
        except Exception as exc:  # noqa: BLE001
            raise ValueError(f"Backend rejected move: {exc}") from exc
    except Exception as exc:  # noqa: BLE001
        raise ValueError(f"Backend rejected move: {exc}") from exc
    # Re-read the canonical record from backend — gateway local registry is a view,
    # never the source of truth.
    backend_space_id = new_space_id
    backend_space_name = _space_name_for_id(client, new_space_id)
    backend_allowed_spaces: list[dict[str, object]] | None = None
    read_back_methods = [
        method
        for method in (getattr(client, "get_agent_placement", None), getattr(client, "get_agent", None))
        if callable(method)
    ]
    for read_back in read_back_methods:
        try:
            record = read_back(identifier)
            if isinstance(record, dict) and isinstance(record.get("_record"), dict):
                record = record["_record"]
            elif isinstance(record, dict):
                record = record.get("agent", record)
            if not isinstance(record, dict):
                continue
            canonical = str(
                record.get("space_id") or record.get("current_space") or record.get("default_space_id") or ""
            ).strip()
            if canonical:
                backend_space_id = canonical
                backend_space_name = _space_name_for_id(client, backend_space_id) or backend_space_name
            allowed = record.get("allowed_spaces")
            if isinstance(allowed, list):
                try:
                    space_names_by_id = {
                        str(item.get("id") or item.get("space_id") or "").strip(): str(
                            item.get("name") or item.get("space_name") or item.get("slug") or ""
                        ).strip()
                        for item in _space_list_from_response(client.list_spaces())
                        if isinstance(item, dict) and str(item.get("id") or item.get("space_id") or "").strip()
                    }
                except Exception:
                    space_names_by_id = {}
                backend_allowed_spaces = [
                    {
                        **item,
                        "name": str(
                            item.get("name")
                            or space_names_by_id.get(str(item.get("space_id") or item.get("id") or "").strip())
                            or item.get("space_id")
                            or item.get("id")
                        ),
                    }
                    if isinstance(item, dict)
                    else {
                        "space_id": str(item),
                        "name": space_names_by_id.get(str(item)) or str(item),
                        "is_default": str(item) == backend_space_id,
                    }
                    for item in allowed
                    if item
                ]
            break
        except Exception:  # noqa: BLE001
            # Resync best-effort; the placement write already succeeded.
            continue
    previous_space_id = str(entry.get("space_id") or "").strip() or None
    previous_space_name = str(entry.get("active_space_name") or entry.get("space_name") or "").strip() or None
    if backend_allowed_spaces is not None:
        entry["allowed_spaces"] = backend_allowed_spaces
    apply_entry_current_space(entry, backend_space_id, space_name=backend_space_name)
    ensure_gateway_identity_binding(registry, entry, session=load_gateway_session())
    # Persist the prior space so `ax gateway agents move <name> --revert` can
    # find its way back without the operator needing to remember the UUID.
    # Only record when the move actually changed spaces — a no-op move
    # (already in the requested space) shouldn't blank the revert pointer.
    if previous_space_id and previous_space_id != backend_space_id:
        entry["previous_space_id"] = previous_space_id
        if previous_space_name:
            entry["previous_space_name"] = previous_space_name
    # Mark the entry as moving for any concurrent send guard / UI panel that
    # reads `current_status`. Cleared once the rebind wait below resolves
    # (or the deadline elapses) so a stuck move doesn't permanently freeze
    # sends. The send guard itself raises off `_identity_space_send_guard`
    # via `annotate_runtime_health`; this surface is for human-readable text.
    entry["current_status"] = "moving"
    entry["current_activity"] = f"Moving to {backend_space_name or backend_space_id}; sends paused until reconnect."
    # Capture the rebind marker BEFORE writing the registry so the wait below
    # is guaranteed to see only post-move runtime/listener events.
    rebind_marker = datetime.now(timezone.utc).isoformat()
    save_gateway_registry(registry)
    record_gateway_activity(
        "managed_agent_moved_space",
        entry=entry,
        new_space_id=backend_space_id,
        requested_space_id=new_space_id,
        previous_space_id=previous_space_id,
    )
    if backend_space_id != new_space_id:
        # Backend coerced the move (likely allowed_spaces enforcement). Surface to operator
        # logs so backend_sentinel can pick it up if it indicates a quarantine gap.
        record_gateway_activity(
            "managed_agent_move_coerced",
            entry=entry,
            requested_space_id=new_space_id,
            applied_space_id=backend_space_id,
        )
    # Wait for the daemon to finish the rebind before returning. The daemon
    # is a separate process polling the registry every ~1s; once it sees
    # space_id changed it stops the old runtime and starts a new one.
    # Without this wait, a follow-up POST /api/agents/<name>/test can land
    # on the new switchboard before the new SSE listener has connected,
    # stranding the message. Listener-backed runtimes are not ready at
    # runtime_started; wait for listener_connected so an immediate test send
    # does not race the new SSE connection. Cap at 5s — if no listener event
    # appears we still return with the refreshed registry state.
    # Skip when no daemon is running (e.g. tests, offline operator) since
    # nothing will produce the rebind events we are waiting on.
    if previous_space_id and previous_space_id != backend_space_id and active_gateway_pid() is not None:
        runtime_type = entry.get("runtime_type")
        ready_events = {"runtime_started"} if _is_passive_runtime(runtime_type) else {"listener_connected"}
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            recent = load_recent_gateway_activity(limit=20, agent_name=name)
            if any((event.get("ts") or "") > rebind_marker and event.get("event") in ready_events for event in recent):
                break
            time.sleep(0.2)
    # Reconnect window has resolved (or its 5s deadline elapsed). Clear the
    # human-readable "moving" status so subsequent sends through the
    # send-guard read normal state. Re-read the registry first because a
    # concurrent runtime/listener event may have already updated the entry.
    registry_after = load_gateway_registry()
    settled = find_agent_entry(registry_after, name)
    if settled is not None and str(settled.get("current_status") or "") == "moving":
        settled["current_status"] = None
        settled["current_activity"] = None
        save_gateway_registry(registry_after)
        # Mirror onto the local entry so the return value reflects the cleared state.
        entry["current_status"] = None
        entry["current_activity"] = None
    return annotate_runtime_health(entry, registry=registry)


def _ack_managed_agent_message(
    name: str,
    *,
    message_id: str,
    reply_id: str | None = None,
    reply_preview: str | None = None,
) -> dict:
    """Pass-through ack: agent reports it processed message_id and optionally
    sent reply_id. Updates local registry's reply timestamps + counters, drops
    the message from the pending queue, fires reply_sent activity event so
    the simple-gateway drawer surfaces 'Replied · just now' on the row.
    """
    name = name.strip()
    if not name:
        raise ValueError("Managed agent name is required.")
    message_id = (message_id or "").strip()
    if not message_id:
        raise ValueError("message_id is required.")
    registry = load_gateway_registry()
    entry = find_agent_entry(registry, name)
    if not entry:
        raise LookupError(f"Managed agent not found: {name}")
    now_iso = datetime.now(timezone.utc).isoformat()
    # Drop from pending queue (best-effort; the agent may have already cleaned
    # it up locally).
    items = load_agent_pending_messages(name)
    remaining = [item for item in items if str(item.get("message_id") or "") != message_id]
    if len(remaining) != len(items):
        save_agent_pending_messages(name, remaining)
    # Update registry entry so the row's last-action label and counters reflect
    # the reply that just went out via the agent's PAT.
    entry["last_work_completed_at"] = now_iso
    entry["last_reply_at"] = now_iso
    entry["last_received_message_id"] = message_id
    if reply_id:
        entry["last_reply_message_id"] = reply_id
    if reply_preview:
        entry["last_reply_preview"] = reply_preview[:240]
    entry["processed_count"] = int(entry.get("processed_count") or 0) + 1
    save_gateway_registry(registry)
    record_gateway_activity(
        "reply_sent",
        entry=entry,
        message_id=message_id,
        reply_message_id=reply_id,
        reply_preview=reply_preview,
    )
    return annotate_runtime_health(entry, registry=registry)


def _set_managed_agent_pin(name: str, pinned: bool) -> dict:
    name = name.strip()
    if not name:
        raise ValueError("Managed agent name is required.")
    registry = load_gateway_registry()
    entry = find_agent_entry(registry, name)
    if not entry:
        raise LookupError(f"Managed agent not found: {name}")
    entry["pinned"] = bool(pinned)
    save_gateway_registry(registry)
    record_gateway_activity(
        "managed_agent_pinned" if pinned else "managed_agent_unpinned",
        entry=entry,
    )
    return annotate_runtime_health(entry, registry=registry)


@agents_app.command("add")
def add_agent(
    name: str = typer.Argument(..., help="Managed agent name"),
    template_id: str = typer.Option(
        None,
        "--template",
        help="Agent template: " + " | ".join(t["id"] for t in agent_template_list()),
    ),
    runtime_type: str = typer.Option(
        None,
        "--type",
        help="Advanced/internal runtime backend: echo | exec | hermes_plugin | sentinel_inference_sdk | sentinel_cli | claude_code_channel | inbox",
    ),
    exec_cmd: str = typer.Option(None, "--exec", help="Advanced override for exec-based templates"),
    workdir: str = typer.Option(None, "--workdir", help="Advanced working directory override"),
    provider: str = typer.Option(
        None,
        "--provider",
        help=(
            "LLM provider for Hermes agents (anthropic | openrouter | bedrock). "
            "Overrides the operator's ~/.hermes/config.yaml provider/model/providers "
            "sections in the per-agent config. Validated against ~/.hermes/auth.json "
            "credential pool at registration time."
        ),
    ),
    space_id: str = typer.Option(
        None,
        "--space",
        "--space-id",
        "-s",
        help="Target space (defaults to gateway session). Accepts a slug, name, or UUID.",
    ),
    audience: str = typer.Option("both", "--audience", help="Minted PAT audience"),
    description: str = typer.Option(None, "--description", help="Create/update description"),
    model: str = typer.Option(None, "--model", help="Create/update model"),
    system_prompt: str = typer.Option(
        None,
        "--system-prompt",
        help="Operator-supplied system instructions describing the agent's role. Appended with the gateway's environment context (multi-agent network awareness + CLI usage) when handed to the runtime.",
    ),
    system_prompt_file: str = typer.Option(
        None,
        "--system-prompt-file",
        help="Path to a file containing the system prompt. Mutually exclusive with --system-prompt.",
    ),
    timeout_seconds: int = typer.Option(
        None, "--timeout", "--timeout-seconds", help="Max seconds a runtime may process one message"
    ),
    allow_all_users: bool = typer.Option(
        False,
        "--allow-all-users",
        help=(
            "Hermes plugin runtime only: open the agent to mentions from anyone in its space. "
            "Sets AX_ALLOW_ALL_USERS=1 + GATEWAY_ALLOW_ALL_USERS=true in the scaffolded "
            "HERMES_HOME/.env. Default-closed; without this (or --allowed-users) the agent "
            "denies all incoming mentions."
        ),
    ),
    allowed_users: str = typer.Option(
        None,
        "--allowed-users",
        help="Hermes plugin runtime only: comma-separated agent/user names allowed to mention this agent.",
    ),
    connector_ref: str = typer.Option(
        None,
        "--connector-ref",
        help="Outbound connector name (required for langgraph_composio; sets AX_GATEWAY_CONNECTOR_REF).",
    ),
    client: str = typer.Option(
        None,
        "--client",
        help=_CLIENT_HELP,
    ),
    start: bool = typer.Option(True, "--start/--no-start", help="Desired running state after registration"),
    as_json: bool = JSON_OPTION,
):
    """Register a managed agent and mint a Gateway-owned PAT for it.

    The ``--space`` option accepts a slug, name, or UUID. Slug/name resolution
    runs through the local space cache first; if that misses, the resolution
    falls through to the gateway user client's ``list_spaces`` lookup.
    """
    if space_id:
        cached = _resolve_space_via_cache(space_id)
        if cached is not None:
            space_id = cached
        else:
            try:
                client = _load_gateway_user_client()
                space_id = resolve_space_id(client, explicit=space_id)
            except (typer.Exit, typer.BadParameter):
                raise
            except Exception as exc:
                err_console.print(f"[red]Could not resolve space '{space_id}': {exc}[/red]")
                raise typer.Exit(1)
    selected_template = template_id or ("echo_test" if not runtime_type else None)
    try:
        resolved_prompt = _resolve_system_prompt_input(
            system_prompt=system_prompt,
            system_prompt_file=system_prompt_file,
            current=None,
        )
        entry = _register_managed_agent(
            name=name,
            template_id=selected_template,
            runtime_type=runtime_type,
            exec_cmd=exec_cmd,
            workdir=workdir,
            provider=provider,
            space_id=space_id,
            audience=audience,
            description=description,
            model=model,
            system_prompt=resolved_prompt,
            timeout_seconds=timeout_seconds,
            allow_all_users=allow_all_users,
            allowed_users=allowed_users,
            connector_ref=connector_ref,
            agent_client=client,
            start=start,
        )
    except (ValueError, LookupError) as exc:
        err_console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1)

    wiped_path = _wipe_ephemeral_session_if_marked()

    if as_json:
        if wiped_path is not None:
            entry = {**entry, "ephemeral_session_wiped": str(wiped_path)}
        print_json(entry)
    else:
        err_console.print(f"[green]Managed agent ready:[/green] @{name}")
        if entry.get("template_label"):
            err_console.print(f"  type = {entry['template_label']}")
        if entry.get("connector_ref"):
            err_console.print(f"  connector_ref = {entry['connector_ref']}")
        if entry.get("asset_type_label"):
            err_console.print(f"  asset = {entry['asset_type_label']}")
        err_console.print(f"  desired_state = {entry['desired_state']}")
        if entry.get("timeout_seconds"):
            err_console.print(f"  timeout = {entry.get('timeout_seconds')}s")
        err_console.print(f"  token_file = {entry['token_file']}")
        if wiped_path is not None:
            err_console.print(f"[cyan]Ephemeral session wiped:[/cyan] {wiped_path}")


@agents_app.command("update")
def update_agent(
    name: str = typer.Argument(..., help="Managed agent name"),
    template_id: str = typer.Option(None, "--template", help="Replace the agent template"),
    runtime_type: str = typer.Option(
        None,
        "--type",
        help="Advanced/internal runtime backend override: echo | exec | hermes_plugin | sentinel_inference_sdk | sentinel_cli | claude_code_channel | inbox",
    ),
    exec_cmd: str = typer.Option(None, "--exec", help="Advanced override for exec-based templates"),
    workdir: str = typer.Option(None, "--workdir", help="Advanced working directory override"),
    provider: str = typer.Option(
        None,
        "--provider",
        help=(
            "LLM provider for Hermes agents (anthropic | openrouter | bedrock). "
            "Overrides the operator's ~/.hermes/config.yaml provider/model/providers "
            "sections in the per-agent config."
        ),
    ),
    description: str = typer.Option(None, "--description", help="Update platform agent description"),
    model: str = typer.Option(
        None, "--model", help="Model name for this agent (e.g. gemini-2.0-flash, gpt-4o, gemma4:latest for Ollama)"
    ),
    system_prompt: str = typer.Option(
        None,
        "--system-prompt",
        help="Replace the operator-supplied system instructions. Pass an empty string to clear. Appended with the gateway's environment context at runtime.",
    ),
    system_prompt_file: str = typer.Option(
        None,
        "--system-prompt-file",
        help="Path to a file containing the system prompt. Mutually exclusive with --system-prompt.",
    ),
    timeout_seconds: int = typer.Option(
        None, "--timeout", "--timeout-seconds", help="Max seconds a runtime may process one message"
    ),
    allow_all_users: bool = typer.Option(
        None,
        "--allow-all-users/--no-allow-all-users",
        help=(
            "Hermes plugin runtime only: open the agent to mentions from anyone in its space "
            "(or close it back down). Sets AX_ALLOW_ALL_USERS / GATEWAY_ALLOW_ALL_USERS in "
            "the scaffolded HERMES_HOME/.env on the next start."
        ),
    ),
    allowed_users: str = typer.Option(
        None,
        "--allowed-users",
        help=(
            "Hermes plugin runtime only: comma-separated agent/user names allowed to mention this agent. "
            "Pass an empty string to clear."
        ),
    ),
    connector_ref: str = typer.Option(
        None,
        "--connector-ref",
        help="Outbound connector name for langgraph_composio (clears when passed as empty).",
    ),
    client: str = typer.Option(
        None,
        "--client",
        help=_CLIENT_HELP,
    ),
    python_path: str = typer.Option(
        None,
        "--python",
        help="Path to the Python interpreter used by sentinel_inference_sdk agents. Pass an empty string to clear.",
    ),
    desired_state: str = typer.Option(None, "--desired-state", help="running | stopped"),
    as_json: bool = JSON_OPTION,
):
    """Update a managed agent without redoing Gateway bootstrap."""
    try:
        prompt_unset = system_prompt is None and system_prompt_file is None
        resolved_prompt: str | object = _UNSET
        if not prompt_unset:
            resolved_prompt = (
                _resolve_system_prompt_input(
                    system_prompt=system_prompt,
                    system_prompt_file=system_prompt_file,
                    current=None,
                )
                or ""
            )
        entry = _update_managed_agent(
            name=name,
            template_id=template_id,
            runtime_type=runtime_type,
            exec_cmd=exec_cmd if exec_cmd is not None else _UNSET,
            workdir=workdir if workdir is not None else _UNSET,
            provider=provider,
            description=description,
            model=model if model is not None else _UNSET,
            system_prompt=resolved_prompt,
            timeout_seconds=timeout_seconds if timeout_seconds is not None else _UNSET,
            allow_all_users=allow_all_users if allow_all_users is not None else _UNSET,
            allowed_users=allowed_users if allowed_users is not None else _UNSET,
            connector_ref=connector_ref if connector_ref is not None else _UNSET,
            agent_client=client if client is not None else _UNSET,
            python_path=python_path if python_path is not None else _UNSET,
            desired_state=desired_state,
        )
    except (LookupError, ValueError) as exc:
        err_console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1)

    if as_json:
        print_json(entry)
        return
    err_console.print(f"[green]Managed agent updated:[/green] @{name}")
    err_console.print(f"  type = {entry.get('template_label') or entry.get('runtime_type')}")
    if entry.get("connector_ref"):
        err_console.print(f"  connector_ref = {entry['connector_ref']}")
    err_console.print(f"  desired_state = {entry.get('desired_state')}")
    if entry.get("timeout_seconds"):
        err_console.print(f"  timeout = {entry.get('timeout_seconds')}s")


@agents_app.command("apply")
def apply_manifest(
    manifest_path: str = typer.Argument(..., help="Path to a TOML agent manifest"),
    diff_only: bool = typer.Option(
        False,
        "--diff",
        "--plan",
        help="Show the planned changes without applying. Exits 0 if the manifest matches current state; 0 with changes shown if it doesn't.",
    ),
    auto_confirm: bool = typer.Option(
        False,
        "--auto-confirm",
        "-y",
        help="Skip the interactive confirmation prompt. Required in non-TTY contexts (CI, devcontainer init).",
    ),
    as_json: bool = JSON_OPTION,
):
    """Apply a declarative agent manifest (closes #91).

    The manifest is a TOML file declaring the agent's intended configuration:
    name, template, space, and any of the same fields ``ax gateway agents
    add`` / ``update`` accept. Idempotent — running ``apply`` against an
    already-matching registry is a no-op.

    Common flows:

    \b
        ax gateway agents apply /workspace/.ax/nova.agent.toml
        ax gateway agents apply nova.agent.toml --diff
        ax gateway agents apply nova.agent.toml --auto-confirm

    Field semantics:

    Fields ABSENT from the manifest are left untouched on the existing entry
    (the same ``_UNSET`` semantics ``ax gateway agents update`` uses). Fields
    PRESENT in the manifest are applied. An explicit empty string clears the
    field on update — same as passing an empty ``--system-prompt``.
    """
    from ..agent_manifests import (
        ManifestError,
        build_register_kwargs,
        build_update_kwargs,
        compute_diff,
        parse_manifest,
        render_diff,
    )

    try:
        manifest = parse_manifest(manifest_path)
    except ManifestError as exc:
        err_console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1)

    name = str(manifest.get("name") or "").strip()
    registry = load_gateway_registry()
    current_entry = find_agent_entry(registry, name)
    rows = compute_diff(manifest, current_entry)

    if diff_only:
        if as_json:
            print_json({"name": name, "creating": current_entry is None, "diff": rows})
            return
        creating = current_entry is None
        verb = "CREATE" if creating else "UPDATE"
        err_console.print(f"[bold]Planned: {verb} @{name}[/bold]")
        err_console.print(render_diff(rows))
        return

    # Resolve the system prompt input once (manifest may declare either
    # system_prompt or system_prompt_file; never both — enforced in parse_manifest)
    sys_prompt_value = manifest.get("system_prompt")
    sys_prompt_file = manifest.get("system_prompt_file")
    resolved_prompt: str | object
    if sys_prompt_value is None and sys_prompt_file is None:
        resolved_prompt = _UNSET
    else:
        resolved_prompt = (
            _resolve_system_prompt_input(
                system_prompt=sys_prompt_value,
                system_prompt_file=sys_prompt_file,
                current=(current_entry or {}).get("system_prompt"),
            )
            or ""
        )

    # Interactive confirmation — skipped via --auto-confirm or when stdout is
    # piped (apply is operator-fronted; CI / programmatic use must opt in).
    actionable_rows = [r for r in rows if r["op"] != "noop"]
    if not actionable_rows:
        if as_json:
            print_json({"name": name, "applied": False, "no_changes": True, "agent": current_entry})
        else:
            err_console.print(f"[green]@{name}: manifest matches current state; nothing to do.[/green]")
        return

    if not auto_confirm:
        import sys as _sys

        if _sys.stdin.isatty() and _sys.stdout.isatty():
            creating = current_entry is None
            verb = "create" if creating else "update"
            err_console.print(f"[bold]About to {verb} @{name}:[/bold]")
            err_console.print(render_diff(rows))
            confirmed = typer.confirm("Apply these changes?", default=False)
            if not confirmed:
                err_console.print("[yellow]Aborted.[/yellow]")
                raise typer.Exit(1)
        else:
            err_console.print("[red]Refusing to apply non-interactively without --auto-confirm.[/red]")
            raise typer.Exit(1)

    # Resolve relative workdir at apply time so "." means the directory the
    # operator ran apply from, not the daemon's cwd when the process launches.
    if "workdir" in manifest and manifest["workdir"]:
        manifest["workdir"] = str(Path(manifest["workdir"]).expanduser().resolve())

    try:
        if current_entry is None:
            kwargs = build_register_kwargs(manifest)
            if resolved_prompt is not _UNSET:
                kwargs["system_prompt"] = resolved_prompt
            entry = _register_managed_agent(**kwargs)
        else:
            kwargs = build_update_kwargs(manifest, unset_sentinel=_UNSET)
            kwargs["system_prompt"] = resolved_prompt
            entry = _update_managed_agent(name=name, **kwargs)
    except (LookupError, ValueError) as exc:
        err_console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1)

    if as_json:
        print_json({"name": name, "applied": True, "creating": current_entry is None, "agent": entry})
        return
    verb = "created" if current_entry is None else "updated"
    err_console.print(f"[green]@{name}: {verb} from manifest[/green]")
    err_console.print(f"  type = {entry.get('template_label') or entry.get('runtime_type')}")
    err_console.print(f"  desired_state = {entry.get('desired_state')}")
    if entry.get("timeout_seconds"):
        err_console.print(f"  timeout = {entry.get('timeout_seconds')}s")


@agents_app.command("export")
def export_manifest(
    name: str = typer.Argument(..., help="Managed agent name"),
    output: str = typer.Option(
        None,
        "--output",
        "-o",
        help="Write the manifest to this path. If omitted, prints to stdout.",
    ),
):
    """Export a managed agent's current state as a TOML manifest (closes #91).

    Round-trip with ``ax gateway agents apply``: ``export`` captures the live
    registry state to a file the operator can commit, edit, and re-apply. Fields
    that aren't set on the registry entry are omitted from the output rather
    than written as empty values, so re-applying the exported manifest is a
    no-op.

    \b
        ax gateway agents export nova                  # to stdout
        ax gateway agents export nova -o nova.agent.toml
    """
    from ..agent_manifests import entry_to_manifest, serialize_toml

    registry = load_gateway_registry()
    entry = find_agent_entry(registry, name)
    if not entry:
        err_console.print(f"[red]Managed agent not found:[/red] {name}")
        raise typer.Exit(1)

    manifest = entry_to_manifest(entry)
    toml_text = serialize_toml(manifest)

    if output:
        from pathlib import Path as _Path

        out_path = _Path(output).expanduser()
        out_path.write_text(toml_text, encoding="utf-8")
        err_console.print(f"[green]Wrote manifest:[/green] {out_path}")
    else:
        # Print to stdout so the operator can pipe to a file. Use raw print
        # (not console.print) so Rich doesn't apply any styling.
        print(toml_text, end="")


templates_app = typer.Typer(
    name="templates",
    help="Discover bundled and user-local agent manifest templates (closes #259)",
    no_args_is_help=True,
)
agents_app.add_typer(templates_app)


@templates_app.command("list")
def list_manifest_templates(
    as_json: bool = JSON_OPTION,
    include_advanced: bool = typer.Option(
        False,
        "--advanced",
        help="Include advanced-only templates (e.g. passive inbox).",
    ),
):
    """List discoverable agent manifest templates."""
    from ..commands.gateway_runtime_cmd import _agent_templates_payload

    payload = _agent_templates_payload(include_advanced=include_advanced)
    templates = payload["templates"]
    payload = {"templates": templates, "count": len(templates)}
    if as_json:
        print_json(payload)
        return
    rows = []
    for item in templates:
        rows.append(
            {
                "id": item["id"],
                "label": item["label"],
                "availability": item.get("availability"),
                "summary": item.get("operator_summary"),
                "manifest": str(template_manifest_path(item["id"])),
            }
        )
    print_table(
        ["Template", "Label", "Status", "Why Pick It", "Manifest"],
        rows,
        keys=["id", "label", "availability", "summary", "manifest"],
    )


@templates_app.command("show")
def show_manifest_template(
    template_id: str = typer.Argument(..., help="Template id (e.g. hermes, ollama)"),
    as_json: bool = JSON_OPTION,
):
    """Show template metadata and the bundled manifest path."""
    try:
        template = agent_template_definition(template_id)
    except KeyError:
        err_console.print(f"[red]Unknown template:[/red] {template_id}")
        raise typer.Exit(1)
    manifest_path = template_manifest_path(template_id)
    if as_json:
        print_json({"template": template, "manifest_path": str(manifest_path)})
        return
    print_kv(
        {
            "id": template["id"],
            "label": template["label"],
            "availability": template.get("availability"),
            "runtime_type": template.get("runtime_type"),
            "manifest": str(manifest_path),
            "operator_summary": template.get("operator_summary"),
        }
    )
    what_you_need = template.get("what_you_need") or []
    if what_you_need:
        err_console.print("[bold]What you need[/bold]")
        for item in what_you_need:
            err_console.print(f"  • {item}")


@templates_app.command("copy")
def copy_manifest_template(
    template_id: str = typer.Argument(..., help="Template id to copy"),
    output: str = typer.Option(
        None,
        "--output",
        "-o",
        help="Write manifest to this path instead of stdout",
    ),
    name: str = typer.Option(
        None,
        "--name",
        help="Override the suggested agent name in the copied manifest",
    ),
):
    """Copy a template manifest for editing and ``ax gateway agents apply``."""
    try:
        template = agent_template_definition(template_id)
    except KeyError:
        err_console.print(f"[red]Unknown template:[/red] {template_id}")
        raise typer.Exit(1)
    suggested = name or str(template.get("suggested_name") or template_id)
    text = copy_template_manifest(template_id, suggested_name=suggested)
    if output:
        out_path = Path(output).expanduser()
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(text, encoding="utf-8")
        err_console.print(f"[green]Wrote manifest:[/green] {out_path}")
        err_console.print(f"[dim]Apply with:[/dim] ax gateway agents apply {out_path}")
    else:
        print(text, end="")


@agents_app.command("list")
def list_agents(
    as_json: bool = JSON_OPTION,
    show_all: bool = typer.Option(
        False,
        "--all",
        "-a",
        help="Include archived, hidden (auto-swept stale), and system (switchboard / service-account) agents.",
    ),
    archived_only: bool = typer.Option(
        False,
        "--archived",
        help="Show only archived (user-disabled) agents — the inactive section.",
    ),
):
    """List Gateway-managed agents."""
    payload = _status_payload(include_hidden=show_all or archived_only)
    agents = payload["agents"]
    if archived_only:
        agents = [a for a in agents if str(a.get("lifecycle_phase") or "active") == "archived"]
    if as_json:
        print_json(
            {
                "agents": agents,
                "count": len(agents),
                "archived": payload["summary"].get("archived_agents", 0),
                "hidden": payload["summary"].get("hidden_agents", 0),
                "system": payload["summary"].get("system_agents", 0),
            }
        )
        return
    print_table(
        ["Ref", "Agent", "Type", "Mode", "Presence", "Output", "Confidence", "Space"],
        [{**agent, "type": _agent_type_label(agent), "output": _agent_output_label(agent)} for agent in agents],
        keys=["registry_ref", "name", "type", "mode", "presence", "output", "confidence", "space_id"],
    )
    archived_n = payload["summary"].get("archived_agents", 0)
    hidden_n = payload["summary"].get("hidden_agents", 0)
    system_n = payload["summary"].get("system_agents", 0)
    if not show_all and not archived_only and (archived_n or hidden_n or system_n):
        err_console.print(
            f"[dim]({archived_n} archived, {hidden_n} hidden, {system_n} system — "
            "pass --all to include, --archived to show only archived)[/dim]"
        )


@agents_app.command("show")
def show_agent(
    name: str = typer.Argument(..., help="Managed agent name"),
    activity_limit: int = typer.Option(12, "--activity-limit", help="Number of recent agent events to display"),
    as_json: bool = JSON_OPTION,
):
    """Show one managed agent in detail."""
    result = _agent_detail_payload(name, activity_limit=activity_limit)
    if result is None:
        err_console.print(f"[red]Managed agent not found:[/red] {name}")
        raise typer.Exit(1)
    if as_json:
        print_json(result)
        return
    console.print(_render_agent_detail(result["agent"], activity=result["recent_activity"]))


@agents_app.command("test")
def test_agent(
    name: str = typer.Argument(..., help="Managed agent name"),
    message: str = typer.Option(None, "--message", help="Override the recommended Gateway test prompt"),
    author: str = typer.Option("agent", "--author", help="Who should author the test message: agent | user"),
    sender_agent: str = typer.Option(None, "--sender-agent", help="Managed sender identity to use when --author agent"),
    as_json: bool = JSON_OPTION,
):
    """Send a Gateway-authored test message to one managed agent."""
    try:
        result = _send_gateway_test_to_managed_agent(name, content=message, author=author, sender_agent=sender_agent)
    except ValueError as exc:
        err_console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1)

    if as_json:
        print_json(result)
        return

    err_console.print(f"[green]Gateway test sent:[/green] @{result['target_agent']}")
    err_console.print(f"  prompt = {result['recommended_prompt']}")
    message_payload = result.get("message") or {}
    if isinstance(message_payload, dict) and message_payload.get("id"):
        err_console.print(f"  message_id = {message_payload['id']}")


@agents_app.command("smoke")
def smoke_agent(
    name: str = typer.Argument(..., help="Managed agent name"),
    message: str = typer.Option(
        None,
        "--message",
        "-m",
        help="Prompt to send directly to the handler (default: agent's recommended_test_message or 'ping')",
    ),
    timeout: int = typer.Option(60, "--timeout", "-t", help="Seconds to wait for a reply from channel/hermes agents"),
    as_json: bool = JSON_OPTION,
):
    """Invoke a managed agent's handler in-process and show the response.

    Bypasses the platform SSE loop entirely — useful for offline development
    (AX_OFFLINE=1) to confirm an agent's handler logic works end-to-end.
    Supports echo and exec runtime types.
    """
    entry = _load_managed_agent_or_exit(name)
    runtime_type = str(entry.get("runtime_type") or "echo").lower()
    prompt = (message or "").strip() or _recommended_test_message(entry) or "ping"

    _channel_runtimes = {"claude_code_channel", "hermes_plugin", "sentinel_inference_sdk", "hermes"}

    try:
        if runtime_type == "echo":
            response = gateway_core._echo_handler(prompt, entry)
            result = {"agent": name, "runtime_type": runtime_type, "prompt": prompt, "response": response}
        elif runtime_type in {"exec", "command"}:
            command = str(entry.get("exec_command") or "").strip()
            if not command:
                err_console.print("[red]exec runtime requires exec_command in the registry entry.[/red]")
                raise typer.Exit(1)
            exec_timeout = (
                gateway_core.runtime_timeout_seconds(entry)
                if hasattr(gateway_core, "runtime_timeout_seconds")
                else None
            )
            response = gateway_core._run_exec_handler(command, prompt, entry, timeout_seconds=exec_timeout)
            result = {"agent": name, "runtime_type": runtime_type, "prompt": prompt, "response": response}
        elif runtime_type in _channel_runtimes:
            import time as _time

            import httpx as _httpx

            gateway_url = os.environ.get("AX_LOCAL_GATEWAY_URL") or "http://localhost:8765"
            payload = {
                "content": f"@{name} {prompt}".strip(),
                "space_id": "00000000-0000-0000-0000-000000000001",
            }
            try:
                r = _httpx.post(f"{gateway_url}/api/v1/messages", json=payload, timeout=5.0)
                r.raise_for_status()
                data = r.json()
            except Exception as exc:
                err_console.print(f"[red]Delivery failed:[/red] {exc}")
                err_console.print("  Is `AX_OFFLINE=1 ax gateway start` running?")
                raise typer.Exit(1)
            delivered = data.get("delivered_to") or []
            if not delivered:
                err_console.print(f"[yellow]Message posted but @{name} is not connected.[/yellow]")
                if runtime_type == "claude_code_channel":
                    err_console.print(f"  Start Claude Code with: AX_BASE_URL={gateway_url} [claude command]")
                else:
                    err_console.print(f"  Agent must be running and subscribed to {gateway_url}")
                raise typer.Exit(1)
            sent_id = data.get("id")
            # Poll offline-replies.jsonl for a reply from the agent.
            # Record the file position now so we only read lines written after the send.
            replies_path = _offline_replies_path()
            start_pos = replies_path.stat().st_size if replies_path.exists() else 0
            reply_content: str | None = None
            deadline = _time.monotonic() + timeout
            while _time.monotonic() < deadline:
                if replies_path.exists():
                    with replies_path.open() as _f:
                        _f.seek(start_pos)
                        for line in _f.read().splitlines():
                            try:
                                msg = json.loads(line)
                            except json.JSONDecodeError:
                                continue
                            if str(msg.get("author") or "").lower() == name.lower():
                                reply_content = str(msg.get("content") or "")
                                break
                if reply_content is not None:
                    break
                _time.sleep(1.0)
            result = {
                "agent": name,
                "runtime_type": runtime_type,
                "prompt": prompt,
                "delivered": True,
                "message_id": sent_id,
                "response": reply_content or f"[no reply within {timeout}s — check agent session]",
            }
        else:
            err_console.print(f"[yellow]smoke not supported for runtime_type={runtime_type!r}[/yellow]")
            err_console.print("  Supported: echo, exec, claude_code_channel, hermes_plugin, sentinel_inference_sdk")
            raise typer.Exit(1)
    except typer.Exit:
        raise
    except Exception as exc:
        err_console.print(f"[red]Handler error:[/red] {exc}")
        raise typer.Exit(1)

    if as_json:
        print_json(result)
        return
    err_console.print(f"[green]Smoke:[/green] @{name}")
    err_console.print(f"  prompt    = {prompt}")
    if runtime_type in _channel_runtimes:
        err_console.print(f"  delivered = {result.get('delivered')} (message_id={result.get('message_id')})")
    err_console.print(f"  response  = {result.get('response')}")


@agents_app.command("move")
def move_agent(
    name: str = typer.Argument(..., help="Managed agent name"),
    space_id: str = typer.Option(None, "--space", "--space-id", "-s", help="Target space slug, name, or id"),
    revert: bool = typer.Option(
        False,
        "--revert",
        help=(
            "Move the agent back to its previous space. "
            "Mutually exclusive with --space; requires a prior move on this entry."
        ),
    ),
    as_json: bool = JSON_OPTION,
):
    """Move a Gateway-managed agent to another allowed space.

    Pass ``--space`` to move to a specific space, or ``--revert`` to move
    back to the previously-recorded space without retyping its id. The
    revert pointer is captured automatically on every successful move,
    so the standard "move out, move back" loop works without bookkeeping.
    """
    if not revert and not (space_id and space_id.strip()):
        err_console.print("[red]Provide --space or --revert.[/red]")
        raise typer.Exit(1)
    try:
        result = _move_managed_agent_space(name, space_id, revert=revert)
    except (LookupError, ValueError) as exc:
        err_console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1)

    if as_json:
        print_json(result)
        return

    err_console.print(f"[green]Managed agent moved:[/green] @{name}")
    err_console.print(
        f"  space = {result.get('active_space_name') or result.get('active_space_id') or result.get('space_id')}"
    )
    if result.get("previous_space_id"):
        previous_label = result.get("previous_space_name") or result.get("previous_space_id")
        err_console.print(f"  previous = {previous_label} (use --revert to move back)")


@agents_app.command("doctor")
def doctor_agent(
    name: str = typer.Argument(..., help="Managed agent name"),
    send_test: bool = typer.Option(False, "--send-test", help="Also send a Gateway-authored smoke test"),
    as_json: bool = JSON_OPTION,
):
    """Run Gateway Doctor checks for one managed agent."""
    try:
        result = _run_gateway_doctor(name, send_test=send_test)
    except (LookupError, ValueError) as exc:
        err_console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1)

    if as_json:
        print_json(result)
        return

    tone = {"passed": "green", "warning": "yellow", "failed": "red"}.get(result["status"], "cyan")
    err_console.print(f"[{tone}]Gateway Doctor {result['status']}:[/{tone}] @{name}")
    err_console.print(f"  summary = {result['summary']}")
    print_table(["Check", "Status", "Detail"], result["checks"], keys=["name", "status", "detail"])


@agents_app.command("archive")
def archive_agent(
    names: list[str] = typer.Argument(..., help="One or more managed agent names to archive"),
    reason: str = typer.Option(None, "--reason", "-r", help="Optional note describing why this is archived"),
    as_json: bool = JSON_OPTION,
):
    """Archive (disable) one or more managed agents.

    Archived agents are sticky-hidden — they don't appear in default views
    and the daemon will not auto-restore them on reconnect. Use
    `agents restore` to bring them back.
    """
    archived: list[dict] = []
    not_found: list[str] = []
    for name in names:
        try:
            archived.append(_archive_managed_agent(name, reason=reason))
        except LookupError:
            not_found.append(name)
    if as_json:
        print_json({"archived": archived, "not_found": not_found, "count": len(archived)})
        if not_found and not archived:
            raise typer.Exit(1)
        return
    for entry in archived:
        err_console.print(f"[green]Archived:[/green] @{entry.get('name')}")
    for name in not_found:
        err_console.print(f"[red]Managed agent not found:[/red] {name}")
    if not archived and not_found:
        raise typer.Exit(1)


@agents_app.command("restore")
def restore_agent(
    names: list[str] = typer.Argument(..., help="One or more archived agent names to restore"),
    as_json: bool = JSON_OPTION,
):
    """Restore (re-enable) one or more archived agents.

    Restores `lifecycle_phase=active`. The runtime returns to the desired
    state captured at archive time; if none was captured, defaults to
    stopped. Start the runtime explicitly with `agents start <name>`.
    """
    restored: list[dict] = []
    not_found: list[str] = []
    for name in names:
        try:
            restored.append(_restore_managed_agent(name))
        except LookupError:
            not_found.append(name)
    if as_json:
        print_json({"restored": restored, "not_found": not_found, "count": len(restored)})
        if not_found and not restored:
            raise typer.Exit(1)
        return
    for entry in restored:
        ds = str(entry.get("desired_state") or "stopped")
        err_console.print(f"[green]Restored:[/green] @{entry.get('name')} (desired_state={ds})")
    for name in not_found:
        err_console.print(f"[red]Managed agent not found:[/red] {name}")
    if not restored and not_found:
        raise typer.Exit(1)


@agents_app.command("recover")
def recover_agents(
    names: list[str] = typer.Argument(..., help="One or more agent names whose registry rows were lost"),
    as_json: bool = JSON_OPTION,
):
    """Recover registry rows from local evidence (token + activity log).

    Use when a managed_agent_added event was recorded but the registry
    row is missing — typically pre-race-fix damage. Reads the most
    recent managed_agent_added event for each name from the activity
    log, confirms the token file exists, and inserts a minimal row
    with the verified fields. The daemon hydrates the rest from
    upstream on the next reconcile pass.

    Refuses to recover agents already present in the registry. Refuses
    to recover agents lacking either the activity event or the token
    file (we don't fabricate credentials).
    """
    try:
        result = _recover_managed_agents_from_evidence(list(names))
    except ValueError as exc:
        err_console.print(f"[red]Error:[/red] {exc}")
        raise typer.Exit(2) from exc
    if as_json:
        print_json(result)
        if result["count"] == 0:
            raise typer.Exit(1)
        return
    for entry in result.get("recovered", []):
        err_console.print(f"[green]Recovered:[/green] @{entry.get('name')} (agent_id={entry.get('agent_id')})")
    for name in result.get("already_present", []):
        err_console.print(f"[yellow]Already present:[/yellow] @{name} (no recovery needed)")
    for name in result.get("no_evidence", []):
        err_console.print(
            f"[red]No recovery evidence:[/red] @{name} (need both managed_agent_added activity + token file)"
        )
    if result["count"] == 0 and (result.get("no_evidence") or not result.get("already_present")):
        raise typer.Exit(1)


@agents_app.command("remove")
def remove_agent(name: str = typer.Argument(..., help="Managed agent name")):
    """Remove a managed agent from local Gateway control."""
    try:
        entry = _remove_managed_agent(name)
    except LookupError:
        err_console.print(f"[red]Managed agent not found:[/red] {name}")
        raise typer.Exit(1)
    err_console.print(f"[green]Removed managed agent:[/green] @{name}")
    workdir = str(entry.get("workdir") or "").strip()
    if workdir:
        config_path = Path(workdir) / ".ax" / "config.toml"
        if config_path.exists():
            try:
                try:
                    import tomllib
                except ImportError:
                    import tomli as tomllib  # type: ignore[no-redef]
                cfg = tomllib.loads(config_path.read_text())
                if str(cfg.get("agent_name") or "").strip() == name:
                    err_console.print(
                        f"[yellow]Warning:[/yellow] {config_path} still references @{name}. "
                        "Update or remove it to avoid errors when running commands from that directory."
                    )
            except Exception:
                pass  # best-effort — never block the remove on a config read failure


# Deferred cross-module imports (bottom-of-file to avoid import cycles; bound
# into module globals after defs, resolved at call time).
from .gateway_auth import (  # noqa: E402
    INTERACTIVE_429_BASE_WAIT,
    INTERACTIVE_429_MAX_RETRIES,
    _load_gateway_session_or_exit,
    _load_gateway_user_client,
    _wipe_ephemeral_session_if_marked,
    _with_upstream_429_retry,
)
from .gateway_diagnostics import _agent_detail_payload, _run_gateway_doctor, _status_payload  # noqa: E402
from .gateway_local import _gateway_local_config_text  # noqa: E402
from .gateway_messaging import (  # noqa: E402
    _build_session_client_silent,
    _recommended_test_message,
    _send_gateway_test_to_managed_agent,
)
from .gateway_runtime_cmd import (  # noqa: E402
    _normalize_runtime_type,
    _normalize_timeout_seconds,
    _resolve_hermes_model,
    _validate_hermes_provider,
    _validate_runtime_registration,
)
from .gateway_spaces import (  # noqa: E402
    _existing_agent_home_space,
    _resolve_gateway_agent_home_space,
    _resolve_space_via_cache,
    _space_list_from_response,
    _space_name_for_id,
)
from .gateway_ui import (  # noqa: E402
    _agent_output_label,
    _agent_type_label,
    _offline_replies_path,
    _render_agent_detail,
)
