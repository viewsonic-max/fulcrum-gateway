"""ax agents profiles — manage Claude Code settings.local.json via profile fragments."""

from __future__ import annotations

from typing import Optional

import typer

from ..agent_settings_profiles import (
    RegistryLookupError,
    agent_info_from_registry,
    apply,
    current_profile_list,
    diff,
    list_all,
    list_available,
)
from ..output import JSON_OPTION, console, err_console, print_json, print_table

profiles_app = typer.Typer(
    name="profiles",
    help="Apply runtime permission profiles to a gateway agent's workspace.",
    no_args_is_help=True,
)


def _registry_info_or_exit(agent_name: str) -> dict[str, str | None]:
    """Look up *agent_name* in the Gateway registry, exiting with the failure
    that actually happened: registry missing/unreadable (message from
    `RegistryLookupError`) vs. agent absent from a healthy registry (#298).
    """
    try:
        info = agent_info_from_registry(agent_name)
    except RegistryLookupError as exc:
        err_console.print(f"[red]Error:[/red] {exc}")
        raise typer.Exit(1)
    if info is None:
        err_console.print(f"[red]Error:[/red] Agent '{agent_name}' not found in the local Gateway registry.")
        raise typer.Exit(1)
    return info


def _resolve_workdir(agent_name: str, workdir_override: str | None) -> str:
    if workdir_override:
        return workdir_override
    info = _registry_info_or_exit(agent_name)
    workdir = info["workdir"]
    if not workdir:
        err_console.print(
            f"[red]Error:[/red] Agent '{agent_name}' has no workdir in the Gateway registry.\n"
            "  Try passing --workdir explicitly."
        )
        raise typer.Exit(1)
    return workdir


def _resolve_client(agent_name: str) -> str:
    """Derive the profile client for *agent_name* from the Gateway registry.

    The client is a fact about the agent (which tool/settings format it runs,
    determined by its `runtime_type`), not a parameter callers can choose —
    so unlike `--workdir`, there is no override flag for it.
    """
    info = _registry_info_or_exit(agent_name)
    client = info["client"]
    if client:
        return client

    runtime_type = info["runtime_type"]
    if runtime_type:
        err_console.print(
            f"[red]Error:[/red] Agent '{agent_name}' runtime '{runtime_type}' does not support profiles yet."
        )
    else:
        err_console.print(f"[red]Error:[/red] Agent '{agent_name}' has no runtime_type in the Gateway registry.")
    raise typer.Exit(1)


@profiles_app.command("list")
def profiles_list(
    client: Optional[str] = typer.Option(None, "--client", help="Filter to a specific client (e.g. claude_cli)"),
    as_json: bool = JSON_OPTION,
):
    """List available profiles, optionally filtered by client."""
    if client:
        by_client = {client: list_available(client)}
    else:
        by_client = list_all()

    if as_json:
        print_json(by_client)
        return

    rows = [{"client": c, "profile": p} for c, profiles in by_client.items() for p in profiles]
    if not rows:
        msg = f"No profiles found for client '{client}'." if client else "No profiles found."
        console.print(msg)
        return
    print_table(["Client", "Profile"], rows, keys=["client", "profile"])


@profiles_app.command("diff")
def profiles_diff(
    agent_name: str = typer.Argument(..., help="Agent name (as registered with Gateway)"),
    profile: Optional[list[str]] = typer.Option(None, "--profile", "-p", help="Profile(s) to diff against"),
    workdir: Optional[str] = typer.Option(None, "--workdir", help="Agent workdir (overrides Gateway registry lookup)"),
    reset: bool = typer.Option(False, "--reset", help="Preview replacing existing settings instead of merging"),
    as_json: bool = JSON_OPTION,
):
    """Show what applying profiles would change in the agent's settings.local.json.

    By default, mirrors `apply`'s merge semantics: ``remove`` is empty unless
    a profile fragment removes a profile-managed key. Use --reset to preview
    `apply --reset` instead, which replaces the file's current content.
    """
    if not profile:
        err_console.print("[red]Error:[/red] At least one --profile is required.")
        raise typer.Exit(1)

    resolved_workdir = _resolve_workdir(agent_name, workdir)
    resolved_client = _resolve_client(agent_name)
    try:
        result = diff(list(profile), resolved_client, resolved_workdir, reset=reset)
    except ValueError as exc:
        err_console.print(f"[red]Error:[/red] {exc}")
        raise typer.Exit(1)

    if as_json:
        print_json(result)
        return

    console.print(f"[bold]@{agent_name}[/bold] — profile diff ({resolved_client})")
    console.print(f"  profiles before: {result['profiles_before'] or '(none)'}")
    console.print(f"  profiles after:  {result['profiles_after']}")
    if result["add"]:
        for entry in result["add"]:
            console.print(f"  [green]+ {entry}[/green]")
    if result["remove"]:
        for entry in result["remove"]:
            console.print(f"  [red]- {entry}[/red]")
    if not result["add"] and not result["remove"]:
        console.print("  (no changes)")


@profiles_app.command("apply")
def profiles_apply(
    agent_name: str = typer.Argument(..., help="Agent name (as registered with Gateway)"),
    profile: Optional[list[str]] = typer.Option(None, "--profile", "-p", help="Profile(s) to apply"),
    workdir: Optional[str] = typer.Option(None, "--workdir", help="Agent workdir (overrides Gateway registry lookup)"),
    reset: bool = typer.Option(False, "--reset", help="Replace existing settings instead of merging"),
    as_json: bool = JSON_OPTION,
):
    """Apply profiles to an agent's settings.local.json.

    Merges profile permissions into the agent's workspace settings.  Use
    --reset to replace the file's current content rather than merging.
    """
    if not profile:
        err_console.print("[red]Error:[/red] At least one --profile is required.")
        raise typer.Exit(1)

    profiles_to_apply = list(profile)
    resolved_workdir = _resolve_workdir(agent_name, workdir)
    resolved_client = _resolve_client(agent_name)
    try:
        written = apply(profiles_to_apply, resolved_client, resolved_workdir, reset=reset)
    except ValueError as exc:
        err_console.print(f"[red]Error:[/red] {exc}")
        raise typer.Exit(1)

    if as_json:
        print_json(
            {
                "agent": agent_name,
                "profiles": profiles_to_apply,
                "client": resolved_client,
                "workdir": resolved_workdir,
                "settings_path": str(written),
                "reset": reset,
            }
        )
        return

    console.print(f"[green]Applied[/green] profiles {profiles_to_apply} to [bold]@{agent_name}[/bold]")
    console.print(f"  settings: {written}")


@profiles_app.command("show")
def profiles_show(
    agent_name: str = typer.Argument(..., help="Agent name (as registered with Gateway)"),
    as_json: bool = JSON_OPTION,
):
    """Show which profiles are currently applied to an agent.

    Resolves the agent's workdir and client from the local Gateway registry.
    """
    info = _registry_info_or_exit(agent_name)

    workdir = info["workdir"]
    runtime_type = info["runtime_type"]
    client = info["client"]

    if not workdir:
        err_console.print(f"[red]Error:[/red] Agent '{agent_name}' has no workdir in the Gateway registry.")
        raise typer.Exit(1)
    if not runtime_type:
        err_console.print(f"[red]Error:[/red] Agent '{agent_name}' has no runtime_type in the Gateway registry.")
        raise typer.Exit(1)
    if not client:
        err_console.print(
            f"[red]Error:[/red] Agent '{agent_name}' runtime '{runtime_type}' does not support profiles yet."
        )
        raise typer.Exit(1)

    applied = current_profile_list(workdir, client)

    if as_json:
        print_json({"agent": agent_name, "client": client, "profiles": applied})
        return

    if not applied:
        console.print(f"@{agent_name} ({client}): no profiles applied.")
    else:
        console.print(f"[bold]@{agent_name}[/bold] ({client}) applied profiles: {applied}")
