"""ax gateway — send-as-agent, inbox, passive-queue sync, and gateway test sends.

Extracted from ``ax_cli/commands/gateway.py`` (issue #28 Phase 1).
"""

from __future__ import annotations

import os
import time
from datetime import datetime, timezone

import typer

from .. import gateway as gateway_core
from ..client import AxClient
from ..commands import auth as auth_cmd
from ..gateway import (
    annotate_runtime_health,
    ensure_gateway_identity_binding,
    find_agent_entry,
    load_agent_pending_messages,
    load_gateway_registry,
    load_gateway_session,
    record_gateway_activity,
    save_agent_pending_messages,
    save_gateway_registry,
)
from ..gateway_runtime_types import (
    agent_template_definition,
)
from ..output import JSON_OPTION, console, err_console, print_json
from .gateway_app import agents_app


def _build_session_client_silent() -> AxClient | None:
    """Build a user-PAT session client without raising. Returns None when
    the gateway is not logged in or the session token is missing/invalid.

    Used for best-effort upstream calls during local cleanup paths where a
    missing session must not abort the command.
    """
    session = load_gateway_session()
    if not session:
        return None
    token = str(session.get("token") or "")
    if not token:
        return None
    try:
        return AxClient(
            base_url=str(session.get("base_url") or auth_cmd.DEFAULT_LOGIN_BASE_URL),
            token=token,
        )
    except Exception:  # noqa: BLE001
        return None


def _identity_space_send_guard(entry: dict, *, explicit_space_id: str | None = None) -> dict:
    registry = load_gateway_registry()
    stored = find_agent_entry(registry, str(entry.get("name") or "")) or entry
    ensure_gateway_identity_binding(registry, stored, session=load_gateway_session())
    snapshot = annotate_runtime_health(stored, registry=registry, explicit_space_id=explicit_space_id)
    save_gateway_registry(registry)
    if str(snapshot.get("confidence") or "").upper() == "BLOCKED":
        reason = str(snapshot.get("confidence_reason") or "blocked")
        detail = str(snapshot.get("confidence_detail") or "Gateway blocked this action.")
        raise ValueError(f"{detail} ({reason})")
    return snapshot


def _sync_passive_queue_after_manual_send(
    *,
    entry: dict,
    handled_message_id: str | None,
    reply_message_id: str | None,
    reply_preview: str | None,
) -> None:
    runtime_type = str(entry.get("runtime_type") or "").lower()
    if runtime_type not in {"inbox", "passive", "monitor"}:
        return

    pending_items = gateway_core.remove_agent_pending_message(str(entry.get("name") or ""), handled_message_id)
    registry = load_gateway_registry()
    stored = find_agent_entry(registry, str(entry.get("name") or "")) or entry
    backlog_depth = len(pending_items)
    last_pending = pending_items[-1] if pending_items else {}

    if handled_message_id:
        stored["processed_count"] = int(stored.get("processed_count") or 0) + 1
        stored["last_work_completed_at"] = datetime.now(timezone.utc).isoformat()

    stored["backlog_depth"] = backlog_depth
    stored["current_status"] = "queued" if backlog_depth > 0 else None
    stored["current_activity"] = (
        gateway_core._gateway_pickup_activity(runtime_type, backlog_depth)[:240] if backlog_depth > 0 else None
    )
    stored["last_reply_message_id"] = reply_message_id or stored.get("last_reply_message_id")
    stored["last_reply_preview"] = reply_preview or stored.get("last_reply_preview")
    if last_pending:
        stored["last_received_message_id"] = last_pending.get("message_id")
        stored["last_work_received_at"] = (
            last_pending.get("queued_at") or last_pending.get("created_at") or stored.get("last_work_received_at")
        )
    elif handled_message_id:
        stored["last_received_message_id"] = None
        stored["last_work_received_at"] = None

    save_gateway_registry(registry)
    if handled_message_id:
        record_gateway_activity(
            "manual_queue_acknowledged",
            entry=stored,
            message_id=handled_message_id,
            reply_message_id=reply_message_id,
            backlog_depth=backlog_depth,
        )


def _poll_managed_agent_inbox_after_send(
    *,
    name: str,
    space_id: str | None,
    limit: int,
    wait_seconds: int,
    channel: str = "main",
    poll_interval: float = 1.0,
) -> dict:
    """Bundle "what arrived while you were drafting" for a managed-agent send.

    Mirrors ``_poll_local_inbox_over_http``'s wait loop, but uses the
    in-process ``_inbox_for_managed_agent`` (Live Listener / managed-agent
    path) instead of the local-session HTTP proxy. Closes aX task
    ``663d9e6f``: every send-as-agent path should return inbound messages
    that arrived during the send so two agents don't talk past each other.

    ``mark_read=True`` so the same messages don't re-appear on the next
    poll. The wait loop exits as soon as we have messages or the deadline
    elapses.
    """
    deadline = time.monotonic() + max(0, int(wait_seconds))
    while True:
        result = _inbox_for_managed_agent(
            name=name,
            limit=max(1, int(limit)),
            channel=channel,
            space_id=space_id,
            unread_only=True,
            mark_read=True,
        )
        if result.get("messages") or wait_seconds <= 0 or time.monotonic() >= deadline:
            return result
        time.sleep(poll_interval)


def _send_from_managed_agent(
    *,
    name: str,
    content: str,
    to: str | None = None,
    parent_id: str | None = None,
    space_id: str | None = None,
    sent_via: str = "gateway_cli",
    metadata_extra: dict[str, object] | None = None,
    include_inbox: bool = True,
    inbox_wait: int = 2,
    inbox_limit: int = 10,
    inbox_channel: str = "main",
) -> dict:
    if not content.strip():
        raise ValueError("Message content is required.")
    entry = _load_managed_agent_or_exit(name)
    if str(entry.get("desired_state") or "").strip().lower() == "stopped":
        raise ValueError(f"@{name} is stopped. Start it before it can send.")
    snapshot = _identity_space_send_guard(entry, explicit_space_id=space_id)
    client = _load_managed_agent_client(entry)
    selected_space_id = str(space_id or snapshot.get("active_space_id") or entry.get("space_id") or "")
    if not selected_space_id:
        raise ValueError(f"Managed agent is missing a space id: @{name}")

    message_content = content.strip()
    mention = str(to or "").strip().lstrip("@")
    if mention:
        prefix = f"@{mention}"
        if not message_content.startswith(prefix):
            message_content = f"{prefix} {message_content}".strip()

    metadata = {
        "control_plane": "gateway",
        "gateway": {
            "managed": True,
            "agent_name": entry.get("name"),
            "agent_id": entry.get("agent_id"),
            "runtime_type": entry.get("runtime_type"),
            "transport": entry.get("transport", "gateway"),
            "credential_source": entry.get("credential_source", "gateway"),
            "sent_via": sent_via,
        },
    }
    if metadata_extra:
        gateway_meta = metadata["gateway"]
        if isinstance(gateway_meta, dict):
            gateway_meta.update(metadata_extra)
    result = client.send_message(
        selected_space_id,
        message_content,
        agent_id=str(entry.get("agent_id") or "") or None,
        parent_id=parent_id or None,
        metadata=metadata,
    )
    payload = result.get("message", result) if isinstance(result, dict) else result
    if isinstance(payload, dict):
        record_gateway_activity(
            "manual_message_sent",
            entry=entry,
            message_id=payload.get("id"),
            reply_preview=message_content[:120] or None,
        )
        _sync_passive_queue_after_manual_send(
            entry=entry,
            handled_message_id=parent_id,
            reply_message_id=str(payload.get("id") or "") or None,
            reply_preview=message_content[:120] or None,
        )
    response: dict = {"agent": entry.get("name"), "message": payload, "content": message_content}
    if include_inbox:
        try:
            response["inbox"] = _poll_managed_agent_inbox_after_send(
                name=str(entry.get("name") or name),
                space_id=selected_space_id,
                limit=inbox_limit,
                wait_seconds=inbox_wait,
                channel=inbox_channel,
            )
        except Exception as exc:
            # Inbox bundling is a best-effort enhancement on top of the send.
            # If it fails (transient API error, etc.) we still return the send
            # result the operator/agent actually depends on.
            response["inbox_error"] = str(exc)
    return response


def _inbox_for_managed_agent(
    *,
    name: str,
    limit: int = 20,
    channel: str = "main",
    space_id: str | None = None,
    unread_only: bool = False,
    mark_read: bool = False,
) -> dict:
    """Read a Gateway-managed agent's inbox using its Gateway-loaded credentials.

    Mirrors the read side of ``_send_from_managed_agent``. Works uniformly
    across Live Listener (claude_code_channel, hermes) and pass-through
    templates so the operator surface is the same regardless of how the
    agent is wired — that's the P1 the original task (``70f08787``) calls
    out: a Live Listener seat without a channel MCP attached has no way to
    peek its own inbox today.

    Defaults are deliberately peek-friendly (``unread_only=False``,
    ``mark_read=False``) because the typical caller is an operator
    inspecting on the agent's behalf, not the agent consuming work.
    """
    registry = load_gateway_registry()
    entry = find_agent_entry(registry, name)
    if not entry:
        raise LookupError(f"Managed agent not found: {name}")
    selected_space = str(space_id or entry.get("space_id") or "").strip() or None
    if not selected_space:
        raise ValueError(f"Managed agent is missing a space id: @{name}")
    client = _load_managed_agent_client(entry)
    # Capture the local pending queue first — it's the Gateway's view of
    # "messages addressed to this agent that haven't been picked up yet".
    # The drawer's "X unread messages" badge counts these. Use it to filter
    # the upstream listing when unread_only=True so the drawer's body matches
    # its own header (without this, upstream returns ALL messages and the
    # drawer says "3 unread" while showing 20).
    agent_name = str(entry.get("name") or name)
    pending_items_for_filter = load_agent_pending_messages(agent_name)
    pending_ids = {
        str(item.get("message_id") or item.get("id") or "").strip()
        for item in pending_items_for_filter
        if str(item.get("message_id") or item.get("id") or "").strip()
    }
    data = client.list_messages(
        limit=limit,
        channel=channel,
        space_id=selected_space,
        agent_id=str(entry.get("agent_id") or "") or None,
        unread_only=unread_only,
        mark_read=mark_read,
    )
    messages = data if isinstance(data, list) else data.get("messages", [])
    if unread_only:
        if pending_ids:
            messages = [
                msg for msg in messages if str(msg.get("id") or msg.get("message_id") or "").strip() in pending_ids
            ]
        else:
            messages = []
    # Mirror `_local_session_inbox`: when the operator explicitly marks read,
    # the local pending queue (which powers `backlog_depth` and the UI badge)
    # must also be cleared. Without this, the upstream returns
    # `marked_read_count=N` but the side app keeps showing N unread because
    # `backlog_depth` is read straight off the queue file.
    local_marked_read_count = 0
    if mark_read:
        local_marked_read_count = len(pending_items_for_filter)
        save_agent_pending_messages(agent_name, [])
        registry_after = load_gateway_registry()
        stored = find_agent_entry(registry_after, agent_name)
        if stored is not None:
            stored["backlog_depth"] = 0
            stored["queue_depth"] = 0
            stored["current_status"] = None
            stored["current_activity"] = None
            save_gateway_registry(registry_after)
    record_gateway_activity(
        "managed_inbox_polled",
        entry=entry,
        message_count=len(messages),
        mark_read=mark_read,
        space_id=selected_space,
        local_marked_read_count=local_marked_read_count,
    )
    return {
        "agent": entry.get("name"),
        "agent_id": entry.get("agent_id"),
        "space_id": selected_space,
        "messages": messages,
        # When unread_only=True, the count returned reflects the pending
        # queue intersection (what the drawer actually shows), not the
        # upstream's idea of unread. Operators see one consistent number.
        "unread_count": (
            len(messages) if unread_only else (data.get("unread_count") if isinstance(data, dict) else None)
        ),
        "marked_read_count": data.get("marked_read_count") if isinstance(data, dict) else None,
        "local_marked_read_count": local_marked_read_count if mark_read else None,
    }


def _gateway_test_sender_name(space_id: str) -> str:
    normalized = "".join(ch for ch in str(space_id or "") if ch.isalnum()).lower()
    suffix = normalized[:8] or "default"
    return f"switchboard-{suffix}"


def _space_cache_with(space_rows: object, space_id: str, *, name: str | None = None) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    seen: set[str] = set()
    if isinstance(space_rows, list):
        for item in space_rows:
            if isinstance(item, dict):
                item_space_id = str(item.get("space_id") or item.get("id") or "").strip()
                item_name = str(item.get("name") or item.get("space_name") or item_space_id)
                is_default = bool(item.get("is_default", False))
            else:
                item_space_id = str(item or "").strip()
                item_name = item_space_id
                is_default = False
            if not item_space_id or item_space_id in seen:
                continue
            seen.add(item_space_id)
            rows.append({"space_id": item_space_id, "name": item_name, "is_default": is_default})
    if space_id and space_id not in seen:
        rows.append({"space_id": space_id, "name": name or space_id, "is_default": not rows})
    return rows


def _ensure_gateway_test_sender(target_entry: dict) -> dict:
    """Auto-register or fetch the per-space switchboard service account.

    Service-account-only utility. Used by service-event flows (reminders, log
    fan-outs, system notifications) that legitimately need a Gateway-managed
    service identity. Must NOT be called from the default `agents test` path —
    principal-invoked surfaces author as the invoking principal, not as a
    service account. See `feedback_invoking_principal_default` (Madtank/
    supervisor, 2026-05-02) for the conceptual model.
    """
    target_space = str(target_entry.get("space_id") or "").strip()
    if not target_space:
        raise ValueError("Managed agent is missing a space id for Gateway test delivery.")
    sender_name = _gateway_test_sender_name(target_space)
    registry = load_gateway_registry()
    existing = find_agent_entry(registry, sender_name)
    if existing:
        return annotate_runtime_health(existing, registry=registry)
    return _register_managed_agent(
        name=sender_name,
        template_id="inbox",
        space_id=target_space,
        description="Gateway-managed passive sender for service-event sends.",
        start=True,
        source="switchboard:autocreate",
    )


def _resolve_invoking_principal() -> str | None:
    """Return the workspace's bound Gateway-managed agent name, if any.

    Resolves through `resolve_gateway_config()`, which reads the local
    `.ax/config.toml` for `[gateway]` + `[agent]` blocks. Returns None when
    the workspace has no Gateway-managed identity (no Gateway local config,
    or `[agent].agent_name` missing). This is the source of truth for the
    default sender on any principal-invoked send-message command.
    """
    from ..config import resolve_gateway_config

    cfg = resolve_gateway_config()
    if not cfg:
        return None
    name = str(cfg.get("agent_name") or "").strip()
    return name or None


def _no_invoking_principal_error() -> ValueError:
    # Avoid literal square brackets so Rich console.print() does not strip
    # them as markup tags when this message is echoed.
    return ValueError(
        "No invoking principal resolvable for this workspace. "
        "Run from a Gateway-managed workdir/local session (a directory whose "
        "`.ax/config.toml` declares the 'gateway' and 'agent' sections), or pass "
        "`--sender-agent <name>` to author the message as a specific service "
        "account for diagnostic service-event sends."
    )


def _recommended_test_message(entry: dict) -> str:
    template_id = str(entry.get("template_id") or "").strip()
    if template_id:
        try:
            template = agent_template_definition(template_id)
            message = str(template.get("recommended_test_message") or "").strip()
            if message:
                return message
        except KeyError:
            pass
    runtime_type = str(entry.get("runtime_type") or "").lower()
    if runtime_type == "echo":
        return "gateway test ping"
    if runtime_type == "inbox":
        return "Queue this test job, mark it received, and do not reply inline."
    return "Reply with exactly: Gateway test OK."


def _send_gateway_test_to_managed_agent(
    name: str,
    *,
    content: str | None = None,
    author: str = "agent",
    sender_agent: str | None = None,
) -> dict:
    """Send a Gateway-brokered test message to a managed agent.

    Default sender = invoking principal resolved from the workspace's local
    Gateway config (per Madtank/supervisor 2026-05-02: principal-invoked
    surfaces author as user/agent, never as a service account). Pass an
    explicit `sender_agent` to author as a named service account or other
    Gateway-managed identity. Fails hard when no invoking principal resolves
    AND no `sender_agent` override is provided — the alternative is silent
    misattribution, which is the bug this signature replaces.
    """
    entry = _load_managed_agent_or_exit(name)
    if str(entry.get("desired_state") or "").strip().lower() == "stopped":
        raise ValueError(f"@{name} is stopped. Start it before sending a test.")
    registry = load_gateway_registry()
    stored = find_agent_entry(registry, str(entry.get("name") or "")) or entry
    ensure_gateway_identity_binding(registry, stored, session=load_gateway_session())
    snapshot = annotate_runtime_health(stored, registry=registry)
    save_gateway_registry(registry)
    reachability = str(snapshot.get("reachability") or "").strip().lower()
    if reachability == "sse_disconnected":
        raise ValueError(
            f"@{name} is attached but the platform SSE subscription is down — "
            "messages will not be delivered. Reconnect the ax-channel MCP server. If that does not help, the agent token may need to be re-minted."
        )
    if reachability == "attach_required":
        workdir = str(snapshot.get("workdir") or stored.get("workdir") or "").strip()
        suffix = f" Start Claude Code from {workdir}." if workdir else " Start Claude Code first."
        raise ValueError(f"@{name} is stopped and cannot receive messages yet.{suffix}")
    space_id = str(snapshot.get("active_space_id") or stored.get("space_id") or entry.get("space_id") or "")
    if not space_id:
        raise ValueError(f"Managed agent is missing a space id: @{name}")

    prompt = (content or "").strip() or _recommended_test_message(entry)
    target = str(entry.get("name") or "").lstrip("@")
    normalized_author = str(author or "agent").strip().lower()
    if normalized_author not in {"agent", "user"}:
        raise ValueError("Gateway test author must be one of: agent, user.")

    sender_name = None
    if normalized_author == "agent":
        if sender_agent:
            sender_name = str(sender_agent).strip()
        else:
            sender_name = _resolve_invoking_principal()
            if not sender_name:
                if os.environ.get("AX_OFFLINE"):
                    normalized_author = "user"
                else:
                    raise _no_invoking_principal_error()

    if normalized_author == "agent":
        result = _send_from_managed_agent(
            name=sender_name,
            content=prompt,
            to=target,
            space_id=space_id,
            sent_via="gateway_test",
            metadata_extra={
                "managed_target": True,
                "target_agent_name": stored.get("name"),
                "target_agent_id": stored.get("agent_id"),
                "target_template": stored.get("template_id"),
                "target_runtime_type": stored.get("runtime_type"),
                "test_author": "agent",
                "test_sender_explicit": bool(sender_agent),
            },
        )
        payload = result.get("message", result) if isinstance(result, dict) else result
        message_content = str(result.get("content") or f"@{target} {prompt}".strip())
    else:
        client = _load_gateway_user_client()
        message_content = f"@{target} {prompt}".strip()
        metadata = {
            "control_plane": "gateway",
            "gateway": {
                "managed_target": True,
                "target_agent_name": stored.get("name"),
                "target_agent_id": stored.get("agent_id"),
                "target_template": stored.get("template_id"),
                "target_runtime_type": stored.get("runtime_type"),
                "sent_via": "gateway_test",
                "test_author": "user",
            },
        }
        result = client.send_message(space_id, message_content, metadata=metadata)
        payload = result.get("message", result) if isinstance(result, dict) else result

    if isinstance(payload, dict):
        record_gateway_activity(
            "gateway_test_sent",
            entry=entry,
            message_id=payload.get("id"),
            reply_preview=message_content[:120] or None,
            sender_agent_name=sender_name,
            test_author=normalized_author,
        )
    return {
        "target_agent": entry.get("name"),
        "sender_agent": sender_name,
        "author": normalized_author,
        "message": payload,
        "content": message_content,
        "recommended_prompt": prompt,
    }


@agents_app.command("send")
def send_as_agent(
    name: str = typer.Argument(..., help="Managed agent name to send as"),
    content: str = typer.Argument(..., help="Message content"),
    to: str = typer.Option(None, "--to", help="Prepend a mention like @codex automatically"),
    parent_id: str = typer.Option(None, "--parent-id", help="Reply inside an existing thread"),
    include_inbox: bool = typer.Option(
        True,
        "--inbox/--no-inbox",
        help="After sending, include unread messages addressed to this agent in the response. "
        "Default ON so two agents don't talk past each other when one replies while the other is mid-draft.",
    ),
    inbox_wait: int = typer.Option(
        2,
        "--inbox-wait",
        min=0,
        help="Seconds to wait for inbound messages after sending. 0 only checks immediately.",
    ),
    inbox_limit: int = typer.Option(
        10, "--inbox-limit", min=1, max=100, help="Max inbound messages to bundle in the response."
    ),
    as_json: bool = JSON_OPTION,
):
    """Send a message as a Gateway-managed agent."""
    try:
        result = _send_from_managed_agent(
            name=name,
            content=content,
            to=to,
            parent_id=parent_id,
            include_inbox=include_inbox,
            inbox_wait=inbox_wait,
            inbox_limit=inbox_limit,
        )
    except ValueError as exc:
        err_console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1)

    if as_json:
        print_json(result)
        return
    err_console.print(f"[green]Sent as managed agent:[/green] @{result['agent']}")
    if isinstance(result["message"], dict) and result["message"].get("id"):
        err_console.print(f"  id = {result['message']['id']}")
    err_console.print(f"  content = {result['content']}")
    inbox = result.get("inbox") if isinstance(result.get("inbox"), dict) else None
    if inbox:
        unread = inbox.get("unread_count") or 0
        if unread:
            err_console.print(
                f"[yellow]Inbox while drafting:[/yellow] {unread} unread message(s) addressed to @{result['agent']}"
            )
            for msg in (inbox.get("messages") or [])[:5]:
                if not isinstance(msg, dict):
                    continue
                sender = msg.get("agent_name") or msg.get("user_name") or msg.get("sender") or "unknown"
                preview = str(msg.get("content") or "").strip().splitlines()[0][:120] if msg.get("content") else ""
                err_console.print(f"  - @{sender}: {preview}")
    elif result.get("inbox_error"):
        err_console.print(f"[dim]Inbox poll failed: {result['inbox_error']}[/dim]")


@agents_app.command("inbox")
def inbox_for_agent(
    name: str = typer.Argument(..., help="Managed agent name"),
    limit: int = typer.Option(20, "--limit", min=1, max=200, help="Max messages to return"),
    channel: str = typer.Option("main", "--channel", help="Message channel"),
    space_id: str = typer.Option(
        None,
        "--space",
        "--space-id",
        "-s",
        help="Override the agent's home space. Accepts a slug, name, or UUID.",
    ),
    unread_only: bool = typer.Option(
        False,
        "--unread-only/--all",
        help="Filter to unread messages only (default: show recent regardless of read state)",
    ),
    mark_read: bool = typer.Option(
        False,
        "--mark-read/--no-mark-read",
        help=(
            "Mark returned messages as read. Defaults to peek (no-mark-read) so an "
            "operator inspecting on an agent's behalf does not silently consume work."
        ),
    ),
    as_json: bool = JSON_OPTION,
):
    """Read a Gateway-managed agent's recent inbox.

    Works for both Live Listeners (claude_code_channel, hermes) and pass-through
    agents — uses the agent's Gateway-loaded credentials, so no PAT is exposed
    to the caller. Pairs with `ax gateway agents send` for a uniform read/write
    surface from any operator seat without needing the channel MCP attached.

    The ``--space`` option accepts a slug, name, or UUID. Slugs and names
    resolve through the local space cache; the operator's user PAT is not
    required for this lookup.
    """
    if space_id:
        resolved = _resolve_space_via_cache(space_id)
        if resolved is None:
            err_console.print(
                f"[red]Could not resolve space '{space_id}' from the local space cache. "
                "Pass a UUID, or run `ax spaces list` once to populate the cache.[/red]"
            )
            raise typer.Exit(1)
        space_id = resolved
    try:
        result = _inbox_for_managed_agent(
            name=name,
            limit=limit,
            channel=channel,
            space_id=space_id,
            unread_only=unread_only,
            mark_read=mark_read,
        )
    except LookupError:
        err_console.print(f"[red]Managed agent not found:[/red] {name}")
        raise typer.Exit(1)
    except ValueError as exc:
        err_console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1)

    if as_json:
        print_json(result)
        return
    messages = result.get("messages") or []
    console.print(f"[bold]inbox[/bold] @{result.get('agent')}: {len(messages)} message(s)")
    unread = result.get("unread_count")
    if unread is not None:
        console.print(f"  [dim]unread_count = {unread}[/dim]")
    for message in messages:
        if not isinstance(message, dict):
            continue
        created = str(message.get("created_at") or "")
        author = str(message.get("display_name") or message.get("agent_name") or message.get("sender") or "-")
        content = str(message.get("content") or "").replace("\n", " ")
        console.print(f"  {created} {author}: {content[:160]}")


# Deferred cross-module imports (bottom-of-file to avoid import cycles; bound
# into module globals after defs, resolved at call time).
from .gateway_agents import (  # noqa: E402
    _load_managed_agent_client,
    _load_managed_agent_or_exit,
    _register_managed_agent,
)
from .gateway_auth import _load_gateway_user_client  # noqa: E402
from .gateway_spaces import _resolve_space_via_cache  # noqa: E402
