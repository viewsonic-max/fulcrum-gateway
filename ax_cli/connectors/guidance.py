"""Shared LLM-facing connector instructions for agent context renderers."""

from __future__ import annotations

from typing import Any


def connector_ref_for_agent(entry: dict[str, Any]) -> str | None:
    """Return the per-agent connector ref when configured on a registry entry."""
    ref = str(entry.get("connector_ref") or "").strip()
    return ref or None


def connector_instruction_lines(connector_name: str) -> list[str]:
    """Lines appended to agent context when a connector ref is configured.

    Used by both ``--system-prompt`` composition and ``AGENTS.md`` rendering so
    the sentinel and runtime see identical connector guidance.
    """
    name = str(connector_name or "").strip()
    if not name:
        return []
    return [
        "",
        f"CONNECTORS: {name}",
        "IMPORTANT — when using connectors, use ONLY these facts:",
        f"- You have connector tools. Always use connector={name!r} unless told otherwise.",
        "- connector_apps: shows currently connected apps",
        "- connector_search: finds action tools by use case",
        "- connector_call: executes an action",
        "- 500+ apps supported (Gmail, Slack, GitHub, Jira, etc.)",
        "- To connect a NEW app the user must run:",
        f"    ax gateway connectors connect {name} --app <app_name>",
        "  DO NOT guess other commands. This is the only correct command.",
    ]


def render_connector_block(connector_name: str) -> str:
    """Render connector instructions as a newline-delimited block."""
    lines = connector_instruction_lines(connector_name)
    if not lines:
        return ""
    return "\n".join(lines)
