"""ax gateway — shared Typer apps, sub-app wiring, and display constants.

Base module imported by every gateway_* concern module; imports nothing from them.
"""

from __future__ import annotations

import logging
import subprocess

import typer

log = logging.getLogger("ax.gateway")

app = typer.Typer(name="gateway", help="Run the local Gateway control plane", no_args_is_help=True)
agents_app = typer.Typer(name="agents", help="Manage Gateway-controlled agents", no_args_is_help=True)
spaces_app = typer.Typer(name="spaces", help="Manage Gateway current space", no_args_is_help=True)
approvals_app = typer.Typer(name="approvals", help="Review and decide Gateway approval requests", no_args_is_help=True)
runtime_app = typer.Typer(
    name="runtime", help="Install and inspect runtime templates (Hermes, etc.)", no_args_is_help=True
)
runtime_auth_app = typer.Typer(name="auth", help="Manage runtime provider credentials", no_args_is_help=True)
local_app = typer.Typer(name="local", help="Connect local pass-through agents to Gateway", no_args_is_help=True)
connectors_app = typer.Typer(name="connectors", help="Manage outbound tool connectors", no_args_is_help=True)
connectors_auth_app = typer.Typer(name="auth", help="Manage connector credentials", no_args_is_help=True)
connectors_tools_app = typer.Typer(name="tools", help="Discover and search connector tools", no_args_is_help=True)
audit_app = typer.Typer(
    name="audit", help="Export the activity audit log in SIEM-compatible formats", no_args_is_help=True
)
_ATTACHED_SESSION_PROCESSES: list[subprocess.Popen[bytes]] = []
app.add_typer(agents_app, name="agents")
app.add_typer(spaces_app, name="spaces")
app.add_typer(approvals_app, name="approvals")
app.add_typer(runtime_app, name="runtime")
runtime_app.add_typer(runtime_auth_app, name="auth")
app.add_typer(local_app, name="local")
app.add_typer(connectors_app, name="connectors")
app.add_typer(audit_app, name="audit")
connectors_app.add_typer(connectors_auth_app, name="auth")
connectors_app.add_typer(connectors_tools_app, name="tools")

_STATE_STYLES = {
    "running": "green",
    "starting": "cyan",
    "reconnecting": "yellow",
    "stale": "yellow",
    "error": "red",
    "stopped": "dim",
}
# Tone contract (ADR-008): gray is reserved for operator-intent off states;
# desired=running but broken renders red — so BLOCKED and OFFLINE are red.
_PRESENCE_STYLES = {
    "IDLE": "green",
    "QUEUED": "cyan",
    "WORKING": "green",
    "BLOCKED": "red",
    "STALE": "yellow",
    "OFFLINE": "red",
    "ERROR": "red",
}
_CONFIDENCE_STYLES = {
    "HIGH": "green",
    "MEDIUM": "cyan",
    "LOW": "yellow",
    "BLOCKED": "red",
}
_PRESENCE_ORDER = {
    "ERROR": 0,
    "BLOCKED": 1,
    "WORKING": 2,
    "QUEUED": 3,
    "STALE": 4,
    "OFFLINE": 5,
    "IDLE": 6,
}

_UNSET = object()
