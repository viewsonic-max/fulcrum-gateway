"""Gateway runtime backends and operator-facing templates.

Runtime types are the low-level execution adapters used by the Gateway.
Templates are the higher-level, user-facing choices presented in CLI and UI.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .gateway_hermes import inference_sdk_client_names


def _repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


def _gateway_setup_skill_path() -> Path:
    return _repo_root() / "skills" / "gateway-agent-setup" / "SKILL.md"


def _gateway_composio_connectors_skill_path() -> Path:
    return _repo_root() / "skills" / "gateway-composio-connectors" / "SKILL.md"


def _bridge_python() -> str:
    """Return the Python interpreter path Gateway should use for exec-bridge agents.

    Prefers the project's virtualenv (`.venv/bin/python` on POSIX,
    `.venv/Scripts/python.exe` on Windows) over the bare `python3` from PATH.
    The virtualenv is where the bridge's optional deps live (langgraph,
    langchain-core, langchain-groq, etc.); the system `python3` typically
    doesn't have them, so a bare `python3` invocation silently degrades the
    bridge to its string-fallback tier (see PM artifact #183 LangGraph survey
    + the project memory `project_gateway_exec_bridge_env_pitfalls` for the
    full story).

    Falls back to `python3` when no venv is found at the expected paths, so
    operators who don't use a venv (or who run the bridges out of a system
    site-packages install) still get a working command.

    Returns a string suitable for embedding in an `exec_command` template.
    Absolute paths are preferred over relative paths because Gateway
    resolves `exec_command` from the agent's `workdir`, which may differ
    from the repo root in advanced configurations.
    """
    import sys

    repo_root = _repo_root()
    # POSIX venv layout
    posix_venv = repo_root / ".venv" / "bin" / "python"
    # Windows venv layout
    win_venv = repo_root / ".venv" / "Scripts" / "python.exe"
    for candidate in (posix_venv, win_venv):
        if candidate.is_file():
            return str(candidate)
    # No venv found. Use the current interpreter if it looks venv-shaped
    # (e.g. we're running under the `ax` console script from a venv install
    # not at .venv but at some other path the user picked).
    current = Path(sys.executable)
    if ".venv" in current.parts or "venv" in current.parts:
        return str(current)
    return "python3"


def _shared_signals() -> dict[str, str]:
    return {
        "delivery": "Gateway confirms when a message was queued or claimed.",
        "liveness": "Gateway heartbeat and reconnect logic determine connected or stale state.",
    }


def runtime_type_catalog() -> dict[str, dict[str, Any]]:
    repo_root = _repo_root()
    return {
        "echo": {
            "id": "echo",
            "label": "Echo",
            "description": "Built-in test runtime for proving delivery, queueing, and reply flow.",
            "kind": "builtin",
            "passive": False,
            "requires": [],
            "form_fields": [],
            "examples": [],
            "signals": {
                **_shared_signals(),
                "activity": "Gateway emits built-in working and completed phases for echo replies.",
                "tools": "No tool-call telemetry. Echo is intentionally simple.",
            },
        },
        "exec": {
            "id": "exec",
            "label": "Command Bridge",
            "description": (
                "Gateway-owned command execution for bridges and adapters that print AX_GATEWAY_EVENT lines."
            ),
            "kind": "exec",
            "passive": False,
            "requires": ["exec_command"],
            "form_fields": [
                {
                    "name": "exec_command",
                    "label": "Exec Command",
                    "required": True,
                    "placeholder": "python3 examples/sentinel_inference_sdk/hermes_bridge.py",
                },
                {
                    "name": "workdir",
                    "label": "Workdir",
                    "required": True,
                    "placeholder": str(repo_root),
                },
            ],
            "examples": [
                {
                    "label": "Gateway Probe",
                    "exec_command": "python3 examples/gateway_probe/probe_bridge.py",
                    "workdir": str(repo_root),
                },
                {
                    "label": "Codex Bridge",
                    "exec_command": "python3 examples/codex_gateway/codex_bridge.py",
                    "workdir": str(repo_root),
                },
                {
                    "label": "Hermes Sentinel",
                    "exec_command": "python3 examples/sentinel_inference_sdk/hermes_bridge.py",
                    "workdir": str(repo_root),
                    "note": "Requires a local hermes-agent checkout plus auth setup.",
                },
                {
                    "label": "Ollama",
                    "exec_command": "python3 examples/gateway_ollama/ollama_bridge.py",
                    "workdir": str(repo_root),
                    "note": "Requires a local Ollama server and model.",
                },
            ],
            "signals": {
                **_shared_signals(),
                "activity": (
                    "Gateway can surface live activity when the bridge prints AX_GATEWAY_EVENT lines. "
                    "Without that, the operator still gets pickup and final completion."
                ),
                "tools": "Gateway can record tool usage when the bridge emits tool events.",
            },
        },
        "sentinel_inference_sdk": {
            "id": "sentinel_inference_sdk",
            "label": "Inference SDK Sentinel",
            "description": (
                "Gateway-supervised sentinel process making direct inference API calls "
                f"to vendor LLMs ({', '.join(inference_sdk_client_names())}). "
                "Requires --client to select the SDK. "
                "Tool access is controlled by connector policy. "
                "See ADR-012 for the rename history."
            ),
            "kind": "supervised_process",
            "passive": False,
            "deprecated": False,
            "requires": [],
            "form_fields": [
                {
                    "name": "workdir",
                    "label": "Workdir",
                    "required": True,
                    "placeholder": "/home/ax-agent/agents/my_sdk_agent",
                },
                {
                    "name": "model",
                    "label": "Model",
                    "required": False,
                    "placeholder": "gpt-4o",
                },
            ],
            "examples": [
                {
                    "label": "OpenAI SDK agent",
                    "runtime_type": "sentinel_inference_sdk",
                    "workdir": "/home/ax-agent/agents/openai_agent",
                    "client": "openai_sdk",
                },
            ],
            "signals": {
                **_shared_signals(),
                "activity": (
                    "Gateway reports process liveness; the sentinel emits processing "
                    "and tool activity signals via the SDK runtime callbacks."
                ),
                "tools": "Tool telemetry comes from connector_call events in the SDK runtime.",
            },
        },
        "sentinel_hermes_sdk": {
            "id": "sentinel_hermes_sdk",
            "label": "Hermes SDK Sentinel",
            "description": (
                "Gateway-supervised sentinel running the in-process Hermes AIAgent loop "
                "(90-turn agentic loop, parallel tool execution, context compression). "
                "Supports Bedrock IAM auth (bedrock:claude-*), Anthropic API "
                "(anthropic:claude-*), OpenRouter (openrouter:<model>), and Codex "
                "(codex:gpt-*) backends. Tool access is controlled by connector policy "
                "plus Hermes tool security shims. Promoted from hermes_sdk inside "
                "sentinel_inference_sdk in 0.7.0 — see ADR-012."
            ),
            "kind": "supervised_process",
            "passive": False,
            "requires": [],
            "form_fields": [
                {
                    "name": "workdir",
                    "label": "Workdir",
                    "required": True,
                    "placeholder": "/home/ax-agent/agents/my_hermes_agent",
                },
                {
                    "name": "model",
                    "label": "Model",
                    "required": True,
                    "placeholder": "bedrock:claude-sonnet-4-6 or anthropic:claude-sonnet-4-6",
                },
            ],
            "examples": [
                {
                    "label": "Bedrock Claude agent",
                    "runtime_type": "sentinel_hermes_sdk",
                    "workdir": "/home/ax-agent/agents/bedrock_agent",
                    "model": "bedrock:claude-sonnet-4-6",
                    "note": "Auth via IAM instance profile — no API key required.",
                },
                {
                    "label": "Anthropic API agent",
                    "runtime_type": "sentinel_hermes_sdk",
                    "workdir": "/home/ax-agent/agents/anthropic_agent",
                    "model": "anthropic:claude-sonnet-4-6",
                },
            ],
            "signals": {
                **_shared_signals(),
                "activity": (
                    "Gateway reports process liveness; the Hermes AIAgent loop emits "
                    "tool-progress and status callbacks that surface as activity signals."
                ),
                "tools": "Tool telemetry from Hermes tool-progress callbacks and connector_call events.",
            },
        },
        "hermes_plugin": {
            "id": "hermes_plugin",
            "label": "Hermes Plugin",
            "description": (
                "Gateway-supervised long-running Hermes process using the native "
                "aX platform plugin at ax_cli/plugins/platforms/ax/. Gateway scaffolds the "
                "agent's HERMES_HOME (plugin symlink + non-secret identity .env + per-agent "
                "config.yaml with `plugins.enabled: [ax-platform]` so Hermes' opt-in plugin "
                "gate doesn't silently drop the adapter) and spawns `hermes gateway run`; "
                "AX_TOKEN is injected at start from the Gateway-owned token file and is "
                "never written to the workspace."
            ),
            "kind": "supervised_process",
            "passive": False,
            "requires": [],
            "form_fields": [
                {
                    "name": "workdir",
                    "label": "Workdir",
                    "required": True,
                    "placeholder": "/Users/jacob/claude_home/ax-wiki",
                },
            ],
            "examples": [
                {
                    "label": "Wiki agent (Hermes plugin)",
                    "runtime_type": "hermes_plugin",
                    "workdir": "/Users/jacob/claude_home/ax-wiki",
                    "note": (
                        "Gateway launches `hermes gateway run` against HERMES_HOME=<workdir>/.hermes; "
                        "the plugin connects to aX over SSE and replies via REST."
                    ),
                },
            ],
            "signals": {
                **_shared_signals(),
                "activity": (
                    "Gateway reports process liveness; the plugin streams progress/tool events "
                    "onto the original mention's activity stream so chat stays final-only."
                ),
                "tools": (
                    "Tool telemetry comes from Hermes platform callbacks via the aX adapter "
                    "(ax_cli/plugins/platforms/ax/adapter.py)."
                ),
            },
        },
        "sentinel_cli": {
            "id": "sentinel_cli",
            "label": "Sentinel CLI",
            "description": (
                "Gateway-owned listener with the original sentinel CLI runner semantics: "
                "session resume, queueing, and parsed Claude/Codex tool activity."
            ),
            "kind": "builtin",
            "passive": False,
            "requires": [],
            "form_fields": [
                {
                    "name": "workdir",
                    "label": "Workdir",
                    "required": False,
                    "placeholder": str(repo_root),
                },
                {
                    "name": "model",
                    "label": "Model",
                    "required": False,
                    "placeholder": "opus or gpt-5.4",
                },
            ],
            "examples": [
                {
                    "label": "Claude sentinel",
                    "runtime_type": "sentinel_cli",
                    "workdir": str(repo_root),
                    "note": "Uses Claude CLI by default and resumes the same session for agent-level continuity.",
                },
            ],
            "signals": {
                **_shared_signals(),
                "activity": "Gateway parses Claude/Codex JSON streams and emits working, thinking, and tool phases.",
                "tools": "Codex command events are recorded as tool calls; Claude tool-use blocks are surfaced as live tool activity.",
            },
        },
        "claude_code_channel": {
            "id": "claude_code_channel",
            "label": "Claude Code Channel",
            "description": "Attached Claude Code live channel. Gateway registers identity; ax-channel owns delivery.",
            "kind": "attached_session",
            "passive": False,
            "requires": [],
            "form_fields": [
                {
                    "name": "workdir",
                    "label": "Workdir",
                    "required": True,
                    "placeholder": str(repo_root),
                },
            ],
            "examples": [
                {
                    "label": "Claude Code channel",
                    "runtime_type": "claude_code_channel",
                    "workdir": str(repo_root),
                    "note": "Run ax channel setup after Gateway registration, then launch Claude Code with ax-channel.",
                },
            ],
            "signals": {
                **_shared_signals(),
                "activity": "ax-channel emits working on delivery and completed after the Claude Code reply tool runs.",
                "tools": "Tool telemetry comes from the attached Claude Code session and channel integration.",
            },
        },
        "inbox": {
            "id": "inbox",
            "label": "Passive Inbox",
            "description": "Passive Gateway-managed identity that receives and queues work without auto-replying.",
            "kind": "builtin",
            "passive": True,
            "requires": [],
            "form_fields": [],
            "examples": [],
            "signals": {
                **_shared_signals(),
                "activity": "Gateway reports queued state only. This runtime is passive by design.",
                "tools": "No tool-call telemetry. Inbox runtimes do not execute work.",
            },
        },
    }


def runtime_type_definition(runtime_type: str) -> dict[str, Any]:
    normalized = runtime_type.lower().strip()
    if normalized == "command":
        normalized = "exec"
    catalog = runtime_type_catalog()
    if normalized not in catalog:
        raise KeyError(runtime_type)
    return catalog[normalized]


def runtime_type_deprecated(runtime_type: str | None) -> bool:
    """Return True when ``runtime_type`` is marked deprecated in the catalog.

    Tolerates unknown / corrupt values: a missing or unrecognized
    ``runtime_type`` is treated as not deprecated rather than raising,
    so callers in display paths can call this unconditionally.
    """
    if not runtime_type:
        return False
    try:
        return bool(runtime_type_definition(runtime_type).get("deprecated"))
    except KeyError:
        return False


def runtime_type_successor(runtime_type: str | None) -> str | None:
    """Return the recommended successor runtime id for a deprecated runtime.

    Returns ``None`` when the runtime is not deprecated, has no recorded
    successor, or is unknown.
    """
    if not runtime_type:
        return None
    try:
        definition = runtime_type_definition(runtime_type)
    except KeyError:
        return None
    if not definition.get("deprecated"):
        return None
    successor = definition.get("successor_runtime_type")
    if isinstance(successor, str) and successor.strip():
        return successor.strip()
    return None


def runtime_type_list() -> list[dict[str, Any]]:
    catalog = runtime_type_catalog()
    ordered_ids = [
        "echo",
        "exec",
        "hermes_plugin",
        "sentinel_hermes_sdk",
        "sentinel_inference_sdk",
        "sentinel_cli",
        "claude_code_channel",
        "inbox",
    ]
    return [catalog[runtime_id] for runtime_id in ordered_ids if runtime_id in catalog]


def agent_template_catalog() -> dict[str, dict[str, Any]]:
    from .manifest_template_library import agent_template_catalog as _catalog

    return _catalog()


def agent_template_definition(template_id: str) -> dict[str, Any]:
    from .manifest_template_library import agent_template_definition as _definition

    return _definition(template_id)


def agent_template_list(*, include_advanced: bool = False) -> list[dict[str, Any]]:
    from .manifest_template_library import agent_template_list as _list

    return _list(include_advanced=include_advanced)
