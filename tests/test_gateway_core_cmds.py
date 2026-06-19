"""Per-module gateway command tests: core/process-level (gateway split #28 Phase 1).

Ported from skipped tests; touch only ax_cli.gateway (core) or pure helpers."""

from __future__ import annotations

import json
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import httpx
import pytest
import typer
from typer.testing import CliRunner

from ax_cli import gateway as gateway_core
from ax_cli import gateway_runtime_types
from ax_cli.offline_client import OfflineAxClient
from ax_cli.offline_sse import OfflineAgentQueues, agent_name_from_token, extract_mentions, make_token
from tests.gateway_cmd_testlib import (
    _GOOD_SPACE_UUID,
    _build_daemon,
    _isolate_gateway_paths,
    _RecordingHeartbeatClient,
    _seed_offline_gateway,
    _SharedRuntimeClient,
    _stale_hermes_entry,
)

runner = CliRunner()


def test_gateway_state_dir_isolated_by_environment(monkeypatch, tmp_path):
    config_dir = tmp_path / "config"
    monkeypatch.setenv("AX_CONFIG_DIR", str(config_dir))
    monkeypatch.setenv("AX_GATEWAY_ENV", "dev/staging")

    assert gateway_core.gateway_environment() == "dev-staging"
    assert gateway_core.gateway_dir() == config_dir / "gateway" / "envs" / "dev-staging"
    assert gateway_core.session_path() == config_dir / "gateway" / "envs" / "dev-staging" / "session.json"


def test_gateway_state_dir_allows_explicit_override(monkeypatch, tmp_path):
    config_dir = tmp_path / "config"
    custom_dir = tmp_path / "custom-gateway"
    monkeypatch.setenv("AX_CONFIG_DIR", str(config_dir))
    monkeypatch.setenv("AX_GATEWAY_ENV", "prod")
    monkeypatch.setenv("AX_GATEWAY_DIR", str(custom_dir))

    assert gateway_core.gateway_environment() == "prod"
    assert gateway_core.gateway_dir() == custom_dir
    assert gateway_core.registry_path() == custom_dir / "registry.json"


def test_clear_gateway_pid_keeps_newer_owner(monkeypatch, tmp_path):
    config_dir = tmp_path / "config"
    monkeypatch.setenv("AX_CONFIG_DIR", str(config_dir))
    gateway_core.write_gateway_pid(22179)

    gateway_core.clear_gateway_pid(5514)

    assert gateway_core.pid_path().exists()
    assert gateway_core.pid_path().read_text().strip() == "22179"


def test_scan_gateway_process_pids_ignores_current_parent_wrapper(monkeypatch):
    monkeypatch.setattr(gateway_core.os, "getpid", lambda: 22179)
    monkeypatch.setattr(gateway_core.os, "getppid", lambda: 22178)
    monkeypatch.setattr("ax_cli.gateway_storage._pid_alive", lambda pid: True)
    monkeypatch.setattr(
        gateway_core.subprocess,
        "check_output",
        lambda *args, **kwargs: "\n".join(
            [
                "22178 uv run ax gateway run",
                "22179 /Users/jacob/claude_home/ax-cli/.venv/bin/python3 /Users/jacob/claude_home/ax-cli/.venv/bin/ax gateway run",
                "5514 /Users/jacob/claude_home/ax-cli/.venv/bin/python3 /Users/jacob/claude_home/ax-cli/.venv/bin/ax gateway run",
            ]
        ),
    )

    assert gateway_core._scan_gateway_process_pids() == [5514]


def test_claude_code_channel_ignores_stale_mailbox_backlog_for_presence():
    annotated = gateway_core.annotate_runtime_health(
        {
            "name": "orion",
            "template_id": "claude_code_channel",
            "runtime_type": "claude_code_channel",
            "effective_state": "stopped",
            "backlog_depth": 3,
            "current_status": "queued",
            "current_activity": "Queued in Gateway (3 pending)",
        }
    )

    assert annotated["mode"] == "LIVE"
    assert annotated["queue_capable"] is False
    assert annotated["queue_depth"] == 0
    assert annotated["presence"] == "OFFLINE"
    assert annotated["work_state"] == "idle"
    assert annotated["current_status"] == "idle"
    assert annotated["current_activity"] is None


def test_gateway_daemon_does_not_launch_claude_code_channel(monkeypatch, tmp_path):
    config_dir = tmp_path / "config"
    monkeypatch.setenv("AX_CONFIG_DIR", str(config_dir))
    started: list[str] = []

    class FakeRuntime:
        def __init__(self, entry, **kwargs):
            self.entry = dict(entry)

        def start(self):
            started.append(str(self.entry.get("name")))

        def stop(self, timeout=None):
            return None

    monkeypatch.setattr("ax_cli.gateway_daemon.ManagedAgentRuntime", FakeRuntime)
    daemon = gateway_core.GatewayDaemon(client_factory=lambda **kwargs: _SharedRuntimeClient({}))
    entry = {
        "name": "orion",
        "agent_id": "agent-orion",
        "space_id": "space-1",
        "base_url": "https://paxai.app",
        "template_id": "claude_code_channel",
        "runtime_type": "claude_code_channel",
        "desired_state": "running",
        "attestation_state": "verified",
        "approval_state": "approved",
        "identity_status": "verified",
        "environment_status": "environment_allowed",
        "space_status": "active_allowed",
        "last_error": "Unsupported runtime_type: claude_code_channel",
        "current_status": "error",
    }

    daemon._reconcile_runtime(entry)

    assert started == []
    assert daemon._runtimes == {}
    assert entry["last_error"] is None
    assert entry["current_status"] is None
    assert entry["backlog_depth"] == 0


def test_find_agent_entry_by_ref_matches_row_and_stable_prefix():
    registry = {
        "agents": [
            {
                "name": "demo-hermes",
                "install_id": "install-hermes-abcdef",
                "agent_id": "agent-hermes-1",
            },
            {
                "name": "codex-pass-through",
                "install_id": "install-pass-123456",
                "agent_id": "agent-pass-1",
            },
        ]
    }

    assert gateway_core.find_agent_entry_by_ref(registry, "#2")["name"] == "codex-pass-through"
    assert gateway_core.find_agent_entry_by_ref(registry, "1")["name"] == "demo-hermes"
    assert gateway_core.find_agent_entry_by_ref(registry, "codex-pass-through")["agent_id"] == "agent-pass-1"
    assert gateway_core.find_agent_entry_by_ref(registry, "install-pass")["name"] == "codex-pass-through"
    assert gateway_core.find_agent_entry_by_ref(registry, "missing") is None


def test_gateway_reconcile_does_not_auto_approve_pass_through(monkeypatch, tmp_path):
    config_dir = tmp_path / "config"
    monkeypatch.setenv("AX_CONFIG_DIR", str(config_dir))
    registry = gateway_core.load_gateway_registry()
    registry["agents"] = [
        {
            "name": "codex-pass",
            "agent_id": "agent-pass-1",
            "space_id": "space-1",
            "base_url": "https://paxai.app",
            "template_id": "pass_through",
            "runtime_type": "inbox",
            "desired_state": "running",
            "requires_approval": True,
            "install_id": "install-pass-1",
        }
    ]

    daemon = gateway_core.GatewayDaemon(client_factory=lambda **kwargs: _SharedRuntimeClient({}))
    reconciled = daemon._reconcile_registry(registry, {"token": "axp_u_test.token", "base_url": "https://paxai.app"})
    agent = reconciled["agents"][0]

    assert reconciled["bindings"] == []
    assert agent["approval_state"] == "pending"
    assert agent["approval_id"]
    assert reconciled["approvals"][0]["approval_kind"] == "new_binding"


def test_gateway_daemon_reconcile_normalizes_legacy_inbox_metadata(monkeypatch, tmp_path):
    config_dir = tmp_path / "config"
    monkeypatch.setenv("AX_CONFIG_DIR", str(config_dir))
    registry = gateway_core.load_gateway_registry()
    entry = {
        "name": "dev_channel_alpha",
        "agent_id": "agent-dev-channel-1",
        "space_id": "space-1",
        "base_url": "https://paxai.app",
        "runtime_type": "inbox",
        "desired_state": "stopped",
        "placement": "hosted",
        "activation": "persistent",
        "reply_mode": "interactive",
        "telemetry_level": "basic",
        "asset_class": "interactive_agent",
        "intake_model": "live_listener",
        "trigger_sources": ["direct_message"],
        "return_paths": ["inline_reply"],
        "tags": ["local", "custom-bridge"],
        "capabilities": ["reply"],
        "created_via": "legacy_registry",
    }
    gateway_core.ensure_local_asset_binding(registry, entry, created_via="legacy_registry", auto_approve=True)
    registry["agents"] = [entry]

    daemon = gateway_core.GatewayDaemon(client_factory=lambda **kwargs: _SharedRuntimeClient({}))
    reconciled = daemon._reconcile_registry(registry, {"token": "axp_u_test.token"})
    agent = reconciled["agents"][0]

    assert agent["placement"] == "mailbox"
    assert agent["activation"] == "queue_worker"
    assert agent["reply_mode"] == "summary_only"
    assert agent["mode"] == "INBOX"
    assert agent["reply"] == "SUMMARY"
    assert agent["asset_class"] == "background_worker"
    assert agent["intake_model"] == "queue_accept"
    assert agent["worker_model"] == "queue_drain"
    assert agent["return_paths"] == ["summary_post"]
    assert agent["asset_type_label"] == "Inbox Worker"
    assert agent["output_label"] == "Summary"


def test_annotate_runtime_health_respects_explicit_user_overrides():
    snapshot = {
        "name": "custom-inbox-ish",
        "agent_id": "agent-custom-1",
        "runtime_type": "inbox",
        "placement": "hosted",
        "activation": "persistent",
        "reply_mode": "interactive",
        "asset_class": "interactive_agent",
        "intake_model": "live_listener",
        "trigger_sources": ["direct_message"],
        "return_paths": ["inline_reply"],
        "user_overrides": {
            "operator": {
                "placement": "hosted",
                "activation": "persistent",
                "reply_mode": "interactive",
            },
            "asset": {
                "asset_class": "interactive_agent",
                "intake_model": "live_listener",
                "trigger_sources": ["direct_message"],
                "return_paths": ["inline_reply"],
            },
        },
        "effective_state": "stopped",
    }

    annotated = gateway_core.annotate_runtime_health(snapshot)

    assert annotated["placement"] == "hosted"
    assert annotated["activation"] == "persistent"
    assert annotated["reply_mode"] == "interactive"
    assert annotated["mode"] == "LIVE"
    assert annotated["reply"] == "REPLY"
    assert annotated["asset_class"] == "interactive_agent"
    assert annotated["intake_model"] == "live_listener"
    assert annotated["return_paths"] == ["inline_reply"]
    assert annotated["asset_type_label"] == "Live Listener"


def test_evaluate_runtime_attestation_detects_binding_drift_and_creates_approval(monkeypatch, tmp_path):
    config_dir = tmp_path / "config"
    monkeypatch.setenv("AX_CONFIG_DIR", str(config_dir))
    registry = gateway_core.load_gateway_registry()
    entry = {
        "name": "docs-worker",
        "agent_id": "agent-docs-1",
        "runtime_type": "exec",
        "exec_command": "python3 worker.py",
        "workdir": str(tmp_path / "repo-a"),
        "created_via": "cli",
    }
    gateway_core.ensure_local_asset_binding(registry, entry, created_via="cli", auto_approve=True)

    drifted = dict(entry)
    drifted["workdir"] = str(tmp_path / "repo-b")

    attestation = gateway_core.evaluate_runtime_attestation(registry, drifted)
    snapshot = gateway_core.annotate_runtime_health({**drifted, **attestation, "effective_state": "stopped"})

    assert attestation["attestation_state"] == "drifted"
    assert attestation["approval_state"] == "pending"
    assert attestation["approval_id"]
    assert registry["approvals"][0]["approval_kind"] == "binding_drift"
    assert snapshot["presence"] == "BLOCKED"
    assert snapshot["confidence"] == "BLOCKED"
    assert snapshot["confidence_reason"] == "binding_drift"


def test_evaluate_runtime_attestation_blocks_asset_mismatch(monkeypatch, tmp_path):
    config_dir = tmp_path / "config"
    monkeypatch.setenv("AX_CONFIG_DIR", str(config_dir))
    registry = gateway_core.load_gateway_registry()
    entry = {
        "name": "codex",
        "agent_id": "agent-codex-1",
        "runtime_type": "exec",
        "exec_command": "python3 codex_bridge.py",
        "workdir": str(tmp_path / "repo"),
        "install_id": "install-1",
        "created_via": "cli",
    }
    gateway_core.ensure_local_asset_binding(registry, entry, created_via="cli", auto_approve=True)

    mismatched = dict(entry)
    mismatched["agent_id"] = "agent-other-2"

    attestation = gateway_core.evaluate_runtime_attestation(registry, mismatched)
    snapshot = gateway_core.annotate_runtime_health({**mismatched, **attestation, "effective_state": "stopped"})

    assert attestation["attestation_state"] == "blocked"
    assert attestation["confidence_reason"] == "asset_mismatch"
    assert snapshot["presence"] == "BLOCKED"
    assert snapshot["confidence"] == "BLOCKED"


def test_gateway_daemon_reconcile_blocks_drifted_runtime(monkeypatch, tmp_path):
    config_dir = tmp_path / "config"
    monkeypatch.setenv("AX_CONFIG_DIR", str(config_dir))
    registry = gateway_core.load_gateway_registry()
    entry = {
        "name": "drift-bot",
        "agent_id": "agent-drift-1",
        "space_id": "space-1",
        "base_url": "https://paxai.app",
        "runtime_type": "exec",
        "exec_command": "python3 drift.py",
        "workdir": str(tmp_path / "repo-a"),
        "token_file": str(tmp_path / "token"),
        "desired_state": "running",
        "created_via": "cli",
    }
    Path(entry["token_file"]).write_text("axp_a_agent.secret")
    gateway_core.ensure_local_asset_binding(registry, entry, created_via="cli", auto_approve=True)
    entry["workdir"] = str(tmp_path / "repo-b")
    registry["agents"] = [entry]

    daemon = gateway_core.GatewayDaemon(client_factory=lambda **kwargs: _SharedRuntimeClient({}))
    reconciled = daemon._reconcile_registry(registry, {"token": "axp_u_test.token"})
    agent = reconciled["agents"][0]

    assert agent["attestation_state"] == "drifted"
    assert agent["approval_state"] == "pending"
    assert agent["presence"] == "BLOCKED"
    assert "drift-bot" not in daemon._runtimes


def test_gateway_daemon_reconcile_blocks_hermes_without_repo(monkeypatch, tmp_path):
    config_dir = tmp_path / "config"
    workdir = tmp_path / "workspace" / "ax-cli"
    workdir.mkdir(parents=True)
    monkeypatch.setenv("AX_CONFIG_DIR", str(config_dir))
    monkeypatch.delenv("HERMES_REPO_PATH", raising=False)
    monkeypatch.setattr(gateway_core.Path, "home", classmethod(lambda cls: tmp_path / "home"))

    registry = gateway_core.load_gateway_registry()
    entry = {
        "name": "hermes-2",
        "agent_id": "agent-hermes-2",
        "space_id": "space-1",
        "base_url": "https://paxai.app",
        "template_id": "hermes",
        "runtime_type": "exec",
        "exec_command": "python3 examples/hermes_sentinel/hermes_bridge.py",
        "workdir": str(workdir),
        "token_file": str(tmp_path / "token"),
        "desired_state": "running",
        "created_via": "cli",
    }
    Path(entry["token_file"]).write_text("axp_a_agent.secret")
    gateway_core.ensure_local_asset_binding(registry, entry, created_via="cli", auto_approve=True)
    registry["agents"] = [entry]

    daemon = gateway_core.GatewayDaemon(client_factory=lambda **kwargs: _SharedRuntimeClient({}))
    reconciled = daemon._reconcile_registry(registry, {"token": "axp_u_test.token", "base_url": "https://paxai.app"})
    agent = reconciled["agents"][0]

    assert daemon._runtimes == {}
    assert agent["effective_state"] == "error"
    assert agent["presence"] == "ERROR"
    assert agent["confidence"] == "BLOCKED"
    assert agent["confidence_reason"] == "setup_blocked"
    assert "Hermes checkout not found" in str(agent["last_error"])
    assert "Hermes checkout not found" in str(agent["confidence_detail"])


def test_gateway_daemon_rebinds_running_runtime_when_space_changes(monkeypatch, tmp_path):
    config_dir = tmp_path / "config"
    monkeypatch.setenv("AX_CONFIG_DIR", str(config_dir))
    started: list[str] = []
    stopped: list[str] = []

    class FakeRuntime:
        def __init__(self, entry, **kwargs):
            self.entry = dict(entry)
            self.name = str(entry.get("name"))
            self.started = False

        def start(self):
            self.started = True
            started.append(str(self.entry.get("space_id")))

        def stop(self):
            stopped.append(str(self.entry.get("space_id")))
            self.started = False

        def snapshot(self):
            return {
                "effective_state": "running" if self.started else "stopped",
                "runtime_instance_id": f"runtime-{self.entry.get('space_id')}",
                "last_error": None,
                "current_status": None,
                "current_activity": None,
                "current_tool": None,
                "current_tool_call_id": None,
                "backlog_depth": 0,
            }

    monkeypatch.setattr("ax_cli.gateway_daemon.ManagedAgentRuntime", FakeRuntime)
    entry = {
        "name": "space-bot",
        "agent_id": "agent-space-1",
        "space_id": "space-1",
        "base_url": "https://paxai.app",
        "runtime_type": "echo",
        "desired_state": "running",
        "attestation_state": "verified",
        "approval_state": "approved",
        "identity_status": "verified",
        "environment_status": "environment_allowed",
        "space_status": "active_allowed",
    }
    daemon = gateway_core.GatewayDaemon(client_factory=lambda **kwargs: _SharedRuntimeClient({}))

    daemon._reconcile_runtime(entry)
    assert started == ["space-1"]
    assert stopped == []

    moved = {**entry, "space_id": "space-2"}
    daemon._reconcile_runtime(moved)

    assert stopped == ["space-1"]
    assert started == ["space-1", "space-2"]
    assert daemon._runtimes["space-bot"].snapshot()["runtime_instance_id"] == "runtime-space-2"
    events = gateway_core.load_recent_gateway_activity()
    assert any(row["event"] == "runtime_rebinding" and row.get("new_space_id") == "space-2" for row in events)


def test_sanitize_exec_env_strips_ax_credentials(monkeypatch):
    monkeypatch.setenv("AX_TOKEN", "secret-token")
    monkeypatch.setenv("AX_USER_TOKEN", "secret-user")
    monkeypatch.setenv("AX_BASE_URL", "https://paxai.app")
    monkeypatch.setenv("AX_AGENT_NAME", "orion")
    monkeypatch.setenv("OPENAI_API_KEY", "keep-me")

    env = gateway_core.sanitize_exec_env("hello", {"name": "echo-bot", "agent_id": "agent-1", "runtime_type": "exec"})

    assert "AX_TOKEN" not in env
    assert "AX_USER_TOKEN" not in env
    assert "AX_BASE_URL" not in env
    assert env["AX_AGENT_NAME"] == "echo-bot"
    assert env["AX_MENTION_CONTENT"] == "hello"
    assert env["AX_GATEWAY_AGENT_NAME"] == "echo-bot"
    assert env["OPENAI_API_KEY"] == "keep-me"


def test_sanitize_exec_env_passes_managed_agent_context(tmp_path):
    token_file = tmp_path / "agent.token"
    token_file.write_text("axp_a_agent.secret")

    env = gateway_core.sanitize_exec_env(
        "remember this",
        {
            "name": "ollama-bot",
            "agent_id": "agent-1",
            "space_id": "space-1",
            "base_url": "https://paxai.app",
            "runtime_type": "exec",
            "token_file": str(token_file),
        },
    )

    assert env["AX_TOKEN_FILE"] == str(token_file)
    assert "AX_TOKEN" not in env
    assert env["AX_BASE_URL"] == "https://paxai.app"
    assert env["AX_SPACE_ID"] == "space-1"
    assert env["AX_AGENT_ID"] == "agent-1"
    assert env["AX_AGENT_NAME"] == "ollama-bot"
    assert env["AX_GATEWAY_AGENT_ID"] == "agent-1"
    assert env["AX_GATEWAY_AGENT_NAME"] == "ollama-bot"


def test_gateway_managed_token_loader_rejects_user_bootstrap_pat(tmp_path):
    token_file = tmp_path / "token"
    token_file.write_text("axp_u_user.secret")

    with pytest.raises(ValueError, match="agent-bound token"):
        gateway_core.load_gateway_managed_agent_token(
            {
                "name": "echo-bot",
                "agent_id": "agent-1",
                "token_file": str(token_file),
            }
        )


def test_gateway_managed_token_loader_requires_bound_agent_id(tmp_path):
    token_file = tmp_path / "token"
    token_file.write_text("axp_a_agent.secret")

    with pytest.raises(ValueError, match="bound agent_id"):
        gateway_core.load_gateway_managed_agent_token(
            {
                "name": "echo-bot",
                "token_file": str(token_file),
            }
        )


def test_sentinel_inference_sdk_env_rejects_user_bootstrap_pat(tmp_path):
    token_file = tmp_path / "token"
    token_file.write_text("axp_u_user.secret")

    with pytest.raises(ValueError, match="agent-bound token"):
        gateway_core._build_sentinel_inference_sdk_env(
            {
                "name": "dev_sentinel",
                "agent_id": "agent-1",
                "space_id": "space-1",
                "base_url": "https://paxai.app",
                "runtime_type": "sentinel_inference_sdk",
                "token_file": str(token_file),
                "workdir": str(tmp_path / "dev_sentinel"),
            }
        )


def test_managed_echo_runtime_processes_message(tmp_path, monkeypatch):
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    monkeypatch.setenv("AX_CONFIG_DIR", str(config_dir))
    token_file = tmp_path / "token"
    token_file.write_text("axp_a_agent.secret")
    payload = {
        "id": "msg-1",
        "content": "@echo-bot ping",
        "author": {"id": "user-1", "name": "madtank", "type": "user"},
        "mentions": ["echo-bot"],
    }
    shared = _SharedRuntimeClient(payload)

    runtime = gateway_core.ManagedAgentRuntime(
        {
            "name": "echo-bot",
            "agent_id": "agent-1",
            "space_id": "space-1",
            "base_url": "https://paxai.app",
            "runtime_type": "echo",
            "token_file": str(token_file),
        },
        client_factory=lambda **kwargs: shared,
    )

    runtime.start()
    deadline = time.time() + 2.0
    while time.time() < deadline and not shared.sent:
        time.sleep(0.05)
    runtime.stop()

    assert shared.sent, "echo runtime should have replied"
    assert shared.sent[0]["content"] == "Echo: ping"
    assert shared.sent[0]["parent_id"] == "msg-1"
    assert shared.sent[0]["agent_id"] == "agent-1"
    assert shared.sent[0]["metadata"]["control_plane"] == "gateway"
    assert shared.sent[0]["metadata"]["gateway"]["managed"] is True
    assert shared.sent[0]["metadata"]["gateway"]["agent_name"] == "echo-bot"
    assert [row["status"] for row in shared.processing] == ["started", "processing", "completed"]
    assert shared.processing[0]["activity"] == "Picked up by Gateway"
    assert shared.processing[0]["detail"] == {"backlog_depth": 1, "pickup_state": "claimed"}
    assert shared.processing[1]["activity"] == "Composing echo reply"
    recent = gateway_core.load_recent_gateway_activity()
    event_names = [row["event"] for row in recent]
    assert "message_received" in event_names
    assert "message_claimed" in event_names
    assert "reply_sent" in event_names


def test_runtime_sends_connected_heartbeat_on_first_sse_event(tmp_path, monkeypatch):
    """Listener loop sends 'connected' heartbeat on first SSE event after connecting."""
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    monkeypatch.setenv("AX_CONFIG_DIR", str(config_dir))
    token_file = tmp_path / "token"
    token_file.write_text("axp_a_agent.secret")
    payload = {
        "id": "msg-hb",
        "content": "ping",
        "author": {"id": "u1", "name": "u", "type": "user"},
        "mentions": ["hb-bot"],
    }
    shared = _SharedRuntimeClient(payload)
    runtime = gateway_core.ManagedAgentRuntime(
        {
            "name": "hb-bot",
            "agent_id": "agent-hb",
            "space_id": "s1",
            "base_url": "https://paxai.app",
            "runtime_type": "echo",
            "token_file": str(token_file),
        },
        client_factory=lambda **kwargs: shared,
    )
    runtime.start()
    deadline = time.time() + 2.0
    while time.time() < deadline and not shared.heartbeats:
        time.sleep(0.05)
    runtime.stop()
    assert any(h["status"] == "connected" for h in shared.heartbeats)


def test_runtime_sends_stale_heartbeat_on_sse_disconnect(tmp_path, monkeypatch):
    """Listener loop sends 'stale' heartbeat when SSE connection drops."""
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    monkeypatch.setenv("AX_CONFIG_DIR", str(config_dir))
    token_file = tmp_path / "token"
    token_file.write_text("axp_a_agent.secret")

    class _FailOnSecondConnect(_SharedRuntimeClient):
        def connect_sse(self, *, space_id, timeout=None):
            self.connect_calls += 1
            if self.connect_calls == 1:
                raise ConnectionError("SSE dropped")
            raise ConnectionError("test done")

    shared = _FailOnSecondConnect({})
    runtime = gateway_core.ManagedAgentRuntime(
        {
            "name": "stale-bot",
            "agent_id": "agent-stale",
            "space_id": "s1",
            "base_url": "https://paxai.app",
            "runtime_type": "echo",
            "token_file": str(token_file),
        },
        client_factory=lambda **kwargs: shared,
    )
    runtime.start()
    deadline = time.time() + 3.0
    while time.time() < deadline and not any(h["status"] == "stale" for h in shared.heartbeats):
        time.sleep(0.05)
    runtime.stop()
    assert any(h["status"] == "stale" for h in shared.heartbeats)


def test_runtime_sends_offline_heartbeat_on_stop(tmp_path, monkeypatch):
    """stop() sends 'offline' heartbeat using the agent-bound client."""
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    monkeypatch.setenv("AX_CONFIG_DIR", str(config_dir))
    token_file = tmp_path / "token"
    token_file.write_text("axp_a_agent.secret")
    shared = _SharedRuntimeClient({})
    runtime = gateway_core.ManagedAgentRuntime(
        {
            "name": "offline-bot",
            "agent_id": "agent-off",
            "space_id": "s1",
            "base_url": "https://paxai.app",
            "runtime_type": "echo",
            "token_file": str(token_file),
        },
        client_factory=lambda **kwargs: shared,
    )
    runtime.stop()
    assert any(h["status"] == "offline" for h in shared.heartbeats)


def test_runtime_sends_setup_error_heartbeat_on_error_state(tmp_path, monkeypatch):
    """_update_state fires 'setup_error' heartbeat on first transition to error."""
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    monkeypatch.setenv("AX_CONFIG_DIR", str(config_dir))
    token_file = tmp_path / "token"
    token_file.write_text("axp_a_agent.secret")
    shared = _SharedRuntimeClient({})
    runtime = gateway_core.ManagedAgentRuntime(
        {
            "name": "err-bot",
            "agent_id": "agent-err",
            "space_id": "s1",
            "base_url": "https://paxai.app",
            "runtime_type": "echo",
            "token_file": str(token_file),
        },
        client_factory=lambda **kwargs: shared,
    )
    runtime._update_state(effective_state="error", last_error="test error")
    assert any(h["status"] == "setup_error" for h in shared.heartbeats)
    # Second transition to error should not fire again
    runtime._update_state(effective_state="error", last_error="still broken")
    assert sum(1 for h in shared.heartbeats if h["status"] == "setup_error") == 1


def test_managed_exec_runtime_parses_gateway_progress_events(tmp_path, monkeypatch):
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    monkeypatch.setenv("AX_CONFIG_DIR", str(config_dir))
    token_file = tmp_path / "token"
    token_file.write_text("axp_a_agent.secret")
    script = tmp_path / "bridge.py"
    script.write_text(
        """
import json
import sys

prefix = "AX_GATEWAY_EVENT "
print(prefix + json.dumps({"kind": "status", "status": "working", "message": "warming up"}), flush=True)
print(prefix + json.dumps({"kind": "status", "status": "working", "message": "warming up", "progress": {"current": 1, "total": 3, "unit": "steps"}}), flush=True)
print(prefix + json.dumps({"kind": "tool_start", "tool_name": "sleep", "tool_call_id": "tool-1", "arguments": {"seconds": 1}}), flush=True)
print(prefix + json.dumps({"kind": "tool_result", "tool_name": "sleep", "tool_call_id": "tool-1", "arguments": {"seconds": 1}, "initial_data": {"slept_seconds": 1}, "status": "success"}), flush=True)
print("done", flush=True)
""".strip()
    )
    payload = {
        "id": "msg-1",
        "content": "@exec-bot pause 1s",
        "author": {"id": "user-1", "name": "madtank", "type": "user"},
        "mentions": ["exec-bot"],
    }
    shared = _SharedRuntimeClient(payload)

    runtime = gateway_core.ManagedAgentRuntime(
        {
            "name": "exec-bot",
            "agent_id": "agent-1",
            "space_id": "space-1",
            "base_url": "https://paxai.app",
            "runtime_type": "exec",
            "exec_command": f"{sys.executable} {script}",
            "token_file": str(token_file),
        },
        client_factory=lambda **kwargs: shared,
    )

    runtime.start()
    deadline = time.time() + 3.0
    while time.time() < deadline and not shared.sent:
        time.sleep(0.05)
    snapshot = runtime.snapshot()
    runtime.stop()

    assert shared.sent, "exec runtime should have replied"
    assert shared.sent[0]["content"] == "done"
    assert [row["status"] for row in shared.processing] == [
        "started",
        "processing",
        "working",
        "working",
        "tool_call",
        "tool_complete",
        "completed",
    ]
    assert shared.processing[0]["activity"] == "Picked up by Gateway"
    assert shared.processing[0]["detail"] == {"backlog_depth": 1, "pickup_state": "claimed"}
    assert shared.processing[1]["activity"] == "Preparing runtime"
    assert shared.processing[2]["activity"] == "warming up"
    assert shared.processing[3]["activity"] == "warming up"
    assert shared.processing[3]["progress"] == {"current": 1, "total": 3, "unit": "steps"}
    assert shared.processing[4]["tool_name"] == "sleep"
    assert shared.processing[4]["activity"] == "Using sleep"
    assert shared.processing[5]["tool_name"] == "sleep"
    assert shared.processing[5]["detail"] == {"slept_seconds": 1}
    assert shared.tool_calls
    assert shared.tool_calls[0]["tool_name"] == "sleep"
    assert shared.tool_calls[0]["message_id"] == "msg-1"
    assert snapshot["current_activity"] in {None, "warming up"}
    recent = gateway_core.load_recent_gateway_activity(limit=20)
    events = [row["event"] for row in recent]
    assert "message_claimed" in events
    assert "tool_started" in events
    assert "tool_finished" in events


def test_managed_exec_runtime_can_decline_without_chat_reply(tmp_path, monkeypatch):
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    monkeypatch.setenv("AX_CONFIG_DIR", str(config_dir))
    token_file = tmp_path / "token"
    token_file.write_text("axp_a_agent.secret")
    script = tmp_path / "decline_bridge.py"
    script.write_text(
        """
import json

prefix = "AX_GATEWAY_EVENT "
print(prefix + json.dumps({"kind": "status", "status": "no_reply", "reason": "ack", "message": "Chose not to respond"}), flush=True)
""".strip()
    )
    payload = {
        "id": "msg-1",
        "content": "@exec-bot thanks, no action needed",
        "author": {"id": "user-1", "name": "madtank", "type": "user"},
        "mentions": ["exec-bot"],
    }
    shared = _SharedRuntimeClient(payload)

    runtime = gateway_core.ManagedAgentRuntime(
        {
            "name": "exec-bot",
            "agent_id": "agent-1",
            "space_id": "space-1",
            "base_url": "https://paxai.app",
            "runtime_type": "exec",
            "exec_command": f"{sys.executable} {script}",
            "token_file": str(token_file),
        },
        client_factory=lambda **kwargs: shared,
    )

    runtime.start()
    deadline = time.time() + 3.0
    while time.time() < deadline and not any(row["status"] == "no_reply" for row in shared.processing):
        time.sleep(0.05)
    runtime.stop()

    assert [row["status"] for row in shared.processing] == ["started", "processing", "no_reply"]
    no_reply = shared.processing[-1]
    assert no_reply["activity"] == "Chose not to respond"
    assert no_reply["reason"] == "no_reply"
    assert no_reply["detail"] == {"terminal": True, "reply_created": False, "reason_code": "ack"}
    assert len(shared.sent) == 1
    pause_row = shared.sent[0]
    assert pause_row["space_id"] == "space-1"
    assert pause_row["content"] == "Chose not to respond"
    assert pause_row["agent_id"] == "agent-1"
    assert pause_row["parent_id"] == "msg-1"
    assert pause_row["message_type"] == "agent_pause"
    assert pause_row["metadata"]["control_plane"] == "gateway"
    assert pause_row["metadata"]["signal_only"] is True
    assert pause_row["metadata"]["reason"] == "no_reply"
    assert pause_row["metadata"]["reason_code"] == "ack"
    assert pause_row["metadata"]["signal_kind"] == "agent_skipped"
    assert pause_row["metadata"]["gateway"]["parent_message_id"] == "msg-1"
    assert pause_row["metadata"]["gateway"]["signal_kind"] == "agent_skipped"
    assert pause_row["metadata"]["gateway"]["reason"] == "no_reply"
    assert pause_row["metadata"]["gateway"]["reason_code"] == "ack"
    assert pause_row["metadata"]["gateway"]["reply_created"] is False
    recent = gateway_core.load_recent_gateway_activity()
    assert "agent_skipped" in [row["event"] for row in recent]


def test_managed_exec_runtime_marks_message_timed_out(tmp_path, monkeypatch):
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    monkeypatch.setenv("AX_CONFIG_DIR", str(config_dir))
    token_file = tmp_path / "token"
    token_file.write_text("axp_a_agent.secret")
    script = tmp_path / "slow_bridge.py"
    script.write_text(
        """
import time

time.sleep(5)
print("too late", flush=True)
""".strip()
    )
    payload = {
        "id": "msg-1",
        "content": "@exec-bot run slow job",
        "author": {"id": "user-1", "name": "madtank", "type": "user"},
        "mentions": ["exec-bot"],
    }
    shared = _SharedRuntimeClient(payload)

    runtime = gateway_core.ManagedAgentRuntime(
        {
            "name": "exec-bot",
            "agent_id": "agent-1",
            "space_id": "space-1",
            "base_url": "https://paxai.app",
            "runtime_type": "exec",
            "exec_command": f"{sys.executable} {script}",
            "timeout_seconds": 1,
            "token_file": str(token_file),
        },
        client_factory=lambda **kwargs: shared,
    )

    runtime.start()
    deadline = time.time() + 4.0
    while time.time() < deadline and not any(row.get("reason") == "runtime_timeout" for row in shared.processing):
        time.sleep(0.05)
    snapshot = runtime.snapshot()
    runtime.stop()

    assert not shared.sent
    assert [row["status"] for row in shared.processing] == ["started", "processing", "error"]
    timeout_status = shared.processing[-1]
    assert timeout_status["activity"] == "Timed out after 1s"
    assert timeout_status["reason"] == "runtime_timeout"
    assert timeout_status["detail"] == {"timeout_seconds": 1, "runtime_type": "exec"}
    assert "timed out after 1s" in timeout_status["error_message"]
    assert snapshot["current_status"] == "error"
    assert snapshot["current_activity"] == "Timed out after 1s"
    recent = gateway_core.load_recent_gateway_activity()
    events = [row["event"] for row in recent]
    assert "runtime_timeout" in events
    assert "reply_sent" not in events


def test_managed_sentinel_cli_runtime_resumes_agent_session(tmp_path, monkeypatch):
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    monkeypatch.setenv("AX_CONFIG_DIR", str(config_dir))
    token_file = tmp_path / "token"
    token_file.write_text("axp_a_agent.secret")
    popen_calls = []

    class _FakePipe:
        def __init__(self, lines=None):
            self.lines = list(lines or [])
            self.writes = []

        def __iter__(self):
            return iter(self.lines)

        def write(self, text):
            self.writes.append(text)

        def read(self):
            return ""

        def close(self):
            return None

    class _FakeProcess:
        def __init__(self, cmd, **kwargs):
            popen_calls.append(cmd)
            self.stdin = _FakePipe()
            self.stderr = _FakePipe()
            self.returncode = 0
            if len(popen_calls) == 1:
                self.stdout = _FakePipe(
                    [
                        json.dumps(
                            {"type": "assistant", "message": {"content": [{"type": "text", "text": "remembered"}]}}
                        ),
                        json.dumps({"type": "result", "result": "remembered", "session_id": "sess-1"}),
                    ]
                )
            else:
                self.stdout = _FakePipe(
                    [
                        json.dumps({"type": "result", "result": "cobalt", "session_id": "sess-1"}),
                    ]
                )

        def wait(self, timeout=None):
            return self.returncode

        def kill(self):
            self.returncode = -9

    monkeypatch.setattr(gateway_core.subprocess, "Popen", lambda cmd, **kwargs: _FakeProcess(cmd, **kwargs))
    shared = _SharedRuntimeClient({})
    runtime = gateway_core.ManagedAgentRuntime(
        {
            "name": "dev_sentinel",
            "agent_id": "agent-1",
            "space_id": "space-1",
            "base_url": "https://paxai.app",
            "runtime_type": "sentinel_cli",
            "workdir": str(tmp_path),
            "token_file": str(token_file),
        },
        client_factory=lambda **kwargs: shared,
    )
    runtime._send_client = shared

    first = runtime._handle_prompt("remember cobalt", message_id="msg-1", data={"id": "msg-1"})
    second = runtime._handle_prompt("what word?", message_id="msg-2", data={"id": "msg-2"})

    assert first == "remembered"
    assert second == "cobalt"
    assert "--resume" not in popen_calls[0]
    assert "--resume" in popen_calls[1]
    assert "sess-1" in popen_calls[1]
    assert [row["status"] for row in shared.processing] == [
        "thinking",
        "thinking",
    ]


def test_sentinel_claude_command_is_current_claude_code_compatible(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    workdir = tmp_path / "repo"
    workdir.mkdir()

    cmd = gateway_core._build_sentinel_claude_cmd(
        {
            "name": "claude_max",
            "runtime_type": "sentinel_cli",
            "sentinel_runtime": "claude",
            "workdir": str(workdir),
        },
        session_id="session-1",
    )

    assert cmd[:5] == ["claude", "-p", "--output-format", "stream-json", "--verbose"]
    assert "--dangerously-skip-permissions" not in cmd
    assert cmd[cmd.index("--add-dir") + 1] == str(workdir)
    assert "/home/ax-agent/shared/repos" not in cmd
    assert cmd[cmd.index("--resume") + 1] == "session-1"


def test_sentinel_claude_command_prefers_explicit_add_dir(tmp_path):
    workdir = tmp_path / "repo"
    add_dir = tmp_path / "shared"
    workdir.mkdir()
    add_dir.mkdir()

    cmd = gateway_core._build_sentinel_claude_cmd(
        {
            "name": "claude_max",
            "workdir": str(workdir),
            "add_dir": str(add_dir),
        },
        session_id=None,
    )

    assert cmd[cmd.index("--add-dir") + 1] == str(add_dir)


def test_managed_sentinel_inference_sdk_runtime_supervises_long_running_listener(tmp_path, monkeypatch):
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    monkeypatch.setenv("AX_CONFIG_DIR", str(config_dir))
    token_file = tmp_path / "token"
    token_file.write_text("axp_a_agent.secret")
    workdir = tmp_path / "agents" / "dev_sentinel"
    workdir.mkdir(parents=True)
    script = tmp_path / "agents" / "claude_agent_v2.py"
    observed = tmp_path / "observed.json"
    monkeypatch.setenv("TEST_HERMES_SENTINEL_OBSERVED", str(observed))
    script.write_text(
        """
import json
import os
import time

path = os.environ["TEST_HERMES_SENTINEL_OBSERVED"]
with open(path, "w", encoding="utf-8") as handle:
    json.dump(
        {
            "AX_TOKEN": os.environ.get("AX_TOKEN"),
            "AX_BASE_URL": os.environ.get("AX_BASE_URL"),
            "AX_AGENT_NAME": os.environ.get("AX_AGENT_NAME"),
            "AX_AGENT_ID": os.environ.get("AX_AGENT_ID"),
            "AX_SPACE_ID": os.environ.get("AX_SPACE_ID"),
            "AX_CONFIG_DIR": os.environ.get("AX_CONFIG_DIR"),
        },
        handle,
    )
while True:
    time.sleep(1)
""".strip()
    )
    hermes_repo = tmp_path / "hermes-agent"
    hermes_repo.mkdir()

    runtime = gateway_core.ManagedAgentRuntime(
        {
            "name": "dev_sentinel",
            "agent_id": "agent-1",
            "space_id": "space-1",
            "base_url": "https://dev.paxai.app",
            "runtime_type": "sentinel_inference_sdk",
            "client": "openai_sdk",
            "template_id": "hermes",
            "workdir": str(workdir),
            "token_file": str(token_file),
            "hermes_repo_path": str(hermes_repo),
            "hermes_python": sys.executable,
            "log_path": str(tmp_path / "hermes.log"),
        }
    )

    runtime.start()
    deadline = time.time() + 3.0
    while time.time() < deadline and not observed.exists():
        time.sleep(0.05)
    snapshot = runtime.snapshot()
    runtime.stop()

    assert observed.exists()
    env = json.loads(observed.read_text())
    assert env["AX_TOKEN"] == "axp_a_agent.secret"
    assert env["AX_BASE_URL"] == "https://dev.paxai.app"
    assert env["AX_AGENT_NAME"] == "dev_sentinel"
    assert env["AX_AGENT_ID"] == "agent-1"
    assert env["AX_SPACE_ID"] == "space-1"
    assert env["AX_CONFIG_DIR"] == str(workdir / ".ax")
    assert snapshot["effective_state"] == "running"
    assert snapshot["current_activity"] == "Sentinel listener running"


def test_managed_sentinel_hermes_sdk_runtime_always_uses_hermes_sdk_runtime(tmp_path, monkeypatch):
    """The dispatch layer must hardcode --runtime hermes_sdk for sentinel_hermes_sdk
    agents — client is irrelevant and not consulted.  Regression
    guard for the architectural boundary introduced in ADR-012 decision 5."""
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    monkeypatch.setenv("AX_CONFIG_DIR", str(config_dir))
    token_file = tmp_path / "token"
    token_file.write_text("axp_a_agent.secret")
    workdir = tmp_path / "agents" / "hermes_sdk_agent"
    workdir.mkdir(parents=True)
    script = tmp_path / "agents" / "claude_agent_v2.py"
    observed = tmp_path / "observed.json"
    monkeypatch.setenv("TEST_HERMES_SDK_OBSERVED", str(observed))
    script.write_text(
        """
import json
import os
import sys
import time

path = os.environ["TEST_HERMES_SDK_OBSERVED"]
with open(path, "w", encoding="utf-8") as handle:
    json.dump({"argv": sys.argv}, handle)
while True:
    time.sleep(1)
""".strip()
    )
    hermes_repo = tmp_path / "hermes-agent"
    hermes_repo.mkdir()

    runtime = gateway_core.ManagedAgentRuntime(
        {
            "name": "hermes_sdk_agent",
            "agent_id": "agent-2",
            "space_id": "space-2",
            "base_url": "https://dev.paxai.app",
            "runtime_type": "sentinel_hermes_sdk",
            "workdir": str(workdir),
            "token_file": str(token_file),
            "hermes_repo_path": str(hermes_repo),
            "hermes_python": sys.executable,
            "log_path": str(tmp_path / "hermes.log"),
        }
    )

    runtime.start()
    deadline = time.time() + 3.0
    while time.time() < deadline and not observed.exists():
        time.sleep(0.05)
    runtime.stop()

    assert observed.exists(), "sentinel_hermes_sdk process did not start — check dispatch guard"
    data = json.loads(observed.read_text())
    assert "--runtime" in data["argv"]
    runtime_idx = data["argv"].index("--runtime") + 1
    assert data["argv"][runtime_idx] == "hermes_sdk", (
        f"Expected --runtime hermes_sdk but got {data['argv'][runtime_idx]!r}; "
        "dispatch must hardcode hermes_sdk, not consult client"
    )


def test_managed_inbox_runtime_queues_message_without_reply(tmp_path, monkeypatch):
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    monkeypatch.setenv("AX_CONFIG_DIR", str(config_dir))
    token_file = tmp_path / "token"
    token_file.write_text("axp_a_agent.secret")
    payload = {
        "id": "msg-1",
        "content": "@inbox-bot hello there",
        "author": {"id": "user-1", "name": "madtank", "type": "user"},
        "mentions": ["inbox-bot"],
    }
    shared = _SharedRuntimeClient(payload)

    runtime = gateway_core.ManagedAgentRuntime(
        {
            "name": "inbox-bot",
            "agent_id": "agent-1",
            "space_id": "space-1",
            "base_url": "https://paxai.app",
            "runtime_type": "inbox",
            "token_file": str(token_file),
        },
        client_factory=lambda **kwargs: shared,
    )

    runtime.start()
    deadline = time.time() + 2.0
    snapshot = runtime.snapshot()
    while time.time() < deadline and snapshot["backlog_depth"] < 1:
        time.sleep(0.05)
        snapshot = runtime.snapshot()
    runtime.stop()

    assert not shared.sent
    assert snapshot["backlog_depth"] >= 1
    assert [row["status"] for row in shared.processing] == ["queued"]
    assert shared.processing[0]["activity"] == "Queued in Gateway"
    assert shared.processing[0]["detail"] == {"backlog_depth": 1, "pickup_state": "queued"}
    pending = gateway_core.load_agent_pending_messages("inbox-bot")
    assert pending == [
        {
            "message_id": "msg-1",
            "parent_id": None,
            "conversation_id": None,
            "content": "@inbox-bot hello there",
            "display_name": None,
            "created_at": pending[0]["created_at"],
            "queued_at": pending[0]["queued_at"],
        }
    ]
    assert snapshot["last_work_received_at"] == pending[0]["queued_at"]
    recent = gateway_core.load_recent_gateway_activity()
    events = [row["event"] for row in recent]
    assert "message_received" in events
    assert "message_queued" in events


def test_passive_runtime_snapshot_rehydrates_manual_queue_updates(tmp_path, monkeypatch):
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    monkeypatch.setenv("AX_CONFIG_DIR", str(config_dir))
    token_file = tmp_path / "token"
    token_file.write_text("axp_a_agent.secret")
    registry = gateway_core.load_gateway_registry()
    registry["agents"] = [
        {
            "name": "inbox-bot",
            "agent_id": "agent-1",
            "space_id": "space-1",
            "base_url": "https://paxai.app",
            "runtime_type": "inbox",
            "token_file": str(token_file),
            "backlog_depth": 0,
            "current_status": None,
            "current_activity": None,
            "processed_count": 1,
            "last_reply_message_id": "reply-1",
            "last_reply_preview": "handled",
        }
    ]
    gateway_core.save_gateway_registry(registry)
    gateway_core.save_agent_pending_messages("inbox-bot", [])

    runtime = gateway_core.ManagedAgentRuntime(
        {
            "name": "inbox-bot",
            "agent_id": "agent-1",
            "space_id": "space-1",
            "base_url": "https://paxai.app",
            "runtime_type": "inbox",
            "token_file": str(token_file),
        },
        client_factory=lambda **kwargs: _SharedRuntimeClient({}),
    )
    runtime._update_state(backlog_depth=1, current_status="queued", current_activity="Queued in Gateway")

    snapshot = runtime.snapshot()

    assert snapshot["backlog_depth"] == 0
    assert snapshot["current_status"] is None
    assert snapshot["current_activity"] is None
    assert snapshot["processed_count"] == 1
    assert snapshot["last_reply_message_id"] == "reply-1"
    assert snapshot["last_reply_preview"] == "handled"


def test_annotate_runtime_health_marks_stale_after_missed_heartbeat():
    old_seen = (
        datetime.now(timezone.utc) - timedelta(seconds=gateway_core.RUNTIME_STALE_AFTER_SECONDS + 5)
    ).isoformat()

    snapshot = gateway_core.annotate_runtime_health(
        {
            "effective_state": "running",
            "last_seen_at": old_seen,
        }
    )

    assert snapshot["effective_state"] == "stale"
    assert snapshot["connected"] is False
    assert snapshot["last_seen_age_seconds"] >= gateway_core.RUNTIME_STALE_AFTER_SECONDS


def test_annotate_runtime_health_treats_managed_attached_session_as_connected(monkeypatch, tmp_path):
    log_path = tmp_path / "attached-session.log"
    log_path.write_text("Listening for channel messages from: server:ax-channel\n")
    monkeypatch.setattr("ax_cli.gateway_health._pid_is_alive", lambda pid: int(pid) == 1234)

    snapshot = gateway_core.annotate_runtime_health(
        {
            "template_id": "claude_code_channel",
            "placement": "attached",
            "activation": "attach_only",
            "reply_mode": "interactive",
            "desired_state": "running",
            "effective_state": "running",
            "last_seen_at": (
                datetime.now(timezone.utc) - timedelta(seconds=gateway_core.RUNTIME_STALE_AFTER_SECONDS + 20)
            ).isoformat(),
            "attached_session_pid": 1234,
            "attached_session_log_path": str(log_path),
        }
    )

    assert snapshot["connected"] is True
    assert snapshot["presence"] == "IDLE"
    assert snapshot["reachability"] == "live_now"
    assert snapshot["local_attach_state"] == "connected"


def test_channel_agent_shows_degraded_when_sse_broken_despite_process_running(monkeypatch, tmp_path):
    """A claude-channel agent must show LOW confidence when the SSE subscription
    is down, even if Claude Code is running and sending MCP pings.  This was a
    production bug where the agent appeared ready but could not receive messages."""
    monkeypatch.setattr("ax_cli.gateway_health._pid_is_alive", lambda pid: int(pid) == 1234)

    snapshot = gateway_core.annotate_runtime_health(
        {
            "template_id": "claude_code_channel",
            "placement": "attached",
            "activation": "attach_only",
            "reply_mode": "interactive",
            "desired_state": "running",
            "effective_state": "running",
            "last_seen_at": datetime.now(timezone.utc).isoformat(),
            "attached_session_pid": 1234,
            "sse_connected": False,
        }
    )

    assert snapshot["connected"] is False
    assert snapshot["liveness"] == "stale"
    assert snapshot["reachability"] == "sse_disconnected"
    assert snapshot["confidence"] == "LOW"


def test_annotate_runtime_health_derives_identity_space_snapshot(monkeypatch, tmp_path):
    config_dir = tmp_path / "config"
    monkeypatch.setenv("AX_CONFIG_DIR", str(config_dir))
    gateway_core.save_gateway_session(
        {
            "token": "axp_u_test.token",
            "base_url": "https://paxai.app",
            "space_id": "space-1",
            "space_name": "ax-cli-dev",
            "username": "codex",
        }
    )
    token_file = tmp_path / "identity.token"
    token_file.write_text("axp_a_agent.secret")
    registry = gateway_core.load_gateway_registry()
    registry["agents"] = [
        {
            "name": "identity-bot",
            "agent_id": "agent-1",
            "space_id": "space-1",
            "base_url": "https://paxai.app",
            "runtime_type": "echo",
            "credential_source": "gateway",
            "token_file": str(token_file),
            "desired_state": "running",
            "effective_state": "running",
            "last_seen_at": datetime.now(timezone.utc).isoformat(),
            "install_id": "inst-identity-1",
        }
    ]
    gateway_core.ensure_gateway_identity_binding(
        registry, registry["agents"][0], session=gateway_core.load_gateway_session()
    )

    snapshot = gateway_core.annotate_runtime_health(registry["agents"][0], registry=registry)

    assert snapshot["acting_agent_name"] == "identity-bot"
    assert snapshot["environment_label"] == "prod"
    assert snapshot["environment_status"] == "environment_allowed"
    assert snapshot["active_space_id"] == "space-1"
    assert snapshot["active_space_source"] == "gateway_binding"
    assert snapshot["space_status"] == "active_allowed"
    assert snapshot["identity_status"] == "verified"
    assert snapshot["confidence"] == "HIGH"


def test_annotate_runtime_health_blocks_environment_mismatch(monkeypatch, tmp_path):
    config_dir = tmp_path / "config"
    monkeypatch.setenv("AX_CONFIG_DIR", str(config_dir))
    token_file = tmp_path / "identity.token"
    token_file.write_text("axp_a_agent.secret")
    registry = gateway_core.load_gateway_registry()
    registry["agents"] = [
        {
            "name": "identity-bot",
            "agent_id": "agent-1",
            "space_id": "space-1",
            "base_url": "https://paxai.app",
            "runtime_type": "echo",
            "credential_source": "gateway",
            "token_file": str(token_file),
            "desired_state": "running",
            "effective_state": "running",
            "last_seen_at": datetime.now(timezone.utc).isoformat(),
            "install_id": "inst-identity-1",
        }
    ]
    gateway_core.ensure_gateway_identity_binding(registry, registry["agents"][0])

    snapshot = gateway_core.annotate_runtime_health(
        {**registry["agents"][0], "base_url": "https://dev.paxai.app"},
        registry=registry,
    )

    assert snapshot["environment_status"] == "environment_mismatch"
    assert snapshot["presence"] == "BLOCKED"
    assert snapshot["confidence"] == "BLOCKED"
    assert snapshot["confidence_reason"] == "environment_mismatch"


@pytest.mark.parametrize(
    ("input_snapshot", "expected"),
    [
        (
            {
                "template_id": "claude_code_channel",
                "placement": "attached",
                "activation": "attach_only",
                "reply_mode": "interactive",
                "effective_state": "stale",
            },
            {
                "mode": "LIVE",
                "presence": "STALE",
                "reply": "REPLY",
                "confidence": "LOW",
                "reachability": "attach_required",
            },
        ),
        (
            {
                "placement": "hosted",
                "activation": "persistent",
                "reply_mode": "interactive",
                "effective_state": "stopped",
            },
            {
                "mode": "LIVE",
                "presence": "OFFLINE",
                "reply": "REPLY",
                "confidence": "LOW",
                "reachability": "unavailable",
            },
        ),
        (
            {
                "runtime_type": "inbox",
                "placement": "mailbox",
                "activation": "queue_worker",
                "reply_mode": "summary_only",
                "effective_state": "running",
                "last_seen_at": "__recent__",
                "backlog_depth": 0,
            },
            {
                "mode": "INBOX",
                "presence": "IDLE",
                "reply": "SUMMARY",
                "confidence": "HIGH",
                "reachability": "queue_available",
            },
        ),
        (
            {
                "runtime_type": "inbox",
                "placement": "mailbox",
                "activation": "queue_worker",
                "reply_mode": "summary_only",
                "effective_state": "running",
                "last_seen_at": "__recent__",
                "backlog_depth": 3,
                "last_doctor_result": {
                    "status": "failed",
                    "summary": "Queue writable but worker smoke test failed.",
                    "checks": [{"name": "test_claim", "status": "failed"}],
                },
            },
            {
                "mode": "INBOX",
                "presence": "QUEUED",
                "reply": "SUMMARY",
                "confidence": "LOW",
                "reachability": "queue_available",
            },
        ),
        (
            {
                "template_id": "ollama",
                "placement": "hosted",
                "activation": "on_demand",
                "reply_mode": "interactive",
                "effective_state": "stopped",
            },
            {
                "mode": "ON-DEMAND",
                "presence": "IDLE",
                "reply": "REPLY",
                "confidence": "MEDIUM",
                "reachability": "launch_available",
            },
        ),
        (
            {
                "template_id": "hermes",
                "effective_state": "error",
                "reply_mode": "interactive",
                "last_error": "missing repo",
            },
            {
                "mode": "LIVE",
                "presence": "ERROR",
                "reply": "REPLY",
                "confidence": "BLOCKED",
                "reachability": "unavailable",
            },
        ),
    ],
)
def test_annotate_runtime_health_derives_gateway_operator_model(input_snapshot, expected):
    input_snapshot = dict(input_snapshot)
    if input_snapshot.get("last_seen_at") == "__recent__":
        input_snapshot["last_seen_at"] = datetime.now(timezone.utc).isoformat()
    snapshot = gateway_core.annotate_runtime_health(input_snapshot)

    assert snapshot["mode"] == expected["mode"]
    assert snapshot["presence"] == expected["presence"]
    assert snapshot["reply"] == expected["reply"]
    assert snapshot["confidence"] == expected["confidence"]
    assert snapshot["reachability"] == expected["reachability"]


def test_annotate_runtime_health_prefers_doctor_summary_for_setup_error_detail():
    snapshot = gateway_core.annotate_runtime_health(
        {
            "template_id": "hermes",
            "effective_state": "error",
            "last_reply_preview": "(stderr: ERROR: hermes-agent repo not found at /Users/jacob/hermes-agent. Set HERMES_REPO_PATH or clone hermes-agent.)",
            "last_doctor_result": {
                "status": "failed",
                "summary": "Hermes checkout not found at /Users/jacob/hermes-agent.",
                "checks": [{"name": "hermes_repo", "status": "failed"}],
            },
        }
    )

    assert snapshot["confidence"] == "BLOCKED"
    assert snapshot["confidence_reason"] == "setup_blocked"
    assert snapshot["confidence_detail"] == "Hermes checkout not found at /Users/jacob/hermes-agent."


def test_hermes_setup_status_prefers_sibling_checkout(monkeypatch, tmp_path):
    workdir = tmp_path / "workspace" / "ax-cli"
    sibling = tmp_path / "workspace" / "hermes-agent"
    workdir.mkdir(parents=True)
    sibling.mkdir(parents=True)
    monkeypatch.delenv("HERMES_REPO_PATH", raising=False)
    monkeypatch.setattr(gateway_core.Path, "home", classmethod(lambda cls: tmp_path / "home"))

    status = gateway_core.hermes_setup_status({"template_id": "hermes", "workdir": str(workdir)})

    assert status["ready"] is True
    assert status["resolved_path"] == str(sibling)


def test_sanitize_exec_env_sets_resolved_hermes_repo_path():
    env = gateway_core.sanitize_exec_env(
        "Gateway test OK.",
        {
            "agent_id": "agent-hermes-2",
            "name": "hermes-2",
            "runtime_type": "exec",
            "hermes_repo_path": "/tmp/hermes-agent",
        },
    )

    assert env["HERMES_REPO_PATH"] == "/tmp/hermes-agent"


def test_sanitize_exec_env_sets_ollama_model_override():
    env = gateway_core.sanitize_exec_env(
        "Gateway test OK.",
        {
            "agent_id": "agent-ember-1",
            "name": "ember",
            "runtime_type": "exec",
            "model": "gemma4:latest",
        },
    )

    assert env["OLLAMA_MODEL"] == "gemma4:latest"


def test_ollama_setup_status_recommends_recent_local_chat_model(monkeypatch):
    class _FakeResponse:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, object]:
            return {
                "models": [
                    {
                        "name": "nomic-embed-text:latest",
                        "modified_at": "2026-01-06T21:04:28.576252397-08:00",
                        "details": {"family": "nomic-bert", "families": ["nomic-bert"], "parameter_size": "137M"},
                    },
                    {
                        "name": "nemotron-3-nano:latest",
                        "modified_at": "2025-12-16T14:03:52.946489046-08:00",
                        "details": {
                            "family": "nemotron_h_moe",
                            "families": ["nemotron_h_moe"],
                            "parameter_size": "31.6B",
                        },
                    },
                    {
                        "name": "gemma4:latest",
                        "modified_at": "2026-04-02T19:28:17.519867961-07:00",
                        "details": {"family": "gemma4", "families": ["gemma4"], "parameter_size": "8.0B"},
                    },
                    {
                        "name": "gpt-oss:120b-cloud",
                        "modified_at": "2025-11-11T16:50:56.418111483-08:00",
                        "remote_host": "https://ollama.com:443",
                        "details": {"family": "gptoss", "families": ["gptoss"], "parameter_size": "116.8B"},
                    },
                ]
            }

    monkeypatch.setattr(gateway_core.httpx, "get", lambda *args, **kwargs: _FakeResponse())

    status = gateway_core.ollama_setup_status()

    assert status["server_reachable"] is True
    assert status["recommended_model"] == "gemma4:latest"
    assert status["available_models"] == [
        "nomic-embed-text:latest",
        "nemotron-3-nano:latest",
        "gemma4:latest",
        "gpt-oss:120b-cloud",
    ]
    assert status["local_models"] == [
        "nomic-embed-text:latest",
        "nemotron-3-nano:latest",
        "gemma4:latest",
    ]


@pytest.mark.parametrize(
    ("input_snapshot", "expected"),
    [
        (
            {
                "template_id": "hermes",
                "effective_state": "running",
                "last_seen_at": datetime.now(timezone.utc).isoformat(),
            },
            {
                "asset_class": "interactive_agent",
                "intake_model": "live_listener",
                "asset_type_label": "Live Listener",
                "output_label": "Reply",
                "telemetry_shape": "rich",
            },
        ),
        (
            {
                "template_id": "ollama",
                "effective_state": "stopped",
            },
            {
                "asset_class": "interactive_agent",
                "intake_model": "launch_on_send",
                "asset_type_label": "On-Demand Agent",
                "output_label": "Reply",
                "telemetry_shape": "basic",
            },
        ),
        (
            {
                "runtime_type": "inbox",
                "template_id": "inbox",
                "effective_state": "running",
                "last_seen_at": datetime.now(timezone.utc).isoformat(),
            },
            {
                "asset_class": "background_worker",
                "intake_model": "queue_accept",
                "asset_type_label": "Inbox Worker",
                "output_label": "Summary",
                "telemetry_shape": "basic",
                "worker_model": "queue_drain",
            },
        ),
    ],
)
def test_annotate_runtime_health_derives_asset_taxonomy_fields(input_snapshot, expected):
    snapshot = gateway_core.annotate_runtime_health(input_snapshot)

    for key, value in expected.items():
        assert snapshot[key] == value
    assert isinstance(snapshot["asset_descriptor"], dict)
    assert snapshot["asset_descriptor"]["asset_class"] == expected["asset_class"]


def test_listener_timeout_enters_reconnecting_state(tmp_path, monkeypatch):
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    monkeypatch.setenv("AX_CONFIG_DIR", str(config_dir))
    token_file = tmp_path / "token"
    token_file.write_text("axp_a_agent.secret")

    class _TimeoutRuntimeClient:
        def __init__(self):
            self.timeout = None

        def connect_sse(self, *, space_id, timeout=None):
            self.timeout = timeout
            raise httpx.ReadTimeout("boom", request=httpx.Request("GET", "https://paxai.app/api/v1/sse/messages"))

        def close(self):
            return None

    shared = _TimeoutRuntimeClient()
    runtime = gateway_core.ManagedAgentRuntime(
        {
            "name": "echo-bot",
            "agent_id": "agent-1",
            "space_id": "space-1",
            "base_url": "https://paxai.app",
            "runtime_type": "echo",
            "token_file": str(token_file),
        },
        client_factory=lambda **kwargs: shared,
    )

    runtime.start()
    deadline = time.time() + 1.0
    snapshot = runtime.snapshot()
    while time.time() < deadline and snapshot["effective_state"] != "reconnecting":
        time.sleep(0.05)
        snapshot = runtime.snapshot()
    runtime.stop()

    assert shared.timeout is not None
    assert shared.timeout.read == gateway_core.SSE_IDLE_TIMEOUT_SECONDS
    assert snapshot["effective_state"] == "reconnecting"
    assert snapshot["last_error"] == "idle timeout after 45s without SSE heartbeat"
    recent = gateway_core.load_recent_gateway_activity()
    assert recent[-1]["event"] in {"runtime_stopped", "listener_timeout"}
    assert any(row["event"] == "listener_timeout" for row in recent)


def test_gateway_template_echo_alias_resolves():
    assert gateway_runtime_types.agent_template_definition("echo")["id"] == "echo_test"


def test_gateway_daemon_does_not_launch_managed_process_for_external_runtime(tmp_path):
    entry = {
        "name": "nova",
        "template_id": "hermes",
        "runtime_type": "sentinel_inference_sdk",
        "desired_state": "running",
        "effective_state": "running",
        "external_runtime_state": "connected",
        "external_runtime_kind": "hermes_plugin",
        "external_runtime_instance_id": "external:hermes_plugin:nova:12345",
        "last_seen_at": datetime.now(timezone.utc).isoformat(),
    }

    daemon = gateway_core.GatewayDaemon(client_factory=lambda **kwargs: None)
    daemon._reconcile_runtime(entry)

    assert daemon._runtimes == {}
    assert entry["effective_state"] == "running"
    assert entry["runtime_instance_id"] == "external:hermes_plugin:nova:12345"


def test_gateway_daemon_external_runtime_respects_operator_stop(tmp_path):
    entry = {
        "name": "nova",
        "template_id": "hermes",
        "runtime_type": "sentinel_inference_sdk",
        "desired_state": "stopped",
        "effective_state": "running",
        "external_runtime_state": "connected",
        "external_runtime_kind": "hermes_plugin",
        "external_runtime_instance_id": "external:hermes_plugin:nova:12345",
        "runtime_instance_id": "external:hermes_plugin:nova:12345",
        "current_status": "processing",
        "current_tool": "search_docs",
        "current_tool_call_id": "tool-1",
        "last_seen_at": datetime.now(timezone.utc).isoformat(),
    }

    daemon = gateway_core.GatewayDaemon(client_factory=lambda **kwargs: None)
    daemon._reconcile_runtime(entry)

    assert daemon._runtimes == {}
    assert entry["effective_state"] == "stopped"
    assert entry["runtime_instance_id"] is None
    assert entry["current_status"] is None
    assert entry["current_tool"] is None
    assert entry["current_tool_call_id"] is None
    assert entry["local_attach_state"] == "external_stopped"


def test_gateway_daemon_preserves_stale_external_plugin_without_legacy_fallback(tmp_path):
    entry = {
        "name": "nova",
        "template_id": "hermes",
        "runtime_type": "sentinel_inference_sdk",
        "desired_state": "running",
        "effective_state": "running",
        "external_runtime_managed": True,
        "external_runtime_kind": "hermes_plugin",
        "external_runtime_instance_id": "external:hermes_plugin:nova:12345",
        "last_seen_at": (
            datetime.now(timezone.utc) - timedelta(seconds=gateway_core.RUNTIME_STALE_AFTER_SECONDS + 10)
        ).isoformat(),
    }

    daemon = gateway_core.GatewayDaemon(client_factory=lambda **kwargs: None)
    daemon._reconcile_runtime(entry)

    assert daemon._runtimes == {}
    assert entry["effective_state"] == "stale"
    assert entry["runtime_instance_id"] == "external:hermes_plugin:nova:12345"
    assert entry["local_attach_state"] == "external_stale"
    assert "fresh external runtime heartbeat" in entry["local_attach_detail"]


def test_gateway_daemon_marks_stopped_when_desired_state_is_stopped():
    entry = {
        "name": "nova",
        "template_id": "hermes",
        "runtime_type": "sentinel_inference_sdk",
        "desired_state": "stopped",
        "effective_state": "running",
        "runtime_instance_id": "old-runtime",
        "current_status": "processing",
        "current_activity": "Hermes sentinel listener running",
        "last_seen_at": datetime.now(timezone.utc).isoformat(),
    }

    daemon = gateway_core.GatewayDaemon(client_factory=lambda **kwargs: None)
    daemon._reconcile_runtime(entry)

    assert entry["effective_state"] == "stopped"
    assert entry["runtime_instance_id"] is None
    assert entry["current_status"] is None
    assert entry["current_activity"] is None


def test_gateway_activity_phase_set_covers_supervisor_lifecycle():
    """The phase enum is the supervisor-loop / aX bubble contract; freezing
    it here means a runtime change cannot silently drop a phase the
    supervisor depends on."""
    expected = {
        "received",
        "routed",
        "delivered",
        "claimed",
        "working",
        "tool",
        "reply",
        "result",
        "blocked",
        "stale",
        "reminder",
    }
    assert set(gateway_core.GATEWAY_ACTIVITY_PHASES) == expected


def test_gateway_activity_event_vocabulary_phase_mapping():
    """Every registered event maps to a registered phase. New event names
    that don't appear here should pass review with an explicit mapping."""
    mapping = gateway_core.GATEWAY_ACTIVITY_EVENTS
    assert mapping["message_received"] == "received"
    assert mapping["message_queued"] == "received"
    assert mapping["delivered_to_inbox"] == "delivered"
    assert mapping["message_claimed"] == "claimed"
    assert mapping["runtime_activity"] == "working"
    assert mapping["tool_started"] == "tool"
    assert mapping["tool_call_recorded"] == "tool"
    assert mapping["tool_call_record_failed"] == "tool"
    assert mapping["reply_sent"] == "reply"
    assert mapping["runtime_error"] == "result"
    assert mapping["agent_skipped"] == "result"
    # All registered events use a registered phase.
    for event_name, phase in mapping.items():
        assert phase in gateway_core.GATEWAY_ACTIVITY_PHASES, (event_name, phase)


def test_gateway_activity_phase_for_event_returns_none_for_unknown():
    assert gateway_core.phase_for_event("not_a_real_event") is None
    assert gateway_core.phase_for_event("") is None
    assert gateway_core.phase_for_event("message_received") == "received"


def test_record_gateway_activity_attaches_phase_for_known_event(monkeypatch, tmp_path):
    monkeypatch.setenv("AX_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("AX_GATEWAY_DIR", str(tmp_path / "gw"))
    rec = gateway_core.record_gateway_activity(
        "message_received",
        message_id="msg-1",
    )
    assert rec["phase"] == "received"
    rec2 = gateway_core.record_gateway_activity(
        "tool_started",
        message_id="msg-1",
        tool_name="bash",
    )
    assert rec2["phase"] == "tool"


def test_record_gateway_activity_omits_phase_for_unknown_event(monkeypatch, tmp_path, caplog):
    monkeypatch.setenv("AX_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("AX_GATEWAY_DIR", str(tmp_path / "gw"))
    rec = gateway_core.record_gateway_activity(
        "totally_made_up_event",
        message_id="msg-1",
    )
    # Unknown events still record (legacy callers, future events) but carry
    # no phase so consumers can spot drift instead of trusting a fake phase.
    assert "phase" not in rec


def test_sweep_does_not_auto_hide_stale_agent(monkeypatch, tmp_path):
    """Sweep must never mutate lifecycle_phase. Hide is operator-driven only."""
    _isolate_gateway_paths(monkeypatch, tmp_path)
    client = _RecordingHeartbeatClient()
    daemon = _build_daemon(client)
    entry = _stale_hermes_entry("hermes-old", age_seconds=20 * 60)
    registry = {"agents": [entry]}
    daemon._sweep_lifecycle(registry, session={"token": "axp_u_test", "base_url": "http://x"})
    assert entry.get("lifecycle_phase", "active") == "active"
    assert "hidden_at" not in entry
    assert "hidden_reason" not in entry
    recent = gateway_core.load_recent_gateway_activity()
    assert not any(r.get("event") == "managed_agent_hidden" for r in recent)
    # Sweep no longer sends heartbeats — agent runtimes send them from their
    # own bound clients. Sweep's user token is rejected by the heartbeat endpoint.
    assert client.heartbeats == []


def test_sweep_skips_switchboard(monkeypatch, tmp_path):
    _isolate_gateway_paths(monkeypatch, tmp_path)
    client = _RecordingHeartbeatClient()
    daemon = _build_daemon(client)
    entry = {
        "name": "switchboard-deadbeef",
        "agent_id": "agent-switchboard",
        "template_id": "inbox",
        "effective_state": "running",
        "liveness": "stale",
        "last_seen_age_seconds": 24 * 60 * 60,
    }
    registry = {"agents": [entry]}
    daemon._sweep_lifecycle(registry, session={"token": "axp_u_test"})
    assert entry.get("lifecycle_phase", "active") == "active"
    assert client.heartbeats == []


def test_sweep_skips_service_account(monkeypatch, tmp_path):
    _isolate_gateway_paths(monkeypatch, tmp_path)
    client = _RecordingHeartbeatClient()
    daemon = _build_daemon(client)
    entry = {
        "name": "system-events",
        "agent_id": "agent-service",
        "template_id": "service_account",
        "effective_state": "running",
        "liveness": "offline",
        "last_seen_age_seconds": 24 * 60 * 60,
    }
    registry = {"agents": [entry]}
    daemon._sweep_lifecycle(registry, session={"token": "axp_u_test"})
    assert entry.get("lifecycle_phase", "active") == "active"
    assert client.heartbeats == []


def test_sweep_does_not_auto_unhide_on_reconnect(monkeypatch, tmp_path):
    """A user-hidden agent that reconnects stays hidden — operator intent
    sticks across liveness changes. Only ``unhide`` (CLI / UI) restores it."""
    _isolate_gateway_paths(monkeypatch, tmp_path)
    client = _RecordingHeartbeatClient()
    daemon = _build_daemon(client)
    entry = _stale_hermes_entry("hermes-back", age_seconds=2.0, liveness="connected")
    entry["lifecycle_phase"] = "hidden"
    entry["hidden_at"] = gateway_core._now_iso()
    entry["hidden_reason"] = "operator_cleanup"
    registry = {"agents": [entry]}
    daemon._sweep_lifecycle(registry, session={"token": "axp_u_test"})
    assert entry["lifecycle_phase"] == "hidden"
    assert entry["hidden_reason"] == "operator_cleanup"
    recent = gateway_core.load_recent_gateway_activity()
    assert not any(r.get("event") == "managed_agent_unhidden" for r in recent)


def test_compose_agent_system_prompt_combines_operator_and_environment():
    """Composed prompt = operator's role text first, gateway environment
    context second. Operator instructions take precedence; the appended
    context tells the agent it's on a multi-agent network and how to use
    the CLI."""
    entry = {
        "name": "mission_orchestrator",
        "space_id": "ceb7e238-3e9d-4bcd-aaaf-27765c24f58c",
        "active_space_name": "GBR — Ground-Based Radar AI Agent Architecture",
        "base_url": "https://paxai.app",
        "system_prompt": "You coordinate the Golden Dome mission. Delegate sensor work.",
    }
    composed = gateway_core._compose_agent_system_prompt(entry)
    assert composed is not None
    # Operator first.
    assert composed.startswith("You coordinate the Golden Dome mission.")
    # Gateway context appended.
    assert "aX environment context" in composed
    assert "@mission_orchestrator" in composed
    assert "GBR — Ground-Based Radar AI Agent Architecture" in composed
    assert "https://paxai.app" in composed
    # CLI usage included so the agent knows how to interact.
    assert "ax send" in composed
    assert "ax tasks create" in composed
    assert "ax messages list" in composed


def test_compose_agent_system_prompt_with_no_operator_prompt_still_returns_environment():
    """An agent without an operator prompt still gets the environment
    context — every agent should know it's on the network."""
    entry = {"name": "echo-bot", "space_id": "space-1", "base_url": "https://paxai.app"}
    composed = gateway_core._compose_agent_system_prompt(entry)
    assert composed is not None
    assert "aX environment context" in composed
    assert "@echo-bot" in composed


def test_compose_agent_system_prompt_skip_environment_returns_operator_only():
    """Escape hatch for an operator who wants the environment context off:
    setting system_prompt_skip_environment returns just the operator text."""
    entry = {
        "name": "specialist",
        "system_prompt": "You are a specialist.",
        "system_prompt_skip_environment": "true",
    }
    composed = gateway_core._compose_agent_system_prompt(entry)
    assert composed == "You are a specialist."


def test_hermes_command_includes_composed_system_prompt():
    """The Hermes runtime command builder must pass the composed prompt
    (operator + environment) via --system-prompt, not just the operator's
    text alone."""
    entry = {
        "name": "sensor_fusion",
        "system_prompt": "Fuse sensor inputs into a coherent track.",
        "space_id": "space-x",
        "active_space_name": "GBR",
        "base_url": "https://paxai.app",
        "workdir": "/tmp/hermes-fusion",
        "runtime_type": "sentinel_inference_sdk",
    }
    cmd = gateway_core._build_sentinel_inference_sdk_cmd(entry, sdk_runtime="openai_sdk")
    assert "--system-prompt" in cmd
    payload_index = cmd.index("--system-prompt") + 1
    payload = cmd[payload_index]
    assert payload.startswith("Fuse sensor inputs")
    assert "aX environment context" in payload
    assert "ax send" in payload


def test_claude_command_includes_composed_system_prompt_via_append_flag():
    """Claude/Sentinel command builder uses --append-system-prompt; same
    composed contents must flow through."""
    entry = {
        "name": "threat_classifier",
        "system_prompt": "You classify aerial threats by signature.",
        "space_id": "space-y",
        "active_space_name": "GBR",
        "base_url": "https://paxai.app",
        "workdir": "/tmp/claude-threat",
        "runtime_type": "sentinel_cli",
    }
    cmd = gateway_core._build_sentinel_claude_cmd(entry, session_id=None)
    assert "--append-system-prompt" in cmd
    payload_index = cmd.index("--append-system-prompt") + 1
    payload = cmd[payload_index]
    assert payload.startswith("You classify aerial threats by signature.")
    assert "aX environment context" in payload


def test_exec_runtime_exposes_composed_prompt_via_env(monkeypatch, tmp_path):
    """exec-runtime bridges (Ollama, custom python bridges) read the operator
    prompt via AX_AGENT_SYSTEM_PROMPT. Without this wiring, an Ollama agent
    with a system_prompt set in the registry would never see it because the
    bridge is launched as a subprocess and gets no CLI flag."""
    captured: dict = {}

    class _StubProcess:
        returncode = 0
        stdout = None
        stderr = None
        pid = 12345

        def wait(self, *args, **kwargs):
            return 0

        def kill(self):
            pass

    def _fake_popen(argv, **kwargs):
        captured["env"] = dict(kwargs.get("env") or {})
        captured["argv"] = list(argv)
        return _StubProcess()

    monkeypatch.setattr(gateway_core.subprocess, "Popen", _fake_popen)

    entry = {
        "name": "gbr-orchestrator",
        "system_prompt": "You orchestrate the GBR mission.",
        "space_id": "ceb7e238",
        "active_space_name": "GBR",
        "base_url": "https://paxai.app",
        "exec_command": "python3 /tmp/fake-bridge.py",
        "runtime_type": "exec",
    }

    output = gateway_core._run_exec_handler(
        "python3 /tmp/fake-bridge.py", "hello", entry, message_id="m1", space_id="ceb7e238"
    )
    # Subprocess wasn't real, so output is "(no output)" — that's fine; we're
    # asserting on the captured env, not the result.
    assert "AX_AGENT_SYSTEM_PROMPT" in captured["env"]
    composed = captured["env"]["AX_AGENT_SYSTEM_PROMPT"]
    assert composed.startswith("You orchestrate the GBR mission.")
    assert "aX environment context" in composed
    assert output == "(no output)"


def test_exec_runtime_skips_env_var_when_no_prompt(monkeypatch, tmp_path):
    """No system_prompt and (extreme edge case) skip-environment set → don't
    set the env var at all. Bridges fall back to their built-in defaults."""
    captured: dict = {}

    class _StubProcess:
        returncode = 0
        stdout = None
        stderr = None

        def wait(self, *args, **kwargs):
            return 0

        def kill(self):
            pass

    def _fake_popen(argv, **kwargs):
        captured["env"] = dict(kwargs.get("env") or {})
        return _StubProcess()

    monkeypatch.setattr(gateway_core.subprocess, "Popen", _fake_popen)

    entry = {
        "name": "no-persona",
        "system_prompt_skip_environment": "true",
        "exec_command": "python3 /tmp/fake-bridge.py",
        "runtime_type": "exec",
    }
    gateway_core._run_exec_handler("python3 /tmp/fake-bridge.py", "hello", entry)
    assert "AX_AGENT_SYSTEM_PROMPT" not in captured["env"]


def test_sweep_skips_hidden_agents_no_upstream(monkeypatch, tmp_path):
    """Hidden agents must not produce upstream traffic from the sweep.
    Same contract as archived — operator has taken them out of the roster,
    Gateway shouldn't keep heartbeating on their behalf."""
    _isolate_gateway_paths(monkeypatch, tmp_path)
    client = _RecordingHeartbeatClient()
    daemon = _build_daemon(client)
    entry = _stale_hermes_entry("hermes-hidden", age_seconds=20 * 60)
    entry["lifecycle_phase"] = "hidden"
    registry = {"agents": [entry]}
    daemon._sweep_lifecycle(registry, session={"token": "axp_u_test", "base_url": "http://x"})
    assert client.heartbeats == []
    assert "last_lifecycle_signal" not in entry


def test_reconcile_skips_hidden_agents_no_runtime_no_upstream(monkeypatch, tmp_path):
    """Hidden agents must be skipped from the per-tick reconcile entirely:
    no identity-binding refresh, no attestation eval, no runtime start.
    With 20+ hidden agents in a workspace, this is the difference between
    a quiet daemon and one hammering paxai.app into 429s."""
    _isolate_gateway_paths(monkeypatch, tmp_path)
    client = _RecordingHeartbeatClient()
    daemon = _build_daemon(client)
    hidden = {
        "name": "stale-hidden",
        "agent_id": "agent-hidden",
        "template_id": "echo",
        "runtime_type": "echo",
        "desired_state": "running",  # would normally trigger runtime start
        "lifecycle_phase": "hidden",
    }
    archived = {
        "name": "stale-archived",
        "agent_id": "agent-archived",
        "template_id": "echo",
        "runtime_type": "echo",
        "desired_state": "running",
        "lifecycle_phase": "archived",
    }
    active = {
        "name": "live-agent",
        "agent_id": "agent-live",
        "template_id": "echo",
        "runtime_type": "echo",
        "desired_state": "stopped",
        "lifecycle_phase": "active",
    }
    registry = {"agents": [hidden, archived, active]}
    daemon._reconcile_registry(registry, session={"token": "axp_u_test", "base_url": "http://x"})
    # Neither hidden nor archived produced a runtime entry.
    assert "stale-hidden" not in daemon._runtimes
    assert "stale-archived" not in daemon._runtimes
    # The active entry was processed (no runtime since desired_state=stopped,
    # but its identity-binding side effects ran — proven by transport default).
    stored_active = next(a for a in registry["agents"] if a["name"] == "live-agent")
    assert stored_active.get("transport") == "gateway"
    # The hidden + archived entries got their default fields (the early-skip
    # is AFTER setdefaults), but no further processing.
    stored_hidden = next(a for a in registry["agents"] if a["name"] == "stale-hidden")
    assert stored_hidden.get("transport") == "gateway"
    # Hidden + archived must NOT have attestation_state populated by reconcile.
    # (evaluate_runtime_attestation runs only on the active path.)
    assert "attestation_state" not in stored_hidden or stored_hidden["attestation_state"] in (None, "")
    stored_archived = next(a for a in registry["agents"] if a["name"] == "stale-archived")
    assert "attestation_state" not in stored_archived or stored_archived["attestation_state"] in (None, "")


def test_reconcile_stops_runtime_when_agent_transitions_to_hidden(monkeypatch, tmp_path):
    """If the daemon had already started a runtime for an agent that the
    operator then hid, the next reconcile must stop that runtime so the
    hidden agent stops generating upstream traffic immediately."""
    _isolate_gateway_paths(monkeypatch, tmp_path)
    client = _RecordingHeartbeatClient()
    daemon = _build_daemon(client)
    entry = {
        "name": "transition-hide",
        "agent_id": "agent-th",
        "template_id": "echo",
        "runtime_type": "echo",
        "desired_state": "running",
        "lifecycle_phase": "hidden",  # operator just hid it
    }

    class _StoppableRuntime:
        def __init__(self):
            self.stopped = False

        def stop(self):
            self.stopped = True

    fake_runtime = _StoppableRuntime()
    daemon._runtimes["transition-hide"] = fake_runtime

    daemon._reconcile_registry({"agents": [entry]}, session={"token": "axp_u_test", "base_url": "http://x"})

    assert fake_runtime.stopped is True
    assert "transition-hide" not in daemon._runtimes


def test_legacy_entry_without_lifecycle_phase_loads_as_active(monkeypatch, tmp_path):
    _isolate_gateway_paths(monkeypatch, tmp_path)
    client = _RecordingHeartbeatClient()
    daemon = _build_daemon(client)
    # No lifecycle_phase field at all — represents pre-v1 on-disk entry.
    entry = {
        "name": "hermes-legacy",
        "agent_id": "agent-legacy",
        "template_id": "hermes",
        "runtime_type": "sentinel_inference_sdk",
        "effective_state": "running",
        "liveness": "stale",
        "last_seen_age_seconds": 30 * 60,
    }
    registry = {"agents": [entry]}
    daemon._sweep_lifecycle(registry, session={"token": "axp_u_test"})
    # Legacy entry stays active — sweep no longer auto-hides. The default
    # ``active`` phase is implied for entries with no lifecycle_phase field.
    assert entry.get("lifecycle_phase", "active") == "active"


def test_send_local_session_message_extracts_mentions_when_client_omits_them():
    """Defense-in-depth: a client that forgets to populate metadata.mentions
    must still result in mentions reaching the backend, because Gateway sees
    the same content and re-extracts."""
    from ax_cli.mentions import merge_explicit_mentions_metadata

    metadata_input = {"purpose": "test"}
    body = {
        "space_id": "space-1",
        "content": "@night_owl heads up",
        "parent_id": "parent-7",
        "metadata": metadata_input,
    }

    metadata = {
        **metadata_input,
        "gateway_local_session_id": "sess-1",
        "gateway_pass_through_agent": "codex-local",
    }
    if body["parent_id"]:
        metadata.setdefault("routing_intent", "reply_with_mentions")
    metadata = merge_explicit_mentions_metadata(metadata, body["content"], exclude=["codex-local"]) or metadata

    assert metadata["mentions"] == ["night_owl"]
    assert metadata["routing_intent"] == "reply_with_mentions"
    assert metadata["purpose"] == "test"


def test_sweep_does_not_unhide_archived_agent(monkeypatch, tmp_path):
    """Archived is sticky — sweep must not auto-restore even when liveness=connected."""
    _isolate_gateway_paths(monkeypatch, tmp_path)
    client = _RecordingHeartbeatClient()
    daemon = _build_daemon(client)
    entry = _stale_hermes_entry("probe-archived", age_seconds=2.0, liveness="connected")
    entry["lifecycle_phase"] = "archived"
    entry["archived_at"] = gateway_core._now_iso()
    registry = {"agents": [entry]}
    daemon._sweep_lifecycle(registry, session={"token": "axp_u_test"})
    # Sticky — sweep must not flip back to active.
    assert entry["lifecycle_phase"] == "archived"
    # No upstream signaling for archived entries either.
    assert client.heartbeats == []


def test_save_registry_preserves_other_writer_added_row(monkeypatch, tmp_path):
    """Race regression: daemon's load → modify → save must not clobber an
    agent row added by another writer (e.g. the UI server's
    POST /api/agents) between the daemon's load and the daemon's save.

    Reproduces the cc-backend bug from 2026-05-06: managed_agent_added
    activity recorded, registry write succeeded, then daemon's reconcile
    tick wrote back without the new row.
    """
    _isolate_gateway_paths(monkeypatch, tmp_path)
    initial = {
        "agents": [
            {
                "name": "incumbent",
                "agent_id": "agent-incumbent",
                "template_id": "hermes",
                "runtime_type": "sentinel_inference_sdk",
                "lifecycle_phase": "active",
                "desired_state": "running",
            }
        ]
    }
    gateway_core.save_gateway_registry(initial)

    # Daemon's stale in-memory copy from the start of its tick — knows only
    # about the incumbent.
    daemon_view = gateway_core.load_gateway_registry()
    daemon_view["agents"][0]["effective_state"] = "running"  # daemon-side update

    # Another writer (UI server, channel setup, etc.) loads, adds a new
    # agent, and saves between the daemon's load and the daemon's save.
    other_writer = gateway_core.load_gateway_registry()
    other_writer["agents"].append(
        {
            "name": "newcomer",
            "agent_id": "agent-newcomer",
            "template_id": "claude_code_channel",
            "runtime_type": "claude_code_channel",
            "lifecycle_phase": "active",
            "desired_state": "running",
        }
    )
    gateway_core.save_gateway_registry(other_writer)

    # Daemon now saves its stale copy that never saw newcomer. Row
    # preservation should keep newcomer.
    gateway_core.save_gateway_registry(daemon_view)

    final = gateway_core.load_gateway_registry()
    names = {a["name"] for a in final["agents"]}
    assert names == {"incumbent", "newcomer"}, "newcomer was clobbered by daemon save — registry write race regressed"
    # Daemon's effective_state update on the incumbent should still apply.
    incumbent = next(a for a in final["agents"] if a["name"] == "incumbent")
    assert incumbent["effective_state"] == "running"


def test_save_registry_preserves_other_writer_row_deletion(monkeypatch, tmp_path):
    """Race regression (#42): daemon's load → modify → save must not
    resurrect an agent row that another writer (the CLI) removed between
    the daemon's load and the daemon's save.

    Reproduces the agents-remove bug from the #42 report:
    ``axctl gateway agents remove <name>`` exited 0 and deleted the token
    file, but the daemon's stale in-memory copy wrote the entry back on
    the next poll cycle — agent reappeared in `agents list`, doctor went
    red, and the operator was told the remove "didn't take".

    Symmetric to test_save_registry_preserves_other_writer_added_row.
    """
    _isolate_gateway_paths(monkeypatch, tmp_path)
    initial = {
        "agents": [
            {
                "name": "incumbent",
                "agent_id": "agent-incumbent",
                "template_id": "hermes",
                "runtime_type": "sentinel_inference_sdk",
                "lifecycle_phase": "active",
                "desired_state": "running",
            },
            {
                "name": "to-remove",
                "agent_id": "agent-to-remove",
                "template_id": "hermes",
                "runtime_type": "sentinel_inference_sdk",
                "lifecycle_phase": "active",
                "desired_state": "running",
            },
        ]
    }
    gateway_core.save_gateway_registry(initial)

    # Daemon's stale in-memory copy from the start of its tick — still
    # sees both agents.
    daemon_view = gateway_core.load_gateway_registry()
    daemon_view["agents"][0]["effective_state"] = "running"  # daemon-side update

    # CLI loads, removes "to-remove", saves between daemon's load and save.
    cli_view = gateway_core.load_gateway_registry()
    cli_view["agents"] = [a for a in cli_view["agents"] if a["name"] != "to-remove"]
    gateway_core.save_gateway_registry(cli_view)

    # Daemon now saves its stale copy that still has to-remove. Row
    # deletion preservation should drop to-remove from the daemon's view
    # rather than resurrect it on disk.
    gateway_core.save_gateway_registry(daemon_view)

    final = gateway_core.load_gateway_registry()
    names = {a["name"] for a in final["agents"]}
    assert names == {"incumbent"}, "to-remove was resurrected by daemon save — registry remove race regressed (#42)"
    # Daemon's effective_state update on the incumbent should still apply.
    incumbent = next(a for a in final["agents"] if a["name"] == "incumbent")
    assert incumbent["effective_state"] == "running"


def test_save_registry_keeps_caller_added_row_when_disk_lost_it(monkeypatch, tmp_path):
    """A row the caller just added (not in their snapshot) must survive
    the new deletion-preservation logic. Snapshot is the gate that
    distinguishes 'we just added this' from 'we always knew about it'.
    """
    _isolate_gateway_paths(monkeypatch, tmp_path)
    initial = {
        "agents": [
            {
                "name": "incumbent",
                "agent_id": "agent-incumbent",
                "template_id": "hermes",
                "runtime_type": "sentinel_inference_sdk",
                "lifecycle_phase": "active",
                "desired_state": "running",
            }
        ]
    }
    gateway_core.save_gateway_registry(initial)

    cli_view = gateway_core.load_gateway_registry()
    cli_view["agents"].append(
        {
            "name": "freshly-added",
            "agent_id": "agent-freshly-added",
            "template_id": "claude_code_channel",
            "runtime_type": "claude_code_channel",
            "lifecycle_phase": "active",
            "desired_state": "running",
        }
    )
    gateway_core.save_gateway_registry(cli_view)

    final = gateway_core.load_gateway_registry()
    names = {a["name"] for a in final["agents"]}
    assert names == {"incumbent", "freshly-added"}


def test_save_registry_preserves_other_writer_field_update(monkeypatch, tmp_path):
    """Race regression (field-level): the daemon's stale `desired_state=running`
    in-memory view must not clobber the CLI's freshly written
    `desired_state=stopped` on disk.

    Reproduces the agents-stop bug from 2026-05-06: `ax gateway agents stop`
    set desired_state=stopped, activity log recorded
    managed_agent_desired_stopped, but the daemon's next reconcile save
    flipped it back to running within seconds — making demo-hermes
    impossible to actually stop.
    """
    _isolate_gateway_paths(monkeypatch, tmp_path)
    initial = {
        "agents": [
            {
                "name": "race-stop",
                "agent_id": "agent-race-stop",
                "template_id": "hermes",
                "runtime_type": "sentinel_inference_sdk",
                "lifecycle_phase": "active",
                "desired_state": "running",
                "effective_state": "running",
            }
        ]
    }
    gateway_core.save_gateway_registry(initial)

    # Daemon's stale in-memory copy from the start of its tick.
    daemon_view = gateway_core.load_gateway_registry()
    daemon_view["agents"][0]["effective_state"] = "running"  # daemon-side telemetry update

    # CLI runs `agents stop` between the daemon's load and the daemon's
    # save: writes desired_state=stopped to disk.
    cli_view = gateway_core.load_gateway_registry()
    cli_view["agents"][0]["desired_state"] = "stopped"
    gateway_core.save_gateway_registry(cli_view)

    # Daemon now saves its (stale) copy. Field-level preservation should
    # take disk's freshly-written desired_state=stopped, not memory's
    # stale desired_state=running.
    gateway_core.save_gateway_registry(daemon_view)

    final = gateway_core.load_gateway_registry()
    stored = next(a for a in final["agents"] if a["name"] == "race-stop")
    assert stored["desired_state"] == "stopped", (
        "daemon clobbered CLI's desired_state=stopped — field-level race preservation regressed"
    )
    # Daemon's effective_state telemetry update should still apply.
    assert stored["effective_state"] == "running"


def test_save_registry_honors_caller_remove(monkeypatch, tmp_path):
    """Row preservation must distinguish "caller removed this" from
    "another writer added this." A row that was present at load time and
    is missing in memory at save time means the caller removed it; we
    must not resurrect it from disk.
    """
    _isolate_gateway_paths(monkeypatch, tmp_path)
    initial = {
        "agents": [
            {"name": "to-remove", "agent_id": "a1", "template_id": "echo"},
            {"name": "to-keep", "agent_id": "a2", "template_id": "echo"},
        ]
    }
    gateway_core.save_gateway_registry(initial)

    # Caller loads, removes one row, saves. Disk still had to-remove at
    # the moment we re-read inside save — the load snapshot tells us we
    # had it and the caller removed it intentionally.
    registry = gateway_core.load_gateway_registry()
    registry["agents"] = [a for a in registry["agents"] if a["name"] != "to-remove"]
    gateway_core.save_gateway_registry(registry)

    final = gateway_core.load_gateway_registry()
    names = {a["name"] for a in final["agents"]}
    assert names == {"to-keep"}, "to-remove was resurrected — remove was lost"


def test_runtime_start_skips_when_in_setup_error_backoff(monkeypatch, tmp_path):
    """Setup-error backoff: runtime.start() must early-return when a
    runtime_error fired within the last SETUP_ERROR_BACKOFF_SECONDS,
    so the daemon's per-tick reconcile (every ~1s) doesn't fire a
    runtime_error storm and pressure upstream rate limits.

    Reproduces the demo-hermes spam: missing token file + desired_state
    running → 140 runtime_error events in 5 minutes.
    """
    _isolate_gateway_paths(monkeypatch, tmp_path)
    token_file = tmp_path / "token-does-not-exist"  # intentionally missing

    runtime = gateway_core.ManagedAgentRuntime(
        {
            "name": "stuck-hermes",
            "agent_id": "agent-stuck",
            "space_id": "space-1",
            "base_url": "https://paxai.app",
            "runtime_type": "sentinel_inference_sdk",
            "token_file": str(token_file),
            # Simulates the entry state after a setup error: error 1s ago,
            # well within the 30s backoff window.
            "last_runtime_error_at": gateway_core._now_iso(),
        },
        client_factory=lambda **kwargs: object(),
    )

    before = gateway_core.load_recent_gateway_activity()
    runtime.start()
    after = gateway_core.load_recent_gateway_activity()

    # Gate must early-return without firing a fresh runtime_error event.
    assert len(after) == len(before), (
        "runtime.start() emitted a runtime_error event while in backoff window — gate regressed"
    )
    # State must be untouched — no transition through "starting".
    assert runtime._state.get("effective_state") != "starting"


def test_runtime_start_proceeds_after_setup_error_backoff_expires(monkeypatch, tmp_path):
    """Once the backoff window expires, the runtime is allowed to retry.
    The retry will fire its own runtime_error if the precondition is
    still broken — that fresh error stamps a new last_runtime_error_at,
    re-arming the gate.
    """
    _isolate_gateway_paths(monkeypatch, tmp_path)
    token_file = tmp_path / "token-does-not-exist"  # still missing

    # last_runtime_error_at older than the backoff window.
    long_ago = (
        datetime.now(timezone.utc) - timedelta(seconds=gateway_core.SETUP_ERROR_BACKOFF_SCHEDULE[0] + 60)
    ).isoformat()

    runtime = gateway_core.ManagedAgentRuntime(
        {
            "name": "expired-hermes",
            "agent_id": "agent-expired",
            "space_id": "space-1",
            "base_url": "https://paxai.app",
            "runtime_type": "sentinel_inference_sdk",
            "token_file": str(token_file),
            "last_runtime_error_at": long_ago,
        },
        client_factory=lambda **kwargs: object(),
    )

    before = gateway_core.load_recent_gateway_activity()
    runtime.start()
    after = gateway_core.load_recent_gateway_activity()

    new_events = [e for e in after if e not in before]
    runtime_errors = [e for e in new_events if e.get("event") == "runtime_error"]
    assert len(runtime_errors) == 1, (
        f"expected exactly one runtime_error after backoff expired, got {len(runtime_errors)}"
    )
    # Fresh error stamps a new last_runtime_error_at, re-arming the gate
    # against repeat retries from the next reconcile tick.
    assert runtime.entry.get("last_runtime_error_at") is not None
    assert runtime.entry["last_runtime_error_at"] != long_ago


def test_reconcile_corrupt_space_ids_recovers_uuid_from_active_space():
    registry = {
        "agents": [
            {
                "name": "taskforge_backend",
                "space_id": "madtank's Workspace",
                "active_space_id": _GOOD_SPACE_UUID,
                "active_space_name": "madtank's Workspace",
                "default_space_id": _GOOD_SPACE_UUID,
            }
        ]
    }
    repaired = gateway_core.reconcile_corrupt_space_ids(registry)
    assert repaired == 1
    assert registry["agents"][0]["space_id"] == _GOOD_SPACE_UUID


def test_reconcile_corrupt_space_ids_falls_back_to_allowed_spaces():
    registry = {
        "agents": [
            {
                "name": "x",
                "space_id": "Workspace-Name",
                "allowed_spaces": [{"space_id": _GOOD_SPACE_UUID, "name": "ws"}],
            }
        ]
    }
    assert gateway_core.reconcile_corrupt_space_ids(registry) == 1
    assert registry["agents"][0]["space_id"] == _GOOD_SPACE_UUID


def test_reconcile_corrupt_space_ids_idempotent_on_clean_registry():
    registry = {
        "agents": [
            {"name": "a", "space_id": _GOOD_SPACE_UUID},
            {"name": "b", "space_id": ""},  # empty is left alone
            {"name": "c"},  # no space_id at all is left alone
        ]
    }
    assert gateway_core.reconcile_corrupt_space_ids(registry) == 0
    assert registry["agents"][0]["space_id"] == _GOOD_SPACE_UUID
    assert registry["agents"][1]["space_id"] == ""
    assert "space_id" not in registry["agents"][2]


def test_reconcile_corrupt_space_ids_skips_when_no_uuid_anywhere():
    registry = {
        "agents": [
            {"name": "lost", "space_id": "Some Name", "active_space_id": "Also Not A UUID"},
        ]
    }
    # Nothing recoverable — leave the bad value in place rather than fabricate.
    assert gateway_core.reconcile_corrupt_space_ids(registry) == 0
    assert registry["agents"][0]["space_id"] == "Some Name"


def test_load_gateway_registry_heals_corrupt_space_id_in_place(monkeypatch, tmp_path):
    config_dir = tmp_path / "config"
    monkeypatch.setenv("AX_CONFIG_DIR", str(config_dir))
    gateway_dir = gateway_core.gateway_dir()
    gateway_dir.mkdir(parents=True, exist_ok=True)
    raw = {
        "version": 1,
        "agents": [
            {
                "name": "taskforge_backend",
                "space_id": "madtank's Workspace",
                "active_space_id": _GOOD_SPACE_UUID,
                "default_space_id": _GOOD_SPACE_UUID,
            }
        ],
    }
    gateway_core.registry_path().write_text(json.dumps(raw), encoding="utf-8")
    loaded = gateway_core.load_gateway_registry()
    assert loaded["agents"][0]["space_id"] == _GOOD_SPACE_UUID


def test_setup_error_backoff_escalates(monkeypatch, tmp_path):
    """Backoff must use the escalating schedule, not a flat 30s.
    A runtime with 3 consecutive errors should wait 120s, not 30s."""
    _isolate_gateway_paths(monkeypatch, tmp_path)
    token_file = tmp_path / "token-missing"

    error_at = (datetime.now(timezone.utc) - timedelta(seconds=35)).isoformat()

    runtime = gateway_core.ManagedAgentRuntime(
        {
            "name": "escalate-hermes",
            "agent_id": "agent-esc",
            "space_id": "space-1",
            "base_url": "https://paxai.app",
            "runtime_type": "sentinel_inference_sdk",
            "token_file": str(token_file),
            "last_runtime_error_at": error_at,
            "consecutive_setup_errors": 3,
        },
        client_factory=lambda **kwargs: object(),
    )

    before = gateway_core.load_recent_gateway_activity()
    runtime.start()
    after = gateway_core.load_recent_gateway_activity()
    assert len(after) == len(before), (
        "runtime.start() should still be in backoff (schedule[2]=120s) but it fired — escalation failed"
    )


def test_setup_error_backoff_clamps_at_schedule_max(monkeypatch, tmp_path):
    """Consecutive errors beyond the schedule length clamp to the last entry."""
    _isolate_gateway_paths(monkeypatch, tmp_path)
    token_file = tmp_path / "token-missing"

    max_backoff = gateway_core.SETUP_ERROR_BACKOFF_SCHEDULE[-1]
    error_at = (datetime.now(timezone.utc) - timedelta(seconds=max_backoff - 10)).isoformat()

    runtime = gateway_core.ManagedAgentRuntime(
        {
            "name": "clamp-hermes",
            "agent_id": "agent-clamp",
            "space_id": "space-1",
            "base_url": "https://paxai.app",
            "runtime_type": "sentinel_inference_sdk",
            "token_file": str(token_file),
            "last_runtime_error_at": error_at,
            "consecutive_setup_errors": 100,
        },
        client_factory=lambda **kwargs: object(),
    )

    before = gateway_core.load_recent_gateway_activity()
    runtime.start()
    after = gateway_core.load_recent_gateway_activity()
    assert len(after) == len(before), (
        f"runtime.start() should still be in backoff (clamped to last schedule entry {max_backoff}s) but it fired"
    )


def test_auto_disable_after_max_consecutive_errors(monkeypatch, tmp_path):
    """After SETUP_ERROR_MAX_CONSECUTIVE identical errors, the agent is
    auto-disabled with setup_disabled=True and a runtime_auto_disabled event."""
    _isolate_gateway_paths(monkeypatch, tmp_path)
    token_file = tmp_path / "token-missing"

    long_ago = (
        datetime.now(timezone.utc) - timedelta(seconds=gateway_core.SETUP_ERROR_BACKOFF_SCHEDULE[-1] + 60)
    ).isoformat()

    runtime = gateway_core.ManagedAgentRuntime(
        {
            "name": "disable-hermes",
            "agent_id": "agent-dis",
            "space_id": "space-1",
            "base_url": "https://paxai.app",
            "runtime_type": "sentinel_inference_sdk",
            "token_file": str(token_file),
            "last_runtime_error_at": long_ago,
            "consecutive_setup_errors": gateway_core.SETUP_ERROR_MAX_CONSECUTIVE - 1,
            "last_setup_error_signature": gateway_core._setup_error_signature(
                f"Gateway-managed token file is missing: {token_file}"
            ),
        },
        client_factory=lambda **kwargs: object(),
    )

    runtime.start()
    assert runtime.entry.get("setup_disabled") is True
    assert runtime.entry.get("setup_disabled_at") is not None
    assert "Auto-disabled" in str(runtime.entry.get("setup_disabled_reason") or "")
    activity = gateway_core.load_recent_gateway_activity()
    auto_disabled = [e for e in activity if e.get("event") == "runtime_auto_disabled"]
    assert len(auto_disabled) >= 1


def test_disabled_runtime_start_is_noop(monkeypatch, tmp_path):
    """A setup_disabled runtime must not attempt any work on start()."""
    _isolate_gateway_paths(monkeypatch, tmp_path)

    runtime = gateway_core.ManagedAgentRuntime(
        {
            "name": "noop-hermes",
            "agent_id": "agent-noop",
            "space_id": "space-1",
            "base_url": "https://paxai.app",
            "runtime_type": "sentinel_inference_sdk",
            "token_file": str(tmp_path / "tok"),
            "setup_disabled": True,
        },
        client_factory=lambda **kwargs: object(),
    )

    before = gateway_core.load_recent_gateway_activity()
    runtime.start()
    after = gateway_core.load_recent_gateway_activity()
    assert len(after) == len(before), "disabled runtime should not emit any activity"
    assert runtime._state.get("effective_state") != "starting"


def test_reconcile_skips_setup_disabled_agents(monkeypatch, tmp_path):
    """Setup-disabled agents must be skipped from reconcile, like hidden/archived."""
    _isolate_gateway_paths(monkeypatch, tmp_path)
    client = _RecordingHeartbeatClient()
    daemon = _build_daemon(client)
    entry = {
        "name": "disabled-agent",
        "agent_id": "agent-disabled",
        "template_id": "echo",
        "runtime_type": "echo",
        "desired_state": "running",
        "lifecycle_phase": "active",
        "setup_disabled": True,
    }
    registry = {"agents": [entry]}
    daemon._reconcile_registry(registry, session={"token": "axp_u_test", "base_url": "http://x"})
    assert "disabled-agent" not in daemon._runtimes
    stored = next(a for a in registry["agents"] if a["name"] == "disabled-agent")
    assert "attestation_state" not in stored or stored.get("attestation_state") in (None, "")


def test_sweep_skips_setup_disabled_no_upstream(monkeypatch, tmp_path):
    """Setup-disabled agents must not generate upstream heartbeat traffic."""
    _isolate_gateway_paths(monkeypatch, tmp_path)
    client = _RecordingHeartbeatClient()
    daemon = _build_daemon(client)
    entry = _stale_hermes_entry("hermes-disabled", age_seconds=20 * 60)
    entry["setup_disabled"] = True
    entry["liveness"] = "offline"
    registry = {"agents": [entry]}
    daemon._sweep_lifecycle(registry, session={"token": "axp_u_test", "base_url": "http://x"})
    assert client.heartbeats == []


def test_proactive_binary_check_catches_missing_python(monkeypatch, tmp_path):
    """When the hermes python binary path is absolute and missing, the error
    should say 'Python binary not found' and increment consecutive_setup_errors."""
    _isolate_gateway_paths(monkeypatch, tmp_path)
    missing_python = str(tmp_path / "venv" / "bin" / "python3")

    long_ago = (datetime.now(timezone.utc) - timedelta(seconds=700)).isoformat()

    runtime = gateway_core.ManagedAgentRuntime(
        {
            "name": "binary-check",
            "agent_id": "agent-bin",
            "space_id": "space-1",
            "base_url": "https://paxai.app",
            "runtime_type": "sentinel_inference_sdk",
            "token_file": str(tmp_path / "tok"),
            "python": missing_python,
            "last_runtime_error_at": long_ago,
            "consecutive_setup_errors": 0,
        },
        client_factory=lambda **kwargs: object(),
    )

    token_file = Path(str(runtime.entry.get("token_file")))
    token_file.parent.mkdir(parents=True, exist_ok=True)
    token_file.write_text("axp_a_test_token")

    sentinel_dir = Path(gateway_core.__file__).resolve().parent / "runtimes" / "hermes"
    sentinel_script = sentinel_dir / "sentinel.py"
    if not sentinel_script.exists():
        sentinel_dir.mkdir(parents=True, exist_ok=True)
        sentinel_script.write_text("# stub")

    runtime.start()
    state_error = runtime._state.get("last_error") or ""
    assert "Python binary not found" in state_error
    assert runtime.entry.get("consecutive_setup_errors") == 1


def test_proactive_binary_check_skips_relative_path(monkeypatch, tmp_path):
    """A relative python path like 'python3' should not be pre-checked
    (PATH resolution differs between validation and Popen time)."""
    _isolate_gateway_paths(monkeypatch, tmp_path)

    long_ago = (datetime.now(timezone.utc) - timedelta(seconds=700)).isoformat()

    workdir = tmp_path / "agents" / "relative-check"
    runtime = gateway_core.ManagedAgentRuntime(
        {
            "name": "relative-check",
            "agent_id": "agent-rel",
            "space_id": "space-1",
            "base_url": "https://paxai.app",
            "runtime_type": "sentinel_inference_sdk",
            "token_file": str(tmp_path / "tok"),
            "hermes_python": "python3",
            "workdir": str(workdir),
            "last_runtime_error_at": long_ago,
            "consecutive_setup_errors": 0,
        },
        client_factory=lambda **kwargs: object(),
    )

    token_file = Path(str(runtime.entry.get("token_file")))
    token_file.parent.mkdir(parents=True, exist_ok=True)
    token_file.write_text("axp_a_test_token")

    sentinel_dir = Path(gateway_core.__file__).resolve().parent / "runtimes" / "hermes"
    sentinel_script = sentinel_dir / "sentinel.py"
    if not sentinel_script.exists():
        sentinel_dir.mkdir(parents=True, exist_ok=True)
        sentinel_script.write_text("# stub")

    runtime.start()
    state_error = runtime._state.get("last_error") or ""
    assert "Python binary not found" not in state_error


def test_different_error_signature_resets_consecutive_count(monkeypatch, tmp_path):
    """When the error message changes, consecutive count resets to 1."""
    _isolate_gateway_paths(monkeypatch, tmp_path)

    runtime = gateway_core.ManagedAgentRuntime(
        {
            "name": "sig-reset",
            "agent_id": "agent-sig",
            "space_id": "space-1",
            "base_url": "https://paxai.app",
            "runtime_type": "echo",
            "consecutive_setup_errors": 5,
            "last_setup_error_signature": "Token file not found: /old/path",
        },
        client_factory=lambda **kwargs: object(),
    )

    runtime._record_setup_error("Python binary not found: /new/path")
    assert runtime.entry["consecutive_setup_errors"] == 1
    assert runtime.entry["last_setup_error_signature"] == gateway_core._setup_error_signature(
        "Python binary not found: /new/path"
    )


def test_same_error_signature_increments_consecutive_count(monkeypatch, tmp_path):
    """Same error signature increments the consecutive count."""
    _isolate_gateway_paths(monkeypatch, tmp_path)

    error_msg = "Token file not found: /some/path"
    runtime = gateway_core.ManagedAgentRuntime(
        {
            "name": "sig-inc",
            "agent_id": "agent-inc",
            "space_id": "space-1",
            "base_url": "https://paxai.app",
            "runtime_type": "echo",
            "consecutive_setup_errors": 3,
            "last_setup_error_signature": gateway_core._setup_error_signature(error_msg),
        },
        client_factory=lambda **kwargs: object(),
    )

    runtime._record_setup_error(error_msg)
    assert runtime.entry["consecutive_setup_errors"] == 4


def test_distinct_errors_sharing_long_prefix_do_not_falsely_dedup(monkeypatch, tmp_path):
    """#34: two distinct errors that share a long common prefix (e.g. the
    same exception class + same module path in a deep stack trace) must
    not be treated as the same error by the consecutive-count dedup. The
    old 120-char prefix signature collided on that pattern and made the
    counter creep toward auto-disable when the root cause was actually
    changing under it."""
    _isolate_gateway_paths(monkeypatch, tmp_path)

    common_prefix = "ValueError: " + ("x" * 130) + ": "
    error_a = common_prefix + "cause_A"
    error_b = common_prefix + "cause_B"
    # Both first 120 chars are identical — the old shape considered them
    # the same error. SHA-256 over the full string does not.
    assert error_a[:120] == error_b[:120]
    assert gateway_core._setup_error_signature(error_a) != gateway_core._setup_error_signature(error_b)

    runtime = gateway_core.ManagedAgentRuntime(
        {
            "name": "long-prefix-bug",
            "agent_id": "agent-lpb",
            "space_id": "space-1",
            "base_url": "https://paxai.app",
            "runtime_type": "echo",
        },
        client_factory=lambda **kwargs: object(),
    )

    runtime._record_setup_error(error_a)
    assert runtime.entry["consecutive_setup_errors"] == 1
    runtime._record_setup_error(error_b)
    # Different root cause → counter resets to 1, not increments to 2.
    assert runtime.entry["consecutive_setup_errors"] == 1, (
        "distinct errors sharing a 120-char prefix were falsely deduped (#34 regression)"
    )


def test_hermes_plugin_setup_error_increments_consecutive_count(monkeypatch, tmp_path):
    """#33: the hermes_plugin runtime must go through the same
    consecutive-error counter + auto-disable plumbing as sentinel_inference_sdk.

    Before this fix, _start_hermes_plugin_process called the old inline
    _record_supervised_setup_error which only recorded state — no counter,
    no auto-disable, no escalating backoff. A plugin agent with a broken
    precondition (missing token, missing scaffold) would retry every
    reconcile tick indefinitely.
    """
    _isolate_gateway_paths(monkeypatch, tmp_path)
    # Token file path points nowhere — load_gateway_managed_agent_token raises
    # at the first call site inside _start_hermes_plugin_process.
    token_file = tmp_path / "token-missing"

    runtime = gateway_core.ManagedAgentRuntime(
        {
            "name": "plugin-esc",
            "agent_id": "agent-plugin-esc",
            "space_id": "space-1",
            "base_url": "https://paxai.app",
            "runtime_type": "hermes_plugin",
            "token_file": str(token_file),
            "consecutive_setup_errors": 0,
        },
        client_factory=lambda **kwargs: object(),
    )

    runtime._start_hermes_plugin_process(runtime_instance_id="ri-test")

    assert runtime.entry.get("consecutive_setup_errors") == 1, (
        "hermes_plugin setup error did not increment counter — escalating backoff plumbing not wired (#33 regression)"
    )
    assert runtime.entry.get("last_setup_error_signature"), (
        "hermes_plugin setup error did not record signature for dedup"
    )


def test_hermes_plugin_setup_error_eventually_auto_disables(monkeypatch, tmp_path):
    """#33 (auto-disable side): repeated plugin setup failures must auto-disable
    after SETUP_ERROR_MAX_CONSECUTIVE identical errors, same as sentinel."""
    _isolate_gateway_paths(monkeypatch, tmp_path)
    # ``hermes_bin`` set to a string short-circuits _hermes_bin so the
    # deterministic failure point is the token-missing ValueError — that's
    # the error whose signature we seed below.
    token_file = tmp_path / "token-missing"

    # Seed at MAX - 1 so the next failure crosses the threshold.
    seed_error_msg = f"Gateway-managed token file is missing: {token_file}"
    runtime = gateway_core.ManagedAgentRuntime(
        {
            "name": "plugin-disable",
            "agent_id": "agent-plugin-disable",
            "space_id": "space-1",
            "base_url": "https://paxai.app",
            "runtime_type": "hermes_plugin",
            "hermes_bin": "/skip-step-1-bin-resolution",
            "token_file": str(token_file),
            "consecutive_setup_errors": gateway_core.SETUP_ERROR_MAX_CONSECUTIVE - 1,
            "last_setup_error_signature": gateway_core._setup_error_signature(seed_error_msg),
        },
        client_factory=lambda **kwargs: object(),
    )

    runtime._start_hermes_plugin_process(runtime_instance_id="ri-test")

    assert runtime.entry.get("setup_disabled") is True, (
        "hermes_plugin did not auto-disable after MAX consecutive setup errors"
    )
    assert "Auto-disabled" in str(runtime.entry.get("setup_disabled_reason") or "")


def test_load_gateway_registry_strips_legacy_gateway_space_keys(monkeypatch, tmp_path):
    """Legacy registries with space_id/space_name in the gateway block must be
    auto-migrated on load: session.json owns active space, registry.gateway
    never carries a duplicate."""
    config_dir = tmp_path / "config"
    monkeypatch.setenv("AX_CONFIG_DIR", str(config_dir))
    legacy = {
        "version": 1,
        "gateway": {
            "gateway_id": "gw-test",
            "space_id": _GOOD_SPACE_UUID,
            "space_name": "madtank's Workspace",
            "session_connected": True,
        },
        "agents": [],
        "bindings": [],
        "identity_bindings": [],
        "approvals": [],
    }
    gateway_core.registry_path().parent.mkdir(parents=True, exist_ok=True)
    gateway_core.registry_path().write_text(json.dumps(legacy), encoding="utf-8")

    loaded = gateway_core.load_gateway_registry()
    assert "space_id" not in loaded["gateway"]
    assert "space_name" not in loaded["gateway"]
    # Gateway-specific metadata must be preserved.
    assert loaded["gateway"]["gateway_id"] == "gw-test"
    assert loaded["gateway"]["session_connected"] is True


def test_resolve_space_ref_uses_cache_when_upstream_429(monkeypatch, tmp_path):
    """A slug we have ever resolved before must be re-resolvable from the
    spaces cache without going upstream — so transient 429s on list_spaces
    don't break `gateway spaces use <slug>`."""
    config_dir = tmp_path / "config"
    monkeypatch.setenv("AX_CONFIG_DIR", str(config_dir))
    team_uuid = "78950af5-4d27-441b-9296-ec46de8ba35d"
    gateway_core.save_space_cache(
        [
            {"id": _GOOD_SPACE_UUID, "name": "madtank's Workspace", "slug": "madtank-workspace"},
            {"id": team_uuid, "name": "aX CLI Dev", "slug": "ax-cli-dev"},
        ]
    )

    class Boom:
        def list_spaces(self):
            raise AssertionError("upstream must NOT be called when the cache has the slug")

    from ax_cli.config import _resolve_space_ref

    resolved = _resolve_space_ref(Boom(), "ax-cli-dev", source="explicit")
    assert resolved == team_uuid


def test_runtime_reconcile_skips_phantom_rebinding_after_corruption_repair(monkeypatch, tmp_path):
    """The reconcile_corrupt_space_ids band-aid repairs entry.space_id from a
    leaked name to a UUID. The daemon's reconcile() must NOT emit a fake
    runtime_rebinding event for that purely-cosmetic change."""
    captured: list[dict] = []

    def fake_record(event, **kwargs):
        captured.append({"event": event, **kwargs})

    monkeypatch.setattr(gateway_core, "record_gateway_activity", fake_record)

    class FakeRuntime:
        def __init__(self, entry):
            self.entry = dict(entry)
            self._stopped = False

        def stop(self):
            self._stopped = True

        def start(self):
            pass

    daemon = gateway_core.GatewayDaemon.__new__(gateway_core.GatewayDaemon)
    daemon._runtimes = {}
    daemon.client_factory = lambda: object()
    daemon.logger = None
    runtime = FakeRuntime(
        {
            "name": "agent-x",
            "agent_id": "00000000-1111-2222-3333-444444444444",
            "space_id": "madtank's Workspace",  # legacy non-UUID corruption
            "base_url": "https://paxai.app",
            "token_file": "/tmp/agent-token",
            "runtime_type": "inbox",
        }
    )
    daemon._runtimes["agent-x"] = runtime
    repaired_entry = {
        "name": "agent-x",
        "agent_id": "00000000-1111-2222-3333-444444444444",
        "space_id": _GOOD_SPACE_UUID,  # repaired by reconcile_corrupt_space_ids
        "base_url": "https://paxai.app",
        "token_file": "/tmp/agent-token",
        "runtime_type": "inbox",
        "desired_state": "running",
        "approval_state": "approved",
        "attestation_state": "verified",
        "identity_status": "verified",
        "environment_status": "environment_allowed",
        "space_status": "active_in_space",
    }

    daemon._reconcile_runtime(repaired_entry)

    rebindings = [c for c in captured if c["event"] == "runtime_rebinding"]
    assert rebindings == [], (
        "reconcile() emitted a phantom runtime_rebinding when the only change was a non-UUID -> UUID space_id repair"
    )
    assert runtime.entry["space_id"] == _GOOD_SPACE_UUID


def test_apply_entry_current_space_uses_global_cache_for_unknown_new_space(monkeypatch, tmp_path):
    """When an agent moves to a space that's NOT in its existing
    allowed_spaces, apply_entry_current_space must consult the global on-disk
    space cache before falling back to the (stale) entry.space_name. Without
    this, active_space_name keeps showing the previous space's name even
    after the id has updated — exactly the gbr-coordinator split-brain
    Jacob hit during the move from GBR to madtank's Workspace.
    """
    config_dir = tmp_path / "config"
    monkeypatch.setenv("AX_CONFIG_DIR", str(config_dir))
    new_space = "78950af5-4d27-441b-9296-ec46de8ba35d"
    gateway_core.save_space_cache(
        [
            {"id": _GOOD_SPACE_UUID, "name": "madtank's Workspace", "slug": "madtank"},
            {"id": new_space, "name": "Claude Code Workshop", "slug": "claude-code-workshop"},
        ]
    )
    entry = {
        "name": "agent-x",
        "space_id": _GOOD_SPACE_UUID,
        "active_space_id": _GOOD_SPACE_UUID,
        "active_space_name": "madtank's Workspace",
        "default_space_id": _GOOD_SPACE_UUID,
        "default_space_name": "madtank's Workspace",
        "space_name": "madtank's Workspace",  # legacy field, the prior space
        "allowed_spaces": [
            {"space_id": _GOOD_SPACE_UUID, "name": "madtank's Workspace", "is_default": True},
        ],
    }

    gateway_core.apply_entry_current_space(entry, new_space)

    # Name must come from the global cache, not the stale entry.space_name.
    assert entry["active_space_id"] == new_space
    assert entry["active_space_name"] == "Claude Code Workshop"
    assert entry["default_space_id"] == new_space
    assert entry["default_space_name"] == "Claude Code Workshop"


def test_space_name_from_cache_rejects_uuid_stored_as_name():
    """When the per-agent allowed_spaces cache stores the space UUID as the
    name field, _space_name_from_cache should treat it as a miss so the
    caller's or-chain falls through to the global cache.

    Operational finding: after a cold restart the upstream populates
    allowed_spaces with {"space_id": "<uuid>", "name": "<same-uuid>"},
    causing active_space_name to show a raw UUID instead of the friendly name.
    """
    space_uuid = "0478b063-4100-497d-bbea-2327bea48bc4"
    allowed_spaces = [{"space_id": space_uuid, "name": space_uuid}]

    result = gateway_core._space_name_from_cache(allowed_spaces, space_uuid)

    # Today this returns the UUID itself (the bug). Once fixed, it should
    # return None so the or-chain falls through to the global disk cache.
    # This test documents the expected behavior — it will FAIL until the
    # fix lands, serving as a regression gate.

    uuid_pattern = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)
    assert result is None or not uuid_pattern.match(result), (
        f"_space_name_from_cache returned UUID-as-name '{result}' — "
        "should return None so the global cache fallback is reached"
    )


def test_annotate_runtime_health_resolves_active_space_name_from_global_cache(monkeypatch, tmp_path):
    """annotate_runtime_health must resolve active_space_name to a friendly
    name even when the per-agent allowed_spaces cache stores UUID-as-name.

    Operational finding: /api/status and `ax gateway agents show` both
    showed the raw UUID for active_space_name because annotate_runtime_health
    only consulted _space_name_from_cache (per-agent), which returned the
    UUID, and never fell through to space_name_from_cache (global disk).
    """
    config_dir = tmp_path / "config"
    monkeypatch.setenv("AX_CONFIG_DIR", str(config_dir))

    space_uuid = "0478b063-4100-497d-bbea-2327bea48bc4"
    friendly_name = "ax-gateway"

    gateway_core.save_space_cache(
        [
            {"id": space_uuid, "name": friendly_name, "slug": "ax-gateway"},
        ]
    )

    binding = {
        "identity_binding_id": "idbind_test",
        "asset_id": "asset-1",
        "gateway_id": "gw-1",
        "install_id": "install-1",
        "active_space_id": space_uuid,
        "default_space_id": space_uuid,
        "allowed_spaces_cache": [
            {"space_id": space_uuid, "name": space_uuid},
        ],
        "environment": {"base_url": "https://paxai.app"},
        "acting_identity": {"agent_id": "agent-test-1", "agent_name": "test-agent"},
    }
    registry = gateway_core.load_gateway_registry()
    registry.setdefault("identity_bindings", []).append(binding)
    registry.setdefault("agents", []).append(
        {
            "name": "test-agent",
            "agent_id": "agent-test-1",
            "identity_binding_id": "idbind_test",
            "install_id": "install-1",
            "base_url": "https://paxai.app",
            "runtime_type": "hermes",
            "template_id": "hermes",
            "desired_state": "running",
            "effective_state": "running",
            "space_id": space_uuid,
            "active_space_id": space_uuid,
            "credential_source": "gateway",
            "token_file": "/tmp/fake.token",
        }
    )
    gateway_core.save_gateway_registry(registry)

    snapshot = {
        "name": "test-agent",
        "agent_id": "agent-test-1",
        "identity_binding_id": "idbind_test",
        "install_id": "install-1",
        "base_url": "https://paxai.app",
        "runtime_type": "hermes",
        "template_id": "hermes",
        "desired_state": "running",
        "effective_state": "running",
        "space_id": space_uuid,
        "active_space_id": space_uuid,
        "credential_source": "gateway",
        "token_file": "/tmp/fake.token",
    }

    annotated = gateway_core.annotate_runtime_health(snapshot, registry=registry)

    uuid_pattern = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)
    active_name = annotated.get("active_space_name", "")
    assert not uuid_pattern.match(active_name), (
        f"active_space_name is a raw UUID '{active_name}' — should be '{friendly_name}' from the global disk cache"
    )
    assert active_name == friendly_name


def test_apply_entry_current_space_falls_through_uuid_name_to_global_cache(monkeypatch, tmp_path):
    """apply_entry_current_space must reach the global disk cache when the
    per-agent allowed_spaces has UUID-as-name for the target space.

    This is a variant of the existing test at line 7072, but with the
    critical difference: the agent's allowed_spaces entry has the UUID
    stored as the name (the real-world failure mode) rather than a clean
    friendly name (the synthetic fixture that masks the bug).
    """
    config_dir = tmp_path / "config"
    monkeypatch.setenv("AX_CONFIG_DIR", str(config_dir))

    space_uuid = _GOOD_SPACE_UUID
    friendly_name = "madtank's Workspace"

    gateway_core.save_space_cache(
        [
            {"id": space_uuid, "name": friendly_name, "slug": "madtank"},
        ]
    )

    entry = {
        "name": "agent-x",
        "space_id": space_uuid,
        "active_space_id": space_uuid,
        "active_space_name": space_uuid,
        "default_space_id": space_uuid,
        "default_space_name": space_uuid,
        "space_name": space_uuid,
        "allowed_spaces": [
            {"space_id": space_uuid, "name": space_uuid, "is_default": True},
        ],
    }

    gateway_core.apply_entry_current_space(entry, space_uuid)

    uuid_pattern = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)
    assert not uuid_pattern.match(entry["active_space_name"]), (
        f"active_space_name is still UUID '{entry['active_space_name']}' — "
        f"should be '{friendly_name}' from the global disk cache"
    )
    assert entry["active_space_name"] == friendly_name


def test_gateway_local_connect_404_uses_actionable_guidance(monkeypatch):
    """Regression for #150: a 404 from /local/connect must point at the Live Listener path."""
    from ax_cli.commands import messages as messages_cmd

    class _FakeResponse:
        status_code = 404

        @staticmethod
        def json():
            return {"error": "not found"}

        text = '{"error": "not found"}'

        def raise_for_status(self):
            raise httpx.HTTPStatusError("404", request=None, response=self)

    def _fake_post(*args, **kwargs):
        return _FakeResponse()

    monkeypatch.setattr(httpx, "post", _fake_post)

    with pytest.raises(typer.BadParameter) as excinfo:
        messages_cmd._gateway_local_connect(
            gateway_url="http://127.0.0.1:8765",
            agent_name="wishy",
            registry_ref=None,
            workdir="/repo",
            space_id=None,
        )
    msg = str(excinfo.value)
    assert "@wishy" in msg
    assert "Live Listener" in msg
    assert "ax gateway local connect wishy --workdir /repo" in msg


def test_resolve_inference_client_returns_none_when_unset():
    """sentinel_inference_sdk requires client — there is no default.
    Returns None so the caller can record a setup error (ADR-012 decision 5)."""
    entry = {"name": "ada", "runtime_type": "sentinel_inference_sdk"}
    assert gateway_core._resolve_inference_client(entry) is None


def test_resolve_inference_client_not_applicable_to_sentinel_hermes_sdk_entries():
    """_resolve_inference_client is only for sentinel_inference_sdk agents.
    For sentinel_hermes_sdk the runtime is hardcoded to hermes_sdk in the
    dispatch layer — this function is never called for those entries."""
    entry = {"name": "ada", "runtime_type": "sentinel_hermes_sdk"}
    assert gateway_core._resolve_inference_client(entry) is None


def test_resolve_inference_client_reads_client_field():
    """Canonical operator knob: `client` (ADR-014)."""
    entry = {"name": "ada", "client": "gemini_sdk"}
    assert gateway_core._resolve_inference_client(entry) == "gemini_sdk"


@pytest.mark.parametrize("client_name", sorted(gateway_core._INFERENCE_SDK_CLIENTS))
def test_resolve_inference_client_accepts_every_allowlisted_client(client_name):
    """Every name in `_INFERENCE_SDK_CLIENTS` must round-trip through the
    resolver. Parameterized so adding a new runtime to the allowlist
    automatically gets a regression test, closing the drift class Eugene
    named in PR #307 (the hand-maintained `test_resolve_inference_client_
    accepts_*` set covered two of seven runtimes). Matches the helper-
    generated approach PR #332 used for the `--client` help text, same
    single-source-of-truth pattern.
    """
    entry = {"name": "ada", "client": client_name}
    assert gateway_core._resolve_inference_client(entry) == client_name


def test_inference_sdk_client_names_returns_sorted_allowlist():
    """The public helper exposes the allowlist as a sorted list so help
    text and runtime-type descriptions can be generated from a single
    source of truth, closing the recurring `--client` help drift
    documented in #326.
    """
    names = gateway_core.inference_sdk_client_names()
    assert names == sorted(gateway_core._INFERENCE_SDK_CLIENTS)
    assert names == sorted(names), "helper output must already be sorted"
    # Every name accepted by the resolver must be advertised by the helper.
    for client_name in gateway_core._INFERENCE_SDK_CLIENTS:
        assert client_name in names


def test_client_help_text_advertises_every_allowlisted_runtime():
    """Regression guard for #326. The `--client` help text on `ax gateway
    agents add` / `update` is generated from `inference_sdk_client_names()`
    so a new runtime added to `_INFERENCE_SDK_CLIENTS` must propagate to
    the operator-facing help text automatically.
    """
    from ax_cli.commands.gateway_agents import _CLIENT_HELP

    for client_name in gateway_core._INFERENCE_SDK_CLIENTS:
        assert client_name in _CLIENT_HELP, (
            f"--client help text drift, {client_name!r} is in the allowlist "
            f"but not in the help string. The help string is now generated "
            f"from inference_sdk_client_names() so this assertion failing "
            f"means a regression that hardcoded the help list back."
        )


def test_sentinel_inference_sdk_description_advertises_every_allowlisted_runtime():
    """Regression guard for #326. The `sentinel_inference_sdk` runtime-type
    description in the catalog is generated from `inference_sdk_client_names()`
    so it cannot drift from the accepted set.
    """
    from ax_cli.gateway_runtime_types import runtime_type_catalog

    description = runtime_type_catalog()["sentinel_inference_sdk"]["description"]
    for client_name in gateway_core._INFERENCE_SDK_CLIENTS:
        assert client_name in description, (
            f"sentinel_inference_sdk description drift, {client_name!r} is in "
            f"the allowlist but not in the catalog description."
        )


def test_resolve_inference_client_returns_none_for_unknown_values():
    """A typo or unrecognised client name returns None — no silent fallback.
    The caller records a setup error so the operator sees the misconfiguration."""
    entry = {"name": "ada", "runtime_type": "sentinel_inference_sdk", "client": "made_up_runtime"}
    assert gateway_core._resolve_inference_client(entry) is None


def test_resolve_inference_client_is_case_insensitive():
    """Operators may write `Gemini_SDK` or `GEMINI_SDK`; both resolve to
    the canonical lowercase id."""
    entry = {"name": "ada", "client": "GEMINI_SDK"}
    assert gateway_core._resolve_inference_client(entry) == "gemini_sdk"


def test_build_sentinel_inference_sdk_cmd_threads_configured_runtime_into_argv():
    """End-to-end: a registry entry with `client` set must cause the launcher
    to emit `--runtime gemini_sdk`. Regression for the bug where the launcher
    hardcoded the runtime name and made every provider runtime unreachable."""
    entry = {
        "name": "gemini-test",
        "space_id": "space-x",
        "active_space_name": "RobertRSW's Workspace",
        "base_url": "https://paxai.app",
        "workdir": "/tmp/gemini-test",
        "runtime_type": "sentinel_inference_sdk",
        "client": "gemini_sdk",
    }
    sdk_runtime = gateway_core._resolve_inference_client(entry)
    cmd = gateway_core._build_sentinel_inference_sdk_cmd(entry, sdk_runtime=sdk_runtime)
    assert "--runtime" in cmd
    runtime_idx = cmd.index("--runtime") + 1
    assert cmd[runtime_idx] == "gemini_sdk"


def test_build_sentinel_inference_sdk_cmd_passes_sdk_runtime_into_argv():
    """sdk_runtime is always passed by the dispatch layer — the command
    builder does not resolve it. Verify it appears verbatim in --runtime."""
    entry = {
        "name": "ada",
        "space_id": "space-y",
        "active_space_name": "WS",
        "base_url": "https://paxai.app",
        "workdir": "/tmp/ada",
        "runtime_type": "sentinel_inference_sdk",
    }
    cmd = gateway_core._build_sentinel_inference_sdk_cmd(entry, sdk_runtime="openai_sdk")
    assert "--runtime" in cmd
    runtime_idx = cmd.index("--runtime") + 1
    assert cmd[runtime_idx] == "openai_sdk"


def test_build_sentinel_inference_sdk_cmd_sentinel_hermes_sdk_uses_hermes_sdk():
    """The dispatch layer resolves sentinel_hermes_sdk → hermes_sdk and passes
    it into the shared command builder. Verify the builder honours it."""
    entry = {
        "name": "ada",
        "space_id": "space-y",
        "active_space_name": "WS",
        "base_url": "https://paxai.app",
        "workdir": "/tmp/ada",
        "runtime_type": "sentinel_hermes_sdk",
    }
    cmd = gateway_core._build_sentinel_inference_sdk_cmd(entry, sdk_runtime="hermes_sdk")
    assert "--runtime" in cmd
    runtime_idx = cmd.index("--runtime") + 1
    assert cmd[runtime_idx] == "hermes_sdk"


def test_subscribe_and_deliver():
    bus = OfflineAgentQueues()
    q = bus.subscribe("alpha")
    assert bus.is_subscribed("alpha")
    assert bus.deliver("alpha", {"id": "1", "content": "hi"})
    msg = q.get_nowait()
    assert msg["id"] == "1"


def test_deliver_returns_false_when_not_subscribed():
    bus = OfflineAgentQueues()
    assert not bus.deliver("nobody", {"id": "x"})


def test_unsubscribe_removes_queue():
    bus = OfflineAgentQueues()
    bus.subscribe("beta")
    bus.unsubscribe("beta")
    assert not bus.is_subscribed("beta")


def test_subscribe_replaces_previous_queue():
    bus = OfflineAgentQueues()
    q1 = bus.subscribe("gamma")
    q2 = bus.subscribe("gamma")
    assert q1 is not q2
    bus.deliver("gamma", {"id": "2"})
    assert q2.qsize() == 1
    assert q1.qsize() == 0


def test_deliver_is_case_insensitive():
    bus = OfflineAgentQueues()
    q = bus.subscribe("MyAgent")
    assert bus.deliver("myagent", {"id": "3"})
    assert q.qsize() == 1


def test_make_and_decode_token():
    assert make_token("claude-test") == "offline-claude-test"
    assert agent_name_from_token("offline-claude-test") == "claude-test"


def test_agent_name_from_token_rejects_non_offline():
    assert agent_name_from_token("axp_a_something") is None
    assert agent_name_from_token("") is None


def test_extract_mentions():
    assert extract_mentions("@echo-bot say hello") == ["echo-bot"]
    assert extract_mentions("no mentions here") == []
    assert extract_mentions("@alice and @bob") == ["alice", "bob"]


def test_offline_client_default_base_url(monkeypatch):
    monkeypatch.delenv("AX_LOCAL_GATEWAY_URL", raising=False)
    client = OfflineAxClient()
    assert client.base_url == "http://localhost:8765"


def test_offline_client_respects_gateway_url_env(monkeypatch):
    monkeypatch.setenv("AX_LOCAL_GATEWAY_URL", "http://localhost:9999")
    client = OfflineAxClient()
    assert client.base_url == "http://localhost:9999"


def test_offline_client_explicit_base_url_wins(monkeypatch):
    monkeypatch.setenv("AX_LOCAL_GATEWAY_URL", "http://localhost:9999")
    client = OfflineAxClient(base_url="http://custom:1234")
    assert client.base_url == "http://custom:1234"


def test_get_client_returns_offline_client_when_flag_set(monkeypatch, tmp_path):
    monkeypatch.setenv("AX_OFFLINE", "1")
    monkeypatch.setenv("AX_CONFIG_DIR", str(tmp_path))
    from ax_cli.config import get_client

    client = get_client()
    assert isinstance(client, OfflineAxClient)
    assert client.base_url == "http://localhost:8765"


def test_get_client_not_offline_without_flag(monkeypatch, tmp_path):

    monkeypatch.delenv("AX_OFFLINE", raising=False)
    monkeypatch.setenv("AX_CONFIG_DIR", str(tmp_path))
    from ax_cli.config import get_client

    # Without a token, should raise typer.Exit(1) — not return OfflineAxClient
    with pytest.raises(typer.Exit):
        get_client()


def test_hermes_plugin_env_uses_gateway_url_offline(monkeypatch, tmp_path):
    monkeypatch.setenv("AX_OFFLINE", "1")
    monkeypatch.setenv("AX_CONFIG_DIR", str(tmp_path))
    token_file = tmp_path / "token"
    token_file.write_text("axp_a_test.token")
    entry = {
        "name": "my-hermes",
        "agent_id": "agent-1",
        "space_id": "space-1",
        "base_url": "https://paxai.app",
    }
    with patch("ax_cli.gateway_hermes.load_gateway_managed_agent_token", return_value="axp_a_test.token"):
        with patch("ax_cli.gateway_hermes._hermes_plugin_home", return_value=tmp_path / "home"):
            env = gateway_core._build_hermes_plugin_env(entry)
    assert env["AX_BASE_URL"] == "http://localhost:8765"
    assert env["AX_OFFLINE"] == "1"


def test_hermes_plugin_env_uses_paxai_when_not_offline(monkeypatch, tmp_path):
    monkeypatch.delenv("AX_OFFLINE", raising=False)
    entry = {"name": "my-hermes", "agent_id": "agent-1", "space_id": "space-1", "base_url": "https://paxai.app"}
    with patch("ax_cli.gateway_hermes.load_gateway_managed_agent_token", return_value="axp_a_tok"):
        with patch("ax_cli.gateway_hermes._hermes_plugin_home", return_value=tmp_path / "home"):
            env = gateway_core._build_hermes_plugin_env(entry)
    assert env["AX_BASE_URL"] == "https://paxai.app"
    assert "AX_OFFLINE" not in env


def test_channel_setup_writes_ax_offline_to_env_file(monkeypatch, tmp_path):
    _seed_offline_gateway(tmp_path, monkeypatch)
    workdir = tmp_path / "workspace"
    workdir.mkdir()
    env_path = tmp_path / "claude-channel.env"
    from ax_cli.commands.channel import write_channel_setup

    write_channel_setup(
        agent_name="my-agent",
        workdir=workdir,
        env_path=env_path,
    )
    env_text = env_path.read_text()
    assert 'AX_OFFLINE="1"' in env_text
    assert 'AX_BASE_URL="http://localhost:8765"' in env_text


def test_channel_setup_no_ax_offline_without_flag(monkeypatch, tmp_path):
    monkeypatch.delenv("AX_OFFLINE", raising=False)
    monkeypatch.setenv("AX_CONFIG_DIR", str(tmp_path / "ax_config"))
    gateway_core.save_gateway_session(
        {
            "token": "axp_u_real.token",
            "base_url": "https://paxai.app",
            "space_id": "space-1",
        }
    )
    agent_dir = tmp_path / "ax_config" / "gateway" / "agents" / "my-agent"
    agent_dir.mkdir(parents=True)
    (agent_dir / "token").write_text("axp_a_real.token")
    gateway_core.save_gateway_registry(
        {
            "agents": [
                {
                    "name": "my-agent",
                    "agent_id": "aid",
                    "space_id": "space-1",
                    "base_url": "https://paxai.app",
                    "token_file": str(agent_dir / "token"),
                }
            ]
        }
    )
    workdir = tmp_path / "workspace"
    workdir.mkdir()
    env_path = tmp_path / "claude-channel.env"
    from ax_cli.commands.channel import write_channel_setup

    write_channel_setup(agent_name="my-agent", workdir=workdir, env_path=env_path)
    env_text = env_path.read_text()
    assert "AX_OFFLINE" not in env_text
