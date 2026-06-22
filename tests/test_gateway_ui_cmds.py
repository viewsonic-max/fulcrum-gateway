"""Per-module gateway command tests: gateway_ui (gateway split #28 Phase 1).

Ported from skipped test_gateway_commands*.py; monkeypatches target ax_cli.commands.gateway_ui."""

from __future__ import annotations

import io
import json
import socket
import threading
from contextlib import closing
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from unittest.mock import MagicMock

import httpx
import pytest
import typer
from typer.testing import CliRunner

from ax_cli import gateway as gateway_core
from ax_cli.commands import gateway_agents as _gw_agents
from ax_cli.commands import gateway_auth as _gw_auth
from ax_cli.commands import gateway_diagnostics as _gw_diagnostics
from ax_cli.commands import gateway_messaging as _gw_messaging
from ax_cli.commands import gateway_ui as _gw_ui
from ax_cli.main import app
from tests.gateway_cmd_testlib import (
    _fake_create_agent_in_space,
    _FakeManagedSendClient,
    _FakeUserClient,
    _invoke_handler,
    _json_response,
    _render_text,
    _SharedRuntimeClient,
    _strip,
)

runner = CliRunner()


def test_gateway_ui_agent_reject_marks_pending_approval_rejected(monkeypatch, tmp_path):
    config_dir = tmp_path / "config"
    monkeypatch.setenv("AX_CONFIG_DIR", str(config_dir))
    registry = gateway_core.load_gateway_registry()
    registry["agents"] = [
        {
            "name": "reject-ui-bot",
            "agent_id": "agent-reject-ui-1",
            "space_id": "space-1",
            "base_url": "https://paxai.app",
            "template_id": "pass_through",
            "runtime_type": "inbox",
            "desired_state": "running",
            "requires_approval": True,
            "install_id": "install-reject-ui-1",
            "workdir": str(tmp_path / "repo-a"),
        }
    ]
    daemon = gateway_core.GatewayDaemon(client_factory=lambda **kwargs: _SharedRuntimeClient({}))
    reconciled = daemon._reconcile_registry(registry, {"token": "axp_u_test.token", "base_url": "https://paxai.app"})
    gateway_core.save_gateway_registry(reconciled)
    attestation = reconciled["agents"][0]

    handler_cls = _gw_ui._build_gateway_ui_handler(activity_limit=5, refresh_ms=1000)

    class FakeHandler(handler_cls):
        def __init__(self):
            self.path = "/api/agents/reject-ui-bot/reject"
            self.headers = {"Content-Length": "2", "Host": "127.0.0.1"}
            self.rfile = __import__("io").BytesIO(b"{}")
            self.status = None
            self.body = b""

        def send_response(self, status):
            self.status = status

        def send_header(self, *args):
            return None

        def end_headers(self):
            return None

        @property
        def wfile(self):
            class Writer:
                def __init__(self, outer):
                    self.outer = outer

                def write(self, data):
                    self.outer.body += data

            return Writer(self)

    handler = FakeHandler()
    handler.do_POST()

    assert handler.status == 201
    payload = json.loads(handler.body.decode("utf-8"))
    assert payload["approval"]["status"] == "rejected"
    assert payload["removed"] is True
    assert payload["removed_agent"]["name"] == "reject-ui-bot"
    stored = gateway_core.load_gateway_registry()
    assert gateway_core.find_agent_entry(stored, "reject-ui-bot") is None
    approval = next(item for item in stored["approvals"] if item["approval_id"] == attestation["approval_id"])
    assert approval["status"] == "rejected"


def test_render_gateway_ui_page_contains_local_dashboard_shell():
    page = _gw_ui._render_gateway_ui_page(refresh_ms=2000)

    assert "Gateway Control Plane" in page
    assert "Agent Operated" in page
    assert "/api/status" in page
    assert "/api/templates" in page
    assert "/api/agents/&lt;name&gt;" in page
    assert "refreshMs = 2000" in page
    assert "Gateway Agent Setup" in page
    assert "Outbound Connectors" in page
    assert "/api/connectors" in page
    assert "gateway-agent-setup" in page
    assert "Agent Type" in page
    assert "Output" in page
    assert "Advanced launch settings" in page
    assert "Alerts" in page


def test_gateway_ui_handler_serves_status_and_agent_detail(monkeypatch, tmp_path):
    config_dir = tmp_path / "config"
    monkeypatch.setenv("AX_CONFIG_DIR", str(config_dir))
    monkeypatch.setattr("ax_cli.gateway_storage._scan_gateway_process_pids", lambda: [])
    monkeypatch.setattr("ax_cli.gateway_storage._scan_gateway_ui_process_pids", lambda: [])
    gateway_core.save_gateway_session(
        {
            "token": "axp_u_test.token",
            "base_url": "https://dev.paxai.app",
            "space_id": "space-1",
            "username": "codex",
        }
    )
    registry = gateway_core.load_gateway_registry()
    registry["gateway"].update(
        {
            "gateway_id": "gw-ui-12345678",
            "desired_state": "running",
            "effective_state": "running",
            "last_reconcile_at": datetime.now(timezone.utc).isoformat(),
        }
    )
    registry["agents"] = [
        {
            "name": "echo-bot",
            "agent_id": "agent-1",
            "space_id": "space-1",
            "runtime_type": "echo",
            "desired_state": "running",
            "effective_state": "running",
            "last_seen_at": datetime.now(timezone.utc).isoformat(),
            "last_reply_preview": "Echo: ping",
            "token_file": "/tmp/echo-token",
            "transport": "gateway",
            "credential_source": "gateway",
        }
    ]
    gateway_core.save_gateway_registry(registry)
    gateway_core.record_gateway_activity("reply_sent", entry=registry["agents"][0], reply_preview="Echo: ping")

    handler = _gw_ui._build_gateway_ui_handler(activity_limit=5, refresh_ms=1500)
    with closing(socket.socket()) as probe:
        probe.bind(("127.0.0.1", 0))
        host, port = probe.getsockname()
    server = _gw_ui._GatewayUiServer((host, port), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with httpx.Client(base_url=f"http://{host}:{port}", timeout=2.0) as client:
            status = client.get("/api/status")
            assert status.status_code == 200
            status_payload = status.json()
            assert status_payload["gateway"]["gateway_id"] == "gw-ui-12345678"
            assert status_payload["agents"][0]["name"] == "echo-bot"
            assert status_payload["agents"][0]["mode"] == "LIVE"
            assert status_payload["agents"][0]["presence"] == "IDLE"
            assert status_payload["agents"][0]["reply"] == "REPLY"
            assert status_payload["agents"][0]["confidence"] == "HIGH"
            assert status_payload["summary"]["alert_count"] >= 1
            assert status_payload["alerts"][0]["title"] == "Gateway daemon is stopped"

            runtime_types = client.get("/api/runtime-types")
            assert runtime_types.status_code == 200
            runtime_payload = runtime_types.json()
            assert runtime_payload["count"] == 8  # +hermes_plugin, +sentinel_hermes_sdk
            assert runtime_payload["runtime_types"][1]["id"] == "exec"

            templates = client.get("/api/templates")
            assert templates.status_code == 200
            template_payload = templates.json()
            assert template_payload["templates"][0]["id"] == "hermes"
            assert template_payload["templates"][2]["id"] == "langgraph"
            assert template_payload["templates"][3]["id"] == "langgraph_composio"
            assert template_payload["templates"][4]["id"] == "autogen"
            assert template_payload["templates"][5]["id"] == "pydantic_ai"
            assert template_payload["templates"][6]["id"] == "strands"
            assert template_payload["templates"][8]["id"] == "service_account"
            channel_template = next(
                item for item in template_payload["templates"] if item["id"] == "claude_code_channel"
            )
            assert channel_template["runtime_type"] == "claude_code_channel"
            assert template_payload["count"] == 12

            detail = client.get("/api/agents/echo-bot")
            assert detail.status_code == 200
            detail_payload = detail.json()
            assert detail_payload["agent"]["name"] == "echo-bot"
            assert detail_payload["recent_activity"][0]["event"] == "reply_sent"

            page = client.get("/")
            assert page.status_code == 200
            assert "Bring your agents" in page.text
            assert "Start" in page.text
            assert "/attach" in page.text
            assert "window.__GATEWAY_DEMO_REFRESH_MS__ = 1500" in page.text
            assert 'href="/operator"' in page.text

            operator_page = client.get("/operator")
            assert operator_page.status_code == 200
            assert "Gateway Control Plane" in operator_page.text
            assert "refreshMs = 1500" in operator_page.text
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2.0)


def test_gateway_ui_handler_supports_agent_mutations(monkeypatch, tmp_path):
    config_dir = tmp_path / "config"
    monkeypatch.setenv("AX_CONFIG_DIR", str(config_dir))
    gateway_core.save_gateway_session(
        {
            "token": "axp_u_test.token",
            "base_url": "https://dev.paxai.app",
            "space_id": "space-1",
            "username": "codex",
        }
    )
    monkeypatch.setattr(_gw_agents, "_load_gateway_user_client", lambda: _FakeUserClient())
    monkeypatch.setattr(_gw_agents, "_find_agent_in_space", lambda *args, **kwargs: None)
    monkeypatch.setattr(_gw_agents, "_create_agent_in_space", _fake_create_agent_in_space)
    monkeypatch.setattr(_gw_agents, "_polish_metadata", lambda *args, **kwargs: None)
    monkeypatch.setattr(_gw_agents, "_mint_agent_pat", lambda *args, **kwargs: ("axp_a_agent.secret", "mgmt"))
    monkeypatch.setattr(_gw_agents, "AxClient", _FakeManagedSendClient)
    # The handler delegates /send + /test to gateway_messaging and /doctor to gateway_diagnostics;
    # those functions resolve their clients in their own module namespaces, so mirror the agent
    # client seams there too (the pre-split test patched a single ax_cli.commands.gateway namespace).
    for _mod in (_gw_messaging, _gw_diagnostics):
        monkeypatch.setattr(_mod, "_load_gateway_user_client", lambda: _FakeUserClient(), raising=False)
        monkeypatch.setattr(_mod, "AxClient", _FakeManagedSendClient, raising=False)

    handler = _gw_ui._build_gateway_ui_handler(activity_limit=5, refresh_ms=1500)
    with closing(socket.socket()) as probe:
        probe.bind(("127.0.0.1", 0))
        host, port = probe.getsockname()
    server = _gw_ui._GatewayUiServer((host, port), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with httpx.Client(base_url=f"http://{host}:{port}", timeout=2.0) as client:
            created = client.post(
                "/api/agents",
                json={
                    "name": "ui-bot",
                    "template_id": "echo_test",
                },
            )
            assert created.status_code == 201
            assert created.json()["name"] == "ui-bot"
            assert created.json()["template_label"] == "Echo (Test)"

            updated = client.put(
                "/api/agents/ui-bot",
                json={
                    "template_id": "ollama",
                    "workdir": str(tmp_path),
                    "exec_command": "python3 examples/gateway_ollama/ollama_bridge.py",
                },
            )
            assert updated.status_code == 200
            updated_payload = updated.json()
            assert updated_payload["template_id"] == "ollama"
            assert updated_payload["workdir"] == str(tmp_path)

            stopped = client.post("/api/agents/ui-bot/stop", json={})
            assert stopped.status_code == 200
            assert stopped.json()["desired_state"] == "stopped"

            started = client.post("/api/agents/ui-bot/start", json={})
            assert started.status_code == 200
            assert started.json()["desired_state"] == "running"

            sent = client.post(
                "/api/agents/ui-bot/send",
                json={"content": "hello there", "to": "codex"},
            )
            assert sent.status_code == 201
            sent_payload = sent.json()
            assert sent_payload["agent"] == "ui-bot"
            assert sent_payload["content"] == "@codex hello there"

            tested = client.post("/api/agents/ui-bot/test", json={})
            assert tested.status_code == 201
            tested_payload = tested.json()
            assert tested_payload["target_agent"] == "ui-bot"
            # UI test endpoint defaults to user-authored per the invoking-principal
            # model (Madtank/supervisor 2026-05-02). The user identity comes from
            # the Gateway session; switchboard auto-creation is no longer reached.
            assert tested_payload["author"] == "user"
            assert tested_payload.get("sender_agent") in (None, "")
            assert (
                tested_payload["content"]
                == "@ui-bot Reply naturally that the Gateway round trip worked, then mention which local model answered."
            )

            doctored = client.post("/api/agents/ui-bot/doctor", json={})
            assert doctored.status_code == 201
            doctor_payload = doctored.json()
            assert doctor_payload["name"] == "ui-bot"
            assert doctor_payload["status"] in {"passed", "warning", "failed"}

            removed = client.delete("/api/agents/ui-bot")
            assert removed.status_code == 200
            assert removed.json()["name"] == "ui-bot"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2.0)


def test_gateway_ui_attach_launches_attached_session(monkeypatch, tmp_path):
    config_dir = tmp_path / "config"
    monkeypatch.setenv("AX_CONFIG_DIR", str(config_dir))
    gateway_core.save_gateway_session(
        {
            "token": "axp_u_test.token",
            "base_url": "https://paxai.app",
            "space_id": "space-1",
            "username": "codex",
        }
    )
    workdir = tmp_path / "roger"
    token_file = tmp_path / "roger.token"
    token_file.write_text("axp_a_agent.secret\n")
    registry = gateway_core.load_gateway_registry()
    registry["agents"] = [
        {
            "name": "roger",
            "agent_id": "agent-roger",
            "space_id": "space-1",
            "base_url": "https://paxai.app",
            "runtime_type": "claude_code_channel",
            "template_id": "claude_code_channel",
            "workdir": str(workdir),
            "desired_state": "stopped",
            "effective_state": "stale",
            "transport": "gateway",
            "credential_source": "gateway",
            "token_file": str(token_file),
            "attestation_state": "verified",
            "approval_state": "approved",
            "identity_status": "verified",
            "environment_status": "environment_allowed",
            "space_status": "active_allowed",
        }
    ]
    gateway_core.save_gateway_registry(registry)

    def fake_write_channel_setup(*, agent_name, workdir, **kwargs):
        return {
            "agent": agent_name,
            "space_id": "space-1",
            "base_url": "https://paxai.app",
            "mode": "local",
            "mcp_path": str(workdir / ".mcp.json"),
            "env_path": str(tmp_path / "roger.env"),
            "cli_config_path": str(workdir / ".ax" / "config.toml"),
            "cli_readme_path": str(workdir / ".ax" / "README.md"),
            "server_name": "ax-channel",
            "launch_command": f"claude --strict-mcp-config --mcp-config {workdir / '.mcp.json'} "
            "--dangerously-load-development-channels server:ax-channel",
        }

    from ax_cli.commands import channel as channel_mod

    monkeypatch.setattr(channel_mod, "write_channel_setup", fake_write_channel_setup)
    monkeypatch.setattr(
        _gw_ui,
        "_launch_attached_agent_session",
        lambda payload: {**payload, "launched": True, "launch_mode": "test", "message": "attached"},
    )

    handler = _gw_ui._build_gateway_ui_handler(activity_limit=5, refresh_ms=1500)
    with closing(socket.socket()) as probe:
        probe.bind(("127.0.0.1", 0))
        host, port = probe.getsockname()
    server = _gw_ui._GatewayUiServer((host, port), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with httpx.Client(base_url=f"http://{host}:{port}", timeout=2.0) as client:
            attached = client.post("/api/agents/roger/attach", json={})
            assert attached.status_code == 202
            payload = attached.json()
            assert payload["agent"] == "roger"
            assert payload["launched"] is True
            assert payload["launch_mode"] == "test"
            updated = gateway_core.find_agent_entry(gateway_core.load_gateway_registry(), "roger")
            assert updated["desired_state"] == "running"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2.0)


def test_gateway_ui_manual_attach_marks_attached_session_active(monkeypatch, tmp_path):
    config_dir = tmp_path / "config"
    monkeypatch.setenv("AX_CONFIG_DIR", str(config_dir))
    token_file = tmp_path / "roger.token"
    token_file.write_text("axp_a_agent.secret\n")
    registry = gateway_core.load_gateway_registry()
    registry["agents"] = [
        {
            "name": "roger",
            "agent_id": "agent-roger",
            "space_id": "space-1",
            "base_url": "https://paxai.app",
            "runtime_type": "claude_code_channel",
            "template_id": "claude_code_channel",
            "workdir": str(tmp_path / "roger"),
            "desired_state": "running",
            "effective_state": "stopped",
            "transport": "gateway",
            "credential_source": "gateway",
            "token_file": str(token_file),
            "attestation_state": "verified",
            "approval_state": "approved",
            "identity_status": "verified",
            "environment_status": "environment_allowed",
            "space_status": "active_allowed",
        }
    ]
    gateway_core.save_gateway_registry(registry)

    handler = _gw_ui._build_gateway_ui_handler(activity_limit=5, refresh_ms=1500)
    with closing(socket.socket()) as probe:
        probe.bind(("127.0.0.1", 0))
        host, port = probe.getsockname()
    server = _gw_ui._GatewayUiServer((host, port), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with httpx.Client(base_url=f"http://{host}:{port}", timeout=2.0) as client:
            marked = client.post("/api/agents/roger/manual-attach", json={"note": "already attached"})
            assert marked.status_code == 200
            payload = marked.json()
            assert payload["manual_attach_state"] == "attached"
            assert payload["connected"] is True
            assert payload["reachability"] == "live_now"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2.0)


def test_gateway_ui_external_runtime_announce_marks_plugin_active(monkeypatch, tmp_path):
    config_dir = tmp_path / "config"
    monkeypatch.setenv("AX_CONFIG_DIR", str(config_dir))
    registry = gateway_core.load_gateway_registry()
    registry["agents"] = [
        {
            "name": "nova",
            "agent_id": "agent-nova",
            "space_id": "space-1",
            "base_url": "https://paxai.app",
            "template_id": "hermes",
            "runtime_type": "sentinel_inference_sdk",
            "desired_state": "running",
            "effective_state": "stopped",
            "transport": "gateway",
            "credential_source": "gateway",
            "attestation_state": "verified",
            "approval_state": "approved",
            "identity_status": "verified",
            "environment_status": "environment_allowed",
            "space_status": "active_allowed",
        }
    ]
    gateway_core.save_gateway_registry(registry)

    handler = _gw_ui._build_gateway_ui_handler(activity_limit=5, refresh_ms=1500)
    with closing(socket.socket()) as probe:
        probe.bind(("127.0.0.1", 0))
        host, port = probe.getsockname()
    server = _gw_ui._GatewayUiServer((host, port), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with httpx.Client(base_url=f"http://{host}:{port}", timeout=2.0) as client:
            announced = client.post(
                "/api/agents/nova/external-runtime-announce",
                json={
                    "runtime_kind": "hermes_plugin",
                    "status": "connected",
                    "pid": 12345,
                    "activity": "Hermes plugin listener connected",
                },
            )
            assert announced.status_code == 200
            payload = announced.json()
            assert payload["connected"] is True
            assert payload["presence"] == "IDLE"
            assert payload["reachability"] == "live_now"
            assert payload["local_attach_state"] == "external_connected"
            assert payload["current_activity"] == "Hermes plugin listener connected"

            stored = gateway_core.find_agent_entry(gateway_core.load_gateway_registry(), "nova")
            assert stored["desired_state"] == "running"
            assert stored["effective_state"] == "running"
            assert stored["external_runtime_managed"] is True
            assert stored["external_runtime_state"] == "connected"
            assert stored["external_runtime_kind"] == "hermes_plugin"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2.0)


def test_gateway_ui_external_runtime_announce_respects_operator_stop(monkeypatch, tmp_path):
    config_dir = tmp_path / "config"
    monkeypatch.setenv("AX_CONFIG_DIR", str(config_dir))
    registry = gateway_core.load_gateway_registry()
    registry["agents"] = [
        {
            "name": "nova",
            "agent_id": "agent-nova",
            "space_id": "space-1",
            "base_url": "https://paxai.app",
            "template_id": "hermes",
            "runtime_type": "sentinel_inference_sdk",
            "desired_state": "stopped",
            "effective_state": "stopped",
            "transport": "gateway",
            "credential_source": "gateway",
            "attestation_state": "verified",
            "approval_state": "approved",
            "identity_status": "verified",
            "environment_status": "environment_allowed",
            "space_status": "active_allowed",
        }
    ]
    gateway_core.save_gateway_registry(registry)

    handler = _gw_ui._build_gateway_ui_handler(activity_limit=5, refresh_ms=1500)
    with closing(socket.socket()) as probe:
        probe.bind(("127.0.0.1", 0))
        host, port = probe.getsockname()
    server = _gw_ui._GatewayUiServer((host, port), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with httpx.Client(base_url=f"http://{host}:{port}", timeout=2.0) as client:
            announced = client.post(
                "/api/agents/nova/external-runtime-announce",
                json={
                    "runtime_kind": "hermes_plugin",
                    "status": "connected",
                    "pid": 12345,
                    "activity": "Hermes plugin listener connected",
                },
            )
            assert announced.status_code == 200
            payload = announced.json()
            assert payload["connected"] is False
            assert payload["effective_state"] == "stopped"
            assert payload["desired_state"] == "stopped"
            assert payload["local_attach_state"] == "external_stopped"

            stored = gateway_core.find_agent_entry(gateway_core.load_gateway_registry(), "nova")
            assert stored["desired_state"] == "stopped"
            assert stored["effective_state"] == "stopped"
            assert stored["runtime_instance_id"] is None
            assert stored["external_runtime_managed"] is True
            assert stored["external_runtime_state"] == "connected"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2.0)


def test_gateway_ui_create_starts_claude_code_channel(monkeypatch, tmp_path):
    config_dir = tmp_path / "config"
    monkeypatch.setenv("AX_CONFIG_DIR", str(config_dir))
    gateway_core.save_gateway_session(
        {
            "token": "axp_u_test.token",
            "base_url": "https://paxai.app",
            "space_id": "space-1",
            "username": "codex",
        }
    )
    workdir = tmp_path / "sam"
    launched = {}

    def fake_register(**kwargs):
        return {
            "name": kwargs["name"],
            "agent_id": "agent-sam",
            "space_id": "space-1",
            "base_url": "https://paxai.app",
            "runtime_type": "claude_code_channel",
            "template_id": "claude_code_channel",
            "workdir": str(workdir),
            "desired_state": "running",
            "effective_state": "stopped",
            "transport": "gateway",
            "credential_source": "gateway",
        }

    def fake_prepare(name):
        return {
            "agent": name,
            "mcp_path": str(workdir / ".mcp.json"),
            "launch_command": "claude --strict-mcp-config --mcp-config .mcp.json",
        }

    def fake_launch(payload):
        launched.update(payload)
        return {**payload, "launched": True, "launch_mode": "test"}

    monkeypatch.setattr(_gw_ui, "_register_managed_agent", fake_register)
    monkeypatch.setattr(_gw_ui, "_prepare_attached_agent_payload", fake_prepare)
    monkeypatch.setattr(_gw_ui, "_launch_attached_agent_session", fake_launch)

    handler = _gw_ui._build_gateway_ui_handler(activity_limit=5, refresh_ms=1500)
    with closing(socket.socket()) as probe:
        probe.bind(("127.0.0.1", 0))
        host, port = probe.getsockname()
    server = _gw_ui._GatewayUiServer((host, port), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with httpx.Client(base_url=f"http://{host}:{port}", timeout=2.0) as client:
            response = client.post(
                "/api/agents",
                json={"name": "sam", "template_id": "claude_code_channel", "workdir": str(workdir)},
            )
            assert response.status_code == 201
            assert response.json()["desired_state"] == "running"
            assert launched["agent"] == "sam"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2.0)


def test_gateway_activity_command_filters_by_message_id(monkeypatch, tmp_path):
    monkeypatch.setenv("AX_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("AX_GATEWAY_DIR", str(tmp_path / "gw"))
    entry = {"name": "agent-a", "agent_id": "a-1", "runtime_type": "ollama"}

    # Two messages interleaved.
    gateway_core.record_gateway_activity("message_received", entry=entry, message_id="msg-A")
    gateway_core.record_gateway_activity("message_received", entry=entry, message_id="msg-B")
    gateway_core.record_gateway_activity("message_claimed", entry=entry, message_id="msg-A")
    gateway_core.record_gateway_activity("tool_started", entry=entry, message_id="msg-A", tool_name="bash")
    gateway_core.record_gateway_activity("reply_sent", entry=entry, message_id="msg-B")
    gateway_core.record_gateway_activity("reply_sent", entry=entry, message_id="msg-A")

    result = runner.invoke(app, ["gateway", "activity", "--message-id", "msg-A", "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["message_id"] == "msg-A"
    events = [item["event"] for item in payload["events"]]
    assert events == ["message_received", "message_claimed", "tool_started", "reply_sent"]
    phases = [item.get("phase") for item in payload["events"]]
    assert phases == ["received", "claimed", "tool", "reply"]
    # Every event row carries the message id we asked for.
    assert all(item.get("message_id") == "msg-A" for item in payload["events"])


def test_gateway_activity_command_orders_chronologically_under_jitter(monkeypatch, tmp_path):
    monkeypatch.setenv("AX_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("AX_GATEWAY_DIR", str(tmp_path / "gw"))
    # Append unsorted to the JSONL — reader must order by ts, not file order.
    log = gateway_core.activity_log_path()
    log.parent.mkdir(parents=True, exist_ok=True)
    rows = [
        {"ts": "2026-04-29T10:00:02Z", "event": "message_claimed", "message_id": "msg-X", "phase": "claimed"},
        {"ts": "2026-04-29T10:00:01Z", "event": "message_received", "message_id": "msg-X", "phase": "received"},
        {"ts": "2026-04-29T10:00:03Z", "event": "reply_sent", "message_id": "msg-X", "phase": "reply"},
    ]
    log.write_text("".join(json.dumps(r) + "\n" for r in rows))

    result = runner.invoke(app, ["gateway", "activity", "--message-id", "msg-X", "--json"])
    assert result.exit_code == 0, result.output
    events = [item["event"] for item in json.loads(result.output)["events"]]
    assert events == ["message_received", "message_claimed", "reply_sent"]


def test_gateway_activity_command_filters_by_agent(monkeypatch, tmp_path):
    monkeypatch.setenv("AX_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("AX_GATEWAY_DIR", str(tmp_path / "gw"))
    entry_a = {"name": "agent-a", "agent_id": "a-1"}
    entry_b = {"name": "agent-b", "agent_id": "b-1"}
    gateway_core.record_gateway_activity("message_received", entry=entry_a, message_id="msg-1")
    gateway_core.record_gateway_activity("message_received", entry=entry_b, message_id="msg-2")

    result = runner.invoke(app, ["gateway", "activity", "--agent", "agent-a", "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert all(item.get("agent_name") == "agent-a" for item in payload["events"])
    assert {item.get("message_id") for item in payload["events"]} == {"msg-1"}


def test_gateway_activity_command_returns_empty_when_no_match(monkeypatch, tmp_path):
    monkeypatch.setenv("AX_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("AX_GATEWAY_DIR", str(tmp_path / "gw"))
    result = runner.invoke(app, ["gateway", "activity", "--message-id", "nope", "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload == {"message_id": "nope", "events": []}


def test_gateway_activity_command_does_not_emit_credentials(monkeypatch, tmp_path):
    """Defense-in-depth: if a runtime ever writes a token-shaped string into
    the activity log, the inspector must surface it as the consumer would
    see it without redaction (this is a read of disk content the daemon
    already owns), AND the command must not introduce any new credential
    surface — it must not require auth or call the backend."""
    monkeypatch.setenv("AX_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("AX_GATEWAY_DIR", str(tmp_path / "gw"))

    def _explode(*args, **kwargs):
        raise AssertionError("activity command must not construct an AxClient")

    monkeypatch.setattr(_gw_auth, "AxClient", _explode)
    gateway_core.record_gateway_activity(
        "message_received",
        entry={"name": "agent-a", "agent_id": "a-1"},
        message_id="msg-1",
    )
    result = runner.invoke(app, ["gateway", "activity", "--message-id", "msg-1", "--json"])
    assert result.exit_code == 0, result.output


class TestIsRequestHostAllowed:
    def test_localhost_allowed(self):
        assert _gw_ui._is_request_host_allowed("localhost") is True

    def test_localhost_with_port(self):
        assert _gw_ui._is_request_host_allowed("localhost:8765") is True

    def test_loopback_ip(self):
        assert _gw_ui._is_request_host_allowed("127.0.0.1") is True

    def test_loopback_ip_with_port(self):
        assert _gw_ui._is_request_host_allowed("127.0.0.1:9999") is True

    def test_external_host_rejected(self):
        assert _gw_ui._is_request_host_allowed("evil.com") is False

    def test_none_rejected(self):
        assert _gw_ui._is_request_host_allowed(None) is False

    def test_empty_rejected(self):
        assert _gw_ui._is_request_host_allowed("") is False

    def test_whitespace_only_rejected(self):
        assert _gw_ui._is_request_host_allowed("   ") is False

    def test_case_insensitive(self):
        assert _gw_ui._is_request_host_allowed("LOCALHOST") is True
        assert _gw_ui._is_request_host_allowed("LocalHost:8080") is True


class TestActivityCommand:
    def test_activity_json_empty(self, monkeypatch, tmp_path):
        monkeypatch.setattr(_gw_ui, "activity_log_path", lambda: tmp_path / "activity.jsonl")
        result = runner.invoke(app, ["gateway", "activity", "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["events"] == []

    def test_activity_json_with_data(self, monkeypatch, tmp_path):
        log = tmp_path / "activity.jsonl"
        events = [
            {"ts": "2026-01-01T00:00:00", "event": "test", "agent_name": "bot1"},
            {"ts": "2026-01-01T00:01:00", "event": "test2", "agent_name": "bot2"},
        ]
        log.write_text("\n".join(json.dumps(e) for e in events))
        monkeypatch.setattr(_gw_ui, "activity_log_path", lambda: log)
        result = runner.invoke(app, ["gateway", "activity", "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert len(data["events"]) == 2

    def test_activity_filter_agent(self, monkeypatch, tmp_path):
        log = tmp_path / "activity.jsonl"
        events = [
            {"ts": "2026-01-01T00:00:00", "event": "test", "agent_name": "bot1"},
            {"ts": "2026-01-01T00:01:00", "event": "test2", "agent_name": "bot2"},
        ]
        log.write_text("\n".join(json.dumps(e) for e in events))
        monkeypatch.setattr(_gw_ui, "activity_log_path", lambda: log)
        result = runner.invoke(app, ["gateway", "activity", "--agent", "bot1", "--json"])
        data = json.loads(result.output)
        assert len(data["events"]) == 1
        assert data["events"][0]["agent_name"] == "bot1"

    def test_activity_filter_message_id(self, monkeypatch, tmp_path):
        log = tmp_path / "activity.jsonl"
        events = [
            {"ts": "2026-01-01T00:00:00", "event": "send", "message_id": "msg-1"},
            {"ts": "2026-01-01T00:01:00", "event": "recv", "message_id": "msg-2"},
        ]
        log.write_text("\n".join(json.dumps(e) for e in events))
        monkeypatch.setattr(_gw_ui, "activity_log_path", lambda: log)
        result = runner.invoke(app, ["gateway", "activity", "--message-id", "msg-1", "--json"])
        data = json.loads(result.output)
        assert len(data["events"]) == 1
        assert data["message_id"] == "msg-1"

    def test_activity_limit(self, monkeypatch, tmp_path):
        log = tmp_path / "activity.jsonl"
        events = [{"ts": f"2026-01-01T00:0{i}:00", "event": f"ev{i}"} for i in range(5)]
        log.write_text("\n".join(json.dumps(e) for e in events))
        monkeypatch.setattr(_gw_ui, "activity_log_path", lambda: log)
        result = runner.invoke(app, ["gateway", "activity", "--limit", "2", "--json"])
        data = json.loads(result.output)
        assert len(data["events"]) == 2

    def test_activity_text_no_data(self, monkeypatch, tmp_path):
        monkeypatch.setattr(_gw_ui, "activity_log_path", lambda: tmp_path / "nope.jsonl")
        result = runner.invoke(app, ["gateway", "activity"])
        assert result.exit_code == 0
        assert "No Gateway activity" in _strip(result.output)


class TestReadJsonRequest:
    def test_empty_content_length_returns_empty_dict(self):
        handler = MagicMock(spec=BaseHTTPRequestHandler)
        handler.headers = {"Content-Length": "0"}
        assert _gw_ui._read_json_request(handler) == {}

    def test_valid_json_body(self):
        body = json.dumps({"key": "val"}).encode()
        handler = MagicMock(spec=BaseHTTPRequestHandler)
        handler.headers = {"Content-Length": str(len(body))}
        handler.rfile = io.BytesIO(body)
        assert _gw_ui._read_json_request(handler) == {"key": "val"}

    def test_non_object_body_raises(self):
        body = json.dumps([1, 2, 3]).encode()
        handler = MagicMock(spec=BaseHTTPRequestHandler)
        handler.headers = {"Content-Length": str(len(body))}
        handler.rfile = io.BytesIO(body)
        with pytest.raises(ValueError, match="must be an object"):
            _gw_ui._read_json_request(handler)

    def test_invalid_json_raises(self):
        body = b"{invalid"
        handler = MagicMock(spec=BaseHTTPRequestHandler)
        handler.headers = {"Content-Length": str(len(body))}
        handler.rfile = io.BytesIO(body)
        with pytest.raises(ValueError, match="Invalid JSON"):
            _gw_ui._read_json_request(handler)

    def test_no_content_length_header(self):
        handler = MagicMock(spec=BaseHTTPRequestHandler)
        handler.headers = {}
        assert _gw_ui._read_json_request(handler) == {}

    def test_empty_rfile_returns_empty(self):
        handler = MagicMock(spec=BaseHTTPRequestHandler)
        handler.headers = {"Content-Length": "10"}
        handler.rfile = io.BytesIO(b"")
        assert _gw_ui._read_json_request(handler) == {}


class TestGatewayUiHandlerGET:
    """Tests for do_GET in _build_gateway_ui_handler."""

    def test_forbidden_host(self, monkeypatch):
        status, body, _ = _invoke_handler("GET", "/", host="evil.com", monkeypatch=monkeypatch)
        assert status == 403
        data = _json_response(status, body)
        assert "Forbidden" in data.get("error", "")

    @pytest.mark.parametrize("path", ["/", "/demo"])
    def test_demo_routes_return_html(self, monkeypatch, path):
        monkeypatch.setattr(_gw_ui, "_render_gateway_demo_page", lambda **kw: "<html>demo</html>")
        status, body, _ = _invoke_handler("GET", path, monkeypatch=monkeypatch)
        assert status == 200
        assert b"demo" in body

    def test_operator_returns_html(self, monkeypatch):
        monkeypatch.setattr(_gw_ui, "_render_gateway_ui_page", lambda **kw: "<html>operator</html>")
        status, body, _ = _invoke_handler("GET", "/operator", monkeypatch=monkeypatch)
        assert status == 200

    def test_healthz(self, monkeypatch):
        status, body, _ = _invoke_handler("GET", "/healthz", monkeypatch=monkeypatch)
        assert status == 200
        data = _json_response(status, body)
        assert data["ok"] is True

    def test_favicon(self, monkeypatch):
        status, body, _ = _invoke_handler("GET", "/favicon.svg", monkeypatch=monkeypatch)
        assert status == 200
        assert b"<svg" in body or b"svg" in body

    def test_favicon_ico(self, monkeypatch):
        status, body, _ = _invoke_handler("GET", "/favicon.ico", monkeypatch=monkeypatch)
        assert status == 200

    def test_api_status(self, monkeypatch):
        payload = {"agents": [], "summary": {}, "recent_activity": []}
        monkeypatch.setattr(_gw_ui, "_status_payload", lambda **kw: payload)
        status, body, _ = _invoke_handler("GET", "/api/status", monkeypatch=monkeypatch)
        assert status == 200

    def test_api_status_with_all_flag(self, monkeypatch):
        payload = {"agents": [], "summary": {}, "recent_activity": []}
        captured = {}

        def _mock_status(**kw):
            captured.update(kw)
            return payload

        monkeypatch.setattr(_gw_ui, "_status_payload", _mock_status)
        status, body, _ = _invoke_handler("GET", "/api/status?all=true", monkeypatch=monkeypatch)
        assert status == 200
        assert captured.get("include_hidden") is True

    def test_local_inbox(self, monkeypatch):
        monkeypatch.setattr(_gw_ui, "_local_session_inbox", lambda **kw: {"messages": [], "agent": "bot1"})
        status, body, _ = _invoke_handler(
            "GET",
            "/local/inbox?limit=5&channel=main",
            headers={"X-Gateway-Session": "tok-1"},
            monkeypatch=monkeypatch,
        )
        assert status == 200

    def test_local_sessions(self, monkeypatch):
        monkeypatch.setattr(_gw_ui, "load_gateway_registry", lambda: {"local_sessions": [{"name": "s1"}]})
        status, body, _ = _invoke_handler("GET", "/local/sessions", monkeypatch=monkeypatch)
        assert status == 200
        data = _json_response(status, body)
        assert data["count"] == 1

    def test_api_runtime_types(self, monkeypatch):
        monkeypatch.setattr(_gw_ui, "_runtime_types_payload", lambda: {"runtime_types": []})
        status, body, _ = _invoke_handler("GET", "/api/runtime-types", monkeypatch=monkeypatch)
        assert status == 200

    def test_api_templates(self, monkeypatch):
        monkeypatch.setattr(_gw_ui, "_agent_templates_payload", lambda: {"templates": []})
        status, body, _ = _invoke_handler("GET", "/api/templates", monkeypatch=monkeypatch)
        assert status == 200

    def test_api_approvals(self, monkeypatch):
        monkeypatch.setattr(_gw_ui, "_approval_rows_payload", lambda **kw: {"approvals": [], "count": 0, "pending": 0})
        status, body, _ = _invoke_handler("GET", "/api/approvals", monkeypatch=monkeypatch)
        assert status == 200

    def test_api_approvals_with_status_filter(self, monkeypatch):
        captured = {}

        def _mock(**kw):
            captured.update(kw)
            return {"approvals": [], "count": 0, "pending": 0}

        monkeypatch.setattr(_gw_ui, "_approval_rows_payload", _mock)
        status, body, _ = _invoke_handler("GET", "/api/approvals?status=pending", monkeypatch=monkeypatch)
        assert status == 200
        assert captured.get("status") == "pending"

    def test_api_approval_detail(self, monkeypatch):
        monkeypatch.setattr(
            _gw_ui,
            "_approval_detail_payload",
            lambda aid: {"approval": {"approval_id": aid}},
        )
        status, body, _ = _invoke_handler("GET", "/api/approvals/appr-1", monkeypatch=monkeypatch)
        assert status == 200
        data = _json_response(status, body)
        assert data["approval"]["approval_id"] == "appr-1"

    def test_api_approval_detail_not_found(self, monkeypatch):
        def _raise(aid):
            raise LookupError("not found")

        monkeypatch.setattr(_gw_ui, "_approval_detail_payload", _raise)
        status, body, _ = _invoke_handler("GET", "/api/approvals/bad", monkeypatch=monkeypatch)
        assert status == 404

    def test_api_spaces_with_data(self, monkeypatch):
        monkeypatch.setattr(
            _gw_ui,
            "_spaces_payload",
            lambda: {"spaces": [{"id": "sp-1"}], "active_space_id": "sp-1"},
        )
        status, body, _ = _invoke_handler("GET", "/api/spaces", monkeypatch=monkeypatch)
        assert status == 200

    def test_api_spaces_no_data(self, monkeypatch):
        monkeypatch.setattr(_gw_ui, "_spaces_payload", lambda: {"spaces": [], "active_space_id": None})
        status, body, _ = _invoke_handler("GET", "/api/spaces", monkeypatch=monkeypatch)
        assert status == 503

    def test_api_agents_inbox(self, monkeypatch):
        monkeypatch.setattr(
            _gw_ui,
            "_inbox_for_managed_agent",
            lambda **kw: {"messages": [], "agent": "bot1"},
        )
        status, body, _ = _invoke_handler("GET", "/api/agents/bot1/inbox", monkeypatch=monkeypatch)
        assert status == 200

    def test_api_agents_inbox_not_found(self, monkeypatch):
        def _raise(**kw):
            raise LookupError("not found")

        monkeypatch.setattr(_gw_ui, "_inbox_for_managed_agent", _raise)
        status, body, _ = _invoke_handler("GET", "/api/agents/bot1/inbox", monkeypatch=monkeypatch)
        assert status == 404

    def test_api_agents_inbox_bad_request(self, monkeypatch):
        def _raise(**kw):
            raise ValueError("bad param")

        monkeypatch.setattr(_gw_ui, "_inbox_for_managed_agent", _raise)
        status, body, _ = _invoke_handler("GET", "/api/agents/bot1/inbox", monkeypatch=monkeypatch)
        assert status == 400

    def test_api_agents_detail(self, monkeypatch):
        monkeypatch.setattr(
            _gw_ui,
            "_agent_detail_payload",
            lambda name, **kw: {"agent": {"name": name}, "recent_activity": []},
        )
        status, body, _ = _invoke_handler("GET", "/api/agents/bot1", monkeypatch=monkeypatch)
        assert status == 200
        data = _json_response(status, body)
        assert data["agent"]["name"] == "bot1"

    def test_api_agents_detail_not_found(self, monkeypatch):
        monkeypatch.setattr(_gw_ui, "_agent_detail_payload", lambda name, **kw: None)
        status, body, _ = _invoke_handler("GET", "/api/agents/bot1", monkeypatch=monkeypatch)
        assert status == 404

    def test_unknown_path(self, monkeypatch):
        status, body, _ = _invoke_handler("GET", "/unknown", monkeypatch=monkeypatch)
        assert status == 404


class TestGatewayUiHandlerPOST:
    """Tests for do_POST in _build_gateway_ui_handler."""

    def test_post_forbidden_host(self, monkeypatch):
        status, body, _ = _invoke_handler("POST", "/api/agents", body={}, host="evil.com", monkeypatch=monkeypatch)
        assert status == 403

    def test_post_templates_install_not_allowed(self, monkeypatch):
        status, body, _ = _invoke_handler(
            "POST", "/api/templates/bogus_template/install", body={}, monkeypatch=monkeypatch
        )
        assert status == 400
        data = _json_response(status, body)
        assert "allowlist" in data.get("error", "")

    def test_post_templates_install_no_session(self, monkeypatch):
        monkeypatch.setattr(_gw_ui, "load_gateway_session", lambda: None)
        # Use a real template from the allowlist
        template_id = list(_gw_ui._RUNTIME_INSTALL_RECIPES.keys())[0] if _gw_ui._RUNTIME_INSTALL_RECIPES else "hermes"
        status, body, _ = _invoke_handler(
            "POST", f"/api/templates/{template_id}/install", body={}, monkeypatch=monkeypatch
        )
        assert status == 403
        data = _json_response(status, body)
        assert "login" in data.get("error", "").lower()

    def test_post_templates_install_success(self, monkeypatch):
        monkeypatch.setattr(_gw_ui, "load_gateway_session", lambda: {"token": "axp_u_x"})
        monkeypatch.setattr(
            _gw_ui,
            "_install_runtime_payload",
            lambda tid, **kw: {"ready": True, "target": "/opt/hermes"},
        )
        template_id = list(_gw_ui._RUNTIME_INSTALL_RECIPES.keys())[0] if _gw_ui._RUNTIME_INSTALL_RECIPES else "hermes"
        status, body, _ = _invoke_handler(
            "POST", f"/api/templates/{template_id}/install", body={}, monkeypatch=monkeypatch
        )
        assert status == 200

    def test_post_templates_install_not_ready(self, monkeypatch):
        monkeypatch.setattr(_gw_ui, "load_gateway_session", lambda: {"token": "axp_u_x"})
        monkeypatch.setattr(
            _gw_ui,
            "_install_runtime_payload",
            lambda tid, **kw: {"ready": False, "target": "/opt/hermes"},
        )
        template_id = list(_gw_ui._RUNTIME_INSTALL_RECIPES.keys())[0] if _gw_ui._RUNTIME_INSTALL_RECIPES else "hermes"
        status, body, _ = _invoke_handler(
            "POST", f"/api/templates/{template_id}/install", body={}, monkeypatch=monkeypatch
        )
        assert status == 422

    def test_post_templates_install_permission_error(self, monkeypatch):
        monkeypatch.setattr(_gw_ui, "load_gateway_session", lambda: {"token": "axp_u_x"})

        def _raise(tid, **kw):
            raise PermissionError("no perm")

        monkeypatch.setattr(_gw_ui, "_install_runtime_payload", _raise)
        template_id = list(_gw_ui._RUNTIME_INSTALL_RECIPES.keys())[0] if _gw_ui._RUNTIME_INSTALL_RECIPES else "hermes"
        status, body, _ = _invoke_handler(
            "POST", f"/api/templates/{template_id}/install", body={}, monkeypatch=monkeypatch
        )
        assert status == 403

    def test_post_templates_install_value_error(self, monkeypatch):
        monkeypatch.setattr(_gw_ui, "load_gateway_session", lambda: {"token": "axp_u_x"})

        def _raise(tid, **kw):
            raise ValueError("bad value")

        monkeypatch.setattr(_gw_ui, "_install_runtime_payload", _raise)
        template_id = list(_gw_ui._RUNTIME_INSTALL_RECIPES.keys())[0] if _gw_ui._RUNTIME_INSTALL_RECIPES else "hermes"
        status, body, _ = _invoke_handler(
            "POST", f"/api/templates/{template_id}/install", body={}, monkeypatch=monkeypatch
        )
        assert status == 400

    def test_post_agents_register(self, monkeypatch):
        entry = {"name": "bot1", "desired_state": "running"}
        monkeypatch.setattr(_gw_ui, "_register_managed_agent", lambda **kw: entry)
        monkeypatch.setattr(_gw_ui, "gateway_core", MagicMock())
        monkeypatch.setattr(
            _gw_ui.gateway_core, "infer_operator_profile", lambda e: {"placement": "hosted", "activation": "on_demand"}
        )
        status, body, _ = _invoke_handler("POST", "/api/agents", body={"name": "bot1"}, monkeypatch=monkeypatch)
        assert status == 201

    def test_post_agents_rate_limited(self, monkeypatch):
        def _raise(**kw):
            raise _gw_ui.UpstreamRateLimitedError(Exception("429"), 1)

        monkeypatch.setattr(_gw_ui, "_register_managed_agent", _raise)
        status, body, _ = _invoke_handler("POST", "/api/agents", body={"name": "bot1"}, monkeypatch=monkeypatch)
        assert status == 429
        data = _json_response(status, body)
        assert "rate" in data.get("error", "").lower()

    def test_post_agents_cleanup_hide(self, monkeypatch):
        monkeypatch.setattr(_gw_ui, "_hide_managed_agents", lambda names, **kw: {"hidden": names, "count": len(names)})
        status, body, _ = _invoke_handler(
            "POST", "/api/agents/cleanup-hide", body={"names": ["bot1", "bot2"]}, monkeypatch=monkeypatch
        )
        assert status == 200

    def test_post_agents_cleanup_hide_bad_names(self, monkeypatch):
        status, body, _ = _invoke_handler(
            "POST", "/api/agents/cleanup-hide", body={"names": "not a list"}, monkeypatch=monkeypatch
        )
        assert status == 400

    def test_post_agents_cleanup_restore(self, monkeypatch):
        monkeypatch.setattr(
            _gw_ui, "_restore_hidden_managed_agents", lambda names: {"restored": names, "count": len(names)}
        )
        status, body, _ = _invoke_handler(
            "POST", "/api/agents/cleanup-restore", body={"names": ["bot1"]}, monkeypatch=monkeypatch
        )
        assert status == 200

    def test_post_agents_cleanup_restore_bad_names(self, monkeypatch):
        status, body, _ = _invoke_handler(
            "POST", "/api/agents/cleanup-restore", body={"names": 42}, monkeypatch=monkeypatch
        )
        assert status == 400

    def test_post_agents_recover(self, monkeypatch):
        monkeypatch.setattr(
            _gw_ui,
            "_recover_managed_agents_from_evidence",
            lambda names: {"recovered": [], "count": 0},
        )
        status, body, _ = _invoke_handler(
            "POST", "/api/agents/recover", body={"names": ["bot1"]}, monkeypatch=monkeypatch
        )
        assert status == 200

    def test_post_agents_recover_bad_names(self, monkeypatch):
        status, body, _ = _invoke_handler("POST", "/api/agents/recover", body={"names": "bad"}, monkeypatch=monkeypatch)
        assert status == 400

    def test_post_agents_recover_value_error(self, monkeypatch):
        def _raise(names):
            raise ValueError("broken")

        monkeypatch.setattr(_gw_ui, "_recover_managed_agents_from_evidence", _raise)
        status, body, _ = _invoke_handler(
            "POST", "/api/agents/recover", body={"names": ["bot1"]}, monkeypatch=monkeypatch
        )
        assert status == 400

    @pytest.mark.parametrize(
        ("payload", "expected_status"),
        [
            ({"status": "approved", "session_token": "tok"}, 200),
            ({"status": "pending"}, 202),
        ],
    )
    def test_post_local_connect_statuses(self, monkeypatch, payload, expected_status):
        monkeypatch.setattr(
            _gw_ui,
            "_connect_local_pass_through_agent",
            lambda **kw: payload,
        )
        status, body, _ = _invoke_handler(
            "POST", "/local/connect", body={"agent_name": "bot1"}, monkeypatch=monkeypatch
        )
        assert status == expected_status

    def test_post_local_send(self, monkeypatch):
        monkeypatch.setattr(
            _gw_ui,
            "_send_local_session_message",
            lambda **kw: {"agent": "bot1", "message": {"id": "m1"}},
        )
        status, body, _ = _invoke_handler(
            "POST",
            "/local/send",
            body={"content": "hello"},
            headers={"X-Gateway-Session": "tok-1"},
            monkeypatch=monkeypatch,
        )
        assert status == 201

    def test_post_local_tasks(self, monkeypatch):
        monkeypatch.setattr(
            _gw_ui,
            "_create_local_session_task",
            lambda **kw: {"task_id": "t1"},
        )
        status, body, _ = _invoke_handler(
            "POST",
            "/local/tasks",
            body={"title": "task"},
            headers={"X-Gateway-Session": "tok-1"},
            monkeypatch=monkeypatch,
        )
        assert status == 201

    def test_post_local_proxy(self, monkeypatch):
        monkeypatch.setattr(
            _gw_ui,
            "_proxy_local_session_call",
            lambda **kw: {"result": "ok"},
        )
        status, body, _ = _invoke_handler(
            "POST",
            "/local/proxy",
            body={"method": "list_messages"},
            headers={"X-Gateway-Session": "tok-1"},
            monkeypatch=monkeypatch,
        )
        assert status == 200

    @pytest.mark.parametrize(
        ("exc", "expected_status"),
        [
            (LookupError("missing"), 404),
            (ValueError("bad"), 400),
        ],
    )
    def test_post_local_proxy_error_mapping(self, monkeypatch, exc, expected_status):
        monkeypatch.setattr(_gw_ui, "_proxy_local_session_call", lambda **kw: (_ for _ in ()).throw(exc))
        status, body, _ = _invoke_handler(
            "POST",
            "/local/proxy",
            body={},
            headers={"X-Gateway-Session": "tok"},
            monkeypatch=monkeypatch,
        )
        assert status == expected_status

    def test_post_agents_start(self, monkeypatch):
        monkeypatch.setattr(
            _gw_ui, "_set_managed_agent_desired_state", lambda name, state: {"name": name, "desired_state": state}
        )
        status, body, _ = _invoke_handler("POST", "/api/agents/bot1/start", body={}, monkeypatch=monkeypatch)
        assert status == 200

    def test_post_agents_stop(self, monkeypatch):
        monkeypatch.setattr(
            _gw_ui, "_set_managed_agent_desired_state", lambda name, state: {"name": name, "desired_state": state}
        )
        status, body, _ = _invoke_handler("POST", "/api/agents/bot1/stop", body={}, monkeypatch=monkeypatch)
        assert status == 200

    def test_post_agents_attach(self, monkeypatch):
        payload = {"mcp_path": "/tmp/w/.ax/mcp.json", "launch_mode": "bg"}
        monkeypatch.setattr(_gw_ui, "_prepare_attached_agent_payload", lambda name: payload)
        monkeypatch.setattr(_gw_ui, "_launch_attached_agent_session", lambda p: {**p, "launched": True})
        monkeypatch.setattr(_gw_ui, "record_gateway_activity", lambda *a, **kw: None)
        status, body, _ = _invoke_handler("POST", "/api/agents/bot1/attach", body={}, monkeypatch=monkeypatch)
        assert status == 202

    def test_post_agents_manual_attach(self, monkeypatch):
        monkeypatch.setattr(
            _gw_ui, "_mark_attached_agent_session", lambda name, **kw: {"name": name, "state": "active"}
        )
        status, body, _ = _invoke_handler("POST", "/api/agents/bot1/manual-attach", body={}, monkeypatch=monkeypatch)
        assert status == 200

    def test_post_agents_manual_attach_error(self, monkeypatch):
        def _raise(name, **kw):
            raise LookupError("missing")

        monkeypatch.setattr(_gw_ui, "_mark_attached_agent_session", _raise)
        status, body, _ = _invoke_handler("POST", "/api/agents/bot1/manual-attach", body={}, monkeypatch=monkeypatch)
        assert status == 400

    def test_post_agents_external_runtime_announce(self, monkeypatch):
        monkeypatch.setattr(
            _gw_ui, "_announce_external_agent_runtime", lambda name, body: {"name": name, "status": "ok"}
        )
        status, body, _ = _invoke_handler(
            "POST", "/api/agents/bot1/external-runtime-announce", body={"runtime": "ext"}, monkeypatch=monkeypatch
        )
        assert status == 200

    def test_post_agents_external_runtime_announce_not_found(self, monkeypatch):
        def _raise(name, body):
            raise LookupError("not found")

        monkeypatch.setattr(_gw_ui, "_announce_external_agent_runtime", _raise)
        status, body, _ = _invoke_handler(
            "POST", "/api/agents/bot1/external-runtime-announce", body={}, monkeypatch=monkeypatch
        )
        assert status == 404

    def test_post_agents_external_runtime_announce_value_error(self, monkeypatch):
        def _raise(name, body):
            raise ValueError("bad")

        monkeypatch.setattr(_gw_ui, "_announce_external_agent_runtime", _raise)
        status, body, _ = _invoke_handler(
            "POST", "/api/agents/bot1/external-runtime-announce", body={}, monkeypatch=monkeypatch
        )
        assert status == 400

    def test_post_agents_send(self, monkeypatch):
        monkeypatch.setattr(
            _gw_ui,
            "_send_from_managed_agent",
            lambda **kw: {"agent": "bot1", "message": {"id": "m1"}, "content": "hi"},
        )
        status, body, _ = _invoke_handler(
            "POST", "/api/agents/bot1/send", body={"content": "hello"}, monkeypatch=monkeypatch
        )
        assert status == 201

    def test_post_agents_test(self, monkeypatch):
        monkeypatch.setattr(
            _gw_ui,
            "_send_gateway_test_to_managed_agent",
            lambda name, **kw: {"target_agent": name, "message": {"id": "m1"}},
        )
        status, body, _ = _invoke_handler("POST", "/api/agents/bot1/test", body={}, monkeypatch=monkeypatch)
        assert status == 201

    def test_post_agents_ack(self, monkeypatch):
        monkeypatch.setattr(
            _gw_ui,
            "_ack_managed_agent_message",
            lambda name, **kw: {"acked": True},
        )
        status, body, _ = _invoke_handler(
            "POST",
            "/api/agents/bot1/ack",
            body={"message_id": "m1", "reply_id": "r1"},
            monkeypatch=monkeypatch,
        )
        assert status == 200

    def test_post_agents_move(self, monkeypatch):
        monkeypatch.setattr(
            _gw_ui,
            "_move_managed_agent_space",
            lambda name, sid, **kw: {"space_id": sid},
        )
        status, body, _ = _invoke_handler(
            "POST", "/api/agents/bot1/move", body={"space_id": "sp-2"}, monkeypatch=monkeypatch
        )
        assert status == 200

    def test_post_agents_system_prompt(self, monkeypatch):
        monkeypatch.setattr(
            _gw_ui,
            "_update_managed_agent",
            lambda **kw: {"name": "bot1", "system_prompt": kw.get("system_prompt")},
        )
        status, body, _ = _invoke_handler(
            "POST",
            "/api/agents/bot1/system-prompt",
            body={"system_prompt": "You are a helper"},
            monkeypatch=monkeypatch,
        )
        assert status == 200

    def test_post_agents_system_prompt_clear(self, monkeypatch):
        captured = {}

        def _mock(**kw):
            captured.update(kw)
            return {"name": "bot1"}

        monkeypatch.setattr(_gw_ui, "_update_managed_agent", _mock)
        status, body, _ = _invoke_handler("POST", "/api/agents/bot1/system-prompt", body={}, monkeypatch=monkeypatch)
        assert status == 200
        assert captured.get("system_prompt") == ""

    def test_post_agents_pin(self, monkeypatch):
        monkeypatch.setattr(
            _gw_ui,
            "_set_managed_agent_pin",
            lambda name, pinned: {"name": name, "pinned": pinned},
        )
        status, body, _ = _invoke_handler(
            "POST", "/api/agents/bot1/pin", body={"pinned": True}, monkeypatch=monkeypatch
        )
        assert status == 200

    def test_post_agents_doctor(self, monkeypatch):
        monkeypatch.setattr(
            _gw_ui,
            "_run_gateway_doctor",
            lambda name, **kw: {"status": "passed", "checks": []},
        )
        status, body, _ = _invoke_handler("POST", "/api/agents/bot1/doctor", body={}, monkeypatch=monkeypatch)
        assert status == 201

    def test_post_agents_approve(self, monkeypatch):
        monkeypatch.setattr(
            _gw_ui,
            "_agent_detail_payload",
            lambda name, **kw: {"agent": {"name": name, "approval_id": "appr-1"}},
        )
        monkeypatch.setattr(
            _gw_ui,
            "approve_gateway_approval",
            lambda aid, **kw: {"approval": {"approval_id": aid}},
        )
        status, body, _ = _invoke_handler("POST", "/api/agents/bot1/approve", body={}, monkeypatch=monkeypatch)
        assert status == 201

    def test_post_agents_approve_not_found(self, monkeypatch):
        monkeypatch.setattr(_gw_ui, "_agent_detail_payload", lambda name, **kw: None)
        status, body, _ = _invoke_handler("POST", "/api/agents/bot1/approve", body={}, monkeypatch=monkeypatch)
        assert status == 404

    def test_post_agents_approve_no_approval_id(self, monkeypatch):
        monkeypatch.setattr(
            _gw_ui,
            "_agent_detail_payload",
            lambda name, **kw: {"agent": {"name": name}},
        )
        status, body, _ = _invoke_handler("POST", "/api/agents/bot1/approve", body={}, monkeypatch=monkeypatch)
        assert status == 400

    def test_post_approvals_approve(self, monkeypatch):
        monkeypatch.setattr(
            _gw_ui,
            "approve_gateway_approval",
            lambda aid, **kw: {"approval": {"approval_id": aid}},
        )
        status, body, _ = _invoke_handler("POST", "/api/approvals/appr-1/approve", body={}, monkeypatch=monkeypatch)
        assert status == 201

    def test_post_agents_reject(self, monkeypatch):
        monkeypatch.setattr(
            _gw_ui,
            "_reject_managed_agent_approval",
            lambda name: {"name": name, "rejected": True},
        )
        status, body, _ = _invoke_handler("POST", "/api/agents/bot1/reject", body={}, monkeypatch=monkeypatch)
        assert status == 201

    def test_post_approvals_reject(self, monkeypatch):
        monkeypatch.setattr(
            _gw_ui,
            "deny_gateway_approval",
            lambda aid: {"approval_id": aid, "status": "rejected"},
        )
        status, body, _ = _invoke_handler("POST", "/api/approvals/appr-1/reject", body={}, monkeypatch=monkeypatch)
        assert status == 201

    def test_post_unknown_path(self, monkeypatch):
        status, body, _ = _invoke_handler("POST", "/api/unknown", body={}, monkeypatch=monkeypatch)
        assert status == 404

    @pytest.mark.parametrize(
        ("exc", "expected_status"),
        [
            (LookupError("agent not found"), 404),
            (ValueError("bad input"), 400),
            (typer.Exit(1), 400),
            (RuntimeError("internal"), 500),
        ],
    )
    def test_post_top_level_error_mapping(self, monkeypatch, exc, expected_status):
        monkeypatch.setattr(_gw_ui, "_set_managed_agent_desired_state", lambda name, state: (_ for _ in ()).throw(exc))
        status, body, _ = _invoke_handler("POST", "/api/agents/bot1/start", body={}, monkeypatch=monkeypatch)
        assert status == expected_status


class TestGatewayUiHandlerPUT:
    def test_put_agent(self, monkeypatch):
        monkeypatch.setattr(
            _gw_ui,
            "_update_managed_agent",
            lambda **kw: {"name": "bot1", "updated": True},
        )
        status, body, _ = _invoke_handler(
            "PUT", "/api/agents/bot1", body={"description": "updated"}, monkeypatch=monkeypatch
        )
        assert status == 200

    @pytest.mark.parametrize(
        ("exc", "expected_status"),
        [
            (LookupError("not found"), 404),
            (ValueError("bad"), 400),
            (typer.Exit(1), 400),
            (RuntimeError("crash"), 500),
        ],
    )
    def test_put_agent_error_mapping(self, monkeypatch, exc, expected_status):
        monkeypatch.setattr(_gw_ui, "_update_managed_agent", lambda **kw: (_ for _ in ()).throw(exc))
        status, body, _ = _invoke_handler("PUT", "/api/agents/bot1", body={}, monkeypatch=monkeypatch)
        assert status == expected_status

    def test_put_unknown_path(self, monkeypatch):
        status, body, _ = _invoke_handler("PUT", "/api/unknown", body={}, monkeypatch=monkeypatch)
        assert status == 404


class TestGatewayUiHandlerDELETE:
    def test_delete_agent(self, monkeypatch):
        monkeypatch.setattr(_gw_ui, "_remove_managed_agent", lambda name: {"name": name, "removed": True})
        status, body, _ = _invoke_handler("DELETE", "/api/agents/bot1", monkeypatch=monkeypatch)
        assert status == 200

    def test_delete_agent_not_found(self, monkeypatch):
        def _raise(name):
            raise LookupError("not found")

        monkeypatch.setattr(_gw_ui, "_remove_managed_agent", _raise)
        status, body, _ = _invoke_handler("DELETE", "/api/agents/bot1", monkeypatch=monkeypatch)
        assert status == 404

    def test_delete_unknown_path(self, monkeypatch):
        status, body, _ = _invoke_handler("DELETE", "/api/unknown", monkeypatch=monkeypatch)
        assert status == 404


class TestRenderAgentDetail:
    def test_renders_basic_entry(self):
        entry = {
            "name": "test-bot",
            "template_id": "echo_test",
            "runtime_type": "echo",
            "mode": "live",
            "presence": "IDLE",
            "reply": "auto",
            "confidence": "HIGH",
            "confidence_reason": "all good",
            "confidence_detail": "nothing wrong",
            "asset_class": "processor",
            "intake_model": "direct",
            "trigger_sources": ["direct_message"],
            "return_paths": ["reply"],
            "telemetry_shape": "standard",
            "worker_model": "claude-3",
            "attestation_state": "verified",
            "approval_state": "approved",
            "acting_agent_name": "test-bot",
            "identity_status": "ok",
            "environment_label": "local",
            "environment_status": "healthy",
            "active_space_name": "Work",
            "space_status": "active",
            "default_space_name": "Work",
            "allowed_space_count": 1,
            "install_id": "inst-1",
            "runtime_instance_id": "ri-1",
            "desired_state": "running",
            "effective_state": "running",
            "connected": True,
            "backlog_depth": 0,
            "last_seen_age_seconds": 5,
            "reconnect_backoff_seconds": 0,
            "processed_count": 10,
            "dropped_count": 0,
            "last_work_received_at": "2026-01-01T00:00:00Z",
            "last_work_completed_at": "2026-01-01T00:01:00Z",
            "current_status": "idle",
            "current_activity": "waiting",
            "current_tool": None,
            "timeout_seconds": 30,
            "space_id": "sp-1",
            "credential_source": "gateway",
            "token_file": "/tmp/tok",
            "agent_id": "a-1",
            "last_reply_preview": "ok",
            "last_error": None,
            "last_successful_doctor_at": "2026-01-01",
            "last_doctor_result": {"status": "passed"},
            "workdir": "/tmp/w",
            "exec_command": None,
            "added_at": "2026-01-01T00:00:00Z",
            "system_prompt": "You are a helper.",
        }
        activity = [
            {"ts": "2026-01-01", "event": "test", "agent_name": "test-bot", "message_id": "m1"},
        ]
        result = _gw_ui._render_agent_detail(entry, activity=activity)
        rendered = _render_text(result)
        assert "Managed Agent" in rendered
        assert "@test-bot" in rendered
        assert "Operator System Prompt" in rendered
        assert "You are a helper." in rendered

    def test_renders_empty_entry(self):
        entry = {"name": "minimal"}
        result = _gw_ui._render_agent_detail(entry, activity=[])
        rendered = _render_text(result)
        assert "@minimal" in rendered
        assert "Runtime Details" in rendered

    def test_renders_without_system_prompt(self):
        entry = {"name": "no-prompt", "system_prompt": ""}
        result = _gw_ui._render_agent_detail(entry, activity=[])
        rendered = _render_text(result)
        assert "Operator System Prompt" in rendered
        assert "--system-prompt" in rendered

    def test_renders_with_doctor_result_non_dict(self):
        entry = {"name": "bot", "last_doctor_result": "not-a-dict"}
        result = _gw_ui._render_agent_detail(entry, activity=[])
        rendered = _render_text(result)
        assert "Doctor Status" in rendered

    def test_adapter_row_quiet_for_current_runtime(self):
        entry = {"name": "current", "runtime_type": "hermes_plugin"}
        rendered = _render_text(_gw_ui._render_agent_detail(entry, activity=[]))
        assert "hermes_plugin" in rendered
        assert "deprecated" not in rendered
        assert " - " in rendered or "\n-\n" in rendered


class TestActivityExtraBranches:
    def test_invalid_json_lines_skipped(self, monkeypatch, tmp_path):
        log = tmp_path / "activity.jsonl"
        log.write_text('{"ts":"2026-01-01","event":"test"}\nnot-json\n42\n{"ts":"2026-01-02","event":"ok"}')
        monkeypatch.setattr(_gw_ui, "activity_log_path", lambda: log)
        result = runner.invoke(app, ["gateway", "activity", "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert len(data["events"]) == 2

    def test_text_with_data(self, monkeypatch, tmp_path):
        log = tmp_path / "activity.jsonl"
        events = [
            {"ts": "2026-01-01T00:00:00", "event": "test", "agent_name": "bot1", "phase": "idle"},
        ]
        log.write_text("\n".join(json.dumps(e) for e in events))
        monkeypatch.setattr(_gw_ui, "activity_log_path", lambda: log)
        result = runner.invoke(app, ["gateway", "activity"])
        assert result.exit_code == 0

    def test_oserror_returns_empty(self, monkeypatch, tmp_path):
        # Create a log that will raise OSError
        log = tmp_path / "activity.jsonl"
        log.write_text('{"event":"x"}')

        def _broken_read_text(*a, **kw):
            raise OSError("disk error")

        monkeypatch.setattr(_gw_ui, "activity_log_path", lambda: log)
        # Patch Path.read_text to raise
        monkeypatch.setattr(Path, "read_text", _broken_read_text)
        result = runner.invoke(app, ["gateway", "activity", "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["events"] == []


class TestFormatAge:
    def test_none(self):
        assert _gw_ui._format_age(None) == "-"

    def test_non_numeric(self):
        assert _gw_ui._format_age("not a number") == "-"

    def test_seconds(self):
        result = _gw_ui._format_age(45)
        assert "45" in result and "s" in result

    def test_minutes(self):
        result = _gw_ui._format_age(120)
        assert "2" in result and "m" in result

    def test_hours(self):
        result = _gw_ui._format_age(7200)
        assert "2" in result and "h" in result

    def test_days(self):
        result = _gw_ui._format_age(172800)
        assert "2" in result and "d" in result


class TestFormatTimestamp:
    def test_none(self):
        assert _gw_ui._format_timestamp(None) == "-"

    def test_invalid(self):
        assert _gw_ui._format_timestamp("not a date") == "-"


class TestAgentLabels:
    def test_type_label(self):
        result = _gw_ui._agent_type_label({"template_id": "echo_test", "runtime_type": "echo"})
        assert result == "Connected Asset"

    def test_output_label(self):
        result = _gw_ui._agent_output_label({"template_id": "echo_test"})
        assert result == "Reply"

    def test_template_label(self):
        result = _gw_ui._agent_template_label({"template_id": "echo_test"})
        assert result == "-"

    def test_labels_with_empty_entry(self):
        assert _gw_ui._agent_type_label({}) == "Connected Asset"
        assert _gw_ui._agent_output_label({}) == "Reply"
        assert _gw_ui._agent_template_label({}) == "-"


class TestReachabilityCopy:
    def test_basic(self):
        result = _gw_ui._reachability_copy({"reachability": "live_now"})
        assert result == "Live listener ready to claim work."

    def test_empty_entry(self):
        result = _gw_ui._reachability_copy({})
        assert result == "Gateway does not currently have a working path."


class TestRenderGatewayDashboard:
    def test_renders(self, monkeypatch):
        payload = {
            "gateway_dir": "/tmp/gw",
            "connected": True,
            "daemon": {"running": True, "pid": 100},
            "ui": {"running": True, "pid": 200, "url": "http://localhost:8765"},
            "base_url": "https://paxai.app",
            "space_id": "sp-1",
            "space_name": "Test",
            "user": "tester",
            "agents": [
                {
                    "name": "bot1",
                    "runtime_type": "echo",
                    "template_id": "echo_test",
                    "mode": "live",
                    "presence": "IDLE",
                    "output": "text",
                    "confidence": "HIGH",
                    "acting_agent_name": "bot1",
                    "active_space_name": "Test",
                    "last_seen_age_seconds": 5,
                    "backlog_depth": 0,
                    "confidence_reason": "ok",
                },
            ],
            "recent_activity": [],
            "summary": {
                "managed_agents": 1,
                "live_agents": 1,
                "on_demand_agents": 0,
                "inbox_agents": 0,
                "hidden_agents": 0,
                "system_agents": 0,
                "alert_count": 0,
                "pending_approvals": 0,
            },
            "alerts": [],
        }
        result = _gw_ui._render_gateway_dashboard(payload)
        rendered = _render_text(result)
        assert "Gateway Overview" in rendered
        assert "Managed Agents" in rendered
        assert "bot1" in rendered


class TestRenderActivityTable:
    def test_empty(self):
        result = _gw_ui._render_activity_table([])
        rendered = _render_text(result)
        assert "No activity yet" in rendered

    def test_with_items(self):
        result = _gw_ui._render_activity_table(
            [
                {"ts": "2026-01-01", "event": "test", "agent_name": "bot1", "message_id": "m1"},
            ]
        )
        rendered = _render_text(result)
        assert "test" in rendered
        assert "@bot1" in rendered
        assert "m1" in rendered


def test_write_and_clear_offline_lock_round_trip(monkeypatch, tmp_path):
    """The marker file lifecycle: write at run-time, clear on shutdown."""
    monkeypatch.setenv("AX_CONFIG_DIR", str(tmp_path / "ax_config"))

    assert not _gw_ui._is_offline_mode_active()

    _gw_ui._write_offline_mode_lock()
    assert _gw_ui._is_offline_mode_active()
    assert _gw_ui._offline_mode_lock_path().exists()
    # Lock content is the pid; useful for forensic debugging of stale locks.
    assert _gw_ui._offline_mode_lock_path().read_text().strip().isdigit()

    _gw_ui._clear_offline_mode_lock()
    assert not _gw_ui._is_offline_mode_active()
    assert not _gw_ui._offline_mode_lock_path().exists()


def test_clear_offline_lock_is_idempotent_when_missing(monkeypatch, tmp_path):
    """Clearing a non-existent lock must not raise (covers the shutdown path
    where the daemon may not have written the lock yet)."""
    monkeypatch.setenv("AX_CONFIG_DIR", str(tmp_path / "ax_config"))
    # Must not raise.
    _gw_ui._clear_offline_mode_lock()
    assert not _gw_ui._is_offline_mode_active()
