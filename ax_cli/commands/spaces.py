"""ax spaces — list, create, and manage spaces."""

import logging
from typing import Optional

import httpx
import typer

from ..config import get_client, resolve_gateway_config, resolve_space_id, save_space_id
from ..output import JSON_OPTION, console, handle_error, print_json, print_kv, print_table, unwrap_envelope

log = logging.getLogger("ax.spaces")

app = typer.Typer(name="spaces", help="Space management", no_args_is_help=True)


def _space_items(result: object) -> list[dict]:
    if isinstance(result, list):
        return [item for item in result if isinstance(item, dict)]
    if not isinstance(result, dict):
        return []
    for key in ("spaces", "items", "results"):
        items = result.get(key)
        if isinstance(items, list):
            return [item for item in items if isinstance(item, dict)]
    return []


def _space_label(space: dict, fallback: str) -> str:
    return str(space.get("slug") or space.get("name") or space.get("space_name") or fallback)


def _find_space(client, space_id: str) -> dict | None:
    try:
        for space in _space_items(client.list_spaces()):
            sid = str(space.get("id") or space.get("space_id") or "")
            if sid == space_id:
                return space
    except Exception:
        return None
    return None


def _bound_agent_allows_space(client, space_id: str) -> tuple[bool | None, str | None]:
    try:
        me = client.whoami()
    except Exception:
        return None, None
    bound = me.get("bound_agent") if isinstance(me, dict) else None
    if not isinstance(bound, dict) or not bound:
        return None, None
    agent_name = str(bound.get("agent_name") or bound.get("name") or "bound agent")
    allowed_spaces = bound.get("allowed_spaces")
    if not isinstance(allowed_spaces, list):
        return None, agent_name
    allowed_ids = {
        str(item.get("space_id") or item.get("id") or "")
        for item in allowed_spaces
        if isinstance(item, dict) and str(item.get("space_id") or item.get("id") or "")
    }
    return space_id in allowed_ids, agent_name


@app.command("list")
def list_spaces(
    as_json: bool = JSON_OPTION,
):
    """List all spaces you belong to."""
    gateway_cfg = resolve_gateway_config()
    if gateway_cfg:
        from .messages import _gateway_local_call

        spaces = _gateway_local_call(gateway_cfg=gateway_cfg, method="list_spaces")
    else:
        client = get_client()
        try:
            spaces = client.list_spaces()
        except httpx.HTTPStatusError as e:
            handle_error(e)
    if not isinstance(spaces, list):
        spaces = spaces.get("spaces", spaces.get("items", []))
    if as_json:
        print_json(spaces)
    else:
        # Columns match the keys the API actually returns in list_spaces. The
        # previous `Visibility` column rendered blank because the response has
        # no `visibility` field (#50); `slug` is the disambiguator for
        # same-name spaces and pairs with the fail-closed ambiguity error
        # from #47/#48 (#49).
        print_table(
            ["ID", "Name", "Slug", "Members"],
            spaces,
            keys=["id", "name", "slug", "member_count"],
        )


@app.command("create")
def create(
    name: str = typer.Argument(..., help="Space name"),
    description: Optional[str] = typer.Option(None, "--description", "-d", help="Space description"),
    visibility: str = typer.Option("private", "--visibility", "-v", help="private, invite_only, or public"),
    as_json: bool = JSON_OPTION,
):
    """Create a new space."""
    client = get_client()
    try:
        result = client.create_space(name, description=description, visibility=visibility)
    except httpx.HTTPStatusError as e:
        handle_error(e)
    space = unwrap_envelope(result, "space")
    if as_json:
        print_json(space)
    else:
        console.print(
            f"[green]Created:[/green] {space.get('name')} (id={str(space.get('id', ''))[:8]}…, visibility={space.get('visibility')})"
        )


@app.command("use")
def use_space(
    space: str = typer.Argument(..., help="Space id, slug, or name to make current"),
    global_config: bool = typer.Option(
        False, "--global", help="Save to global config instead of local .ax/config.toml"
    ),
    as_json: bool = JSON_OPTION,
):
    """Set the current CLI space by id, slug, or name."""
    client = get_client()
    sid = resolve_space_id(client, explicit=space)
    space_row = _find_space(client, sid) or {}
    label = _space_label(space_row, sid)
    save_space_id(sid, local=not global_config)
    # Keep the Gateway bootstrap session pointed at the same space so the two
    # stores can't silently diverge (issue #82). Best-effort: a Gateway problem
    # must never break the primary CLI-config write above. The session is a
    # single global file, so we sync it regardless of --global.
    gw_sync = None
    try:
        from ..gateway import apply_space_to_gateway_session

        gw_sync = apply_space_to_gateway_session(sid, space_name=space_row.get("name"))
    except Exception:
        # Fail-soft: a Gateway-side problem must never break the CLI-config write
        # above. Log at debug (visible under -v) so a swallowed programming error
        # from a future refactor is still traceable by maintainers (issue #160).
        log.debug("gateway session sync failed during `ax spaces use`", exc_info=True)
        gw_sync = None
    allowed, agent_name = _bound_agent_allows_space(client, sid)
    result = {
        "space_id": sid,
        "space_label": label,
        "scope": "global" if global_config else "local",
        "bound_agent": agent_name,
        "bound_agent_allowed": allowed,
        "gateway_session": gw_sync,
    }
    if as_json:
        print_json(result)
        return
    console.print(f"[green]Current space:[/green] {label}")
    console.print(f"[dim]Saved to {'global config' if global_config else 'local .ax/config.toml'}.[/dim]")
    if gw_sync and gw_sync.get("updated"):
        console.print(f"[dim]Gateway session also set to {gw_sync.get('space_name') or label}.[/dim]")
        if gw_sync.get("daemon_running"):
            console.print(
                "[yellow]Warning:[/yellow] Gateway daemon is running — restart it "
                "(`ax gateway stop && ax gateway start`) to apply the new space."
            )
    if allowed is False and agent_name:
        console.print(
            f"[yellow]Warning:[/yellow] @{agent_name} is not attached to this space; agent-authored writes may be rejected."
        )


@app.command("get")
def get_space(
    space_id: str = typer.Argument(..., help="Space ID"),
    as_json: bool = JSON_OPTION,
):
    """Get space details."""
    client = get_client()
    try:
        data = client.get_space(space_id)
    except httpx.HTTPStatusError as e:
        handle_error(e)
    space = unwrap_envelope(data, "space")
    if as_json:
        print_json(space)
    else:
        print_kv(space) if isinstance(space, dict) else print_kv(data)


@app.command("members")
def members(
    space_id: Optional[str] = typer.Argument(None, help="Space ID (default: current space)"),
    as_json: bool = JSON_OPTION,
):
    """List members of a space."""
    client = get_client()
    sid = space_id or resolve_space_id(client)
    try:
        data = client.list_space_members(sid)
    except httpx.HTTPStatusError as e:
        handle_error(e)
    members_list = data if isinstance(data, list) else data.get("members", [])
    if as_json:
        print_json(members_list)
    else:
        # API returns each member as {id, display_name, type, role, status, ...}.
        # The previous keys=["username", ...] rendered blank because no
        # `username` field exists in the response (#55). `type` is surfaced
        # so operators can distinguish human members from agent members.
        print_table(
            ["Member", "Type", "Role"],
            members_list,
            keys=["display_name", "type", "role"],
        )
