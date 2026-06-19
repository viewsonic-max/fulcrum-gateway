"""ax gateway — web UI rendering, HTTP handler/server, and `ui`/`activity` commands.

Extracted from ``ax_cli/commands/gateway.py`` (issue #28 Phase 1).
"""

from __future__ import annotations

import json
import os
import secrets
import uuid
import webbrowser
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

import typer
from rich import box
from rich.columns import Columns
from rich.console import Group
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from .. import gateway as gateway_core
from ..gateway import (
    _ui_request_logger,
    activity_log_path,
    agent_token_path,
    annotate_runtime_health,
    approve_gateway_approval,
    clear_gateway_ui_state,
    deny_gateway_approval,
    find_agent_entry,
    gateway_dir,
    load_gateway_registry,
    load_gateway_session,
    record_gateway_activity,
    write_gateway_ui_state,
)
from ..gateway_runtime_types import (
    runtime_type_deprecated,
    runtime_type_successor,
)
from ..output import JSON_OPTION, err_console, print_json, print_table
from .gateway_app import _CONFIDENCE_STYLES, _PRESENCE_ORDER, _PRESENCE_STYLES, _STATE_STYLES, _UNSET, app


def _parse_iso8601(value: object) -> datetime | None:
    if not value or not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _age_seconds(value: object) -> int | None:
    parsed = _parse_iso8601(value)
    if parsed is None:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return max(0, int((datetime.now(timezone.utc) - parsed.astimezone(timezone.utc)).total_seconds()))


def _format_age(seconds: object) -> str:
    if seconds is None:
        return "-"
    try:
        total = int(seconds)
    except (TypeError, ValueError):
        return "-"
    if total < 60:
        return f"{total}s"
    minutes, seconds = divmod(total, 60)
    if minutes < 60:
        return f"{minutes}m {seconds:02d}s"
    hours, minutes = divmod(minutes, 60)
    if hours < 24:
        return f"{hours}h {minutes:02d}m"
    days, hours = divmod(hours, 24)
    return f"{days}d {hours:02d}h"


def _format_timestamp(value: object) -> str:
    return _format_age(_age_seconds(value))


def _state_text(state: object) -> Text:
    label = str(state or "unknown").lower()
    style = _STATE_STYLES.get(label, "white")
    return Text(f"● {label}", style=style)


def _presence_text(presence: object) -> Text:
    label = str(presence or "OFFLINE").upper()
    style = _PRESENCE_STYLES.get(label, "white")
    return Text(label, style=style)


def _confidence_text(confidence: object) -> Text:
    label = str(confidence or "MEDIUM").upper()
    style = _CONFIDENCE_STYLES.get(label, "white")
    return Text(label, style=style)


def _mode_text(mode: object) -> Text:
    label = str(mode or "ON-DEMAND").upper()
    style = {
        "LIVE": "green",
        "ON-DEMAND": "cyan",
        "INBOX": "blue",
    }.get(label, "white")
    return Text(label, style=style)


def _reply_text(reply: object) -> Text:
    label = str(reply or "REPLY").upper()
    style = {
        "REPLY": "green",
        "SUMMARY": "yellow",
        "SILENT": "dim",
    }.get(label, "white")
    return Text(label, style=style)


def _reachability_copy(agent: dict) -> str:
    reachability = str(agent.get("reachability") or "unavailable")
    mode = str(agent.get("mode") or "")
    if reachability == "live_now":
        return "Live listener ready to claim work."
    if reachability == "queue_available":
        return "Gateway can safely queue work now."
    if reachability == "launch_available":
        return "Gateway can launch this runtime on send."
    if reachability == "sse_disconnected":
        return "The attached session's SSE subscription is down — messages will not be delivered."
    if reachability == "attach_required":
        return "Start the attached session before sending."
    if mode == "INBOX":
        return "Queue path is unavailable."
    return "Gateway does not currently have a working path."


def _agent_template_label(agent: dict) -> str:
    return str(agent.get("template_label") or agent.get("runtime_type") or "-")


def _agent_type_label(agent: dict) -> str:
    return str(agent.get("asset_type_label") or "Connected Asset")


def _agent_output_label(agent: dict) -> str:
    return str(agent.get("output_label") or agent.get("reply") or "Reply")


def _adapter_label(agent: dict) -> str:
    """Adapter cell for ``agents show`` — annotates deprecated runtimes.

    Surfaces the silent-drift bug in #90: a registry minted by an older
    axctl may carry a runtime_type that is now deprecated. The user
    runs the modern axctl but the entry pins the legacy code path. We
    print the deprecation and a copy-pasteable migration command so the
    drift stops being invisible.
    """
    runtime_type = str(agent.get("runtime_type") or "").strip()
    if not runtime_type:
        return "-"
    if not runtime_type_deprecated(runtime_type):
        return runtime_type
    successor = runtime_type_successor(runtime_type)
    name = str(agent.get("name") or "").strip()
    if successor and name:
        return f"{runtime_type} (deprecated — migrate with `ax gateway agents update {name} --type {successor}`)"
    if successor:
        return f"{runtime_type} (deprecated — migrate with `--type {successor}`)"
    return f"{runtime_type} (deprecated)"


def _metric_panel(label: str, value: object, *, tone: str = "cyan", subtitle: str | None = None) -> Panel:
    body = Text()
    body.append(str(value), style=f"bold {tone}")
    body.append(f"\n{label}", style="dim")
    if subtitle:
        body.append(f"\n{subtitle}", style="dim")
    return Panel(body, border_style=tone, padding=(1, 2))


def _sorted_agents(agents: list[dict]) -> list[dict]:
    return sorted(
        agents,
        key=lambda agent: (
            _PRESENCE_ORDER.get(str(agent.get("presence") or "").upper(), 99),
            str(agent.get("name") or "").lower(),
        ),
    )


def _render_gateway_overview(payload: dict) -> Panel:
    gateway = payload.get("gateway") or {}
    ui = payload.get("ui") or {}
    grid = Table.grid(expand=True, padding=(0, 2))
    grid.add_column(style="bold")
    grid.add_column(ratio=2)
    grid.add_column(style="bold")
    grid.add_column(ratio=2)
    grid.add_row(
        "Gateway",
        str(gateway.get("gateway_id") or "-")[:8],
        "Daemon",
        "running" if payload["daemon"]["running"] else "stopped",
    )
    grid.add_row("User", str(payload.get("user") or "-"), "Base URL", str(payload.get("base_url") or "-"))
    space_label = str(payload.get("space_name") or payload.get("space_id") or "-")
    grid.add_row("Space", space_label, "Environment", str(payload.get("gateway_environment") or "default"))
    grid.add_row("PID", str(payload["daemon"].get("pid") or "-"), "State Dir", str(payload.get("gateway_dir") or "-"))
    grid.add_row("UI", str(ui.get("url") or "-"), "UI PID", str(ui.get("pid") or "-"))
    grid.add_row(
        "Session",
        "connected" if payload.get("connected") else "disconnected",
        "Last Reconcile",
        _format_timestamp(gateway.get("last_reconcile_at")),
    )
    return Panel(grid, title="Gateway Overview", border_style="cyan")


def _render_agent_table(agents: list[dict]) -> Table:
    table = Table(expand=True, box=box.SIMPLE_HEAVY)
    table.add_column("Agent", style="bold")
    table.add_column("Type")
    table.add_column("Mode")
    table.add_column("Presence")
    table.add_column("Output")
    table.add_column("Confidence")
    table.add_column("Acting As")
    table.add_column("Current Space")
    table.add_column("Queue", justify="right")
    table.add_column("Seen", justify="right")
    table.add_column("Activity", overflow="fold")
    if not agents:
        table.add_row(
            "No managed agents",
            "-",
            Text("ON-DEMAND", style="dim"),
            Text("OFFLINE", style="dim"),
            Text("Reply", style="dim"),
            Text("MEDIUM", style="dim"),
            "-",
            "-",
            "0",
            "-",
            "-",
        )
        return table
    for agent in _sorted_agents(agents):
        activity = str(
            agent.get("current_activity")
            or agent.get("confidence_detail")
            or agent.get("current_tool")
            or agent.get("last_reply_preview")
            or "-"
        )
        table.add_row(
            f"@{agent.get('name')}",
            _agent_type_label(agent),
            _mode_text(agent.get("mode")),
            _presence_text(agent.get("presence")),
            Text(
                _agent_output_label(agent),
                style="green" if str(agent.get("output_label") or "").lower() == "reply" else "yellow",
            ),
            _confidence_text(agent.get("confidence")),
            str(agent.get("acting_agent_name") or agent.get("name") or "-"),
            str(agent.get("active_space_name") or agent.get("active_space_id") or agent.get("space_id") or "-"),
            str(agent.get("backlog_depth") or 0),
            _format_age(agent.get("last_seen_age_seconds")),
            activity,
        )
    return table


def _render_activity_table(activity: list[dict]) -> Table:
    table = Table(expand=True, box=box.SIMPLE_HEAVY)
    table.add_column("When", justify="right", no_wrap=True)
    table.add_column("Event", no_wrap=True)
    table.add_column("Agent", no_wrap=True)
    table.add_column("Detail", overflow="fold")
    if not activity:
        table.add_row("-", "idle", "-", "No activity yet")
        return table
    for item in activity:
        detail = (
            item.get("activity_message")
            or item.get("reply_preview")
            or item.get("tool_name")
            or item.get("error")
            or item.get("message_id")
            or "-"
        )
        agent_name = item.get("agent_name")
        table.add_row(
            _format_timestamp(item.get("ts")),
            str(item.get("event") or "-"),
            f"@{agent_name}" if agent_name else "-",
            str(detail),
        )
    return table


def _render_alert_table(alerts: list[dict]) -> Table:
    table = Table(expand=True, box=box.SIMPLE_HEAVY)
    table.add_column("Level", no_wrap=True)
    table.add_column("Alert", no_wrap=True)
    table.add_column("Agent", no_wrap=True)
    table.add_column("Detail", overflow="fold")
    if not alerts:
        table.add_row("info", "No active alerts", "-", "Gateway looks healthy.")
        return table
    for item in alerts:
        severity = str(item.get("severity") or "info").lower()
        style = {"error": "red", "warning": "yellow", "info": "cyan"}.get(severity, "white")
        agent_name = str(item.get("agent_name") or "")
        table.add_row(
            Text(severity, style=style),
            str(item.get("title") or "-"),
            f"@{agent_name}" if agent_name else "-",
            str(item.get("detail") or "-"),
        )
    return table


def _render_gateway_dashboard(payload: dict) -> Group:
    agents = payload.get("agents", [])
    summary = payload.get("summary", {})
    queue_depth = sum(int(agent.get("backlog_depth") or 0) for agent in agents)
    metrics = Columns(
        [
            _metric_panel("managed agents", summary.get("managed_agents", 0), tone="cyan"),
            _metric_panel("live", summary.get("live_agents", 0), tone="green"),
            _metric_panel("on-demand", summary.get("on_demand_agents", 0), tone="blue"),
            _metric_panel("inbox", summary.get("inbox_agents", 0), tone="cyan"),
            _metric_panel("pending approvals", summary.get("pending_approvals", 0), tone="yellow"),
            _metric_panel("low confidence", summary.get("low_confidence_agents", 0), tone="yellow"),
            _metric_panel("blocked", summary.get("blocked_agents", 0), tone="red"),
            _metric_panel("queue depth", queue_depth, tone="blue"),
        ],
        expand=True,
        equal=True,
    )
    return Group(
        _render_gateway_overview(payload),
        metrics,
        Panel(_render_alert_table(payload.get("alerts", [])), title="Alerts", border_style="red"),
        Panel(_render_agent_table(agents), title="Managed Agents", border_style="green"),
        Panel(
            _render_activity_table(payload.get("recent_activity", [])), title="Recent Activity", border_style="magenta"
        ),
    )


def _render_gateway_ui_page(*, refresh_ms: int) -> str:
    template = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>ax gateway ui</title>
  <link rel="icon" type="image/svg+xml" href="/favicon.svg" />
  <style>
    :root {
      --bg: #081018;
      --panel: #0e1a24;
      --panel-2: #111f2b;
      --line: #1d3342;
      --text: #e7f7ff;
      --muted: #93afbf;
      --cyan: #47e7ff;
      --green: #53f977;
      --yellow: #f1d45f;
      --red: #ff6e6e;
      --blue: #5c98ff;
      --magenta: #ff5fe6;
      --shadow: 0 24px 80px rgba(0, 0, 0, 0.35);
      --radius: 20px;
      --radius-sm: 14px;
      --mono: "SFMono-Regular", "Menlo", "Monaco", "Consolas", monospace;
      --sans: "Avenir Next", "Segoe UI", "Helvetica Neue", sans-serif;
    }

    * { box-sizing: border-box; }

    body {
      margin: 0;
      min-height: 100vh;
      background:
        radial-gradient(circle at top left, rgba(71, 231, 255, 0.18), transparent 32%),
        radial-gradient(circle at top right, rgba(92, 152, 255, 0.16), transparent 28%),
        linear-gradient(180deg, #071019 0%, #0b131c 100%);
      color: var(--text);
      font-family: var(--sans);
    }

    .shell {
      width: min(1400px, calc(100vw - 32px));
      margin: 20px auto 40px;
      display: grid;
      gap: 16px;
    }

    .panel {
      background: linear-gradient(180deg, rgba(14, 26, 36, 0.96), rgba(10, 21, 29, 0.96));
      border: 1px solid var(--line);
      border-radius: var(--radius);
      box-shadow: var(--shadow);
      overflow: hidden;
    }

    .panel-header {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 12px;
      padding: 18px 22px 0;
      font-family: var(--mono);
      text-transform: uppercase;
      letter-spacing: 0.12em;
      color: var(--cyan);
      font-size: 13px;
    }

    .header-actions {
      display: flex;
      align-items: center;
      gap: 10px;
    }

    .panel-body {
      padding: 18px 22px 22px;
    }

    .hero {
      display: grid;
      grid-template-columns: 1.25fr 1fr;
      gap: 16px;
    }

    .hero-copy h1 {
      margin: 0 0 10px;
      font-size: clamp(28px, 3.3vw, 52px);
      line-height: 0.95;
      letter-spacing: -0.03em;
    }

    .hero-copy p {
      margin: 0;
      max-width: 44rem;
      color: var(--muted);
      line-height: 1.55;
      font-size: 15px;
    }

    .hero-meta {
      display: grid;
      gap: 12px;
      align-content: start;
    }

    .meta-chip {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 12px;
      padding: 14px 16px;
      border-radius: var(--radius-sm);
      border: 1px solid var(--line);
      background: rgba(6, 17, 24, 0.6);
      font-family: var(--mono);
      font-size: 13px;
    }

    .metrics {
      display: grid;
      grid-template-columns: repeat(5, minmax(0, 1fr));
      gap: 16px;
    }

    .metric {
      padding: 18px;
      border-radius: var(--radius);
      border: 1px solid var(--line);
      background: rgba(8, 19, 27, 0.78);
    }

    .metric strong {
      display: block;
      font-size: 34px;
      margin-bottom: 4px;
      font-family: var(--mono);
    }

    .metric span {
      color: var(--muted);
      font-size: 14px;
    }

    .metric.cyan strong { color: var(--cyan); }
    .metric.green strong { color: var(--green); }
    .metric.yellow strong { color: var(--yellow); }
    .metric.red strong { color: var(--red); }
    .metric.blue strong { color: var(--blue); }

    .metric.red span,
    .metric.yellow span {
      color: var(--text);
    }

    .dashboard {
      display: grid;
      grid-template-columns: minmax(0, 1.3fr) minmax(360px, 0.9fr);
      gap: 16px;
    }

    .alerts-list {
      display: grid;
      gap: 12px;
    }

    .alert-card {
      padding: 14px 16px;
      border-radius: 14px;
      border: 1px solid var(--line);
      background: rgba(8, 19, 27, 0.7);
    }

    .alert-card.warning {
      border-color: rgba(241, 212, 95, 0.45);
      background: rgba(241, 212, 95, 0.08);
    }

    .alert-card.error {
      border-color: rgba(255, 110, 110, 0.45);
      background: rgba(255, 110, 110, 0.08);
    }

    .alert-head {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 12px;
      margin-bottom: 6px;
      font-family: var(--mono);
      font-size: 12px;
      text-transform: uppercase;
      letter-spacing: 0.08em;
    }

    .alert-body {
      color: var(--muted);
      line-height: 1.5;
      font-size: 14px;
    }

    .control-grid {
      display: grid;
      grid-template-columns: minmax(280px, 0.95fr) minmax(0, 1.05fr);
      gap: 16px;
    }

    table {
      width: 100%;
      border-collapse: collapse;
      font-size: 14px;
    }

    th {
      text-align: left;
      padding: 0 0 10px;
      color: var(--muted);
      font-family: var(--mono);
      font-size: 12px;
      letter-spacing: 0.08em;
      text-transform: uppercase;
      border-bottom: 1px solid var(--line);
    }

    td {
      padding: 12px 0;
      border-bottom: 1px solid rgba(29, 51, 66, 0.45);
      vertical-align: top;
    }

    tbody tr:last-child td {
      border-bottom: none;
    }

    .agent-button {
      width: 100%;
      border: 1px solid transparent;
      background: transparent;
      color: inherit;
      text-align: left;
      padding: 10px 12px;
      border-radius: 12px;
      transition: background 0.15s ease, border-color 0.15s ease, transform 0.15s ease;
      cursor: pointer;
    }

    .agent-button:hover,
    .agent-button.is-active {
      background: rgba(71, 231, 255, 0.08);
      border-color: rgba(71, 231, 255, 0.35);
      transform: translateY(-1px);
    }

    .agent-name {
      font-family: var(--mono);
      font-weight: 700;
      margin-bottom: 4px;
    }

    .agent-meta,
    .caption,
    .detail-list dd,
    .event-detail {
      color: var(--muted);
    }

    .status-pill {
      display: inline-flex;
      align-items: center;
      gap: 8px;
      padding: 7px 11px;
      border-radius: 999px;
      border: 1px solid currentColor;
      font-family: var(--mono);
      font-size: 12px;
      text-transform: uppercase;
      letter-spacing: 0.08em;
    }

    .status-live,
    .status-idle,
    .status-reply,
    .status-high { color: var(--green); }
    .status-on-demand,
    .status-queued,
    .status-medium { color: var(--cyan); }
    .status-inbox { color: var(--blue); }
    .status-summary,
    .status-blocked,
    .status-stale,
    .status-low { color: var(--yellow); }
    .status-error,
    .status-blocked { color: var(--red); }
    .status-offline,
    .status-silent { color: var(--muted); }

    .detail-card {
      display: grid;
      gap: 16px;
    }

    .action-row,
    .form-grid {
      display: flex;
      flex-wrap: wrap;
      gap: 10px;
    }

    .form-grid {
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 12px;
    }

    .control-group {
      display: grid;
      gap: 8px;
    }

    label {
      color: var(--muted);
      font-family: var(--mono);
      font-size: 12px;
      letter-spacing: 0.06em;
      text-transform: uppercase;
    }

    input,
    select,
    textarea,
    button {
      width: 100%;
      border-radius: 12px;
      border: 1px solid var(--line);
      background: rgba(8, 19, 27, 0.9);
      color: var(--text);
      font: inherit;
      padding: 12px 14px;
    }

    textarea {
      min-height: 96px;
      resize: vertical;
    }

    button {
      width: auto;
      cursor: pointer;
      font-family: var(--mono);
      text-transform: uppercase;
      letter-spacing: 0.08em;
      transition: transform 0.15s ease, border-color 0.15s ease, background 0.15s ease;
    }

    button:hover {
      transform: translateY(-1px);
      border-color: rgba(71, 231, 255, 0.35);
      background: rgba(71, 231, 255, 0.08);
    }

    button.danger:hover {
      border-color: rgba(255, 110, 110, 0.35);
      background: rgba(255, 110, 110, 0.08);
    }

    button.ghost {
      background: transparent;
      border-color: rgba(71, 231, 255, 0.22);
      color: var(--muted);
    }

    .flash {
      min-height: 24px;
      color: var(--muted);
      font-size: 13px;
    }

    .flash.error {
      color: var(--red);
    }

    .flash.success {
      color: var(--green);
    }

    .flash.warning {
      color: var(--yellow);
    }

    .runtime-info {
      display: grid;
      gap: 12px;
      padding: 14px;
      border-radius: 14px;
      border: 1px solid var(--line);
      background: rgba(8, 19, 27, 0.58);
    }

    .runtime-info h3 {
      margin: 0;
      font-size: 16px;
      font-family: var(--mono);
    }

    .runtime-info p {
      margin: 0;
      color: var(--muted);
      line-height: 1.5;
      font-size: 14px;
    }

    .runtime-info summary {
      cursor: pointer;
      font-family: var(--mono);
      font-size: 12px;
      letter-spacing: 0.08em;
      text-transform: uppercase;
      color: var(--text);
      list-style: none;
    }

    .runtime-info summary::-webkit-details-marker {
      display: none;
    }

    .signal-grid {
      display: grid;
      gap: 10px;
    }

    .signal-grid div {
      padding: 10px 12px;
      border-radius: 12px;
      border: 1px solid var(--line);
      background: rgba(6, 17, 24, 0.55);
    }

    .signal-grid strong {
      display: block;
      margin-bottom: 6px;
      font-family: var(--mono);
      font-size: 12px;
      text-transform: uppercase;
      letter-spacing: 0.08em;
      color: var(--cyan);
    }

    .detail-list {
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 12px 20px;
      margin: 0;
    }

    .detail-list div {
      padding: 12px 14px;
      border-radius: 14px;
      border: 1px solid var(--line);
      background: rgba(8, 19, 27, 0.58);
    }

    .detail-list dt {
      margin: 0 0 6px;
      color: var(--muted);
      font-family: var(--mono);
      font-size: 12px;
      letter-spacing: 0.06em;
      text-transform: uppercase;
    }

    .detail-list dd {
      margin: 0;
      line-height: 1.45;
      word-break: break-word;
    }

    .event-list {
      display: grid;
      gap: 10px;
    }

    .event-item {
      padding: 12px 14px;
      border-radius: 14px;
      border: 1px solid var(--line);
      background: rgba(8, 19, 27, 0.58);
    }

    .event-head {
      display: flex;
      justify-content: space-between;
      gap: 12px;
      margin-bottom: 6px;
      font-family: var(--mono);
      font-size: 12px;
      color: var(--text);
    }

    .event-detail {
      font-size: 14px;
      line-height: 1.45;
    }

    .copyable-block {
      position: relative;
    }

    .copyable-block pre {
      margin: 0;
      white-space: pre-wrap;
      word-break: break-word;
      font: inherit;
      color: inherit;
    }

    .empty {
      padding: 18px;
      border-radius: 14px;
      border: 1px dashed var(--line);
      color: var(--muted);
      text-align: center;
    }

    .footer-note {
      display: flex;
      justify-content: space-between;
      gap: 16px;
      align-items: center;
      color: var(--muted);
      font-size: 13px;
    }

    .footer-note code {
      font-family: var(--mono);
      color: var(--text);
    }

    .badge {
      display: inline-block;
      padding: 6px 9px;
      border-radius: 999px;
      background: rgba(71, 231, 255, 0.08);
      color: var(--cyan);
      border: 1px solid rgba(71, 231, 255, 0.22);
      font-family: var(--mono);
      font-size: 12px;
      letter-spacing: 0.06em;
      text-transform: uppercase;
    }

    @media (max-width: 1100px) {
      .hero,
      .dashboard {
        grid-template-columns: 1fr;
      }

      .metrics {
        grid-template-columns: repeat(2, minmax(0, 1fr));
      }
    }

    @media (max-width: 720px) {
      .shell {
        width: min(100vw - 16px, 100%);
        margin: 8px auto 24px;
      }

      .metrics,
      .detail-list {
        grid-template-columns: 1fr;
      }

      .panel-header,
      .panel-body {
        padding-left: 16px;
        padding-right: 16px;
      }
    }
  </style>
</head>
<body>
  <main class="shell">
    <section class="panel">
      <div class="panel-body hero">
        <div class="hero-copy">
          <div class="badge"><a href="/" style="color:inherit; text-decoration:none;">← Back to quick view</a> · Gateway Control Plane · Agent Operated</div>
          <h1>One local Gateway. Every agent in one place.</h1>
          <p>
            This dashboard is served locally by <code>ax gateway ui</code> and reads the
            same Gateway state model as the terminal watch view. The browser is a human
            view over the same local control plane that setup agents use through the CLI
            and local API instead of maintaining separate logic.
          </p>
        </div>
      <div id="overview" class="hero-meta"></div>
      </div>
    </section>

    <section id="metrics" class="metrics"></section>

    <section class="panel">
      <div class="panel-header">
        <span>Alerts</span>
        <span id="alert-summary" class="caption">loading…</span>
      </div>
      <div id="alerts-feed" class="panel-body">
        <div class="empty">Waiting for Gateway alerts…</div>
      </div>
    </section>

    <section class="control-grid">
      <section class="panel">
        <div class="panel-header">
          <span>Gateway Agent Setup</span>
          <span id="setup-mode-chip" class="caption">agent skill · create</span>
        </div>
        <div class="panel-body">
          <form id="add-agent-form" class="detail-card">
            <p class="caption">
              This form mirrors the <code>gateway-agent-setup</code> skill. Agents and humans
              should use the same Gateway-native setup, doctor, and update flow.
            </p>
            <div class="form-grid">
              <div class="control-group">
                <label for="agent-name">Name</label>
                <input id="agent-name" name="name" placeholder="hermes-bot" required />
              </div>
              <div class="control-group">
                <label for="agent-type">Agent Type</label>
                <select id="agent-type" name="template_id">
                </select>
              </div>
            </div>
            <div id="runtime-help" class="runtime-info">
              <h3>Loading agent type…</h3>
            </div>
            <details id="advanced-launch" class="runtime-info" style="display:none;">
              <summary>Advanced launch settings</summary>
              <p>
                Most setups should leave this alone. These fields exist so we can override
                the default launch command while debugging or building new adapters.
              </p>
              <div class="form-grid">
                <div class="control-group" id="exec-command-group">
                  <label for="agent-exec">Command Override</label>
                  <input id="agent-exec" name="exec_command" placeholder="python3 examples/sentinel_inference_sdk/hermes_bridge.py" />
                </div>
                <div class="control-group" id="workdir-group">
                  <label for="agent-workdir">Working Directory Override</label>
                  <input id="agent-workdir" name="workdir" placeholder="/absolute/path/to/workdir" />
                </div>
                <div class="control-group" id="ollama-model-group" style="display:none;">
                  <label for="agent-ollama-model">Ollama Model</label>
                  <input id="agent-ollama-model" name="model" list="ollama-model-options" placeholder="gemma4:latest" />
                  <datalist id="ollama-model-options"></datalist>
                  <div id="ollama-model-caption" class="caption"></div>
                </div>
              </div>
            </details>
            <div class="action-row">
              <button id="add-agent-submit" type="submit">Add Agent</button>
              <button id="add-agent-cancel" type="button" class="ghost" style="display:none;">Cancel Edit</button>
            </div>
            <div id="add-agent-flash" class="flash"></div>
          </form>
        </div>
      </section>

      <section class="panel">
        <div class="panel-header">
          <span>Custom Message</span>
          <span id="quick-send-chip" class="caption">splunk · datadog · cron · manual</span>
        </div>
        <div class="panel-body">
          <form id="send-form" class="detail-card">
            <p class="caption">
              Use <strong>Send Agent Test</strong> for the standard validation path.
              Use this form for custom payloads, alerts, and scheduled-job style messages.
            </p>
            <div class="form-grid">
              <div class="control-group">
                <label for="send-to">To</label>
                <input id="send-to" name="to" placeholder="codex" />
              </div>
              <div class="control-group">
                <label for="send-parent-id">Parent ID</label>
                <input id="send-parent-id" name="parent_id" placeholder="optional thread parent" />
              </div>
            </div>
            <div class="control-group">
              <label for="send-content">Message</label>
              <textarea id="send-content" name="content" placeholder="Send a custom payload through Gateway: Datadog alert, Splunk event, cron reminder, or manual task"></textarea>
            </div>
            <div class="action-row">
              <button type="submit">Send Custom Message</button>
            </div>
            <div id="send-flash" class="flash"></div>
          </form>
        </div>
      </section>
    </section>

    <section class="dashboard">
      <section class="panel">
        <div class="panel-header">
          <span>Outbound Connectors</span>
          <span id="connectors-summary" class="caption">loading…</span>
        </div>
        <div class="panel-body">
          <table>
            <thead>
              <tr>
                <th>Name</th>
                <th>Provider</th>
                <th>Enabled</th>
                <th>Auth</th>
              </tr>
            </thead>
            <tbody id="connector-rows">
              <tr><td colspan="4"><div class="empty">No connectors registered.</div></td></tr>
            </tbody>
          </table>
          <form id="add-connector-form" class="detail-card" style="margin-top:16px;">
            <p class="caption">
              Register a connector with managed auth. Credentials stay in local auth files (0600)
              and are never returned by this API.
            </p>
            <div class="form-grid">
              <div class="control-group">
                <label for="connector-name">Name</label>
                <input id="connector-name" name="name" placeholder="composio-main" required />
              </div>
              <div class="control-group">
                <label for="connector-provider">Provider</label>
                <select id="connector-provider" name="provider"></select>
              </div>
            </div>
            <div class="action-row">
              <button type="submit">Add Connector</button>
            </div>
            <div id="add-connector-flash" class="flash"></div>
          </form>
        </div>
      </section>

      <section class="panel">
        <div class="panel-header">
          <span>Connector Detail</span>
          <span id="selected-connector-chip" class="caption">select a connector</span>
        </div>
        <div id="connector-detail" class="panel-body">
          <div class="empty">Choose a connector to manage auth, apps, and enablement.</div>
        </div>
      </section>
    </section>

    <section class="dashboard">
      <section class="panel">
        <div class="panel-header">
          <span>Managed Agents</span>
          <span id="managed-summary" class="caption">loading…</span>
        </div>
        <div class="panel-body">
          <table>
            <thead>
              <tr>
                <th>Agent</th>
                <th>Type</th>
                <th>Mode</th>
                <th>Presence</th>
                <th>Output</th>
                <th>Confidence</th>
                <th>Queue</th>
                <th>Seen</th>
                <th>Activity</th>
              </tr>
            </thead>
            <tbody id="agent-rows">
              <tr><td colspan="9"><div class="empty">Waiting for Gateway state…</div></td></tr>
            </tbody>
          </table>
        </div>
      </section>

      <section class="panel">
        <div class="panel-header">
          <span>Agent Drill-In</span>
          <div class="header-actions">
            <button id="refresh-toggle" type="button" class="ghost">Pause Refresh</button>
            <span id="selected-agent-chip" class="caption">select an agent</span>
          </div>
        </div>
        <div id="agent-detail" class="panel-body">
          <div class="empty">Choose a managed agent to inspect live detail.</div>
        </div>
      </section>
    </section>

    <section class="panel">
      <div class="panel-header">
        <span>Recent Activity</span>
        <span class="caption">auto-refresh every __REFRESH_MS__ ms</span>
      </div>
      <div id="activity-feed" class="panel-body">
        <div class="empty">Waiting for activity…</div>
      </div>
    </section>

    <section class="panel">
      <div class="panel-body footer-note">
        <span>Local status API: <code>/api/status</code>, <code>/api/agents/&lt;name&gt;</code>, <code>/api/connectors</code></span>
        <span>Setup skill: <code>skills/gateway-agent-setup/SKILL.md</code> · Terminal parity: <code>uv run ax gateway watch</code> · axctl <code>__VERSION__</code></span>
      </div>
    </section>
  </main>

  <script>
    const refreshMs = __REFRESH_MS__;
    let selectedAgent = null;
    let selectedConnector = null;
    let agentTemplates = [];
    let connectorProviders = [];
    let autoRefreshPaused = false;
    let setupMode = "create";
    let setupTarget = null;

    async function apiRequest(path, options = {}) {
      const response = await fetch(path, {
        cache: "no-store",
        headers: { "Content-Type": "application/json", ...(options.headers || {}) },
        ...options,
      });
      const isJson = (response.headers.get("Content-Type") || "").includes("application/json");
      const payload = isJson ? await response.json() : null;
      if (!response.ok) {
        throw new Error(payload?.error || `request failed (${response.status})`);
      }
      return payload;
    }

    function setFlash(id, message, kind = "") {
      const node = document.getElementById(id);
      node.className = `flash ${kind}`.trim();
      node.textContent = message || "";
    }

    function applySetupMode() {
      const chip = document.getElementById("setup-mode-chip");
      const submitButton = document.getElementById("add-agent-submit");
      const cancelButton = document.getElementById("add-agent-cancel");
      const nameInput = document.getElementById("agent-name");
      const editing = setupMode === "update" && setupTarget;
      chip.textContent = editing ? `agent skill · editing @${setupTarget}` : "agent skill · create";
      submitButton.textContent = editing ? "Update Agent" : "Add Agent";
      cancelButton.style.display = editing ? "inline-flex" : "none";
      nameInput.readOnly = Boolean(editing);
    }

    function resetSetupForm() {
      const form = document.getElementById("add-agent-form");
      setupMode = "create";
      setupTarget = null;
      form.reset();
      document.getElementById("agent-type").value = "echo_test";
      renderTemplateHelp("echo_test");
      applySetupMode();
    }

    async function loadAgentIntoSetupForm(name) {
      const detail = await apiRequest(`/api/agents/${encodeURIComponent(name)}`);
      const agent = detail.agent || {};
      const nameInput = document.getElementById("agent-name");
      const typeInput = document.getElementById("agent-type");
      const execInput = document.getElementById("agent-exec");
      const workdirInput = document.getElementById("agent-workdir");
      const ollamaModelInput = document.getElementById("agent-ollama-model");

      setupMode = "update";
      setupTarget = agent.name || name;
      nameInput.value = agent.name || name;
      if (agent.template_id) {
        typeInput.value = agent.template_id;
        renderTemplateHelp(agent.template_id);
      }
      execInput.value = agent.exec_command || "";
      workdirInput.value = agent.workdir || "";
      ollamaModelInput.value = agent.model || "";
      applySetupMode();
      setFlash("add-agent-flash", `Editing @${setupTarget}`, "success");
      document.getElementById("add-agent-form").scrollIntoView({ behavior: "smooth", block: "start" });
    }

    function refreshButtonLabel() {
      const button = document.getElementById("refresh-toggle");
      if (!button) return;
      button.textContent = autoRefreshPaused ? "Resume Refresh" : "Pause Refresh";
    }

    function escapeHtml(value) {
      return String(value ?? "")
        .replaceAll("&", "&amp;")
        .replaceAll("<", "&lt;")
        .replaceAll(">", "&gt;")
        .replaceAll('"', "&quot;")
        .replaceAll("'", "&#39;");
    }

    function formatAge(seconds) {
      if (seconds === null || seconds === undefined || seconds === "" || Number.isNaN(Number(seconds))) {
        return "-";
      }
      const total = Math.max(0, Number(seconds));
      if (total < 60) return `${Math.floor(total)}s`;
      const minutes = Math.floor(total / 60);
      const secs = Math.floor(total % 60);
      if (minutes < 60) return `${minutes}m ${String(secs).padStart(2, "0")}s`;
      const hours = Math.floor(minutes / 60);
      const mins = minutes % 60;
      if (hours < 24) return `${hours}h ${String(mins).padStart(2, "0")}m`;
      const days = Math.floor(hours / 24);
      const remHours = hours % 24;
      return `${days}d ${String(remHours).padStart(2, "0")}h`;
    }

    function stateClass(state) {
      return `status-${String(state || "stopped").toLowerCase()}`;
    }

    function detailText(item) {
      return item?.activity_message || item?.reply_preview || item?.tool_name || item?.error || item?.message_id || "-";
    }

    function getTemplateDefinition(templateId) {
      return agentTemplates.find((item) => item.id === templateId) || null;
    }

    function renderTemplateOptions() {
      const select = document.getElementById("agent-type");
      if (!agentTemplates.length) {
        select.innerHTML = `<option value="echo_test">Echo (Test)</option>`;
        return;
      }
      select.innerHTML = agentTemplates.map((item) => {
        const suffix = item.availability === "coming_soon" ? " (Soon)" : "";
        const disabled = item.launchable ? "" : " disabled";
        return `<option value="${escapeHtml(item.id)}"${disabled}>${escapeHtml(item.label + suffix)}</option>`;
      }).join("");
    }

    function renderTemplateHelp(templateId) {
      const definition = getTemplateDefinition(templateId);
      const help = document.getElementById("runtime-help");
      const advancedLaunch = document.getElementById("advanced-launch");
      const submitButton = document.getElementById("add-agent-submit");
      const agentNameInput = document.getElementById("agent-name");
      const execGroup = document.getElementById("exec-command-group");
      const workdirGroup = document.getElementById("workdir-group");
      const ollamaModelGroup = document.getElementById("ollama-model-group");
      const execInput = document.getElementById("agent-exec");
      const workdirInput = document.getElementById("agent-workdir");
      const ollamaModelInput = document.getElementById("agent-ollama-model");
      const ollamaModelOptions = document.getElementById("ollama-model-options");
      const ollamaModelCaption = document.getElementById("ollama-model-caption");
      if (!definition) {
        help.innerHTML = `<h3>Unknown agent type</h3><p>No template definition found.</p>`;
        advancedLaunch.style.display = "none";
        submitButton.disabled = true;
        return;
      }

      const defaults = definition.defaults || {};
      const advanced = definition.advanced || {};
      const supportsOverride = Boolean(advanced.supports_command_override);
      const supportsOllamaModel = definition.id === "ollama";
      const availableOllamaModels = Array.isArray(definition.ollama_available_models) ? definition.ollama_available_models : [];
      const recommendedOllamaModel = definition.ollama_recommended_model || defaults.model || "";
      advancedLaunch.style.display = supportsOverride ? "grid" : "none";
      execGroup.style.display = supportsOverride ? "grid" : "none";
      workdirGroup.style.display = supportsOverride ? "grid" : "none";
      ollamaModelGroup.style.display = supportsOllamaModel ? "grid" : "none";
      submitButton.disabled = !definition.launchable;

      execInput.placeholder = defaults.exec_command || execInput.placeholder;
      workdirInput.placeholder = defaults.workdir || workdirInput.placeholder;
      ollamaModelInput.placeholder = "gemma4:latest";
      ollamaModelOptions.innerHTML = availableOllamaModels
        .map((item) => `<option value="${escapeHtml(item)}"></option>`)
        .join("");

      if (supportsOverride) {
        execInput.value = defaults.exec_command || "";
        workdirInput.value = defaults.workdir || "";
      }
      if (supportsOllamaModel) {
        ollamaModelInput.value = ollamaModelInput.value || recommendedOllamaModel || "";
        ollamaModelCaption.style.display = "block";
        ollamaModelCaption.textContent = definition.ollama_summary
          || (availableOllamaModels.length
            ? `Installed models: ${availableOllamaModels.join(", ")}`
            : "Gateway could not verify local Ollama models yet.");
      }
      if (!supportsOverride) {
        execInput.value = "";
        workdirInput.value = "";
      }
      if (!supportsOllamaModel) {
        ollamaModelInput.value = "";
        ollamaModelCaption.textContent = "";
        ollamaModelCaption.style.display = "none";
        ollamaModelOptions.innerHTML = "";
      }

      agentNameInput.placeholder = definition.suggested_name || agentNameInput.placeholder;

      const whatYouNeed = (definition.what_you_need || []).length
        ? `<div><strong>What you'll need</strong>${definition.what_you_need.map((note) => `<div>${escapeHtml(note)}</div>`).join("")}</div>`
        : `<div><strong>What you'll need</strong><div>Nothing extra. This one is ready to go.</div></div>`;
      const launchMode = definition.launchable ? "ready to add" : "coming soon";
      const recommendedTest = definition.recommended_test_message
        ? `<div><strong>Recommended test</strong><div>${escapeHtml(definition.recommended_test_message)}</div></div>`
        : "";
      const setupSkill = definition.setup_skill
        ? `<div><strong>Setup skill</strong><div>${escapeHtml(definition.setup_skill)} · ${escapeHtml(definition.setup_skill_path || "")}</div></div>`
        : "";

      help.innerHTML = `
        <h3>${escapeHtml(definition.label)}</h3>
        <p>${escapeHtml(definition.description || "")}</p>
        <div class="signal-grid">
          <div><strong>Type</strong>${escapeHtml(definition.asset_type_label || "-")}</div>
          <div><strong>Output</strong>${escapeHtml(definition.output_label || "-")}</div>
          <div><strong>Intake</strong>${escapeHtml(definition.intake_model || "-")}</div>
          <div><strong>Telemetry</strong>${escapeHtml(definition.telemetry_shape || "-")}</div>
          <div><strong>Why pick this</strong>${escapeHtml(definition.operator_summary || "-")}</div>
          <div><strong>Status</strong>${escapeHtml(definition.availability || "-")} · ${escapeHtml(launchMode)}</div>
          <div><strong>Model</strong>${escapeHtml(definition.id === "ollama" ? (definition.ollama_summary || "Use Ollama Model to pick a local model.") : "-")}</div>
          <div><strong>Delivery</strong>${escapeHtml(definition.signals?.delivery || "-")}</div>
          <div><strong>Liveness</strong>${escapeHtml(definition.signals?.liveness || "-")}</div>
          <div><strong>Activity</strong>${escapeHtml(definition.signals?.activity || "-")}</div>
          <div><strong>Tools</strong>${escapeHtml(definition.signals?.tools || "-")}</div>
          ${setupSkill}
          ${recommendedTest}
          ${whatYouNeed}
        </div>
      `;
    }

    async function loadConnectorProviders() {
      const payload = await apiRequest("/api/connectors/providers");
      connectorProviders = payload.providers || [];
      const select = document.getElementById("connector-provider");
      if (!connectorProviders.length) {
        select.innerHTML = `<option value="composio">Composio</option>`;
        return;
      }
      select.innerHTML = connectorProviders.map((item) => (
        `<option value="${escapeHtml(item.name)}">${escapeHtml(item.display_name || item.name)}</option>`
      )).join("");
    }

    function renderConnectors(payload) {
      const connectors = payload.connectors || [];
      const tbody = document.getElementById("connector-rows");
      document.getElementById("connectors-summary").textContent = `${payload.enabled_count ?? 0} enabled / ${payload.count ?? 0} total`;
      if (!connectors.length) {
        tbody.innerHTML = `<tr><td colspan="4"><div class="empty">No connectors registered.</div></td></tr>`;
        return;
      }
      tbody.innerHTML = connectors.map((connector) => {
        const active = selectedConnector && selectedConnector.toLowerCase() === String(connector.name || "").toLowerCase();
        const auth = connector.auth_status || {};
        const authLabel = auth.exists ? (auth.keys?.length ? auth.keys.join(", ") : "empty") : "not configured";
        return `
          <tr>
            <td colspan="4">
              <button class="agent-button ${active ? "is-active" : ""}" data-connector-name="${escapeHtml(connector.name || "")}">
                <table><tbody><tr>
                  <td style="width:28%"><div class="agent-name">${escapeHtml(connector.name || "-")}</div></td>
                  <td style="width:22%">${escapeHtml(connector.provider || "-")}</td>
                  <td style="width:18%"><span class="status-pill ${connector.enabled ? "status-live" : "status-offline"}">${connector.enabled ? "enabled" : "disabled"}</span></td>
                  <td style="width:32%" class="agent-meta">${escapeHtml(authLabel)}</td>
                </tr></tbody></table>
              </button>
            </td>
          </tr>
        `;
      }).join("");
    }

    function renderConnectorDetail(detail) {
      const panel = document.getElementById("connector-detail");
      const chip = document.getElementById("selected-connector-chip");
      if (!detail || !detail.connector) {
        chip.textContent = "select a connector";
        panel.innerHTML = `<div class="empty">Choose a connector to manage auth, apps, and enablement.</div>`;
        return;
      }
      const connector = detail.connector;
      chip.textContent = connector.name;
      const auth = connector.auth_status || {};
      const configEntries = Object.entries(connector.config || {});
      const configHtml = configEntries.length
        ? configEntries.map(([key, value]) => `<div><strong>${escapeHtml(key)}</strong> ${escapeHtml(String(value ?? "-"))}</div>`).join("")
        : `<div class="caption">No config overrides.</div>`;
      panel.innerHTML = `
        <div class="detail-card">
          <div>
            <div class="agent-name">${escapeHtml(connector.name)}</div>
            <div class="agent-meta">${escapeHtml(connector.provider)} · id ${escapeHtml(connector.id || "-")}</div>
          </div>
          <div class="runtime-info">
            <h3>Auth status</h3>
            <div>${auth.exists ? `Keys: ${escapeHtml((auth.keys || []).join(", ") || "(empty)")}` : "Not configured"}</div>
            <div>Permissions: ${escapeHtml(auth.permissions || "-")}</div>
          </div>
          <div class="runtime-info">
            <h3>Config</h3>
            ${configHtml}
          </div>
          <form id="connector-auth-form" class="detail-card">
            <div class="control-group">
              <label for="connector-api-key">COMPOSIO_API_KEY</label>
              <input id="connector-api-key" name="COMPOSIO_API_KEY" type="password" autocomplete="off" placeholder="ak_..." />
            </div>
            <div class="action-row">
              <button type="submit">Save Auth</button>
              <button type="button" class="ghost" data-connector-action="clear-auth" data-connector-name="${escapeHtml(connector.name)}">Clear Auth</button>
            </div>
          </form>
          <form id="connector-connect-form" class="detail-card">
            <div class="control-group">
              <label for="connector-app">Connect app (OAuth)</label>
              <input id="connector-app" name="app" placeholder="gmail, slack, github" />
            </div>
            <div class="action-row">
              <button type="submit">Start Connect</button>
              <button type="button" class="ghost" data-connector-action="list-apps" data-connector-name="${escapeHtml(connector.name)}">List Apps</button>
            </div>
          </form>
          <div id="connector-action-flash" class="flash"></div>
          <div id="connector-apps-panel" class="runtime-info" style="display:none;"></div>
          <div class="action-row">
            <button type="button" data-connector-action="${connector.enabled ? "disable" : "enable"}" data-connector-name="${escapeHtml(connector.name)}">${connector.enabled ? "Disable" : "Enable"}</button>
            <button type="button" class="danger" data-connector-action="remove" data-connector-name="${escapeHtml(connector.name)}">Remove</button>
          </div>
        </div>
      `;
    }

    async function loadConnectorDetail(name) {
      try {
        const payload = await apiRequest(`/api/connectors/${encodeURIComponent(name)}`);
        renderConnectorDetail(payload);
      } catch {
        renderConnectorDetail(null);
      }
    }

    async function loadConnectors() {
      const payload = await apiRequest("/api/connectors");
      renderConnectors(payload);
      if (!selectedConnector && payload.connectors?.length) {
        selectedConnector = payload.connectors[0].name;
      }
      if (selectedConnector) {
        await loadConnectorDetail(selectedConnector);
      } else {
        renderConnectorDetail(null);
      }
    }

    async function loadTemplates() {
      const payload = await apiRequest("/api/templates");
      agentTemplates = payload.templates || [];
      renderTemplateOptions();
      renderTemplateHelp(document.getElementById("agent-type").value || "echo_test");
    }

    function renderOverview(payload) {
      const gateway = payload.gateway || {};
      const overview = document.getElementById("overview");
      overview.innerHTML = `
        <div class="meta-chip"><span>Gateway</span><strong>${escapeHtml(String(gateway.gateway_id || "-").slice(0, 8))}</strong></div>
        <div class="meta-chip"><span>Daemon</span><strong>${payload.daemon?.running ? "running" : "stopped"}</strong></div>
        <div class="meta-chip"><span>Base URL</span><strong>${escapeHtml(payload.base_url || "-")}</strong></div>
        <div class="meta-chip"><span>User</span><strong>${escapeHtml(payload.user || "-")}</strong></div>
        <div class="meta-chip"><span>Space</span><strong>${escapeHtml(payload.space_name || payload.space_id || "-")}</strong></div>
      `;
    }

    function renderMetrics(payload) {
      const agents = payload.agents || [];
      const summary = payload.summary || {};
      const queueDepth = agents.reduce((sum, agent) => sum + Number(agent.backlog_depth || 0), 0);
      const metrics = [
        ["managed agents", summary.managed_agents ?? 0, "cyan"],
        ["connectors", `${payload.enabled_connectors ?? 0}/${payload.connectors_count ?? 0}`, "green"],
        ["live", summary.live_agents ?? 0, "green"],
        ["on-demand", summary.on_demand_agents ?? 0, "blue"],
        ["inbox", summary.inbox_agents ?? 0, "cyan"],
        ["pending approvals", summary.pending_approvals ?? 0, "yellow"],
        ["low confidence", summary.low_confidence_agents ?? 0, "yellow"],
        ["blocked", summary.blocked_agents ?? 0, "red"],
        ["queue depth", queueDepth, "blue"],
      ];
      document.getElementById("metrics").innerHTML = metrics.map(([label, value, tone]) => `
        <article class="metric ${tone}">
          <strong>${escapeHtml(value)}</strong>
          <span>${escapeHtml(label)}</span>
        </article>
      `).join("");
    }

    function renderAlerts(payload) {
      const alerts = payload.alerts || [];
      document.getElementById("alert-summary").textContent = alerts.length
        ? `${alerts.length} active alert${alerts.length === 1 ? "" : "s"}`
        : "all clear";
      const feed = document.getElementById("alerts-feed");
      if (!alerts.length) {
        feed.innerHTML = `<div class="empty">No active Gateway alerts.</div>`;
        return;
      }
      feed.innerHTML = `<div class="alerts-list">${
        alerts.map((item) => `
          <div class="alert-card ${escapeHtml(item.severity || "info")}">
            <div class="alert-head">
              <span>${escapeHtml(item.severity || "info")}</span>
              <span>${escapeHtml(item.agent_name ? "@" + item.agent_name : "gateway")}</span>
            </div>
            <div><strong>${escapeHtml(item.title || "-")}</strong></div>
            <div class="alert-body">${escapeHtml(item.detail || "-")}</div>
          </div>
        `).join("")
      }</div>`;
    }

    function renderAgents(payload) {
      const agents = payload.agents || [];
      const tbody = document.getElementById("agent-rows");
      document.getElementById("managed-summary").textContent = `${agents.length} managed agent${agents.length === 1 ? "" : "s"}`;
      if (!agents.length) {
        tbody.innerHTML = `<tr><td colspan="9"><div class="empty">No managed agents yet.</div></td></tr>`;
        return;
      }
      tbody.innerHTML = agents.map((agent) => {
        const activity = agent.current_activity || agent.confidence_detail || agent.current_tool || agent.last_reply_preview || "-";
        const active = selectedAgent && selectedAgent.toLowerCase() === String(agent.name || "").toLowerCase();
        return `
          <tr>
            <td colspan="8">
              <button class="agent-button ${active ? "is-active" : ""}" data-agent-name="${escapeHtml(agent.name || "")}">
                <table>
                  <tbody>
                    <tr>
                      <td style="width:16%">
                        <div class="agent-name">@${escapeHtml(agent.name || "-")}</div>
                        <div class="agent-meta">${escapeHtml(agent.template_label || agent.runtime_type || "-")}</div>
                      </td>
                      <td style="width:12%">${escapeHtml(agent.asset_type_label || "Connected Asset")}</td>
                      <td style="width:8%"><span class="status-pill ${stateClass(agent.mode)}">${escapeHtml(agent.mode || "ON-DEMAND")}</span></td>
                      <td style="width:8%"><span class="status-pill ${stateClass(agent.presence)}">${escapeHtml(agent.presence || "OFFLINE")}</span></td>
                      <td style="width:8%">${escapeHtml(agent.output_label || agent.reply || "Reply")}</td>
                      <td style="width:10%"><span class="status-pill ${stateClass(agent.confidence)}">${escapeHtml(agent.confidence || "MEDIUM")}</span></td>
                      <td style="width:10%">${escapeHtml(agent.acting_agent_name || agent.name || "-")}</td>
                      <td style="width:12%">${escapeHtml(agent.active_space_name || agent.active_space_id || agent.space_id || "-")}</td>
                      <td style="width:6%">${escapeHtml(agent.backlog_depth || 0)}</td>
                      <td style="width:8%">${escapeHtml(formatAge(agent.last_seen_age_seconds))}</td>
                      <td style="width:22%">${escapeHtml(activity)}</td>
                    </tr>
                  </tbody>
                </table>
              </button>
            </td>
          </tr>
        `;
      }).join("");
    }

    function renderActivity(payload) {
      const activity = payload.recent_activity || [];
      const feed = document.getElementById("activity-feed");
      if (!activity.length) {
        feed.innerHTML = `<div class="empty">No recent Gateway activity.</div>`;
        return;
      }
      feed.innerHTML = `<div class="event-list">${
        activity.map((item) => `
          <div class="event-item">
            <div class="event-head">
              <span>${escapeHtml(item.event || "-")}</span>
              <span>${escapeHtml(formatAge(item.ts ? Math.max(0, ((Date.now() - Date.parse(item.ts)) / 1000)) : null))}</span>
            </div>
            <div class="event-detail">@${escapeHtml(item.agent_name || "-")} · ${escapeHtml(detailText(item))}</div>
          </div>
        `).join("")
      }</div>`;
    }

    function renderAgentDetail(detail) {
      const panel = document.getElementById("agent-detail");
      const chip = document.getElementById("selected-agent-chip");
      const sendChip = document.getElementById("quick-send-chip");
      if (!detail || !detail.agent) {
        chip.textContent = "select an agent";
        sendChip.textContent = "select an agent";
        panel.innerHTML = `<div class="empty">Choose a managed agent to inspect live detail.</div>`;
        return;
      }
      const agent = detail.agent;
      chip.textContent = `@${agent.name}`;
      sendChip.textContent = `custom send as @${agent.name}`;
      const events = detail.recent_activity || [];
      const lastReply = escapeHtml(agent.last_reply_preview || "-");
      const lastReplyCopy = encodeURIComponent(String(agent.last_reply_preview || "-"));
      panel.innerHTML = `
        <div class="detail-card">
          <div>
            <div class="agent-name">@${escapeHtml(agent.name || "-")}</div>
            <div class="caption">${escapeHtml(agent.asset_type_label || "Connected Asset")} · ${escapeHtml(agent.template_label || agent.runtime_type || "-")} · ${escapeHtml(agent.transport || "-")}</div>
          </div>
          <div class="action-row">
            <button type="button" class="ghost" data-agent-action="edit" data-agent-name="${escapeHtml(agent.name || "")}">Edit Setup</button>
            <button type="button" data-agent-action="test" data-agent-name="${escapeHtml(agent.name || "")}">Send Agent Test</button>
            <button type="button" data-agent-action="doctor" data-agent-name="${escapeHtml(agent.name || "")}">Doctor</button>
            <button type="button" data-agent-action="start" data-agent-name="${escapeHtml(agent.name || "")}">Start</button>
            <button type="button" data-agent-action="stop" data-agent-name="${escapeHtml(agent.name || "")}">Stop</button>
            <button type="button" class="danger" data-agent-action="remove" data-agent-name="${escapeHtml(agent.name || "")}">Remove</button>
          </div>
          <div id="detail-flash" class="flash"></div>
          <dl class="detail-list">
            <div><dt>Type</dt><dd>${escapeHtml(agent.asset_type_label || "-")}</dd></div>
            <div><dt>Template</dt><dd>${escapeHtml(agent.template_label || agent.runtime_type || "-")}</dd></div>
            <div><dt>Mode</dt><dd>${escapeHtml(agent.mode || "-")}</dd></div>
            <div><dt>Presence</dt><dd>${escapeHtml(agent.presence || "-")}</dd></div>
            <div><dt>Output</dt><dd>${escapeHtml(agent.output_label || agent.reply || "-")}</dd></div>
            <div><dt>Confidence</dt><dd>${escapeHtml(agent.confidence || "-")}</dd></div>
            <div><dt>Asset Class</dt><dd>${escapeHtml(agent.asset_class || "-")}</dd></div>
            <div><dt>Intake</dt><dd>${escapeHtml(agent.intake_model || "-")}</dd></div>
            <div><dt>Trigger</dt><dd>${escapeHtml((agent.trigger_sources || [])[0] || "-")}</dd></div>
            <div><dt>Return</dt><dd>${escapeHtml((agent.return_paths || [])[0] || "-")}</dd></div>
            <div><dt>Telemetry</dt><dd>${escapeHtml(agent.telemetry_shape || "-")}</dd></div>
            <div><dt>Runtime Model</dt><dd>${escapeHtml(agent.model || "-")}</dd></div>
            <div><dt>Attestation</dt><dd>${escapeHtml(agent.attestation_state || "-")}</dd></div>
            <div><dt>Approval</dt><dd>${escapeHtml(agent.approval_state || "-")}</dd></div>
            <div><dt>Acting As</dt><dd>${escapeHtml(agent.acting_agent_name || "-")}</dd></div>
            <div><dt>Identity Status</dt><dd>${escapeHtml(agent.identity_status || "-")}</dd></div>
            <div><dt>Environment</dt><dd>${escapeHtml(agent.environment_label || agent.base_url || "-")}</dd></div>
            <div><dt>Environment Status</dt><dd>${escapeHtml(agent.environment_status || "-")}</dd></div>
            <div><dt>Current Space</dt><dd>${escapeHtml(agent.active_space_name || agent.active_space_id || "-")}</dd></div>
            <div><dt>Space Status</dt><dd>${escapeHtml(agent.space_status || "-")}</dd></div>
            <div><dt>Default Space</dt><dd>${escapeHtml(agent.default_space_name || agent.default_space_id || "-")}</dd></div>
            <div><dt>Allowed Spaces</dt><dd>${escapeHtml(agent.allowed_space_count || 0)}</dd></div>
            <div><dt>Install</dt><dd>${escapeHtml(agent.install_id || "-")}</dd></div>
            <div><dt>Runtime Instance</dt><dd>${escapeHtml(agent.runtime_instance_id || "-")}</dd></div>
            <div><dt>Reachability</dt><dd>${escapeHtml(agent.reachability || "-")}</dd></div>
            <div><dt>Reason</dt><dd>${escapeHtml(agent.confidence_reason || "-")}</dd></div>
            <div><dt>Confidence Detail</dt><dd>${escapeHtml(agent.confidence_detail || "-")}</dd></div>
            <div><dt>Queue</dt><dd>${escapeHtml(agent.backlog_depth || 0)}</dd></div>
            <div><dt>Seen</dt><dd>${escapeHtml(formatAge(agent.last_seen_age_seconds))}</dd></div>
            <div><dt>Phase</dt><dd>${escapeHtml(agent.current_status || "-")}</dd></div>
            <div><dt>Activity</dt><dd>${escapeHtml(agent.current_activity || "-")}</dd></div>
            <div><dt>Processed</dt><dd>${escapeHtml(agent.processed_count || 0)}</dd></div>
            <div class="copyable-block">
              <dt>Last Reply</dt>
              <dd><pre>${lastReply}</pre></dd>
              <button type="button" class="ghost" data-copy-text="${lastReplyCopy}">Copy</button>
            </div>
            <div><dt>Last Error</dt><dd>${escapeHtml(agent.last_error || "-")}</dd></div>
            <div><dt>Doctor</dt><dd>${escapeHtml(agent.last_successful_doctor_at || "-")}</dd></div>
            <div><dt>Doctor Result</dt><dd>${escapeHtml(agent.last_doctor_result?.status || "-")}</dd></div>
            <div><dt>Effective</dt><dd>${escapeHtml(agent.effective_state || "-")}</dd></div>
            <div><dt>Workdir</dt><dd>${escapeHtml(agent.workdir || "-")}</dd></div>
            <div><dt>Exec</dt><dd>${escapeHtml(agent.exec_command || "-")}</dd></div>
          </dl>
          <div>
            <div class="panel-header" style="padding:0 0 12px;"><span>Recent Agent Activity</span></div>
            ${
              events.length
                ? `<div class="event-list">${
                    events.map((item) => `
                      <div class="event-item">
                        <div class="event-head">
                          <span>${escapeHtml(item.event || "-")}</span>
                          <span>${escapeHtml(formatAge(item.ts ? Math.max(0, ((Date.now() - Date.parse(item.ts)) / 1000)) : null))}</span>
                        </div>
                        <div class="event-detail">${escapeHtml(detailText(item))}</div>
                      </div>
                    `).join("")
                  }</div>`
                : `<div class="empty">No recent agent activity yet.</div>`
            }
          </div>
        </div>
      `;
    }

    async function loadStatus() {
      const payload = await apiRequest("/api/status");
      renderOverview(payload);
      renderMetrics(payload);
      renderAlerts(payload);
      renderAgents(payload);
      renderActivity(payload);
      if (!selectedAgent && payload.agents?.length) {
        selectedAgent = payload.agents[0].name;
      }
      if (selectedAgent) {
        await loadAgentDetail(selectedAgent);
      } else {
        renderAgentDetail(null);
      }
    }

    async function loadAgentDetail(name) {
      try {
        const payload = await apiRequest(`/api/agents/${encodeURIComponent(name)}`);
        renderAgentDetail(payload);
      } catch {
        renderAgentDetail(null);
      }
    }

    async function tick(force = false) {
      if (!force && autoRefreshPaused) {
        return;
      }
      const selection = window.getSelection ? String(window.getSelection() || "") : "";
      if (!force && selection.trim()) {
        return;
      }
      const active = document.activeElement;
      if (!force && active && ["INPUT", "TEXTAREA", "SELECT"].includes(active.tagName)) {
        return;
      }
      try {
        await loadStatus();
        await loadConnectors();
      } catch (error) {
        document.getElementById("activity-feed").innerHTML = `<div class="empty">Gateway UI lost contact with the local status API: ${escapeHtml(error.message || error)}</div>`;
      }
    }

    document.addEventListener("click", (event) => {
      const button = event.target.closest("[data-connector-name]");
      if (!button) return;
      if (button.hasAttribute("data-connector-action")) return;
      selectedConnector = button.getAttribute("data-connector-name");
      tick();
    });

    document.addEventListener("click", async (event) => {
      const button = event.target.closest("[data-connector-action]");
      if (!button) return;
      const action = button.getAttribute("data-connector-action");
      const connectorName = button.getAttribute("data-connector-name");
      try {
        if (action === "remove") {
          await apiRequest(`/api/connectors/${encodeURIComponent(connectorName)}`, { method: "DELETE" });
          selectedConnector = null;
        } else if (action === "clear-auth") {
          await apiRequest(`/api/connectors/${encodeURIComponent(connectorName)}/auth`, { method: "DELETE" });
        } else if (action === "enable" || action === "disable") {
          await apiRequest(`/api/connectors/${encodeURIComponent(connectorName)}`, {
            method: "PUT",
            body: JSON.stringify({ enabled: action === "enable" }),
          });
        } else if (action === "list-apps") {
          const result = await apiRequest(`/api/connectors/${encodeURIComponent(connectorName)}/apps`);
          const panel = document.getElementById("connector-apps-panel");
          if (!result.apps?.length) {
            panel.style.display = "block";
            panel.innerHTML = `<h3>Connected apps</h3><div class="caption">No connected apps yet.</div>`;
          } else {
            panel.style.display = "block";
            panel.innerHTML = `<h3>Connected apps</h3>${result.apps.map((item) => (
              `<div><strong>${escapeHtml(item.app || "?")}</strong> · ${escapeHtml(item.status || "?")}</div>`
            )).join("")}`;
          }
          setFlash("connector-action-flash", `Loaded ${result.count || 0} app(s).`, "success");
          return;
        }
        selectedConnector = connectorName;
        setFlash("connector-action-flash", `${action} completed for ${connectorName}`, "success");
        await tick(true);
      } catch (error) {
        setFlash("connector-action-flash", error.message || String(error), "error");
      }
    });

    document.getElementById("add-connector-form").addEventListener("submit", async (event) => {
      event.preventDefault();
      const form = event.currentTarget;
      const data = new FormData(form);
      const payload = {
        name: String(data.get("name") || "").trim(),
        provider: String(data.get("provider") || "composio").trim(),
        managed_auth: true,
      };
      try {
        const result = await apiRequest("/api/connectors", {
          method: "POST",
          body: JSON.stringify(payload),
        });
        selectedConnector = result.connector?.name || payload.name;
        form.reset();
        if (connectorProviders.length) {
          document.getElementById("connector-provider").value = connectorProviders[0].name;
        }
        setFlash("add-connector-flash", `Added connector ${selectedConnector}`, "success");
        await tick(true);
      } catch (error) {
        setFlash("add-connector-flash", error.message || String(error), "error");
      }
    });

    document.addEventListener("submit", async (event) => {
      if (event.target.id !== "connector-auth-form" && event.target.id !== "connector-connect-form") {
        return;
      }
      event.preventDefault();
      const form = event.target;
      const connectorName = selectedConnector;
      if (!connectorName) {
        setFlash("connector-action-flash", "Select a connector first.", "error");
        return;
      }
      try {
        if (form.id === "connector-auth-form") {
          const apiKey = String(form.COMPOSIO_API_KEY.value || "").trim();
          if (!apiKey) {
            setFlash("connector-action-flash", "COMPOSIO_API_KEY is required.", "error");
            return;
          }
          await apiRequest(`/api/connectors/${encodeURIComponent(connectorName)}/auth`, {
            method: "POST",
            body: JSON.stringify({ COMPOSIO_API_KEY: apiKey }),
          });
          form.COMPOSIO_API_KEY.value = "";
          setFlash("connector-action-flash", "Auth saved (key names only shown in UI).", "success");
        } else {
          const app = String(form.app.value || "").trim();
          if (!app) {
            setFlash("connector-action-flash", "App name is required.", "error");
            return;
          }
          const result = await apiRequest(`/api/connectors/${encodeURIComponent(connectorName)}/connect`, {
            method: "POST",
            body: JSON.stringify({ app }),
          });
          if (result.redirect_url) {
            setFlash("connector-action-flash", `Open OAuth URL: ${result.redirect_url}`, "success");
            window.open(result.redirect_url, "_blank", "noopener,noreferrer");
          } else {
            setFlash("connector-action-flash", `Connection status: ${result.connection_status || "unknown"}`, "success");
          }
        }
        await tick(true);
      } catch (error) {
        setFlash("connector-action-flash", error.message || String(error), "error");
      }
    });

    document.addEventListener("click", (event) => {
      const button = event.target.closest("[data-agent-name]");
      if (!button) return;
      if (button.hasAttribute("data-agent-action")) return;
      selectedAgent = button.getAttribute("data-agent-name");
      tick();
    });

    document.addEventListener("click", async (event) => {
      const copyButton = event.target.closest("[data-copy-text]");
      if (copyButton) {
        const text = decodeURIComponent(copyButton.getAttribute("data-copy-text") || "");
        try {
          await navigator.clipboard.writeText(text);
          setFlash("detail-flash", "Copied last reply.", "success");
        } catch {
          setFlash("detail-flash", "Could not copy to clipboard.", "warning");
        }
        return;
      }
      const button = event.target.closest("[data-agent-action]");
      if (!button) return;
      const action = button.getAttribute("data-agent-action");
      const agentName = button.getAttribute("data-agent-name");
      try {
        if (action === "edit") {
          await loadAgentIntoSetupForm(agentName);
        } else if (action === "remove") {
          await apiRequest(`/api/agents/${encodeURIComponent(agentName)}`, { method: "DELETE" });
          selectedAgent = null;
        } else if (action === "doctor") {
          const result = await apiRequest(`/api/agents/${encodeURIComponent(agentName)}/doctor`, { method: "POST", body: "{}" });
          selectedAgent = agentName;
          setFlash("detail-flash", `Doctor ${result.status} for @${agentName}`, result.status === "failed" ? "error" : (result.status === "warning" ? "warning" : "success"));
        } else if (action === "test") {
          const result = await apiRequest(`/api/agents/${encodeURIComponent(agentName)}/test`, { method: "POST", body: "{}" });
          selectedAgent = agentName;
          setFlash("detail-flash", `Test sent to @${result.target_agent}`, "success");
        } else {
          await apiRequest(`/api/agents/${encodeURIComponent(agentName)}/${action}`, { method: "POST", body: "{}" });
          selectedAgent = agentName;
          setFlash("detail-flash", `${action} requested for @${agentName}`, "success");
        }
        await tick(true);
      } catch (error) {
        setFlash("detail-flash", error.message || String(error), "error");
      }
    });

    document.getElementById("refresh-toggle").addEventListener("click", () => {
      autoRefreshPaused = !autoRefreshPaused;
      refreshButtonLabel();
      if (!autoRefreshPaused) {
        tick(true);
      }
    });

    document.getElementById("agent-type").addEventListener("change", (event) => {
      renderTemplateHelp(event.target.value);
    });

    document.getElementById("add-agent-cancel").addEventListener("click", () => {
      resetSetupForm();
      setFlash("add-agent-flash", "Setup form reset.", "warning");
    });

    document.getElementById("add-agent-form").addEventListener("submit", async (event) => {
      event.preventDefault();
      const form = event.currentTarget;
      const data = new FormData(form);
      const payload = {
        name: String(data.get("name") || "").trim(),
        template_id: String(data.get("template_id") || "echo_test"),
        exec_command: String(data.get("exec_command") || "").trim(),
        workdir: String(data.get("workdir") || "").trim(),
        model: String(data.get("model") || "").trim(),
        start: true,
      };
      try {
        const updateMode = setupMode === "update" && setupTarget;
        const result = await apiRequest(
          updateMode ? `/api/agents/${encodeURIComponent(setupTarget)}` : "/api/agents",
          {
            method: updateMode ? "PUT" : "POST",
            body: JSON.stringify(payload),
          },
        );
        setFlash("add-agent-flash", `${updateMode ? "Updated" : "Added"} @${result.name}`, "success");
        selectedAgent = result.name;
        resetSetupForm();
        await tick();
      } catch (error) {
        setFlash("add-agent-flash", error.message || String(error), "error");
      }
    });

    document.getElementById("send-form").addEventListener("submit", async (event) => {
      event.preventDefault();
      if (!selectedAgent) {
        setFlash("send-flash", "Select a managed agent first.", "error");
        return;
      }
      const form = event.currentTarget;
      const data = new FormData(form);
      const payload = {
        to: String(data.get("to") || "").trim(),
        parent_id: String(data.get("parent_id") || "").trim(),
        content: String(data.get("content") || "").trim(),
      };
      try {
        const result = await apiRequest(`/api/agents/${encodeURIComponent(selectedAgent)}/send`, {
          method: "POST",
          body: JSON.stringify(payload),
        });
        setFlash("send-flash", `Sent as @${result.agent}`, "success");
        form.content.value = "";
        await tick(true);
      } catch (error) {
        setFlash("send-flash", error.message || String(error), "error");
      }
    });

    async function boot() {
      try {
        await Promise.all([loadTemplates(), loadConnectorProviders()]);
      } catch (error) {
        setFlash("add-agent-flash", error.message || String(error), "error");
      }
      applySetupMode();
      refreshButtonLabel();
      await tick(true);
      window.setInterval(tick, refreshMs);
    }

    boot();
  </script>
</body>
</html>
"""
    from ax_cli import __version__

    return template.replace("__REFRESH_MS__", str(refresh_ms)).replace("__VERSION__", __version__)


_DEMO_HTML_PATH = Path(__file__).resolve().parent.parent / "static" / "demo.html"

# Brand-mark favicon. Same connected-agent node mark as the topbar brand chip
# so the browser tab matches what users see on the page.
# Inline so no separate static asset is needed; served at /favicon.svg.
_GATEWAY_FAVICON_SVG = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 40 40">
  <defs>
    <linearGradient id="g" x1="0" y1="0" x2="1" y2="1">
      <stop offset="0%" stop-color="#5eead4"/>
      <stop offset="100%" stop-color="#7dd3fc"/>
    </linearGradient>
  </defs>
  <rect width="40" height="40" rx="13" fill="url(#g)"/>
  <path d="M12 12 20 20 12 28M20 20h8" stroke="#062018" stroke-width="3" stroke-linecap="round" stroke-linejoin="round" opacity="0.7"/>
  <circle cx="12" cy="12" r="4.1" fill="#062018"/>
  <circle cx="12" cy="28" r="4.1" fill="#062018"/>
  <circle cx="28" cy="20" r="4.1" fill="#062018"/>
  <circle cx="20" cy="20" r="5.2" fill="#062018"/>
  <circle cx="12" cy="12" r="1.25" fill="#a7fff1"/>
  <circle cx="12" cy="28" r="1.25" fill="#a7fff1"/>
  <circle cx="28" cy="20" r="1.25" fill="#a7fff1"/>
  <circle cx="20" cy="20" r="1.8" fill="#a7fff1"/>
</svg>
""".strip()


def _render_gateway_demo_page(*, refresh_ms: int) -> str:
    from ax_cli import __version__

    body = _DEMO_HTML_PATH.read_text(encoding="utf-8")
    inject = (
        f"<script>window.__GATEWAY_DEMO_REFRESH_MS__ = {int(refresh_ms)};"
        f"window.__AXCTL_VERSION__ = {__version__!r};</script></head>"
    )
    return body.replace("</head>", inject, 1)


class _GatewayUiServer(ThreadingHTTPServer):
    allow_reuse_address = True
    daemon_threads = True


def _offline_auth_exchange(handler: BaseHTTPRequestHandler, body: dict) -> None:
    """POST /auth/exchange — return a fake JWT that encodes the agent name."""
    from ..offline_sse import make_token

    agent_name = str(body.get("agent_name") or "").strip()
    if not agent_name:
        auth = str(handler.headers.get("Authorization") or "").removeprefix("Bearer ").strip()
        registry = load_gateway_registry()
        for entry in registry.get("agents", []):
            path = agent_token_path(str(entry.get("name") or ""))
            try:
                if path.exists() and path.read_text().strip() == auth:
                    agent_name = str(entry.get("name") or "")
                    break
            except OSError:
                pass
    if not agent_name:
        _write_json_response(handler, {"error": "agent not found"}, status=HTTPStatus.UNAUTHORIZED)
        return
    _write_json_response(handler, {"access_token": make_token(agent_name), "token_type": "bearer"})


def _offline_replies_path() -> Path:
    return gateway_dir() / "offline-replies.jsonl"


def _offline_mode_lock_path() -> Path:
    """Path to the marker file written by `ax gateway run` when AX_OFFLINE=1.

    Existence of this file is the source of truth for `ax gateway status` to
    decide whether to render the OFFLINE indicator. The status command runs in
    a separate process from the daemon, so it cannot rely on the daemon's
    environment; the file bridges that gap.
    """
    return gateway_dir() / "offline-mode.lock"


def _write_offline_mode_lock() -> None:
    """Write the offline-mode marker so `ax gateway status` can see the state."""
    path = _offline_mode_lock_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(str(os.getpid()))
    except OSError:
        pass


def _clear_offline_mode_lock() -> None:
    """Remove the offline-mode marker on daemon shutdown."""
    try:
        _offline_mode_lock_path().unlink(missing_ok=True)
    except OSError:
        pass


def _is_offline_mode_active() -> bool:
    """Return True if the running daemon was started in offline mode."""
    return _offline_mode_lock_path().exists()


def _offline_message_post(handler: BaseHTTPRequestHandler, body: dict) -> None:
    """POST /api/v1/messages — deliver to the mentioned agent's queue."""
    from ..offline_sse import OfflineAgentQueues, extract_mentions

    message = {
        "id": str(uuid.uuid4()),
        "content": str(body.get("content") or ""),
        "space_id": str(body.get("space_id") or "00000000-0000-0000-0000-000000000001"),
        "channel": str(body.get("channel") or "main"),
        "author": body.get("author") or "offline-user",
    }
    if body.get("parent_id"):
        message["parent_id"] = str(body["parent_id"])
    mentioned = extract_mentions(message["content"])
    bus = OfflineAgentQueues.get()
    delivered = [name for name in mentioned if bus.deliver(name, message)]
    if message["author"] != "offline-user":
        try:
            p = _offline_replies_path()
            p.parent.mkdir(parents=True, exist_ok=True)
            with p.open("a") as f:
                f.write(json.dumps(message) + "\n")
        except OSError:
            pass
    _write_json_response(
        handler,
        {"message": message, "id": message["id"], "delivered_to": delivered},
        status=HTTPStatus.CREATED,
    )


def _offline_sse_stream(handler: BaseHTTPRequestHandler, token: str) -> None:
    """GET /api/v1/sse/messages — stream messages to one specific agent."""
    import queue as _queue

    from ..offline_sse import OfflineAgentQueues, agent_name_from_token, sse_frame

    agent_name = agent_name_from_token(token)
    if not agent_name:
        _write_json_response(handler, {"error": "invalid token"}, status=HTTPStatus.UNAUTHORIZED)
        return
    bus = OfflineAgentQueues.get()
    q = bus.subscribe(agent_name)
    try:
        handler.send_response(HTTPStatus.OK.value)
        handler.send_header("Content-Type", "text/event-stream; charset=utf-8")
        handler.send_header("Cache-Control", "no-cache")
        handler.send_header("Connection", "keep-alive")
        handler.send_header("X-Accel-Buffering", "no")
        _send_security_headers(handler)
        handler.end_headers()
        handler.wfile.write(sse_frame("connected", {"agent": agent_name}))
        handler.wfile.flush()
        while True:
            try:
                message = q.get(timeout=15.0)
                if message is None:
                    break
                handler.wfile.write(sse_frame("message", message))
                handler.wfile.flush()
            except _queue.Empty:
                handler.wfile.write(sse_frame("heartbeat", {}))
                handler.wfile.flush()
    except (BrokenPipeError, ConnectionResetError):
        pass
    finally:
        bus.unsubscribe(agent_name)


def _generate_nonce() -> str:

    return secrets.token_urlsafe(16)


_CSP_STATIC = "default-src 'none'; img-src 'self'; connect-src 'self'; font-src 'none'; frame-ancestors 'none'"


def _send_security_headers(handler: BaseHTTPRequestHandler, *, nonce: str | None = None) -> None:
    handler.send_header("X-Content-Type-Options", "nosniff")
    handler.send_header("X-Frame-Options", "DENY")
    if nonce:
        csp = f"{_CSP_STATIC}; script-src 'nonce-{nonce}'; style-src 'nonce-{nonce}'"
    else:
        csp = _CSP_STATIC
    handler.send_header("Content-Security-Policy", csp)
    handler.send_header("Referrer-Policy", "no-referrer")
    handler.send_header("Permissions-Policy", "camera=(), microphone=(), geolocation=(), payment=()")
    handler.send_header("Cross-Origin-Opener-Policy", "same-origin")
    handler.send_header("Cross-Origin-Resource-Policy", "same-origin")
    handler.send_header("Cross-Origin-Embedder-Policy", "require-corp")


def _write_json_response(handler: BaseHTTPRequestHandler, payload: dict, *, status: HTTPStatus = HTTPStatus.OK) -> None:
    body = json.dumps(payload, indent=2, sort_keys=True).encode("utf-8")
    try:
        handler.send_response(status.value)
        handler.send_header("Content-Type", "application/json; charset=utf-8")
        handler.send_header("Content-Length", str(len(body)))
        handler.send_header("Cache-Control", "no-store")
        _send_security_headers(handler)
        handler.end_headers()
        handler.wfile.write(body)
    except (BrokenPipeError, ConnectionResetError):
        pass


def _write_html_response(handler: BaseHTTPRequestHandler, payload: str) -> None:
    import re

    nonce = _generate_nonce()
    # Inject nonce into <script> and <style> tags exactly once, without
    # double-applying when a tag already has other attributes.
    payload = re.sub(r"<(script|style)(?=[ >])", lambda m: f'<{m.group(1)} nonce="{nonce}"', payload)
    body = payload.encode("utf-8")
    try:
        handler.send_response(HTTPStatus.OK.value)
        handler.send_header("Content-Type", "text/html; charset=utf-8")
        handler.send_header("Content-Length", str(len(body)))
        handler.send_header("Cache-Control", "no-store")
        _send_security_headers(handler, nonce=nonce)
        handler.end_headers()
        handler.wfile.write(body)
    except (BrokenPipeError, ConnectionResetError):
        pass


def _read_json_request(handler: BaseHTTPRequestHandler) -> dict:
    content_length = int(handler.headers.get("Content-Length", "0") or 0)
    if content_length <= 0:
        return {}
    raw = handler.rfile.read(content_length)
    if not raw:
        return {}
    try:
        payload = json.loads(raw.decode("utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid JSON body: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError("JSON body must be an object.")
    return payload


_LOOPBACK_HOSTNAMES = frozenset({"localhost", "127.0.0.1"})


def _connector_api_error_status(exc: BaseException) -> HTTPStatus:
    from ..connectors.errors import (
        ConnectorAuthError,
        ConnectorNotFoundError,
        ConnectorPolicyError,
        ConnectorProviderError,
    )

    if isinstance(exc, ConnectorNotFoundError):
        return HTTPStatus.NOT_FOUND
    if isinstance(exc, ConnectorAuthError):
        return HTTPStatus.UNAUTHORIZED
    if isinstance(exc, ConnectorPolicyError):
        return HTTPStatus.FORBIDDEN
    if isinstance(exc, ConnectorProviderError):
        return HTTPStatus.BAD_GATEWAY
    return HTTPStatus.BAD_REQUEST


def _dispatch_connector_api_get(path: str) -> dict:
    from ..connectors.gateway_api import (
        connector_apps_payload,
        connector_auth_status_payload,
        connector_detail_payload,
        connectors_list_payload,
        connectors_providers_payload,
        parse_connector_api_path,
    )

    kind, ref, _ = parse_connector_api_path(path)
    if kind == "list":
        return connectors_list_payload()
    if kind == "providers":
        return connectors_providers_payload()
    if kind == "detail":
        return connector_detail_payload(ref)
    if kind == "auth":
        return connector_auth_status_payload(ref)
    if kind == "apps":
        return connector_apps_payload(ref)
    if not kind:
        raise LookupError("not found")
    raise LookupError(f"Unsupported connector GET route: {path}")


def _dispatch_connector_api_post(path: str, body: dict) -> tuple[dict, HTTPStatus]:
    from ..connectors.gateway_api import (
        connector_auth_write,
        connector_call,
        connector_connect,
        connector_create,
        connector_search,
        parse_connector_api_path,
    )

    kind, ref, _ = parse_connector_api_path(path)
    if kind == "list":
        return connector_create(body), HTTPStatus.CREATED
    if kind == "auth":
        return connector_auth_write(ref, body), HTTPStatus.OK
    if kind == "connect":
        return connector_connect(ref, body), HTTPStatus.OK
    if kind == "tools_search":
        return connector_search(ref, body), HTTPStatus.OK
    if kind == "tools_call":
        return connector_call(ref, body), HTTPStatus.OK
    raise LookupError("not found")


def _dispatch_connector_api_put(path: str, body: dict) -> dict:
    from ..connectors.gateway_api import connector_update, parse_connector_api_path

    kind, ref, _ = parse_connector_api_path(path)
    if kind == "detail":
        return connector_update(ref, body)
    raise LookupError("not found")


def _dispatch_connector_api_delete(path: str) -> dict:
    from ..connectors.gateway_api import connector_auth_clear, connector_remove, parse_connector_api_path

    kind, ref, _ = parse_connector_api_path(path)
    if kind == "detail":
        return connector_remove(ref)
    if kind == "auth":
        return connector_auth_clear(ref)
    raise LookupError("not found")


def _is_request_host_allowed(host_header: str | None) -> bool:
    # Block DNS-rebinding: only accept Host headers that resolve to loopback.
    # Port is left open so `ax gateway start --port` keeps working.
    if not host_header:
        return False
    candidate = host_header.strip()
    if not candidate:
        return False
    hostname = candidate.rsplit(":", 1)[0] if ":" in candidate else candidate
    return hostname.lower() in _LOOPBACK_HOSTNAMES


def _build_gateway_ui_handler(*, activity_limit: int, refresh_ms: int):
    class GatewayUiHandler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args) -> None:  # noqa: A003
            return

        def _reject_unauthorized_host(self) -> bool:
            if _is_request_host_allowed(self.headers.get("Host")):
                return False
            _write_json_response(
                self,
                {"error": "Forbidden: Host header is not loopback."},
                status=HTTPStatus.FORBIDDEN,
            )
            return True

        def _mark_interactive(self, parsed, *, mutation: bool) -> None:
            # Dashboard mutations are human clicks → aggressive rate-limit
            # threshold. Everything else (auto-refresh GETs, /local/* and
            # /api/v1/* programmatic agent calls) is automated. Set
            # explicitly on every request: keep-alive reuses one thread (and
            # its context) across sequential requests on a connection.
            interactive = mutation and parsed.path.startswith("/api/") and not parsed.path.startswith("/api/v1/")
            gateway_auth._interactive_request.set(interactive)

        def do_GET(self) -> None:  # noqa: N802
            if self._reject_unauthorized_host():
                return
            parsed = urlparse(self.path)
            self._mark_interactive(parsed, mutation=False)
            if os.environ.get("AX_OFFLINE"):
                if parsed.path == "/api/v1/sse/messages":
                    from urllib.parse import parse_qs as _parse_qs

                    token = str((_parse_qs(parsed.query).get("token") or [""])[0]).strip()
                    _offline_sse_stream(self, token)
                    return
                if parsed.path in ("/auth/me", "/api/v1/agents"):
                    _write_json_response(self, {"agents": [], "id": "offline-user", "email": "offline@localhost"})
                    return
            if parsed.path == "/":
                _write_html_response(self, _render_gateway_demo_page(refresh_ms=refresh_ms))
                return
            if parsed.path == "/operator":
                _write_html_response(self, _render_gateway_ui_page(refresh_ms=refresh_ms))
                return
            if parsed.path == "/demo":
                _write_html_response(self, _render_gateway_demo_page(refresh_ms=refresh_ms))
                return
            if parsed.path == "/healthz":
                _write_json_response(self, {"ok": True})
                return
            if parsed.path == "/favicon.svg" or parsed.path == "/favicon.ico":
                body = _GATEWAY_FAVICON_SVG.encode("utf-8")
                try:
                    self.send_response(HTTPStatus.OK.value)
                    self.send_header("Content-Type", "image/svg+xml; charset=utf-8")
                    self.send_header("Content-Length", str(len(body)))
                    self.send_header("Cache-Control", "public, max-age=86400")
                    _send_security_headers(self)
                    self.end_headers()
                    self.wfile.write(body)
                except (BrokenPipeError, ConnectionResetError):
                    pass
                return
            if parsed.path == "/api/status":
                query = parse_qs(parsed.query)
                include_hidden = str((query.get("all") or ["0"])[0] or "0").lower() in {"1", "true", "yes"}
                _write_json_response(
                    self,
                    _status_payload(activity_limit=activity_limit, include_hidden=include_hidden),
                )
                return
            if parsed.path == "/local/inbox":
                query = parse_qs(parsed.query)
                session_token = str(self.headers.get("X-Gateway-Session") or "").strip()
                limit = int((query.get("limit") or ["20"])[0] or 20)
                channel = str((query.get("channel") or ["main"])[0] or "main")
                space_id = str((query.get("space_id") or [""])[0] or "").strip() or None
                unread_only = str((query.get("unread_only") or ["true"])[0]).lower() not in {"0", "false", "no"}
                mark_read = str((query.get("mark_read") or ["true"])[0]).lower() not in {"0", "false", "no"}
                payload = _local_session_inbox(
                    session_token=session_token,
                    limit=limit,
                    channel=channel,
                    space_id=space_id,
                    unread_only=unread_only,
                    mark_read=mark_read,
                )
                _write_json_response(self, payload)
                return
            if parsed.path == "/local/sessions":
                registry = load_gateway_registry()
                sessions = list(registry.get("local_sessions") or [])
                _write_json_response(self, {"sessions": sessions, "count": len(sessions)})
                return
            if parsed.path == "/api/runtime-types":
                _write_json_response(self, _runtime_types_payload())
                return
            if parsed.path == "/api/templates":
                _write_json_response(self, _agent_templates_payload())
                return
            if parsed.path.startswith("/api/connectors"):
                try:
                    _write_json_response(self, _dispatch_connector_api_get(parsed.path))
                except LookupError as exc:
                    _write_json_response(self, {"error": str(exc)}, status=HTTPStatus.NOT_FOUND)
                except Exception as exc:
                    _write_json_response(
                        self,
                        {"error": str(exc)},
                        status=_connector_api_error_status(exc),
                    )
                return
            if parsed.path == "/api/approvals":
                query = parse_qs(parsed.query)
                status_filter = (query.get("status") or [None])[0]
                _write_json_response(self, _approval_rows_payload(status=status_filter))
                return
            if parsed.path.startswith("/api/approvals/"):
                approval_id = unquote(parsed.path.removeprefix("/api/approvals/")).strip()
                try:
                    _write_json_response(self, _approval_detail_payload(approval_id))
                except LookupError as exc:
                    _write_json_response(self, {"error": str(exc)}, status=HTTPStatus.NOT_FOUND)
                return
            if parsed.path == "/api/spaces":
                payload = _spaces_payload()
                # _spaces_payload never raises: upstream failures fall back to
                # cached spaces + session-known active space. Return 200 as
                # long as we have something usable; 503 only when there is
                # neither cache nor session.
                has_data = bool(payload.get("spaces") or payload.get("active_space_id"))
                status = HTTPStatus.OK if has_data else HTTPStatus.SERVICE_UNAVAILABLE
                _write_json_response(self, payload, status=status)
                return
            if parsed.path.startswith("/api/agents/") and parsed.path.endswith("/inbox"):
                name = unquote(parsed.path.removeprefix("/api/agents/").removesuffix("/inbox")).strip()
                query = parse_qs(parsed.query)

                def _flag(values, default=False):
                    if not values:
                        return default
                    return str(values[0]).lower() in {"1", "true", "yes", "on"}

                try:
                    inbox_payload = _inbox_for_managed_agent(
                        name=name,
                        limit=int((query.get("limit") or ["20"])[0]),
                        channel=(query.get("channel") or ["main"])[0],
                        space_id=(query.get("space_id") or [None])[0],
                        unread_only=_flag(query.get("unread_only")),
                        mark_read=_flag(query.get("mark_read")),
                    )
                except LookupError as exc:
                    _write_json_response(self, {"error": str(exc)}, status=HTTPStatus.NOT_FOUND)
                    return
                except ValueError as exc:
                    _write_json_response(self, {"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
                    return
                _write_json_response(self, inbox_payload)
                return
            if parsed.path.startswith("/api/agents/"):
                name = unquote(parsed.path.removeprefix("/api/agents/")).strip()
                payload = _agent_detail_payload(name, activity_limit=activity_limit)
                if payload is None:
                    _write_json_response(
                        self,
                        {"error": f"Managed agent not found: {name}"},
                        status=HTTPStatus.NOT_FOUND,
                    )
                    return
                _write_json_response(self, payload)
                return
            _write_json_response(self, {"error": "not found"}, status=HTTPStatus.NOT_FOUND)

        def do_POST(self) -> None:  # noqa: N802
            if self._reject_unauthorized_host():
                return
            parsed = urlparse(self.path)
            self._mark_interactive(parsed, mutation=True)
            try:
                body = _read_json_request(self)
                if os.environ.get("AX_OFFLINE"):
                    if parsed.path == "/auth/exchange":
                        _offline_auth_exchange(self, body)
                        return
                    if parsed.path == "/api/v1/messages":
                        _offline_message_post(self, body)
                        return
                    if parsed.path in (
                        "/api/v1/agents/heartbeat",
                        "/api/v1/agents/processing-status",
                        "/api/v1/tool-calls",
                    ):
                        _write_json_response(self, {"status": "ok"})
                        return
                if parsed.path.startswith("/api/templates/") and parsed.path.endswith("/install"):
                    template_id = (
                        unquote(parsed.path.removeprefix("/api/templates/").removesuffix("/install")).strip().lower()
                    )
                    if template_id not in _RUNTIME_INSTALL_RECIPES:
                        _write_json_response(
                            self,
                            {"error": f"runtime not on install allowlist: {template_id!r}"},
                            status=HTTPStatus.BAD_REQUEST,
                        )
                        return
                    operator_session = load_gateway_session()
                    if not operator_session:
                        _write_json_response(
                            self,
                            {
                                "error": "install requires an active gateway operator session — run `ax gateway login` first"
                            },
                            status=HTTPStatus.FORBIDDEN,
                        )
                        return
                    target_override = str(body.get("target") or "").strip() or None
                    try:
                        payload = _install_runtime_payload(
                            template_id,
                            target_override=target_override,
                            operator_session=operator_session,
                        )
                    except PermissionError as exc:
                        _write_json_response(self, {"error": str(exc)}, status=HTTPStatus.FORBIDDEN)
                        return
                    except ValueError as exc:
                        _write_json_response(self, {"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
                        return
                    status_code = HTTPStatus.OK if payload.get("ready") else HTTPStatus.UNPROCESSABLE_ENTITY
                    _write_json_response(self, payload, status=status_code)
                    return
                if parsed.path.startswith("/api/connectors"):
                    try:
                        payload, status = _dispatch_connector_api_post(parsed.path, body)
                        _write_json_response(self, payload, status=status)
                    except LookupError as exc:
                        _write_json_response(self, {"error": str(exc)}, status=HTTPStatus.NOT_FOUND)
                    except Exception as exc:
                        _write_json_response(
                            self,
                            {"error": str(exc)},
                            status=_connector_api_error_status(exc),
                        )
                    return
                if parsed.path == "/api/agents":
                    try:
                        payload = _register_managed_agent(
                            name=str(body.get("name") or "").strip(),
                            template_id=str(body.get("template_id") or "").strip() or None,
                            runtime_type=str(body.get("runtime_type") or "").strip() or None,
                            exec_cmd=str(body.get("exec_command") or "").strip() or None,
                            workdir=str(body.get("workdir") or "").strip() or None,
                            space_id=str(body.get("space_id") or "").strip() or None,
                            audience=str(body.get("audience") or "both"),
                            description=str(body.get("description") or "").strip() or None,
                            model=str(body.get("model") or "").strip() or None,
                            timeout_seconds=body.get("timeout_seconds", body.get("timeout")),
                            start=bool(body.get("start", True)),
                            source="ui:api",
                        )
                    except UpstreamRateLimitedError as exc:
                        retry_after = exc.retry_after_seconds or 30
                        _rl_session = load_gateway_session() or {}
                        _rl_host = (
                            (_rl_session.get("base_url") or "paxai.app").replace("https://", "").replace("http://", "")
                        )
                        _write_json_response(
                            self,
                            {
                                "error": f"Upstream rate-limited ({_rl_host} returned 429).",
                                "error_class": "rate_limited",
                                "retry_after_seconds": retry_after,
                                "operator_action": (
                                    f"Wait {retry_after} seconds and try again. "
                                    "Other agent runtimes may be holding the rate-limit budget; "
                                    "stopping or archiving idle agents can reduce pressure."
                                ),
                            },
                            status=HTTPStatus.TOO_MANY_REQUESTS,
                        )
                        return
                    profile = gateway_core.infer_operator_profile(payload)
                    if (
                        profile["placement"] == "attached"
                        and profile["activation"] == "attach_only"
                        and str(payload.get("desired_state") or "").strip().lower() == "running"
                    ):
                        launch_payload = _launch_attached_agent_session(
                            _prepare_attached_agent_payload(payload["name"])
                        )
                        record_gateway_activity(
                            "attached_session_launch_requested",
                            agent_name=payload["name"],
                            launch_mode=launch_payload.get("launch_mode"),
                            workdir=str(Path(str(launch_payload["mcp_path"])).parent),
                        )
                        registry = load_gateway_registry()
                        stored = find_agent_entry(registry, str(payload["name"]))
                        if stored:
                            payload = _with_registry_refs(registry, annotate_runtime_health(stored, registry=registry))
                    _write_json_response(self, payload, status=HTTPStatus.CREATED)
                    return
                if parsed.path == "/api/agents/cleanup-hide":
                    raw_names = body.get("names")
                    if not isinstance(raw_names, list):
                        _write_json_response(
                            self,
                            {"error": "names must be a list of managed agent names"},
                            status=HTTPStatus.BAD_REQUEST,
                        )
                        return
                    payload = _hide_managed_agents(
                        [str(name or "").strip() for name in raw_names],
                        reason=str(body.get("reason") or "operator_cleanup"),
                    )
                    _write_json_response(self, payload)
                    return
                if parsed.path == "/api/agents/cleanup-restore":
                    raw_names = body.get("names")
                    if not isinstance(raw_names, list):
                        _write_json_response(
                            self,
                            {"error": "names must be a list of managed agent names"},
                            status=HTTPStatus.BAD_REQUEST,
                        )
                        return
                    payload = _restore_hidden_managed_agents([str(name or "").strip() for name in raw_names])
                    _write_json_response(self, payload)
                    return
                if parsed.path == "/api/agents/recover":
                    raw_names = body.get("names")
                    if not isinstance(raw_names, list):
                        _write_json_response(
                            self,
                            {"error": "names must be a list of managed agent names"},
                            status=HTTPStatus.BAD_REQUEST,
                        )
                        return
                    try:
                        payload = _recover_managed_agents_from_evidence([str(name or "").strip() for name in raw_names])
                    except ValueError as exc:
                        _write_json_response(self, {"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
                        return
                    _write_json_response(self, payload)
                    return
                if parsed.path == "/local/connect":
                    agent_name = str(body.get("agent_name") or body.get("name") or "").strip()
                    registry_ref = str(
                        body.get("registry_ref") or body.get("registry") or body.get("ref") or ""
                    ).strip()
                    fingerprint = body.get("fingerprint") if isinstance(body.get("fingerprint"), dict) else {}
                    payload = _connect_local_pass_through_agent(
                        agent_name=agent_name or None,
                        registry_ref=registry_ref or None,
                        fingerprint=fingerprint,
                        space_id=str(body.get("space_id") or "").strip() or None,
                    )
                    status = HTTPStatus.OK if payload.get("status") == "approved" else HTTPStatus.ACCEPTED
                    _write_json_response(self, payload, status=status)
                    return
                if parsed.path == "/local/send":
                    session_token = str(self.headers.get("X-Gateway-Session") or "").strip()
                    payload = _send_local_session_message(session_token=session_token, body=body)
                    _write_json_response(self, payload, status=HTTPStatus.CREATED)
                    return
                if parsed.path == "/local/tasks":
                    session_token = str(self.headers.get("X-Gateway-Session") or "").strip()
                    payload = _create_local_session_task(session_token=session_token, body=body)
                    _write_json_response(self, payload, status=HTTPStatus.CREATED)
                    return
                if parsed.path == "/local/proxy":
                    session_token = str(self.headers.get("X-Gateway-Session") or "").strip()
                    try:
                        payload = _proxy_local_session_call(session_token=session_token, body=body)
                    except LookupError as exc:
                        _write_json_response(self, {"error": str(exc)}, status=HTTPStatus.NOT_FOUND)
                        return
                    except ValueError as exc:
                        _write_json_response(self, {"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
                        return
                    _write_json_response(self, payload)
                    return
                if parsed.path.endswith("/start") and parsed.path.startswith("/api/agents/"):
                    name = unquote(parsed.path.removeprefix("/api/agents/").removesuffix("/start")).strip()
                    payload = _set_managed_agent_desired_state(name, "running")
                    _write_json_response(self, payload)
                    return
                if parsed.path.endswith("/stop") and parsed.path.startswith("/api/agents/"):
                    name = unquote(parsed.path.removeprefix("/api/agents/").removesuffix("/stop")).strip()
                    payload = _set_managed_agent_desired_state(name, "stopped")
                    _write_json_response(self, payload)
                    return
                if parsed.path.endswith("/attach") and parsed.path.startswith("/api/agents/"):
                    name = unquote(parsed.path.removeprefix("/api/agents/").removesuffix("/attach")).strip()
                    payload = _launch_attached_agent_session(_prepare_attached_agent_payload(name))
                    record_gateway_activity(
                        "attached_session_launch_requested",
                        agent_name=name,
                        launch_mode=payload.get("launch_mode"),
                        workdir=str(Path(str(payload["mcp_path"])).parent),
                    )
                    _write_json_response(self, payload, status=HTTPStatus.ACCEPTED)
                    return
                if parsed.path.endswith("/manual-attach") and parsed.path.startswith("/api/agents/"):
                    name = unquote(parsed.path.removeprefix("/api/agents/").removesuffix("/manual-attach")).strip()
                    try:
                        payload = _mark_attached_agent_session(
                            name,
                            note=str(body.get("note") or "").strip() or None,
                        )
                    except (LookupError, ValueError) as exc:
                        _write_json_response(self, {"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
                        return
                    _write_json_response(self, payload)
                    return
                if parsed.path.endswith("/external-runtime-announce") and parsed.path.startswith("/api/agents/"):
                    name = unquote(
                        parsed.path.removeprefix("/api/agents/").removesuffix("/external-runtime-announce")
                    ).strip()
                    try:
                        payload = _announce_external_agent_runtime(name, body)
                    except LookupError as exc:
                        _write_json_response(self, {"error": str(exc)}, status=HTTPStatus.NOT_FOUND)
                        return
                    except ValueError as exc:
                        _write_json_response(self, {"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
                        return
                    _write_json_response(self, payload)
                    return
                if parsed.path.endswith("/send") and parsed.path.startswith("/api/agents/"):
                    name = unquote(parsed.path.removeprefix("/api/agents/").removesuffix("/send")).strip()
                    payload = _send_from_managed_agent(
                        name=name,
                        content=str(body.get("content") or ""),
                        to=str(body.get("to") or "").strip() or None,
                        parent_id=str(body.get("parent_id") or "").strip() or None,
                        # UI has its own inbox panel that polls separately;
                        # don't make every UI send block on a 2s post-send poll.
                        inbox_wait=0,
                    )
                    _write_json_response(self, payload, status=HTTPStatus.CREATED)
                    return
                if parsed.path.endswith("/test") and parsed.path.startswith("/api/agents/"):
                    name = unquote(parsed.path.removeprefix("/api/agents/").removesuffix("/test")).strip()
                    # UI test button defaults to user-authored: per Madtank/supervisor
                    # 2026-05-02, principal-invoked surfaces author as the invoking
                    # principal, never as a service account. UI's principal is the
                    # logged-in user (resolved via the Gateway user client).
                    payload = _send_gateway_test_to_managed_agent(
                        name,
                        content=str(body.get("content") or "").strip() or None,
                        author=str(body.get("author") or "user").strip() or "user",
                        sender_agent=str(body.get("sender_agent") or "").strip() or None,
                    )
                    _write_json_response(self, payload, status=HTTPStatus.CREATED)
                    return
                if parsed.path.endswith("/ack") and parsed.path.startswith("/api/agents/"):
                    # Pass-through agents that reply via their own PAT (not via
                    # gateway-mediated send) call this to tell the gateway "I
                    # processed message_id, here's my reply_id." Updates the
                    # registry's last_reply_at + processed_count, drops the
                    # message from the local pending queue, fires a reply_sent
                    # activity event so the simple-gateway drawer surfaces it.
                    name = unquote(parsed.path.removeprefix("/api/agents/").removesuffix("/ack")).strip()
                    payload = _ack_managed_agent_message(
                        name,
                        message_id=str(body.get("message_id") or "").strip(),
                        reply_id=str(body.get("reply_id") or "").strip() or None,
                        reply_preview=str(body.get("reply_preview") or "").strip() or None,
                    )
                    _write_json_response(self, payload)
                    return
                if parsed.path.endswith("/move") and parsed.path.startswith("/api/agents/"):
                    name = unquote(parsed.path.removeprefix("/api/agents/").removesuffix("/move")).strip()
                    payload = _move_managed_agent_space(
                        name,
                        str(body.get("space_id") or "").strip(),
                    )
                    _write_json_response(self, payload)
                    return
                if parsed.path.endswith("/system-prompt") and parsed.path.startswith("/api/agents/"):
                    name = unquote(parsed.path.removeprefix("/api/agents/").removesuffix("/system-prompt")).strip()
                    raw = body.get("system_prompt")
                    next_value: str | object
                    if raw is None:
                        next_value = ""
                    else:
                        next_value = str(raw)
                    payload = _update_managed_agent(name=name, system_prompt=next_value)
                    _write_json_response(self, payload)
                    return
                if parsed.path.endswith("/pin") and parsed.path.startswith("/api/agents/"):
                    name = unquote(parsed.path.removeprefix("/api/agents/").removesuffix("/pin")).strip()
                    payload = _set_managed_agent_pin(name, bool(body.get("pinned", True)))
                    _write_json_response(self, payload)
                    return
                if parsed.path.endswith("/doctor") and parsed.path.startswith("/api/agents/"):
                    name = unquote(parsed.path.removeprefix("/api/agents/").removesuffix("/doctor")).strip()
                    payload = _run_gateway_doctor(
                        name,
                        send_test=bool(body.get("send_test", False)),
                    )
                    _write_json_response(self, payload, status=HTTPStatus.CREATED)
                    return
                if parsed.path.endswith("/approve") and parsed.path.startswith("/api/agents/"):
                    name = unquote(parsed.path.removeprefix("/api/agents/").removesuffix("/approve")).strip()
                    detail = _agent_detail_payload(name, activity_limit=activity_limit)
                    if detail is None:
                        _write_json_response(
                            self,
                            {"error": f"Managed agent not found: {name}"},
                            status=HTTPStatus.NOT_FOUND,
                        )
                        return
                    approval_id = str((detail.get("agent") or {}).get("approval_id") or "").strip()
                    if not approval_id:
                        _write_json_response(
                            self,
                            {"error": f"@{name} does not have a pending Gateway approval."},
                            status=HTTPStatus.BAD_REQUEST,
                        )
                        return
                    payload = approve_gateway_approval(
                        approval_id,
                        scope=str(body.get("scope") or "asset").strip() or "asset",
                    )
                    _write_json_response(self, payload, status=HTTPStatus.CREATED)
                    return
                if parsed.path.endswith("/approve") and parsed.path.startswith("/api/approvals/"):
                    approval_id = unquote(parsed.path.removeprefix("/api/approvals/").removesuffix("/approve")).strip()
                    payload = approve_gateway_approval(
                        approval_id,
                        scope=str(body.get("scope") or "asset").strip() or "asset",
                    )
                    _write_json_response(self, payload, status=HTTPStatus.CREATED)
                    return
                if parsed.path.endswith("/reject") and parsed.path.startswith("/api/agents/"):
                    name = unquote(parsed.path.removeprefix("/api/agents/").removesuffix("/reject")).strip()
                    payload = _reject_managed_agent_approval(name)
                    _write_json_response(self, payload, status=HTTPStatus.CREATED)
                    return
                if parsed.path.endswith("/reject") and parsed.path.startswith("/api/approvals/"):
                    approval_id = unquote(parsed.path.removeprefix("/api/approvals/").removesuffix("/reject")).strip()
                    payload = deny_gateway_approval(approval_id)
                    _write_json_response(self, payload, status=HTTPStatus.CREATED)
                    return
                _write_json_response(self, {"error": "not found"}, status=HTTPStatus.NOT_FOUND)
            except LookupError as exc:
                _write_json_response(self, {"error": str(exc)}, status=HTTPStatus.NOT_FOUND)
            except ValueError as exc:
                _write_json_response(self, {"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
            except typer.Exit as exc:
                status = HTTPStatus.BAD_REQUEST if int(exc.exit_code or 1) == 1 else HTTPStatus.OK
                _write_json_response(self, {"error": "request failed"}, status=status)
            except Exception as exc:
                _write_json_response(self, {"error": str(exc)}, status=HTTPStatus.INTERNAL_SERVER_ERROR)

        def do_PUT(self) -> None:  # noqa: N802
            if self._reject_unauthorized_host():
                return
            parsed = urlparse(self.path)
            self._mark_interactive(parsed, mutation=True)
            try:
                body = _read_json_request(self)
                if parsed.path.startswith("/api/connectors/"):
                    try:
                        payload = _dispatch_connector_api_put(parsed.path, body)
                        _write_json_response(self, payload)
                    except LookupError as exc:
                        _write_json_response(self, {"error": str(exc)}, status=HTTPStatus.NOT_FOUND)
                    except Exception as exc:
                        _write_json_response(
                            self,
                            {"error": str(exc)},
                            status=_connector_api_error_status(exc),
                        )
                    return
                if parsed.path.startswith("/api/agents/"):
                    name = unquote(parsed.path.removeprefix("/api/agents/")).strip()
                    payload = _update_managed_agent(
                        name=name,
                        template_id=str(body.get("template_id") or "").strip() or None,
                        runtime_type=str(body.get("runtime_type") or "").strip() or None,
                        exec_cmd=str(body.get("exec_command") or "") if "exec_command" in body else _UNSET,
                        workdir=str(body.get("workdir") or "") if "workdir" in body else _UNSET,
                        model=str(body.get("model") or "") if "model" in body else _UNSET,
                        description=str(body.get("description") or "").strip() or None,
                        timeout_seconds=body.get("timeout_seconds", body.get("timeout"))
                        if "timeout_seconds" in body or "timeout" in body
                        else _UNSET,
                        desired_state=str(body.get("desired_state") or "").strip() or None,
                    )
                    _write_json_response(self, payload)
                    return
                _write_json_response(self, {"error": "not found"}, status=HTTPStatus.NOT_FOUND)
            except LookupError as exc:
                _write_json_response(self, {"error": str(exc)}, status=HTTPStatus.NOT_FOUND)
            except ValueError as exc:
                _write_json_response(self, {"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
            except typer.Exit as exc:
                status = HTTPStatus.BAD_REQUEST if int(exc.exit_code or 1) == 1 else HTTPStatus.OK
                _write_json_response(self, {"error": "request failed"}, status=status)
            except Exception as exc:
                _write_json_response(self, {"error": str(exc)}, status=HTTPStatus.INTERNAL_SERVER_ERROR)

        def do_DELETE(self) -> None:  # noqa: N802
            if self._reject_unauthorized_host():
                return
            parsed = urlparse(self.path)
            self._mark_interactive(parsed, mutation=True)
            if parsed.path.startswith("/api/connectors/"):
                try:
                    payload = _dispatch_connector_api_delete(parsed.path)
                    _write_json_response(self, payload)
                except LookupError as exc:
                    _write_json_response(self, {"error": str(exc)}, status=HTTPStatus.NOT_FOUND)
                except Exception as exc:
                    _write_json_response(
                        self,
                        {"error": str(exc)},
                        status=_connector_api_error_status(exc),
                    )
                return
            if parsed.path.startswith("/api/agents/"):
                name = unquote(parsed.path.removeprefix("/api/agents/")).strip()
                try:
                    payload = _remove_managed_agent(name)
                    _write_json_response(self, payload)
                except LookupError as exc:
                    _write_json_response(self, {"error": str(exc)}, status=HTTPStatus.NOT_FOUND)
                return
            _write_json_response(self, {"error": "not found"}, status=HTTPStatus.NOT_FOUND)

    return GatewayUiHandler


def _render_agent_detail(entry: dict, *, activity: list[dict]) -> Group:
    overview = Table.grid(expand=True, padding=(0, 2))
    overview.add_column(style="bold")
    overview.add_column(ratio=2)
    overview.add_column(style="bold")
    overview.add_column(ratio=2)
    overview.add_row("Agent", f"@{entry.get('name')}", "Type", _agent_type_label(entry))
    overview.add_row("Template", _agent_template_label(entry), "Output", _agent_output_label(entry))
    overview.add_row("Mode", str(entry.get("mode") or "-"), "Presence", str(entry.get("presence") or "-"))
    overview.add_row("Reply", str(entry.get("reply") or "-"), "Confidence", str(entry.get("confidence") or "-"))
    overview.add_row(
        "Asset Class", str(entry.get("asset_class") or "-"), "Intake", str(entry.get("intake_model") or "-")
    )
    overview.add_row(
        "Trigger",
        str((entry.get("trigger_sources") or [None])[0] or "-"),
        "Return",
        str((entry.get("return_paths") or [None])[0] or "-"),
    )
    overview.add_row(
        "Telemetry", str(entry.get("telemetry_shape") or "-"), "Worker", str(entry.get("worker_model") or "-")
    )
    overview.add_row(
        "Attestation", str(entry.get("attestation_state") or "-"), "Approval", str(entry.get("approval_state") or "-")
    )
    overview.add_row(
        "Acting As", str(entry.get("acting_agent_name") or "-"), "Identity", str(entry.get("identity_status") or "-")
    )
    overview.add_row(
        "Environment",
        str(entry.get("environment_label") or entry.get("base_url") or "-"),
        "Env Status",
        str(entry.get("environment_status") or "-"),
    )
    overview.add_row(
        "Current Space",
        str(entry.get("active_space_name") or entry.get("active_space_id") or "-"),
        "Space Status",
        str(entry.get("space_status") or "-"),
    )
    overview.add_row(
        "Default Space",
        str(entry.get("default_space_name") or entry.get("default_space_id") or "-"),
        "Allowed Spaces",
        str(entry.get("allowed_space_count") or 0),
    )
    overview.add_row(
        "Install", str(entry.get("install_id") or "-"), "Runtime Instance", str(entry.get("runtime_instance_id") or "-")
    )
    overview.add_row("Reachability", _reachability_copy(entry), "Reason", str(entry.get("confidence_reason") or "-"))
    overview.add_row(
        "Desired", str(entry.get("desired_state") or "-"), "Effective", str(entry.get("effective_state") or "-")
    )
    overview.add_row(
        "Connected", "yes" if entry.get("connected") else "no", "Queue", str(entry.get("backlog_depth") or 0)
    )
    overview.add_row(
        "Seen",
        _format_age(entry.get("last_seen_age_seconds")),
        "Reconnect",
        _format_age(entry.get("reconnect_backoff_seconds")),
    )
    overview.add_row(
        "Processed", str(entry.get("processed_count") or 0), "Dropped", str(entry.get("dropped_count") or 0)
    )
    overview.add_row(
        "Last Work",
        _format_timestamp(entry.get("last_work_received_at")),
        "Completed",
        _format_timestamp(entry.get("last_work_completed_at")),
    )
    overview.add_row(
        "Phase", str(entry.get("current_status") or "-"), "Activity", str(entry.get("current_activity") or "-")
    )
    overview.add_row(
        "Tool",
        str(entry.get("current_tool") or "-"),
        "Timeout",
        f"{entry.get('timeout_seconds')}s" if entry.get("timeout_seconds") else "-",
    )
    overview.add_row("Adapter", _adapter_label(entry), "Space", str(entry.get("space_id") or "-"))
    overview.add_row(
        "Cred Source", str(entry.get("credential_source") or "-"), "Token", str(entry.get("token_file") or "-")
    )
    overview.add_row(
        "Agent ID", str(entry.get("agent_id") or "-"), "Last Reply", str(entry.get("last_reply_preview") or "-")
    )
    overview.add_row(
        "Last Error",
        str(entry.get("last_error") or "-"),
        "Confidence Detail",
        str(entry.get("confidence_detail") or "-"),
    )
    overview.add_row(
        "Doctor",
        str(entry.get("last_successful_doctor_at") or "-"),
        "Doctor Status",
        str(
            (entry.get("last_doctor_result") or {}).get("status")
            if isinstance(entry.get("last_doctor_result"), dict)
            else "-"
        ),
    )

    paths = Table.grid(expand=True, padding=(0, 2))
    paths.add_column(style="bold")
    paths.add_column(ratio=3)
    paths.add_row("Token File", str(entry.get("token_file") or "-"))
    paths.add_row("Workdir", str(entry.get("workdir") or "-"))
    paths.add_row("Exec", str(entry.get("exec_command") or "-"))
    paths.add_row("Added", _format_timestamp(entry.get("added_at")))

    panels = [
        Panel(overview, title=f"Managed Agent · @{entry.get('name')}", border_style="cyan"),
        Panel(paths, title="Runtime Details", border_style="blue"),
    ]

    operator_prompt = str(entry.get("system_prompt") or "").strip()
    if operator_prompt:
        prompt_panel_body = operator_prompt
    else:
        prompt_panel_body = (
            "(none) — set with: ax gateway agents update "
            f"{entry.get('name') or '<name>'} --system-prompt '<your role instructions>'"
        )
    panels.append(Panel(prompt_panel_body, title="Operator System Prompt", border_style="green"))
    panels.append(Panel(_render_activity_table(activity), title="Recent Agent Activity", border_style="magenta"))

    return Group(*panels)


@app.command("activity")
def activity(
    message_id: str = typer.Option(None, "--message-id", help="Filter to a single source message_id"),
    agent: str = typer.Option(None, "--agent", help="Filter to a single managed agent name"),
    limit: int = typer.Option(0, "--limit", help="Cap rows returned (0 = no cap)"),
    as_json: bool = JSON_OPTION,
):
    """Inspect Gateway-recorded activity for one message or agent.

    Reads the local activity log Gateway already owns
    (``~/.ax/gateway/activity.jsonl``) and emits the rows in chronological
    order. Each row carries the canonical ``phase`` field for any registered
    event so supervisor loops and the aX UI can consume a stable shape across
    runtime types.

    This command is read-only. It does not authenticate to the backend, does
    not construct an ``AxClient``, and does not surface any new credential
    path — Gateway remains the trust boundary.
    """
    log_path = activity_log_path()
    rows: list[dict] = []
    if log_path.exists():
        try:
            for raw in log_path.read_text(encoding="utf-8").splitlines():
                stripped = raw.strip()
                if not stripped:
                    continue
                try:
                    item = json.loads(stripped)
                except json.JSONDecodeError:
                    continue
                if not isinstance(item, dict):
                    continue
                rows.append(item)
        except OSError:
            rows = []

    msg_filter = (message_id or "").strip()
    agent_filter = (agent or "").strip().lower()
    filtered = []
    for item in rows:
        if msg_filter and str(item.get("message_id") or "") != msg_filter:
            continue
        if agent_filter and str(item.get("agent_name") or "").lower() != agent_filter:
            continue
        filtered.append(item)

    filtered.sort(key=lambda r: str(r.get("ts") or ""))
    if limit and limit > 0:
        filtered = filtered[-limit:]

    if as_json:
        if msg_filter:
            print_json({"message_id": msg_filter, "events": filtered})
        else:
            print_json({"events": filtered})
        return

    if not filtered:
        target = msg_filter or agent_filter or "(any)"
        err_console.print(f"No Gateway activity for {target}.")
        return
    print_table(
        ["Time", "Phase", "Event", "Agent", "Message", "Tool", "Detail"],
        [
            {
                "ts": item.get("ts"),
                "phase": item.get("phase") or "-",
                "event": item.get("event"),
                "agent_name": item.get("agent_name") or "-",
                "message_id": item.get("message_id") or "-",
                "tool_name": item.get("tool_name") or "-",
                "detail": item.get("activity_message") or item.get("reply_preview") or item.get("error") or "",
            }
            for item in filtered
        ],
        keys=["ts", "phase", "event", "agent_name", "message_id", "tool_name", "detail"],
    )


@app.command("ui")
def ui(
    host: str = typer.Option("127.0.0.1", "--host", help="Host interface to bind the local Gateway UI"),
    port: int = typer.Option(8765, "--port", help="Port for the local Gateway UI"),
    activity_limit: int = typer.Option(24, "--activity-limit", help="Number of recent events to expose in the UI"),
    refresh: float = typer.Option(2.0, "--refresh", help="Browser auto-refresh interval in seconds"),
    open_browser: bool = typer.Option(True, "--open/--no-open", help="Open the local UI in a browser"),
):
    """Serve a local Gateway web UI."""
    refresh_ms = max(250, int(refresh * 1000))
    handler = _build_gateway_ui_handler(activity_limit=activity_limit, refresh_ms=refresh_ms)
    try:
        server = _GatewayUiServer((host, port), handler)
    except OSError as exc:
        err_console.print(f"[red]Failed to start Gateway UI:[/red] {exc}")
        raise typer.Exit(1)

    url = f"http://{host}:{server.server_port}"
    err_console.print("[bold]ax gateway ui[/bold] — local Gateway dashboard")
    err_console.print(f"  url      = {url}")
    err_console.print(f"  refresh  = {refresh_ms}ms")
    err_console.print(f"  source   = {gateway_dir()}")
    err_console.print("  stop     = Ctrl-C")
    write_gateway_ui_state(pid=os.getpid(), host=host, port=server.server_port)
    record_gateway_activity("gateway_ui_started", pid=os.getpid(), host=host, port=server.server_port, url=url)
    if open_browser:
        try:
            webbrowser.open_new_tab(url)
        except Exception:
            err_console.print("[yellow]Could not open a browser automatically.[/yellow]")
    # Mark the process so request-log records carry the ui_server role and
    # rate-limit thresholds default to "automated" (dashboard mutations are
    # marked interactive per-request by the handler). Pre-warm the shared
    # rate-limit window before browser polling starts.
    gateway_auth._is_ui_server_process = True
    try:
        gateway_auth._gateway_rate_limit_state.warm(
            gateway_auth._load_gateway_user_client(request_logger=_ui_request_logger)
        )
    except Exception:
        pass  # best-effort — don't block UI startup on a warm failure
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        err_console.print("[yellow]Gateway UI stopped.[/yellow]")
    finally:
        record_gateway_activity("gateway_ui_stopped", pid=os.getpid(), host=host, port=server.server_port, url=url)
        clear_gateway_ui_state(os.getpid())
        server.server_close()


# Deferred cross-module imports (bottom-of-file to avoid import cycles; bound
# into module globals after defs, resolved at call time).
from . import gateway_auth  # noqa: E402
from .gateway_agents import (  # noqa: E402
    _ack_managed_agent_message,
    _hide_managed_agents,
    _move_managed_agent_space,
    _recover_managed_agents_from_evidence,
    _register_managed_agent,
    _reject_managed_agent_approval,
    _remove_managed_agent,
    _restore_hidden_managed_agents,
    _set_managed_agent_pin,
    _update_managed_agent,
    _with_registry_refs,
)
from .gateway_auth import UpstreamRateLimitedError  # noqa: E402
from .gateway_diagnostics import (  # noqa: E402
    _agent_detail_payload,
    _approval_detail_payload,
    _approval_rows_payload,
    _run_gateway_doctor,
    _status_payload,
)
from .gateway_lifecycle import (  # noqa: E402
    _announce_external_agent_runtime,
    _launch_attached_agent_session,
    _mark_attached_agent_session,
    _prepare_attached_agent_payload,
    _set_managed_agent_desired_state,
)
from .gateway_messaging import (  # noqa: E402
    _inbox_for_managed_agent,
    _send_from_managed_agent,
    _send_gateway_test_to_managed_agent,
)
from .gateway_runtime_cmd import (  # noqa: E402
    _RUNTIME_INSTALL_RECIPES,
    _agent_templates_payload,
    _install_runtime_payload,
    _runtime_types_payload,
)
from .gateway_session import (  # noqa: E402
    _connect_local_pass_through_agent,
    _create_local_session_task,
    _local_session_inbox,
    _proxy_local_session_call,
    _send_local_session_message,
)
from .gateway_spaces import _spaces_payload  # noqa: E402
