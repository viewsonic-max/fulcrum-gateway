"""ax gateway — status/doctor/dashboard payloads and the `approvals` sub-app.

Extracted from ``ax_cli/commands/gateway.py`` (issue #28 Phase 1).
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone

import typer

from ..gateway import (
    AX_PLUGIN_NAME,
    _hermes_plugin_home,
    _is_hermes_plugin_runtime,
    _is_supervised_subprocess_runtime,
    _is_system_agent,
    _plugin_source_dir,
    agent_dir,
    annotate_runtime_health,
    approve_gateway_approval,
    archive_stale_gateway_approvals,
    daemon_status,
    deny_gateway_approval,
    ensure_gateway_identity_binding,
    find_agent_entry,
    gateway_dir,
    gateway_environment,
    get_gateway_approval,
    hermes_setup_status,
    list_gateway_approvals,
    load_gateway_registry,
    load_gateway_session,
    load_recent_gateway_activity,
    ollama_setup_status,
    record_gateway_activity,
    resolve_agent_token_file,
    save_gateway_registry,
    ui_status,
)
from ..output import JSON_OPTION, err_console, print_json, print_table
from .gateway_app import app, approvals_app


def _status_payload(*, activity_limit: int = 10, include_hidden: bool = False) -> dict:
    daemon = daemon_status()
    ui = ui_status()
    session = load_gateway_session()
    registry = daemon["registry"]
    all_agents = [
        _with_registry_refs(registry, annotate_runtime_health(agent, registry=registry))
        for agent in registry.get("agents", [])
    ]
    # Partition out archived + hidden + system agents so default surfaces
    # stay tidy. System agents (switchboards, service accounts) are
    # infrastructure plumbing; hidden agents are stale ones the daemon swept
    # away; archived agents are user-disabled entries that are sticky.
    archived_agents_list = [a for a in all_agents if str(a.get("lifecycle_phase") or "active") == "archived"]
    hidden_agents_list = [a for a in all_agents if str(a.get("lifecycle_phase") or "active") == "hidden"]
    system_agents_list = [a for a in all_agents if _is_system_agent(a)]
    visible_agents = [
        a
        for a in all_agents
        if a not in archived_agents_list and a not in hidden_agents_list and a not in system_agents_list
    ]
    agents = all_agents if include_hidden else visible_agents
    approvals = list_gateway_approvals()
    pending_approvals = [item for item in approvals if str(item.get("status") or "") == "pending"]
    live_agents = [a for a in agents if str(a.get("mode") or "") == "LIVE"]
    on_demand_agents = [a for a in agents if str(a.get("mode") or "") == "ON-DEMAND"]
    inbox_agents = [a for a in agents if str(a.get("mode") or "") == "INBOX"]
    connected_agents = [a for a in agents if bool(a.get("connected"))]
    stale_agents = [a for a in agents if str(a.get("presence") or "") == "STALE"]
    offline_agents = [a for a in agents if str(a.get("presence") or "") == "OFFLINE"]
    errored_agents = [a for a in agents if str(a.get("presence") or "") == "ERROR"]
    low_confidence_agents = [a for a in agents if str(a.get("confidence") or "") in {"LOW", "BLOCKED"}]
    blocked_agents = [a for a in agents if str(a.get("confidence") or "") == "BLOCKED"]
    gateway = dict(registry.get("gateway", {}))
    if not daemon["running"]:
        gateway["effective_state"] = "stopped"
        gateway["pid"] = None
    # Active space fallback. The gateway session sometimes ships without a
    # space_id (older sessions, sessions minted before we resolved the user's
    # default workspace). Without this, the operator overview shows Space=—
    # even though every managed agent has a space_id. Pick the most-used space
    # across agents as the implicit active space for display.
    fallback_space_id: str | None = None
    if not (session and session.get("space_id")):
        space_counts: dict[str, int] = {}
        for agent in agents:
            sid = str(agent.get("space_id") or "").strip()
            if not sid:
                continue
            space_counts[sid] = space_counts.get(sid, 0) + 1
        if space_counts:
            fallback_space_id = max(space_counts.items(), key=lambda item: item[1])[0]

    from ..connectors.paths import connectors_registry_path
    from ..connectors.storage import list_connectors as _list_connectors

    _connectors_count = 0
    _enabled_connectors = 0
    _connectors_error: str | None = None
    try:
        _all_connectors = _list_connectors()
        _connectors_count = len(_all_connectors)
        _enabled_connectors = sum(1 for c in _all_connectors if c.enabled)
    except (json.JSONDecodeError, OSError) as exc:
        _connectors_error = str(exc)

    payload = {
        "gateway_dir": str(gateway_dir()),
        "connectors_registry_path": str(connectors_registry_path()),
        "connectors_count": _connectors_count,
        "enabled_connectors": _enabled_connectors,
        "connectors_error": _connectors_error,
        "gateway_environment": gateway_environment(),
        "connected": bool(session),
        "base_url": session.get("base_url") if session else None,
        "space_id": (session.get("space_id") if session else None) or fallback_space_id,
        "space_name": session.get("space_name") if session else None,
        "user": session.get("username") if session else None,
        "offline_mode": _is_offline_mode_active(),
        "daemon": {
            "running": daemon["running"],
            "pid": daemon["pid"],
        },
        "ui": {
            "running": ui["running"],
            "pid": ui["pid"],
            "host": ui["host"],
            "port": ui["port"],
            "url": ui["url"],
            "log_path": ui["log_path"],
        },
        "gateway": gateway,
        "agents": agents,
        "approvals": approvals,
        "recent_activity": load_recent_gateway_activity(limit=activity_limit),
        "summary": {
            "managed_agents": len(agents),
            "live_agents": len(live_agents),
            "on_demand_agents": len(on_demand_agents),
            "inbox_agents": len(inbox_agents),
            "connected_agents": len(connected_agents),
            "stale_agents": len(stale_agents),
            "offline_agents": len(offline_agents),
            "errored_agents": len(errored_agents),
            "low_confidence_agents": len(low_confidence_agents),
            "blocked_agents": len(blocked_agents),
            "hidden_agents": len(hidden_agents_list),
            "system_agents": len(system_agents_list),
            "archived_agents": len(archived_agents_list),
            "pending_approvals": len(pending_approvals),
        },
    }
    alerts = _gateway_alerts(payload)
    payload["alerts"] = alerts
    payload["summary"]["alert_count"] = len(alerts)
    return payload


def _gateway_alerts(payload: dict, *, limit: int = 6) -> list[dict]:
    alerts: list[dict] = []
    seen: set[tuple[str, str, str]] = set()

    def push(severity: str, title: str, detail: str, *, agent_name: str | None = None) -> None:
        key = (severity, title, agent_name or "")
        if key in seen:
            return
        seen.add(key)
        alerts.append(
            {
                "severity": severity,
                "title": title,
                "detail": detail,
                "agent_name": agent_name,
            }
        )

    if not payload.get("connected"):
        push("error", "Gateway is not logged in", "Run `ax gateway login` to bootstrap the local control plane.")
    elif not payload.get("daemon", {}).get("running"):
        push(
            "error",
            "Gateway daemon is stopped",
            "Start it with `uv run ax gateway start` or relaunch the local service.",
        )

    if not payload.get("ui", {}).get("running"):
        push(
            "warning", "Gateway UI is stopped", "Start it with `uv run ax gateway start` to launch the local dashboard."
        )

    for agent in payload.get("agents", []):
        name = str(agent.get("name") or "")
        presence = str(agent.get("presence") or "").upper()
        approval_state = str(agent.get("approval_state") or "").lower()
        attestation_state = str(agent.get("attestation_state") or "").lower()
        preview = str(agent.get("last_reply_preview") or "")
        lowered_preview = preview.lower()
        setup_error_preview = (
            preview.startswith("(stderr:")
            or " repo not found" in lowered_preview
            or lowered_preview.startswith("ollama bridge failed:")
        )
        if approval_state == "pending":
            detail = str(agent.get("confidence_detail") or "Gateway needs approval before this runtime can be trusted.")
            push("warning", f"@{name} needs Gateway approval", detail, agent_name=name)
        elif approval_state == "rejected" or attestation_state == "blocked":
            detail = str(agent.get("confidence_detail") or "Gateway blocked this runtime.")
            push("error", f"@{name} is blocked by Gateway", detail, agent_name=name)
        elif attestation_state == "drifted":
            detail = str(agent.get("confidence_detail") or "Runtime changed since approval and needs review.")
            push("warning", f"@{name} changed since approval", detail, agent_name=name)
        elif presence == "BLOCKED":
            detail = str(
                agent.get("confidence_detail")
                or "Gateway blocked this runtime until identity, space, or approval state is fixed."
            )
            push("error", f"@{name} is blocked", detail, agent_name=name)
        elif presence == "ERROR":
            if setup_error_preview:
                push("error", f"@{name} has a runtime setup error", preview[:180], agent_name=name)
            else:
                detail = str(agent.get("confidence_detail") or agent.get("last_error") or "Runtime reported an error.")
                push("error", f"@{name} hit an error", detail, agent_name=name)
        elif presence == "STALE":
            detail = f"No heartbeat for {_format_age(agent.get('last_seen_age_seconds'))}."
            push("warning", f"@{name} looks stale", detail, agent_name=name)
        elif presence == "OFFLINE" and str(agent.get("mode") or "") == "LIVE":
            detail = str(
                agent.get("confidence_detail")
                or "Expected a live runtime, but Gateway does not currently have a working path."
            )
            push("error", f"@{name} is offline", detail, agent_name=name)
        if setup_error_preview and presence != "ERROR":
            push("error", f"@{name} has a runtime setup error", preview[:180], agent_name=name)
        if int(agent.get("backlog_depth") or 0) > 0 and presence in {"OFFLINE", "ERROR", "STALE"}:
            detail = f"{agent.get('backlog_depth')} queued item(s) may be stuck until the agent is healthy."
            push("warning", f"@{name} has queued work", detail, agent_name=name)

    for item in reversed(payload.get("recent_activity", [])):
        event = str(item.get("event") or "")
        if event == "gateway_start_blocked":
            existing = item.get("existing_pid") or item.get("existing_pids")
            push("warning", "Another Gateway instance is already running", f"Existing process: {existing}.")
        elif event in {"listener_error", "listener_timeout"}:
            agent_name = str(item.get("agent_name") or "")
            detail = str(item.get("error") or "Listener lost contact and is reconnecting.")
            push("warning", f"@{agent_name} had a listener interruption", detail, agent_name=agent_name or None)
        if len(alerts) >= limit:
            break

    return alerts[:limit]


def _agent_detail_payload(name: str, *, activity_limit: int = 12) -> dict | None:
    payload = _status_payload(activity_limit=activity_limit)
    entry = next((agent for agent in payload["agents"] if str(agent.get("name") or "").lower() == name.lower()), None)
    if not entry:
        return None
    activity = load_recent_gateway_activity(limit=activity_limit, agent_name=name)
    return {
        "gateway": {
            "connected": payload["connected"],
            "base_url": payload["base_url"],
            "space_id": payload["space_id"],
            "daemon": payload["daemon"],
        },
        "agent": entry,
        "recent_activity": activity,
    }


def _approval_rows_payload(*, status: str | None = None, include_archived: bool = False) -> dict:
    approvals = list_gateway_approvals(status=status, include_archived=include_archived)
    return {
        "approvals": approvals,
        "count": len(approvals),
        "pending": len([item for item in approvals if str(item.get("status") or "") == "pending"]),
    }


def _approval_detail_payload(approval_id: str) -> dict:
    approval = get_gateway_approval(approval_id)
    return {"approval": approval}


def _doctor_result_status(checks: list[dict]) -> str:
    statuses = {str(item.get("status") or "").strip().lower() for item in checks}
    if "failed" in statuses:
        return "failed"
    if "warning" in statuses:
        return "warning"
    return "passed"


def _doctor_summary(checks: list[dict], status: str) -> str:
    failures = [
        str(item.get("detail") or item.get("name") or "").strip()
        for item in checks
        if str(item.get("status") or "").strip().lower() == "failed"
    ]
    warnings = [
        str(item.get("detail") or item.get("name") or "").strip()
        for item in checks
        if str(item.get("status") or "").strip().lower() == "warning"
    ]
    if status == "failed" and failures:
        return failures[0]
    if status == "warning" and warnings:
        return warnings[0]
    return "Gateway path looks healthy."


def _store_doctor_result(name: str, result: dict[str, object]) -> dict:
    registry = load_gateway_registry()
    entry = find_agent_entry(registry, name)
    if not entry:
        raise LookupError(f"Managed agent not found: {name}")
    completed_at = str(result.get("completed_at") or datetime.now(timezone.utc).isoformat())
    entry["last_doctor_result"] = result
    entry["last_doctor_at"] = completed_at
    if str(result.get("status") or "").lower() != "failed":
        entry["last_successful_doctor_at"] = completed_at
    save_gateway_registry(registry)
    record_gateway_activity(
        "doctor_completed",
        entry=entry,
        activity_message=str(result.get("summary") or ""),
        error=None if str(result.get("status") or "").lower() != "failed" else str(result.get("summary") or ""),
    )
    return annotate_runtime_health(entry, registry=registry)


def _run_gateway_doctor(name: str, *, send_test: bool = False) -> dict:
    registry = load_gateway_registry()
    entry = find_agent_entry(registry, name)
    if not entry:
        raise LookupError(f"Managed agent not found: {name}")
    ensure_gateway_identity_binding(registry, entry, session=load_gateway_session(), verify_spaces=False)
    snapshot = annotate_runtime_health(entry, registry=registry)
    checks: list[dict[str, str]] = []
    asset_class = str(snapshot.get("asset_class") or "")
    intake_model = str(snapshot.get("intake_model") or "")
    return_paths = [str(item) for item in (snapshot.get("return_paths") or []) if str(item)]

    def add_check(check_name: str, status: str, detail: str) -> None:
        checks.append({"name": check_name, "status": status, "detail": detail})

    def has_check(check_name: str) -> bool:
        return any(str(item.get("name") or "") == check_name for item in checks)

    session = load_gateway_session()
    add_check(
        "gateway_auth",
        "passed" if session else "failed",
        "Gateway bootstrap session is present." if session else "Gateway is not logged in.",
    )

    identity_status = str(snapshot.get("identity_status") or "").lower()
    if identity_status == "verified":
        add_check(
            "identity_binding",
            "passed",
            f"Gateway is acting as {snapshot.get('acting_agent_name') or entry.get('name')}.",
        )
    elif identity_status == "bootstrap_only":
        add_check(
            "identity_binding",
            "failed",
            "Gateway would need to use a bootstrap credential for an agent-authored action.",
        )
    else:
        add_check(
            "identity_binding",
            "failed",
            str(snapshot.get("confidence_detail") or "Gateway does not have a valid acting identity binding."),
        )

    environment_status = str(snapshot.get("environment_status") or "").lower()
    if environment_status == "environment_allowed":
        add_check(
            "environment_binding",
            "passed",
            f"Requested environment matches {snapshot.get('environment_label') or snapshot.get('base_url') or entry.get('base_url')}.",
        )
    elif environment_status == "environment_mismatch":
        add_check(
            "environment_binding",
            "failed",
            str(snapshot.get("confidence_detail") or "Requested environment does not match the bound environment."),
        )
    else:
        add_check("environment_binding", "warning", "Gateway could not fully verify the bound environment.")

    allowed_spaces = snapshot.get("allowed_spaces") if isinstance(snapshot.get("allowed_spaces"), list) else []
    if allowed_spaces:
        add_check("allowed_spaces", "passed", f"Gateway resolved {len(allowed_spaces)} allowed space(s).")
    else:
        add_check("allowed_spaces", "warning", "Gateway does not have a cached allowed-space list yet.")

    space_status = str(snapshot.get("space_status") or "").lower()
    if space_status == "active_allowed":
        add_check(
            "space_binding",
            "passed",
            f"Active space is {snapshot.get('active_space_name') or snapshot.get('active_space_id')}.",
        )
    elif space_status == "no_active_space":
        add_check("space_binding", "failed", "Gateway does not have an active space selected for this asset.")
    elif space_status == "active_not_allowed":
        add_check(
            "space_binding",
            "failed",
            str(snapshot.get("confidence_detail") or "Active space is not allowed for this identity."),
        )
    else:
        add_check("space_binding", "warning", "Gateway could not fully verify the active space.")

    attestation_state = str(snapshot.get("attestation_state") or "").lower()
    approval_state = str(snapshot.get("approval_state") or "").lower()
    if approval_state == "pending":
        add_check(
            "binding_approval",
            "warning",
            str(snapshot.get("confidence_detail") or "Gateway needs approval before trusting this runtime binding."),
        )
    elif approval_state == "rejected" or attestation_state == "blocked":
        add_check(
            "binding_approval",
            "failed",
            str(snapshot.get("confidence_detail") or "Gateway blocked this runtime binding."),
        )
    elif attestation_state == "drifted":
        add_check(
            "binding_attestation",
            "failed",
            str(snapshot.get("confidence_detail") or "Runtime binding drifted from its approved launch spec."),
        )
    elif attestation_state == "verified":
        add_check("binding_attestation", "passed", "Runtime matches the approved local binding.")

    token_file = resolve_agent_token_file(entry)
    if token_file.exists() and token_file.read_text().strip():
        add_check("agent_token", "passed", "Managed agent token file is present.")
    else:
        add_check("agent_token", "failed", f"Managed agent token is missing or empty at {token_file}.")

    if asset_class == "background_worker" or intake_model == "queue_accept":
        probe = agent_dir(name) / ".doctor-queue-check"
        try:
            probe.write_text("ok\n")
            probe.unlink(missing_ok=True)
            add_check("queue_writable", "passed", "Gateway queue is writable.")
        except OSError as exc:
            add_check("queue_writable", "failed", f"Gateway queue is not writable: {exc}")
        if bool(snapshot.get("connected")):
            add_check("worker_attached", "passed", "A queue worker is attached.")
        else:
            add_check("worker_attached", "warning", "Queue writable; no worker currently attached.")
        if "summary_post" in return_paths:
            add_check("summary_path", "passed", "Gateway is configured to post a summary after queued work completes.")
    else:
        exec_command = str(entry.get("exec_command") or "").strip()
        runtime_type = str(entry.get("runtime_type") or "").strip().lower()
        if intake_model == "live_listener":
            if snapshot.get("activation") == "attach_only":
                reachability_val = str(snapshot.get("reachability") or "")
                if reachability_val == "sse_disconnected":
                    add_check(
                        "claude_code_session",
                        "passed",
                        "Claude Code is attached to Gateway.",
                    )
                    add_check(
                        "channel_sse",
                        "failed",
                        "Claude Code is attached but the platform SSE subscription is down — "
                        "messages will not be delivered. Reconnect the ax-channel MCP server. If that does not help, the agent token may need to be re-minted.",
                    )
                elif reachability_val == "attach_required":
                    add_check("claude_code_session", "warning", "Start Claude Code before sending.")
                elif bool(snapshot.get("connected")):
                    add_check("claude_code_session", "passed", "Claude Code is connected to Gateway.")
                    add_check("channel_sse", "passed", "Platform SSE subscription is active.")
                else:
                    add_check("claude_code_session", "failed", "Gateway does not currently have Claude Code running.")
            elif _is_supervised_subprocess_runtime(runtime_type):
                # hermes_plugin and the sentinel SDK runtimes carry no
                # exec_command: Gateway builds and supervises the launch command
                # implicitly (see _is_supervised_subprocess_runtime). Treating a
                # missing exec_command as a failure here is a false negative —
                # issue #359.
                if _is_hermes_plugin_runtime(runtime_type):
                    launch_detail = "Gateway supervises `hermes gateway run` for this runtime."
                else:
                    launch_detail = "Gateway supervises this runtime directly; no exec command is required."
                add_check("runtime_launch", "passed", launch_detail)
            elif runtime_type != "echo":
                if exec_command:
                    add_check("runtime_launch", "passed", "Gateway has a launch command for this runtime.")
                else:
                    add_check("runtime_launch", "failed", "Gateway does not have a launch command for this runtime.")
        elif intake_model == "launch_on_send":
            if runtime_type == "echo" or exec_command:
                add_check("launch_ready", "passed", "Gateway can launch this runtime when work arrives.")
            else:
                add_check(
                    "launch_ready", "failed", "Gateway does not have a launch command for this on-demand runtime."
                )
        elif intake_model == "scheduled_run":
            add_check(
                "schedule_ready",
                "warning",
                "Scheduled asset support is taxonomy-defined but not fully implemented in Gateway yet.",
            )
        elif intake_model == "event_triggered":
            add_check(
                "event_source",
                "warning",
                "Alert-driven asset support is taxonomy-defined but not fully implemented in Gateway yet.",
            )
        elif asset_class == "service_proxy":
            if exec_command:
                add_check("runtime_launch", "passed", "Gateway has a launch command for this runtime.")
            else:
                add_check("runtime_launch", "failed", "Gateway does not have a launch command for this runtime.")

    template_id = str(entry.get("template_id") or "").strip().lower()
    if template_id == "hermes":
        hermes_status = hermes_setup_status(entry)
        if hermes_status.get("ready", True):
            add_check("hermes_repo", "passed", str(hermes_status.get("summary") or "Hermes checkout found."))
        else:
            add_check("hermes_repo", "failed", str(hermes_status.get("summary") or "Hermes checkout not found."))
    elif template_id == "ollama":
        ollama_model = str(entry.get("model") or "").strip()
        ollama_status = ollama_setup_status(preferred_model=ollama_model or None)
        if bool(ollama_status.get("server_reachable")):
            add_check("ollama_server", "passed", str(ollama_status.get("summary") or "Ollama server is reachable."))
        else:
            add_check("ollama_server", "failed", str(ollama_status.get("summary") or "Ollama server is not reachable."))
        if ollama_model:
            if bool(ollama_status.get("preferred_model_available")):
                add_check("ollama_model", "passed", f"Gateway will launch Ollama with model {ollama_model}.")
            else:
                add_check("ollama_model", "failed", f"Configured Ollama model is not installed: {ollama_model}.")
        else:
            recommended_model = str(ollama_status.get("recommended_model") or "").strip()
            if recommended_model:
                add_check(
                    "ollama_model", "passed", f"Gateway will use the recommended local model {recommended_model}."
                )
            else:
                add_check("ollama_model", "warning", "No Ollama model is selected yet.")
        add_check("launch_path", "passed", "Gateway can launch the Ollama bridge on send.")

    runtime_type = str(entry.get("runtime_type") or "").strip().lower()
    if runtime_type == "hermes_plugin":
        # Two distinct failure modes silently break the Hermes plugin path,
        # and each presents identically (agent shows running, no replies).
        # Surface them as separate checks so the operator can tell which
        # broke without source-diving.
        try:
            hermes_home = _hermes_plugin_home(entry)
            plugin_link = hermes_home / "plugins" / "ax"
            plugin_source = _plugin_source_dir()
            if plugin_link.is_symlink() and plugin_link.resolve() == plugin_source.resolve():
                add_check(
                    "ax_platform_symlink",
                    "passed",
                    f"{plugin_link} → {plugin_source} (Hermes can load the aX adapter).",
                )
            elif plugin_link.exists():
                add_check(
                    "ax_platform_symlink",
                    "warning",
                    f"{plugin_link} exists but does not resolve to {plugin_source}. "
                    f"Delete it; Gateway will re-link on the next start.",
                )
            else:
                add_check(
                    "ax_platform_symlink",
                    "failed",
                    f"{plugin_link} is missing. Run `ax gateway agents start {entry.get('name') or '<name>'}` "
                    f"to trigger the scaffold.",
                )
        except Exception as exc:
            add_check("ax_platform_symlink", "warning", f"Could not inspect plugin symlink: {exc}")

        try:
            hermes_home = _hermes_plugin_home(entry)
            cfg_path = hermes_home / "config.yaml"
            if cfg_path.exists():
                import yaml as _yaml  # local — gateway import cost

                try:
                    loaded = _yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
                except Exception as exc:
                    loaded = None
                    parse_error = exc
                else:
                    parse_error = None
                if not isinstance(loaded, dict):
                    if parse_error is not None:
                        add_check(
                            "ax_platform_enabled",
                            "failed",
                            f"{cfg_path} did not parse as YAML: {parse_error}",
                        )
                    else:
                        add_check(
                            "ax_platform_enabled",
                            "failed",
                            f"{cfg_path} is not a YAML mapping.",
                        )
                else:
                    plugins_cfg = loaded.get("plugins")
                    enabled_list = plugins_cfg.get("enabled") if isinstance(plugins_cfg, dict) else None
                    if isinstance(enabled_list, list) and AX_PLUGIN_NAME in enabled_list:
                        add_check(
                            "ax_platform_enabled",
                            "passed",
                            f"`plugins.enabled` contains `{AX_PLUGIN_NAME}` (Hermes will load the adapter).",
                        )
                    else:
                        add_check(
                            "ax_platform_enabled",
                            "failed",
                            f"`plugins.enabled` in {cfg_path} does not contain `{AX_PLUGIN_NAME}`. "
                            f"Hermes is opt-in for user plugins; without this the runtime comes up "
                            f"but logs `No messaging platforms enabled` and never replies.",
                        )
            else:
                add_check(
                    "ax_platform_enabled",
                    "failed",
                    f"{cfg_path} is missing. Run `ax gateway agents start {entry.get('name') or '<name>'}` "
                    f"to trigger the scaffold.",
                )
        except Exception as exc:
            add_check("ax_platform_enabled", "warning", f"Could not inspect per-agent config.yaml: {exc}")

    if str(snapshot.get("mode") or "") == "LIVE":
        if str(snapshot.get("presence") or "") == "IDLE":
            add_check("live_path", "passed", "Live listener is connected.")
        elif str(snapshot.get("reachability") or "") == "sse_disconnected":
            pass  # channel_sse check covers this with a more specific message
        elif str(snapshot.get("reachability") or "") == "attach_required":
            add_check("live_path", "warning", "Start Claude Code before sending.")
        elif str(snapshot.get("presence") or "") in {"STALE", "OFFLINE"}:
            add_check("live_path", "failed", str(snapshot.get("confidence_detail") or _reachability_copy(snapshot)))
    elif str(snapshot.get("mode") or "") == "ON-DEMAND" and not has_check("launch_ready"):
        add_check("launch_ready", "passed", "Gateway can launch this runtime on send.")

    if not os.environ.get("AX_OFFLINE"):
        import httpx as _httpx

        agent_id = str(entry.get("agent_id") or "").strip()
        if agent_id:
            try:
                upstream_client = _load_gateway_user_client()
                upstream_client.get_agent(agent_id)
                add_check("upstream_existence", "passed", "Agent record confirmed present on the platform.")
            except _httpx.HTTPStatusError as exc:
                if exc.response.status_code == 404:
                    add_check(
                        "upstream_existence",
                        "failed",
                        "Agent record not found on the platform (404). The upstream registration may have been "
                        "deleted or the gateway may be pointed at a different environment. "
                        "Use `ax gateway agents remove` then `ax gateway agents add` to re-register.",
                    )
                else:
                    add_check(
                        "upstream_existence",
                        "warning",
                        f"Could not verify upstream existence: HTTP {exc.response.status_code}.",
                    )
            except Exception as exc:
                add_check(
                    "upstream_existence", "warning", f"Could not reach platform to verify upstream existence: {exc}"
                )
        else:
            add_check(
                "upstream_existence", "warning", "No agent_id in registry entry — cannot verify upstream existence."
            )

    if send_test:
        try:
            sent = _send_gateway_test_to_managed_agent(name)
            message_id = None
            if isinstance(sent.get("message"), dict):
                message_id = sent["message"].get("id")
            add_check("test_send", "passed", f"Gateway test message sent{f' ({message_id})' if message_id else ''}.")
        except Exception as exc:
            add_check("test_send", "failed", f"Gateway test send failed: {exc}")

    status = _doctor_result_status(checks)
    completed_at = datetime.now(timezone.utc).isoformat()
    result = {
        "status": status,
        "completed_at": completed_at,
        "checks": checks,
        "summary": _doctor_summary(checks, status),
    }
    annotated = _store_doctor_result(name, result)
    return {
        "name": name,
        "status": status,
        "completed_at": completed_at,
        "summary": result["summary"],
        "checks": checks,
        "agent": annotated,
    }


@app.command("status")
def status(
    as_json: bool = JSON_OPTION,
    show_all: bool = typer.Option(
        False,
        "--all",
        "-a",
        help="Include hidden (auto-swept stale) and system (switchboard / service-account) agents.",
    ),
):
    """Show Gateway status, daemon state, and managed runtimes."""
    payload = _status_payload(include_hidden=show_all)
    if as_json:
        print_json(payload)
        return

    err_console.print("[bold]ax gateway status[/bold]")
    if payload.get("offline_mode"):
        err_console.print("[bold yellow]  MODE        = OFFLINE (no platform calls; AX_OFFLINE=1)[/bold yellow]")
    err_console.print(f"  gateway_dir = {payload['gateway_dir']}")
    err_console.print(f"  connected   = {payload['connected']}")
    err_console.print(f"  daemon      = {'running' if payload['daemon']['running'] else 'stopped'}")
    if payload["daemon"]["pid"]:
        err_console.print(f"  pid         = {payload['daemon']['pid']}")
    err_console.print(f"  ui          = {'running' if payload['ui']['running'] else 'stopped'}")
    if payload["ui"]["pid"]:
        err_console.print(f"  ui_pid      = {payload['ui']['pid']}")
    err_console.print(f"  ui_url      = {payload['ui']['url']}")
    err_console.print(f"  base_url    = {payload['base_url']}")
    err_console.print(f"  space_id    = {payload['space_id']}")
    if payload.get("space_name"):
        err_console.print(f"  space_name  = {payload['space_name']}")
    err_console.print(f"  user        = {payload['user']}")
    err_console.print(f"  agents      = {payload['summary']['managed_agents']}")
    err_console.print(f"  live        = {payload['summary']['live_agents']}")
    err_console.print(f"  on_demand   = {payload['summary']['on_demand_agents']}")
    err_console.print(f"  inbox       = {payload['summary']['inbox_agents']}")
    hidden_n = payload["summary"].get("hidden_agents", 0)
    system_n = payload["summary"].get("system_agents", 0)
    if hidden_n or system_n:
        hint = "" if show_all else "  (run with --all to include)"
        err_console.print(f"  hidden      = {hidden_n}{hint}")
        err_console.print(f"  system      = {system_n}")
    err_console.print(f"  alerts      = {payload['summary'].get('alert_count', 0)}")
    err_console.print(f"  approvals   = {payload['summary'].get('pending_approvals', 0)} pending")
    if payload.get("alerts"):
        print_table(
            ["Level", "Alert", "Agent", "Detail"],
            payload["alerts"],
            keys=["severity", "title", "agent_name", "detail"],
        )
    if payload["agents"]:
        print_table(
            [
                "Agent",
                "Type",
                "Mode",
                "Presence",
                "Output",
                "Confidence",
                "Acting As",
                "Current Space",
                "Seen",
                "Backlog",
                "Reason",
            ],
            [
                {**agent, "type": _agent_type_label(agent), "output": _agent_output_label(agent)}
                for agent in payload["agents"]
            ],
            keys=[
                "name",
                "type",
                "mode",
                "presence",
                "output",
                "confidence",
                "acting_agent_name",
                "active_space_name",
                "last_seen_age_seconds",
                "backlog_depth",
                "confidence_reason",
            ],
        )
    if payload["recent_activity"]:
        print_table(
            ["Time", "Event", "Agent", "Message", "Preview"],
            payload["recent_activity"],
            keys=["ts", "event", "agent_name", "message_id", "reply_preview"],
        )


@approvals_app.command("list")
def list_approvals(
    status: str | None = typer.Option(
        None, "--status", help="Optional filter: pending | approved | rejected | archived"
    ),
    include_archived: bool = typer.Option(False, "--include-archived", help="Include archived/stale approvals"),
    as_json: bool = JSON_OPTION,
):
    """List local Gateway approval requests."""
    payload = _approval_rows_payload(status=status, include_archived=include_archived)
    if as_json:
        print_json(payload)
        return
    err_console.print("[bold]ax gateway approvals list[/bold]")
    err_console.print(f"  approvals = {payload['count']}")
    err_console.print(f"  pending   = {payload['pending']}")
    if not payload["approvals"]:
        err_console.print("[dim]No Gateway approvals found.[/dim]")
        return
    print_table(
        ["Approval", "Asset", "Kind", "Status", "Risk", "Reason", "Requested"],
        payload["approvals"],
        keys=["approval_id", "asset_id", "approval_kind", "status", "risk", "reason", "requested_at"],
    )


@approvals_app.command("cleanup")
def cleanup_approvals(as_json: bool = JSON_OPTION):
    """Archive stale approval requests that no longer match managed agents."""
    payload = archive_stale_gateway_approvals()
    if as_json:
        print_json(payload)
        return
    archived_count = int(payload.get("archived_count") or 0)
    err_console.print(f"[green]Archived stale approvals:[/green] {archived_count}")
    err_console.print(f"  pending = {payload.get('remaining_pending', 0)}")


@approvals_app.command("show")
def show_approval(
    approval_id: str = typer.Argument(..., help="Approval request id"),
    as_json: bool = JSON_OPTION,
):
    """Show one local Gateway approval request."""
    try:
        payload = _approval_detail_payload(approval_id)
    except LookupError as exc:
        err_console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1)
    if as_json:
        print_json(payload)
        return
    approval = payload["approval"]
    print_table(
        ["Field", "Value"],
        [
            {"field": "approval_id", "value": approval.get("approval_id")},
            {"field": "asset_id", "value": approval.get("asset_id")},
            {"field": "gateway_id", "value": approval.get("gateway_id")},
            {"field": "install_id", "value": approval.get("install_id")},
            {"field": "kind", "value": approval.get("approval_kind")},
            {"field": "status", "value": approval.get("status")},
            {"field": "risk", "value": approval.get("risk")},
            {"field": "action", "value": approval.get("action")},
            {"field": "resource", "value": approval.get("resource")},
            {"field": "reason", "value": approval.get("reason")},
            {"field": "requested_at", "value": approval.get("requested_at")},
            {"field": "decided_at", "value": approval.get("decided_at")},
            {"field": "decision_scope", "value": approval.get("decision_scope")},
        ],
        keys=["field", "value"],
    )
    candidate = approval.get("candidate_binding") if isinstance(approval.get("candidate_binding"), dict) else None
    if candidate:
        print_table(
            ["Candidate Field", "Value"],
            [
                {"field": "path", "value": candidate.get("path")},
                {"field": "binding_type", "value": candidate.get("binding_type")},
                {"field": "launch_spec_hash", "value": candidate.get("launch_spec_hash")},
                {"field": "candidate_signature", "value": candidate.get("candidate_signature")},
            ],
            keys=["field", "value"],
        )


@approvals_app.command("approve")
def approve_approval(
    approval_id: str = typer.Argument(..., help="Approval request id"),
    scope: str = typer.Option("asset", "--scope", help="Recorded approval scope: once | asset | gateway"),
    as_json: bool = JSON_OPTION,
):
    """Approve a local Gateway binding request."""
    try:
        payload = approve_gateway_approval(approval_id, scope=scope)
    except (LookupError, ValueError) as exc:
        err_console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1)
    if as_json:
        print_json(payload)
        return
    approval = payload["approval"]
    err_console.print(f"[green]Approved:[/green] {approval['approval_id']}")
    err_console.print(f"  asset = {approval.get('asset_id')}")
    err_console.print(f"  scope = {approval.get('decision_scope')}")


@approvals_app.command("deny")
def deny_approval(
    approval_id: str = typer.Argument(..., help="Approval request id"),
    as_json: bool = JSON_OPTION,
):
    """Deny a local Gateway binding request."""
    try:
        payload = deny_gateway_approval(approval_id)
    except LookupError as exc:
        err_console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1)
    if as_json:
        print_json({"approval": payload})
        return
    err_console.print(f"[yellow]Denied:[/yellow] {payload['approval_id']}")
    err_console.print(f"  asset = {payload.get('asset_id')}")


# Deferred cross-module imports (bottom-of-file to avoid import cycles; bound
# into module globals after defs, resolved at call time).
from .gateway_agents import _with_registry_refs  # noqa: E402
from .gateway_auth import _load_gateway_user_client  # noqa: E402
from .gateway_messaging import _send_gateway_test_to_managed_agent  # noqa: E402
from .gateway_ui import (  # noqa: E402
    _agent_output_label,
    _agent_type_label,
    _format_age,
    _is_offline_mode_active,
    _reachability_copy,
)
