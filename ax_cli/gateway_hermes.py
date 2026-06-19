"""Gateway Hermes / sentinel-inference-SDK runtime setup and command/env building.

Extracted from ``ax_cli/gateway.py`` (issue #28 Phase 2).
"""

from __future__ import annotations

import os
import shutil
import uuid
from pathlib import Path
from typing import Any

from .gateway_constants import ENV_DENYLIST, HERMES_KNOWN_PROVIDERS


def _gateway_repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


def _agents_dir_for_entry(entry: dict[str, Any]) -> Path:
    workdir = Path(str(entry.get("workdir") or "")).expanduser() if str(entry.get("workdir") or "").strip() else None
    if workdir is not None:
        return workdir.parent
    return Path("/home/ax-agent/agents")


def _sentinel_inference_sdk_script(entry: dict[str, Any]) -> Path:
    """Resolve the Hermes sentinel script path.

    Order:
        1. Explicit operator override on the agent entry (`sentinel_script` /
           `sentinel_inference_sdk_script`).
        2. Live-host operator copy at `_agents_dir_for_entry(entry) /
           "claude_agent_v2.py"` if it exists (preserves the EC2 dev-fleet
           workflow without requiring ax-cli reinstalls).
        3. Bundled vendored sentinel that ships with ax-cli (`pip install`
           users get this automatically — no external clone required).
    """
    configured = str(entry.get("sentinel_script") or entry.get("sentinel_inference_sdk_script") or "").strip()
    if configured:
        return Path(configured).expanduser()
    operator_copy = _agents_dir_for_entry(entry) / "claude_agent_v2.py"
    if operator_copy.exists():
        return operator_copy
    bundled = Path(__file__).resolve().parent / "runtimes" / "hermes" / "sentinel.py"
    return bundled


def sentinel_sdk_venv_root(client: str) -> Path:
    """Gateway-owned venv root for sentinel_inference_sdk, scoped per client.

    All agents on the same client share this venv — credentials and workdir
    are per-agent and live elsewhere. Returns
    ``~/.ax/runtimes/sentinel_inference_sdk/<client>``.
    """
    return Path.home() / ".ax" / "runtimes" / "sentinel_inference_sdk" / client


def _sentinel_inference_sdk_python(entry: dict[str, Any]) -> str:
    # 1. Explicit per-agent override (set via `agents update --python`).
    configured = str(entry.get("python") or "").strip()
    if configured:
        return configured
    # 2. Client-scoped shared venv under ~/.ax/runtimes/sentinel_inference_sdk/<client>.
    client = str(entry.get("client") or "").strip()
    if client:
        candidate = sentinel_sdk_venv_root(client) / ".venv" / "bin" / "python3"
        if candidate.exists():
            return str(candidate)
    return "python3"


def _sentinel_inference_sdk_model(entry: dict[str, Any]) -> str:
    for key in ("hermes_model", "sentinel_model", "runtime_model", "model"):
        value = str(entry.get(key) or "").strip()
        if value:
            return value
    return str(os.environ.get("AX_GATEWAY_HERMES_MODEL") or "codex:gpt-5.5")


def _sentinel_inference_sdk_workdir(entry: dict[str, Any]) -> Path:
    raw = str(entry.get("workdir") or "").strip()
    if raw:
        return Path(raw).expanduser()
    return Path("/home/ax-agent/agents") / str(entry.get("name") or "agent")


def _gateway_environment_context(entry: dict[str, Any]) -> str:
    """Build the gateway-supplied environment context that is appended to the
    operator's per-agent system prompt.

    Tells the agent (a) what aX is, (b) that it's part of a multi-agent
    network and how collaboration works, and (c) the minimal CLI it can
    use to interact with other agents. Kept short and concrete — long
    appended prompts dilute the operator's role instructions.
    """
    name = str(entry.get("name") or "").strip() or "<this agent>"
    space_id = str(entry.get("space_id") or entry.get("active_space_id") or "").strip()
    space_name = str(entry.get("active_space_name") or entry.get("space_name") or "").strip()
    space_label = space_name or space_id or "<unknown space>"
    base_url = str(entry.get("base_url") or "https://paxai.app").strip()
    lines = [
        "--- aX environment context ---",
        f"You are @{name}, an aX agent on a multi-agent network at {base_url}.",
        f"Your active space: {space_label}.",
        "",
        "Collaboration model:",
        "- Other agents in your space may @-mention you. They expect a reply.",
        "- Reply on the same thread by passing the incoming message_id as parent_id.",
        "- @-mention other agents by name to delegate or ask for help.",
        "- A separate Gateway daemon brokers your credentials and routes messages —",
        "  you do not need to manage tokens yourself.",
        "",
        "CLI you can use from your shell:",
        '  ax send "@target your message"            # send a new message',
        '  ax send -p <message_id> "..."             # reply on a thread',
        "  ax messages list                           # read your inbox",
        '  ax tasks create "title" --assign-to <agent>  # delegate work',
        "  ax tasks list                              # see open tasks for you",
        "  ax agents list                             # see who is online",
        "",
        "Operator-supplied role instructions (above) take precedence over this",
        "environment context. If a field above (space, base_url) is missing, fall",
        "back to the values in your local .ax/config.toml.",
    ]

    from .connectors.guidance import connector_instruction_lines, connector_ref_for_agent

    connector_ref = connector_ref_for_agent(entry)
    if connector_ref:
        lines.extend(connector_instruction_lines(connector_ref))

    return "\n".join(lines)


def _compose_agent_system_prompt(entry: dict[str, Any]) -> str | None:
    """Combine the operator's per-agent system prompt with the gateway-supplied
    environment context. Operator prompt comes first (the agent's role
    identity); gateway context is appended (the collaboration environment).

    Returns None when neither piece is present so the runtime command builder
    omits the flag entirely instead of passing an empty string.
    """
    operator_prompt = str(entry.get("system_prompt") or "").strip()
    if str(entry.get("system_prompt_skip_environment") or "").strip().lower() in {"1", "true", "yes"}:
        return operator_prompt or None
    environment = _gateway_environment_context(entry)
    parts = [p for p in (operator_prompt, environment) if p]
    return "\n\n".join(parts) if parts else None


# Valid inference SDK clients for sentinel_inference_sdk (per ADR-012 / ADR-014).
# hermes_sdk is intentionally excluded: use sentinel_hermes_sdk runtime type.
_INFERENCE_SDK_CLIENTS = {
    "openai_sdk",
    "openrouter_sdk",
    "groq_sdk",
    "gemini_sdk",
    "leapfrog_sdk",
    "mistral_sdk",
    "together_sdk",
    "xai_sdk",
}


def inference_sdk_client_names() -> list[str]:
    """Sorted list of valid inference SDK client values for sentinel_inference_sdk.

    Single source of truth for both validation (via `_INFERENCE_SDK_CLIENTS`)
    and operator-facing help text. Help strings in `ax gateway agents add`
    plus `ax gateway agents update` and the `sentinel_inference_sdk`
    runtime-type description are derived from this helper so they cannot
    drift from the accepted set when a new SDK runtime lands (see #326).
    """
    return sorted(_INFERENCE_SDK_CLIENTS)


# Valid MCP host clients for sentinel_cli. Maps client value → binary name.
# claude_code_channel always uses claude_cli and sets it automatically — operators
# do not supply --client for that runtime type.
_MCP_HOST_CLIENT_BINARIES: dict[str, str] = {
    "claude_cli": "claude",
}


def _resolve_inference_client(entry: dict[str, Any]) -> str | None:
    """Resolve the inference SDK client for a sentinel_inference_sdk agent.

    Reads the `client` field (ADR-014). Returns None if absent or not a
    recognised client — the caller must treat None as a setup error.

    Do not call for sentinel_hermes_sdk agents; their client is always
    hermes_sdk and is hardcoded in the dispatch layer.
    """
    configured = str(entry.get("client") or "").strip().lower()
    if configured in _INFERENCE_SDK_CLIENTS:
        return configured
    return None


def _build_sentinel_inference_sdk_cmd(entry: dict[str, Any], *, sdk_runtime: str) -> list[str]:
    timeout = str(entry.get("timeout_seconds") or entry.get("timeout") or 600)
    update_interval = str(entry.get("update_interval") or 2.0)
    cmd = [
        _sentinel_inference_sdk_python(entry),
        "-u",
        str(_sentinel_inference_sdk_script(entry)),
        "--agent",
        str(entry.get("name") or ""),
        "--workdir",
        str(_sentinel_inference_sdk_workdir(entry)),
        "--timeout",
        timeout,
        "--update-interval",
        update_interval,
        "--runtime",
        sdk_runtime,
        "--model",
        _sentinel_inference_sdk_model(entry),
    ]
    allowed_tools = str(entry.get("allowed_tools") or "").strip()
    if allowed_tools:
        cmd.extend(["--allowed-tools", allowed_tools])
    composed_prompt = _compose_agent_system_prompt(entry)
    if composed_prompt:
        cmd.extend(["--system-prompt", composed_prompt])
    return cmd


def _build_sentinel_inference_sdk_env(entry: dict[str, Any]) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k not in ENV_DENYLIST}
    token = load_gateway_managed_agent_token(entry)
    workdir = _sentinel_inference_sdk_workdir(entry)
    agents_dir = _agents_dir_for_entry(entry)
    repo_root = str(_gateway_repo_root())

    # Per-agent HERMES_HOME so each hermes agent gets its own memories/ dir
    # under ~/.ax/gateway/agents/<name>/hermes-home. Without this, every
    # hermes agent on the host shares ~/.hermes/memories/MEMORY.md and
    # clobbers each other.
    agent_name = str(entry.get("name") or "")
    hermes_home = agent_dir(agent_name) / "hermes-home" if agent_name else None

    env.update(
        {
            "AX_TOKEN": token,
            "AX_BASE_URL": str(entry.get("base_url") or ""),
            "AX_AGENT_NAME": agent_name,
            "AX_AGENT_ID": str(entry.get("agent_id") or ""),
            "AX_SPACE_ID": str(entry.get("space_id") or ""),
            "AX_CONFIG_DIR": str(workdir / ".ax"),
            "AX_PYTHON": _sentinel_inference_sdk_python(entry),
            "HERMES_MAX_ITERATIONS": str(
                entry.get("hermes_max_iterations") or os.environ.get("HERMES_MAX_ITERATIONS") or 60
            ),
        }
    )
    if hermes_home is not None:
        hermes_home.mkdir(parents=True, exist_ok=True)
        env["HERMES_HOME"] = str(hermes_home)
    env.setdefault("AGENT_RUNNER_API_KEY", "staging-dispatch-key")
    env.setdefault("INTERNAL_DISPATCH_API_KEY", env["AGENT_RUNNER_API_KEY"])

    # sentinel_inference_sdk only imports stdlib and ax_cli.mentions — no
    # hermes-agent dependency. PYTHONPATH only needs agents_dir (for any
    # operator-local tools module) and repo_root (for ax_cli itself).
    python_paths = [str(agents_dir), repo_root]
    existing_pythonpath = env.get("PYTHONPATH")
    if existing_pythonpath:
        python_paths.append(existing_pythonpath)
    env["PYTHONPATH"] = ":".join(path for path in python_paths if path)

    path_entries = [str(_gateway_repo_root() / ".venv" / "bin"), "/home/ax-agent/shared/repos/ax-cli/.venv/bin"]
    if env.get("PATH"):
        path_entries.append(env["PATH"])
    env["PATH"] = ":".join(path_entries)
    return env


# ---------------------------------------------------------------------------
# Hermes plugin runtime (`runtime_type == "hermes_plugin"`)
#
# Gateway supervises a single long-running `hermes gateway run` process per
# agent. The Hermes process discovers our aX platform plugin (linked into
# HERMES_HOME/plugins/ax) and connects to aX over SSE; replies post via the
# aX REST API. Gateway's job here is identity + supervision, not message
# brokering. The bootstrap PAT never lives in the workspace — Gateway reads
# the token from its owned token file at spawn time and exports it into the
# child process's env only.
# ---------------------------------------------------------------------------


def _hermes_plugin_workdir(entry: dict[str, Any]) -> Path:
    raw = str(entry.get("workdir") or "").strip()
    if raw:
        return Path(raw).expanduser()
    return Path("/home/ax-agent/agents") / str(entry.get("name") or "agent")


def _hermes_plugin_home(entry: dict[str, Any]) -> Path:
    """Per-agent HERMES_HOME under the workdir. Workdir-as-home matches the
    operator pattern that nova and ax-wiki already use, and keeps each
    agent's memories/sessions/skills next to its workdir rather than under
    a Gateway-owned location."""
    configured = str(entry.get("hermes_home") or "").strip()
    if configured:
        return Path(configured).expanduser()
    return _hermes_plugin_workdir(entry) / ".hermes"


def _hermes_bin(entry: dict[str, Any]) -> str:
    """Resolve the hermes CLI.

    Order:
        1. Explicit operator override on the agent entry (`hermes_bin`).
        2. ``HERMES_BIN`` env var on the Gateway process.
        3. ``<HERMES_REPO_PATH>/.venv/bin/hermes`` if a repo path is configured.
        4. ``~/hermes-agent/.venv/bin/hermes`` (the documented dev default).
        5. ``hermes`` on $PATH (raises ``RuntimeError`` if not present).
    """
    configured = str(entry.get("hermes_bin") or "").strip()
    if configured:
        return configured
    env_override = os.environ.get("HERMES_BIN", "").strip()
    if env_override:
        return env_override
    hermes_repo = str(entry.get("hermes_repo_path") or "").strip()
    if hermes_repo:
        candidate = Path(hermes_repo).expanduser() / ".venv" / "bin" / "hermes"
        if candidate.exists():
            return str(candidate)
    default = Path.home() / "hermes-agent" / ".venv" / "bin" / "hermes"
    if default.exists():
        return str(default)
    found = shutil.which("hermes")
    if found:
        return found
    raise RuntimeError(
        "hermes CLI not found. Install hermes-agent, set HERMES_BIN, or set hermes_bin on the agent entry."
    )


# Canonical name of the aX platform plugin as published in ``plugin.yaml``.
# Used by the scaffold (to enable it in per-agent ``config.yaml``) and the
# doctor (to verify the same name shows up in ``plugins.enabled``).
AX_PLUGIN_NAME = "ax-platform"


def _plugin_source_dir() -> Path:
    """Resolve the aX platform plugin directory shipped with ``ax_cli``.

    The plugin lives at ``ax_cli/plugins/platforms/ax/`` so it ships inside
    the wheel — the prior ``<repo>/plugins/...`` layout only worked for
    editable installs because ``[tool.setuptools.packages.find]`` is
    ``include=["ax_cli*"]`` and never picked up the top-level ``plugins/``
    tree. After this change Gateway can resolve the plugin source from any
    installed ``ax_cli`` (wheel, sdist, or editable) without scaffolding a
    dangling symlink into ``~/.hermes/plugins/ax``.
    """
    import ax_cli as _ax_cli_pkg

    return Path(_ax_cli_pkg.__file__).resolve().parent / "plugins" / "platforms" / "ax"


def _scaffold_hermes_plugin_home(entry: dict[str, Any]) -> Path:
    """Make HERMES_HOME ready for ``hermes gateway run`` without writing
    secrets to disk.

    Idempotent. Creates the directory, links the aX platform plugin into
    ``$HERMES_HOME/plugins/ax``, writes a non-secret ``.env`` with the
    agent's identity, and (if missing) links the host's ``~/.hermes/auth.json``
    and ``~/.hermes/config.yaml`` so the agent inherits the operator's
    provider credentials. Operators who want per-agent provider creds can
    delete the symlinks and provision their own files.
    """
    workdir = _hermes_plugin_workdir(entry)
    workdir.mkdir(parents=True, exist_ok=True)
    home = _hermes_plugin_home(entry)
    home.mkdir(parents=True, exist_ok=True)
    plugins_dir = home / "plugins"
    plugins_dir.mkdir(parents=True, exist_ok=True)
    plugin_link = plugins_dir / "ax"
    plugin_source = _plugin_source_dir()
    if not plugin_link.exists() and not plugin_link.is_symlink():
        try:
            plugin_link.symlink_to(plugin_source)
        except OSError:
            # Some filesystems disallow symlinks; fall back to a marker file
            # so the operator gets a clear "go link this yourself" signal.
            (plugins_dir / "ax.MISSING").write_text(
                f"Could not symlink {plugin_source} → {plugin_link}. "
                f"Link manually so `hermes plugins list` shows ax-platform.\n",
                encoding="utf-8",
            )
    elif plugin_link.is_symlink():
        # Refresh the symlink if it points at a stale source (e.g. repo moved).
        try:
            current_target = plugin_link.resolve()
        except OSError:
            current_target = None
        if current_target != plugin_source.resolve():
            try:
                plugin_link.unlink()
                plugin_link.symlink_to(plugin_source)
            except OSError:
                pass
    # Non-secret identity .env so `hermes gateway run` can come up
    # standalone (without Gateway env injection) for debugging. AX_TOKEN
    # is deliberately omitted — it is injected via subprocess env only.
    env_lines = [
        "# Managed by ax gateway. Identity only; never AX_TOKEN.",
        "# Gateway injects AX_TOKEN into the subprocess env from",
        "# ~/.ax/gateway/agents/<name>/token (mode 600) at spawn time.",
        f"AX_AGENT_NAME={entry.get('name') or ''}",
        f"AX_AGENT_ID={entry.get('agent_id') or ''}",
        f"AX_SPACE_ID={entry.get('space_id') or ''}",
        f"AX_BASE_URL={entry.get('base_url') or 'https://paxai.app'}",
        f"AX_HOME_CHANNEL={entry.get('home_channel_id') or entry.get('space_id') or ''}",
    ]
    # Allowlist controls. Two independent layers:
    #   - AX_ALLOWED_USERS / AX_ALLOW_ALL_USERS: plugin-side filter on who
    #     can @-mention this agent (adapter checks the sender's name).
    #   - GATEWAY_ALLOW_ALL_USERS: hermes-side gate; without it, hermes
    #     refuses to dispatch any request when no platform allowlist is set.
    # Operators opt in by setting `entry["allow_all_users"] = True` (e.g. via
    # `ax gateway agents add/update --allow-all-users`). Default-closed.
    if entry.get("allow_all_users"):
        env_lines.append("AX_ALLOW_ALL_USERS=1")
        env_lines.append("GATEWAY_ALLOW_ALL_USERS=true")
    allowed = str(entry.get("allowed_users") or "").strip()
    if allowed:
        env_lines.append(f"AX_ALLOWED_USERS={allowed}")
    (home / ".env").write_text("\n".join(env_lines) + "\n", encoding="utf-8")
    # Inherit provider creds from the operator's ~/.hermes/auth.json. This
    # is a symlink so credential rotation propagates without re-scaffolding.
    operator_home = Path.home() / ".hermes"
    auth_source = operator_home / "auth.json"
    auth_target = home / "auth.json"
    if not (auth_target.exists() or auth_target.is_symlink()) and auth_source.exists():
        try:
            auth_target.symlink_to(auth_source)
        except OSError:
            pass
    # Render a per-agent config.yaml with terminal.cwd pinned to this
    # agent's workdir. Symlinking the operator's config.yaml verbatim
    # leaked terminal.cwd (e.g. another agent's path) through the
    # `hermes gateway run` bridge in gateway/run.py, which writes
    # TERMINAL_CWD from config.yaml regardless of what the per-agent
    # .env sets — and the LLM then mis-identifies itself from the
    # workdir name in its system prompt. The render starts from the
    # operator's config (so model/provider/agent defaults still apply)
    # and is regenerated on every scaffold call, which means rotating
    # those defaults still propagates the next time the runtime starts.
    _render_hermes_plugin_config_yaml(entry, home=home, operator_home=operator_home)
    return home


def _render_hermes_plugin_config_yaml(entry: dict[str, Any], *, home: Path, operator_home: Path) -> None:
    """Write ``$HERMES_HOME/config.yaml`` with ``terminal.cwd`` pinned to the
    agent's workdir AND the aX platform plugin enabled, seeded from the
    operator's ``~/.hermes/config.yaml``.

    Hermes' plugin system is opt-in by default — discovered user plugins
    are gated behind a ``plugins.enabled`` allowlist
    (``hermes_cli/plugins.py``: "Plugins are opt-in by default — only
    plugins whose name appears in this set are loaded"). Without this
    block the runtime cleanly comes up, ``hermes plugins list`` shows
    ``ax-platform`` as ``not enabled``, the bound platform never reaches
    ``self.config.platforms``, and ``hermes gateway run`` logs the
    silent-but-fatal ``No messaging platforms enabled`` — agent stays
    silent forever with no error visible in ``gateway agents show``.
    Pinning ``plugins.enabled`` here means every ``hermes_plugin`` agent
    self-enables ``ax-platform`` on the next start without the operator
    needing to learn the gate exists.

    ``plugins.disabled`` (if present) is scrubbed of ``ax-platform`` so a
    stale operator-level disable can't override our enable. Other plugin
    names in both lists are left untouched.

    Writes via a temp file + atomic replace so a partial write can't leave
    Hermes booting against a half-yaml. Any non-mapping ``terminal`` or
    ``plugins`` value in the operator config is replaced with a fresh
    mapping so we never silently keep a bogus structure.
    """
    workdir = _hermes_plugin_workdir(entry)
    target = home / "config.yaml"
    operator_config = operator_home / "config.yaml"
    cfg: dict[str, Any] = {}
    if operator_config.exists():
        try:
            import yaml  # local import keeps gateway import cost down for non-Hermes paths

            loaded = yaml.safe_load(operator_config.read_text(encoding="utf-8"))
        except Exception:
            loaded = None
        if isinstance(loaded, dict):
            cfg = loaded
    entry_provider = str(entry.get("provider") or "").strip() or None
    if entry_provider:
        provider_info = HERMES_KNOWN_PROVIDERS.get(entry_provider, {})
        cfg["provider"] = entry_provider
        if provider_info.get("default_model") and not cfg.get("model"):
            cfg["model"] = provider_info["default_model"]
        providers_cfg = cfg.get("providers")
        if not isinstance(providers_cfg, dict):
            providers_cfg = {}
        if entry_provider not in providers_cfg:
            providers_cfg[entry_provider] = {}
        prov_block = providers_cfg[entry_provider]
        if provider_info.get("default_model") and not prov_block.get("default_model"):
            prov_block["default_model"] = provider_info["default_model"]
        if provider_info.get("base_url") and not prov_block.get("base_url"):
            prov_block["base_url"] = provider_info["base_url"]
        providers_cfg[entry_provider] = prov_block
        cfg["providers"] = providers_cfg
        aux = cfg.get("auxiliary")
        if isinstance(aux, dict):
            title_gen = aux.get("title_generation")
            if isinstance(title_gen, dict) and not title_gen.get("provider"):
                title_gen["provider"] = entry_provider
                if provider_info.get("default_model") and not title_gen.get("model"):
                    title_gen["model"] = provider_info["default_model"]

    terminal_cfg = cfg.get("terminal")
    if not isinstance(terminal_cfg, dict):
        terminal_cfg = {}
    terminal_cfg["cwd"] = str(workdir)
    cfg["terminal"] = terminal_cfg

    plugins_cfg = cfg.get("plugins")
    if not isinstance(plugins_cfg, dict):
        plugins_cfg = {}
    enabled = plugins_cfg.get("enabled")
    if not isinstance(enabled, list):
        enabled = []
    if AX_PLUGIN_NAME not in enabled:
        enabled.append(AX_PLUGIN_NAME)
    plugins_cfg["enabled"] = enabled
    disabled = plugins_cfg.get("disabled")
    if isinstance(disabled, list) and AX_PLUGIN_NAME in disabled:
        plugins_cfg["disabled"] = [name for name in disabled if name != AX_PLUGIN_NAME]
    cfg["plugins"] = plugins_cfg
    try:
        import yaml

        rendered = yaml.safe_dump(cfg, sort_keys=False)
    except Exception:
        # Last-resort minimal config so the agent can still come up with a
        # correct terminal.cwd even if the operator config is unreadable.
        rendered = f"terminal:\n  cwd: {workdir}\n"
    # Replace any stale symlink from earlier scaffolds before writing.
    if target.is_symlink():
        try:
            target.unlink()
        except OSError:
            pass
    tmp = target.with_suffix(target.suffix + ".tmp")
    tmp.write_text(rendered, encoding="utf-8")
    os.replace(tmp, target)


def _build_hermes_plugin_cmd(entry: dict[str, Any]) -> list[str]:
    return [_hermes_bin(entry), "gateway", "run"]


def _build_hermes_plugin_env(entry: dict[str, Any]) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k not in ENV_DENYLIST}
    token = load_gateway_managed_agent_token(entry)
    home = _hermes_plugin_home(entry)
    offline_base = os.environ.get("AX_LOCAL_GATEWAY_URL") or "http://localhost:8765"
    base_url = offline_base if os.environ.get("AX_OFFLINE") else str(entry.get("base_url") or "https://paxai.app")
    env.update(
        {
            "AX_TOKEN": token,
            "AX_BASE_URL": base_url,
            "AX_AGENT_NAME": str(entry.get("name") or ""),
            "AX_AGENT_ID": str(entry.get("agent_id") or ""),
            "AX_SPACE_ID": str(entry.get("space_id") or ""),
            "AX_HOME_CHANNEL": str(entry.get("home_channel_id") or entry.get("space_id") or ""),
            "HERMES_HOME": str(home),
        }
    )
    # Local Gateway URL so the adapter can post external-runtime announcements
    # for roster activity (best-effort; the adapter silently no-ops if Gateway
    # isn't reachable).
    gateway_url = os.environ.get("AX_LOCAL_GATEWAY_URL") or os.environ.get("AX_GATEWAY_UI_URL")
    if gateway_url:
        env["AX_LOCAL_GATEWAY_URL"] = gateway_url
    return env


def _sentinel_runtime_name(entry: dict[str, Any]) -> str:
    return "claude"


def _sentinel_session_scope(entry: dict[str, Any]) -> str:
    scope = str(entry.get("sentinel_session_scope") or entry.get("session_scope") or "agent").strip().lower()
    return scope if scope in {"agent", "thread", "message"} else "agent"


def _sentinel_session_key(entry: dict[str, Any], data: dict[str, Any] | None, message_id: str) -> str:
    scope = _sentinel_session_scope(entry)
    if scope == "message":
        return message_id or str(uuid.uuid4())
    if scope == "thread":
        data = data or {}
        return str(data.get("parent_id") or data.get("conversation_id") or message_id or "default")
    return f"space:{entry.get('space_id') or 'unknown'}:agent:{entry.get('name') or 'unknown'}"


def _sentinel_model(entry: dict[str, Any]) -> str | None:
    for key in ("model", "sentinel_model", "claude_model"):
        value = str(entry.get(key) or "").strip()
        if value:
            return value
    return None


def _resolve_sentinel_cli_binary(entry: dict[str, Any]) -> str:
    """Return the MCP host binary for a sentinel_cli agent.

    claude_code_channel always uses 'claude' — its client field is set
    automatically and this function is not called for it.
    """
    configured = str(entry.get("client") or "").strip().lower()
    return _MCP_HOST_CLIENT_BINARIES.get(configured, "claude")


def _build_sentinel_claude_cmd(entry: dict[str, Any], session_id: str | None) -> list[str]:
    add_dir = str(entry.get("add_dir") or entry.get("workdir") or os.getcwd())
    runtime_type = str(entry.get("runtime_type") or "").strip().lower()
    binary = "claude" if runtime_type == "claude_code_channel" else _resolve_sentinel_cli_binary(entry)
    cmd = [
        binary,
        "-p",
        "--output-format",
        "stream-json",
        "--verbose",
        "--add-dir",
        add_dir,
    ]
    if session_id:
        cmd.extend(["--resume", session_id])
    model = _sentinel_model(entry)
    if model:
        cmd.extend(["--model", model])
    allowed_tools = str(entry.get("allowed_tools") or "").strip()
    if allowed_tools:
        cmd.extend(["--allowedTools", allowed_tools])
    composed_prompt = _compose_agent_system_prompt(entry)
    if composed_prompt:
        cmd.extend(["--append-system-prompt", composed_prompt])
    return cmd


def _summarize_sentinel_command(command: str) -> str:
    short = " ".join(command.split())
    if len(short) > 90:
        short = short[:87] + "..."

    lowered = f" {short.lower()} "
    if "apply_patch" in lowered:
        return "Applying patch..."
    if any(token in lowered for token in (" rg ", " grep ", " find ", " fd ", " glob ")):
        return "Searching codebase..."
    if any(
        token in lowered
        for token in (" sed -n", " cat ", " head ", " tail ", " ls ", " pwd ", " git status", " git diff")
    ):
        return "Reading files..."
    if any(token in lowered for token in (" pytest", " npm test", " pnpm test", " uv run", " cargo test")):
        return "Running tests..."
    return f"Running: {short}..."


def _sentinel_tool_summary(tool_name: str, tool_input: dict[str, Any]) -> str:
    lowered = tool_name.lower()
    if lowered in {"read", "read_file"}:
        path = str(tool_input.get("file_path") or tool_input.get("path") or "")
        short = path.rsplit("/", 1)[-1] if "/" in path else path
        return f"Reading {short}..." if short else "Reading file..."
    if lowered in {"write", "write_file"}:
        path = str(tool_input.get("file_path") or tool_input.get("path") or "")
        short = path.rsplit("/", 1)[-1] if "/" in path else path
        return f"Writing {short}..." if short else "Writing file..."
    if lowered in {"edit", "edit_file", "patch"}:
        path = str(tool_input.get("file_path") or tool_input.get("path") or "")
        short = path.rsplit("/", 1)[-1] if "/" in path else path
        return f"Editing {short}..." if short else "Editing file..."
    if lowered in {"bash", "shell"}:
        command = str(tool_input.get("command") or "")[:60]
        return f"Running: {command}..." if command else "Running command..."
    if lowered in {"grep", "search", "search_files"}:
        pattern = str(tool_input.get("pattern") or "")
        return f"Searching: {pattern}..." if pattern else "Searching..."
    if lowered in {"glob", "glob_files"}:
        pattern = str(tool_input.get("pattern") or "")
        return f"Finding files: {pattern}..." if pattern else "Finding files..."
    return f"Using {tool_name}..."


# Deferred cross-module imports (bottom-of-file to avoid import cycles;
# bound into module globals after defs, resolved at call time).
from .gateway_storage import agent_dir, load_gateway_managed_agent_token  # noqa: E402
