"""Per-module gateway command tests: gateway_diagnostics (gateway split #28 Phase 1).

Ported from skipped test_gateway_commands*.py; monkeypatches target ax_cli.commands.gateway_diagnostics."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from typer.testing import CliRunner

from ax_cli import gateway as gateway_core
from ax_cli.commands import gateway_agents as _gw_agents
from ax_cli.commands import gateway_diagnostics as _gw_diagnostics
from ax_cli.main import app
from tests.gateway_cmd_testlib import (
    _GOOD_SPACE_UUID,
    _isolate_gateway_paths,
    _seed_running_daemon_status,
    _stale_hermes_entry,
    _strip,
)

runner = CliRunner()


def test_gateway_approvals_approve_updates_binding(monkeypatch, tmp_path):
    config_dir = tmp_path / "config"
    monkeypatch.setenv("AX_CONFIG_DIR", str(config_dir))
    registry = gateway_core.load_gateway_registry()
    entry = {
        "name": "approve-bot",
        "agent_id": "agent-approve-1",
        "runtime_type": "exec",
        "exec_command": "python3 worker.py",
        "workdir": str(tmp_path / "repo-a"),
        "created_via": "cli",
    }
    gateway_core.ensure_local_asset_binding(registry, entry, created_via="cli", auto_approve=True)
    drifted = dict(entry)
    drifted["workdir"] = str(tmp_path / "repo-b")
    attestation = gateway_core.evaluate_runtime_attestation(registry, drifted)
    gateway_core.save_gateway_registry(registry)

    result = runner.invoke(
        app, ["gateway", "approvals", "approve", attestation["approval_id"], "--scope", "gateway", "--json"]
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["approval"]["status"] == "approved"
    assert payload["approval"]["decision_scope"] == "gateway"
    stored = gateway_core.load_gateway_registry()
    binding = gateway_core.find_binding(stored, install_id=entry["install_id"])
    assert binding is not None
    assert binding["path"] == str(Path(drifted["workdir"]).expanduser())
    assert binding["approval_scope"] == "gateway"


def test_gateway_approvals_deny_marks_request_rejected(monkeypatch, tmp_path):
    config_dir = tmp_path / "config"
    monkeypatch.setenv("AX_CONFIG_DIR", str(config_dir))
    registry = gateway_core.load_gateway_registry()
    entry = {
        "name": "deny-bot",
        "agent_id": "agent-deny-1",
        "runtime_type": "exec",
        "exec_command": "python3 worker.py",
        "workdir": str(tmp_path / "repo-a"),
        "created_via": "cli",
    }
    gateway_core.ensure_local_asset_binding(registry, entry, created_via="cli", auto_approve=True)
    drifted = dict(entry)
    drifted["workdir"] = str(tmp_path / "repo-b")
    attestation = gateway_core.evaluate_runtime_attestation(registry, drifted)
    gateway_core.save_gateway_registry(registry)

    result = runner.invoke(app, ["gateway", "approvals", "deny", attestation["approval_id"], "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["approval"]["status"] == "rejected"
    stored = gateway_core.load_gateway_registry()
    approval = next(item for item in stored["approvals"] if item["approval_id"] == attestation["approval_id"])
    assert approval["status"] == "rejected"


def test_gateway_approvals_cleanup_archives_stale_pending(monkeypatch, tmp_path):
    config_dir = tmp_path / "config"
    monkeypatch.setenv("AX_CONFIG_DIR", str(config_dir))
    registry = gateway_core.load_gateway_registry()
    entry = {
        "name": "stable-bot",
        "agent_id": "agent-stable-1",
        "runtime_type": "exec",
        "exec_command": "python3 worker.py",
        "workdir": str(tmp_path / "repo"),
        "created_via": "cli",
    }
    gateway_core.ensure_local_asset_binding(registry, entry, created_via="cli", auto_approve=True)
    registry["agents"] = [entry]
    candidate = gateway_core._binding_candidate_for_entry(entry, registry)
    asset_id = gateway_core._asset_id_for_entry(entry)
    registry["approvals"] = [
        {
            "approval_id": "approval-superseded",
            "asset_id": asset_id,
            "install_id": entry["install_id"],
            "candidate_signature": candidate["candidate_signature"],
            "candidate_binding": candidate,
            "approval_kind": "new_binding",
            "status": "pending",
            "requested_at": "2026-04-27T12:00:00+00:00",
        },
        {
            "approval_id": "approval-orphaned",
            "asset_id": "missing-agent",
            "install_id": "missing-install",
            "candidate_signature": "sha256:missing",
            "approval_kind": "binding_drift",
            "status": "pending",
            "requested_at": "2026-04-27T12:01:00+00:00",
        },
    ]
    gateway_core.save_gateway_registry(registry)

    result = runner.invoke(app, ["gateway", "approvals", "cleanup", "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["archived_count"] == 2
    assert payload["remaining_pending"] == 0
    stored = gateway_core.load_gateway_registry()
    assert {item["status"] for item in stored["approvals"]} == {"archived"}
    visible = gateway_core.list_gateway_approvals(status="pending")
    assert visible == []
    archived = gateway_core.list_gateway_approvals(status="archived")
    assert len(archived) == 2


def test_gateway_status_payload_surfaces_alerts(monkeypatch, tmp_path):
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
    registry = gateway_core.load_gateway_registry()
    registry["agents"] = [
        {
            "name": "stale-bot",
            "agent_id": "agent-1",
            "space_id": "space-1",
            "runtime_type": "exec",
            "desired_state": "running",
            "effective_state": "running",
            "last_seen_at": (
                datetime.now(timezone.utc) - timedelta(seconds=gateway_core.RUNTIME_STALE_AFTER_SECONDS + 5)
            ).isoformat(),
            "backlog_depth": 2,
            "last_error": None,
            "token_file": "/tmp/stale-token",
        },
        {
            "name": "broken-bot",
            "agent_id": "agent-2",
            "space_id": "space-1",
            "runtime_type": "exec",
            "desired_state": "running",
            "effective_state": "error",
            "last_error": "bridge crashed",
            "token_file": "/tmp/broken-token",
        },
        {
            "name": "setup-bot",
            "agent_id": "agent-3",
            "space_id": "space-1",
            "runtime_type": "exec",
            "desired_state": "running",
            "effective_state": "running",
            "last_reply_preview": "(stderr: ERROR: hermes-agent repo not found at /Users/jacob/hermes-agent.)",
            "token_file": "/tmp/setup-token",
        },
    ]
    gateway_core.save_gateway_registry(registry)

    payload = _gw_diagnostics._status_payload(activity_limit=5)

    assert payload["summary"]["alert_count"] >= 2
    titles = [item["title"] for item in payload["alerts"]]
    assert any("@stale-bot looks stale" == title for title in titles)
    assert any("@broken-bot hit an error" == title for title in titles)
    assert any("@setup-bot has a runtime setup error" == title for title in titles)


def test_status_payload_filters_hidden_by_default(monkeypatch, tmp_path):
    _isolate_gateway_paths(monkeypatch, tmp_path)
    hidden = _stale_hermes_entry("hermes-hidden", age_seconds=30 * 60, liveness="offline", agent_id="agent-hidden")
    hidden["lifecycle_phase"] = "hidden"
    active = {
        "name": "hermes-live",
        "agent_id": "agent-live",
        "template_id": "hermes",
        "runtime_type": "sentinel_inference_sdk",
        "effective_state": "running",
        "liveness": "connected",
        "last_seen_age_seconds": 5.0,
        "last_seen_at": gateway_core._now_iso(),
    }
    gateway_core.save_gateway_registry({"agents": [hidden, active]})

    payload = _gw_diagnostics._status_payload(activity_limit=0)
    names = [a["name"] for a in payload["agents"]]
    assert "hermes-hidden" not in names
    assert "hermes-live" in names
    assert payload["summary"]["hidden_agents"] == 1
    assert payload["summary"]["managed_agents"] == 1


def test_status_payload_include_hidden_returns_all(monkeypatch, tmp_path):
    _isolate_gateway_paths(monkeypatch, tmp_path)
    hidden = _stale_hermes_entry("hermes-hidden", age_seconds=30 * 60, liveness="offline", agent_id="agent-hidden")
    hidden["lifecycle_phase"] = "hidden"
    active = {
        "name": "hermes-live",
        "agent_id": "agent-live",
        "template_id": "hermes",
        "runtime_type": "sentinel_inference_sdk",
        "effective_state": "running",
        "liveness": "connected",
        "last_seen_age_seconds": 5.0,
        "last_seen_at": gateway_core._now_iso(),
    }
    gateway_core.save_gateway_registry({"agents": [hidden, active]})

    payload = _gw_diagnostics._status_payload(activity_limit=0, include_hidden=True)
    names = [a["name"] for a in payload["agents"]]
    assert "hermes-hidden" in names
    assert "hermes-live" in names
    assert payload["summary"]["hidden_agents"] == 1


def test_operator_cleanup_restore_unhides_selected_agents(monkeypatch, tmp_path):
    """Symmetric to hide: _restore_hidden_managed_agents clears the hidden
    bookkeeping, restores desired_state from the captured before-hide value,
    re-emits the row in default /api/status, and records a
    managed_agent_unhidden activity event per restored row.
    """
    _isolate_gateway_paths(monkeypatch, tmp_path)
    registry = {
        "agents": [
            {
                "name": "previously-hidden",
                "agent_id": "agent-prev-hidden",
                "template_id": "claude_code_channel",
                "runtime_type": "claude_code_channel",
                "lifecycle_phase": "hidden",
                "desired_state": "stopped",
                "desired_state_before_hide": "running",
                "hidden_at": gateway_core._now_iso(),
                "hidden_reason": "operator_cleanup",
            },
            {
                "name": "active-keeper",
                "agent_id": "agent-keeper",
                "template_id": "echo",
                "runtime_type": "echo",
                "desired_state": "running",
            },
        ]
    }
    gateway_core.save_gateway_registry(registry)

    # Restore one row plus name a non-existent and a non-hidden row to verify
    # missing/not_hidden partitions.
    payload = _gw_agents._restore_hidden_managed_agents(["previously-hidden", "ghost", "active-keeper"])

    assert payload["count"] == 1
    assert payload["missing"] == ["ghost"]
    assert payload["not_hidden"] == ["active-keeper"]

    stored = {agent["name"]: agent for agent in gateway_core.load_gateway_registry()["agents"]}
    restored = stored["previously-hidden"]
    assert restored["lifecycle_phase"] == "active"
    assert restored["desired_state"] == "running"  # restored from desired_state_before_hide
    assert "desired_state_before_hide" not in restored
    assert "hidden_at" not in restored
    assert "hidden_reason" not in restored

    # Restored row reappears in default /api/status agents list.
    visible_payload = _gw_diagnostics._status_payload(activity_limit=0)
    visible_names = sorted(agent["name"] for agent in visible_payload["agents"])
    assert "previously-hidden" in visible_names
    assert visible_payload["summary"]["hidden_agents"] == 0

    recent = gateway_core.load_recent_gateway_activity()
    assert [event["event"] for event in recent].count("managed_agent_unhidden") == 1


def test_status_payload_partitions_archived_separately(monkeypatch, tmp_path):
    _isolate_gateway_paths(monkeypatch, tmp_path)
    archived = _stale_hermes_entry("hermes-archived", age_seconds=5.0, liveness="connected", agent_id="agent-archived")
    archived["lifecycle_phase"] = "archived"
    archived["archived_at"] = gateway_core._now_iso()
    active = {
        "name": "hermes-live",
        "agent_id": "agent-live",
        "template_id": "hermes",
        "runtime_type": "sentinel_inference_sdk",
        "effective_state": "running",
        "liveness": "connected",
        "last_seen_age_seconds": 5.0,
        "last_seen_at": gateway_core._now_iso(),
    }
    gateway_core.save_gateway_registry({"agents": [archived, active]})

    payload = _gw_diagnostics._status_payload(activity_limit=0)
    names = [a["name"] for a in payload["agents"]]
    assert "hermes-archived" not in names
    assert "hermes-live" in names
    assert payload["summary"]["archived_agents"] == 1
    assert payload["summary"]["managed_agents"] == 1

    # include_hidden=True surfaces archived alongside hidden + system.
    payload_all = _gw_diagnostics._status_payload(activity_limit=0, include_hidden=True)
    all_names = [a["name"] for a in payload_all["agents"]]
    assert "hermes-archived" in all_names
    assert payload_all["summary"]["archived_agents"] == 1


def test_status_payload_active_space_sourced_from_session_only(monkeypatch, tmp_path):
    """Top-level status.space_id/space_name must come from session.json. The
    nested gateway block must not carry duplicate space_id/space_name even if
    callers pass the registry through it (the load-time strip enforces this)."""
    config_dir = tmp_path / "config"
    monkeypatch.setenv("AX_CONFIG_DIR", str(config_dir))
    gateway_core.save_gateway_session(
        {
            "token": "axp_u_test.token",
            "base_url": "https://paxai.app",
            "space_id": _GOOD_SPACE_UUID,
            "space_name": "madtank's Workspace",
            "username": "madtank",
        }
    )
    legacy = {
        "version": 1,
        "gateway": {
            "gateway_id": "gw-test",
            "space_id": "some-other-space",
            "space_name": "Wrong Workspace",
            "session_connected": True,
            "desired_state": "running",
            "effective_state": "running",
        },
        "agents": [],
        "bindings": [],
        "identity_bindings": [],
        "approvals": [],
    }
    gateway_core.registry_path().write_text(json.dumps(legacy), encoding="utf-8")

    payload = _gw_diagnostics._status_payload(activity_limit=1)

    assert payload["space_id"] == _GOOD_SPACE_UUID
    assert payload["space_name"] == "madtank's Workspace"
    assert "space_id" not in payload["gateway"]
    assert "space_name" not in payload["gateway"]


class TestStatusCommand:
    def test_status_json(self, monkeypatch):
        payload = {
            "gateway_dir": "/tmp/gw",
            "connected": True,
            "daemon": {"running": True, "pid": 123},
            "ui": {"running": True, "pid": 456, "url": "http://localhost:8765"},
            "base_url": "https://paxai.app",
            "space_id": "sp-1",
            "space_name": "Test",
            "user": "testuser",
            "agents": [],
            "recent_activity": [],
            "summary": {
                "managed_agents": 0,
                "live_agents": 0,
                "on_demand_agents": 0,
                "inbox_agents": 0,
                "hidden_agents": 0,
                "system_agents": 0,
                "alert_count": 0,
                "pending_approvals": 0,
            },
            "alerts": [],
        }
        monkeypatch.setattr(_gw_diagnostics, "_status_payload", lambda **kw: payload)
        result = runner.invoke(app, ["gateway", "status", "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["connected"] is True

    def test_status_text(self, monkeypatch):
        payload = {
            "gateway_dir": "/tmp/gw",
            "connected": False,
            "daemon": {"running": False, "pid": None},
            "ui": {"running": False, "pid": None, "url": "http://localhost:8765"},
            "base_url": "https://paxai.app",
            "space_id": None,
            "space_name": None,
            "user": None,
            "agents": [],
            "recent_activity": [],
            "summary": {
                "managed_agents": 0,
                "live_agents": 0,
                "on_demand_agents": 0,
                "inbox_agents": 0,
                "hidden_agents": 0,
                "system_agents": 0,
                "alert_count": 0,
                "pending_approvals": 0,
            },
            "alerts": [],
        }
        monkeypatch.setattr(_gw_diagnostics, "_status_payload", lambda **kw: payload)
        result = runner.invoke(app, ["gateway", "status"])
        assert result.exit_code == 0
        output = _strip(result.output)
        assert "gateway_dir" in output


class TestApprovalsListCommand:
    def test_json(self, monkeypatch):
        payload = {"approvals": [], "count": 0, "pending": 0}
        monkeypatch.setattr(_gw_diagnostics, "_approval_rows_payload", lambda **kw: payload)
        result = runner.invoke(app, ["gateway", "approvals", "list", "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["count"] == 0

    def test_text_empty(self, monkeypatch):
        payload = {"approvals": [], "count": 0, "pending": 0}
        monkeypatch.setattr(_gw_diagnostics, "_approval_rows_payload", lambda **kw: payload)
        result = runner.invoke(app, ["gateway", "approvals", "list"])
        assert result.exit_code == 0
        assert "No Gateway approvals" in _strip(result.output)


class TestApprovalsShowCommand:
    def test_json(self, monkeypatch):
        payload = {
            "approval": {
                "approval_id": "a-1",
                "asset_id": "asset-1",
                "gateway_id": "gw-1",
                "install_id": "inst-1",
                "approval_kind": "binding",
                "status": "pending",
                "risk": "low",
                "action": "bind",
                "resource": "/tmp",
                "reason": "test",
                "requested_at": "2026-01-01",
                "decided_at": None,
                "decision_scope": None,
                "candidate_binding": None,
            }
        }
        monkeypatch.setattr(_gw_diagnostics, "_approval_detail_payload", lambda aid: payload)
        result = runner.invoke(app, ["gateway", "approvals", "show", "a-1", "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["approval"]["approval_id"] == "a-1"

    def test_not_found(self, monkeypatch):
        monkeypatch.setattr(
            _gw_diagnostics,
            "_approval_detail_payload",
            lambda aid: (_ for _ in ()).throw(LookupError("not found")),
        )
        result = runner.invoke(app, ["gateway", "approvals", "show", "bogus"])
        assert result.exit_code != 0


class TestApprovalsApproveCommand:
    def test_json(self, monkeypatch):
        payload = {"approval": {"approval_id": "a-1", "asset_id": "x", "decision_scope": "asset"}}
        monkeypatch.setattr(_gw_diagnostics, "approve_gateway_approval", lambda aid, scope="asset": payload)
        result = runner.invoke(app, ["gateway", "approvals", "approve", "a-1", "--json"])
        assert result.exit_code == 0

    def test_error(self, monkeypatch):
        monkeypatch.setattr(
            _gw_diagnostics,
            "approve_gateway_approval",
            lambda aid, scope="asset": (_ for _ in ()).throw(LookupError("nope")),
        )
        result = runner.invoke(app, ["gateway", "approvals", "approve", "bogus"])
        assert result.exit_code != 0


class TestApprovalsDenyCommand:
    def test_json(self, monkeypatch):
        payload = {"approval_id": "a-1", "asset_id": "x"}
        monkeypatch.setattr(_gw_diagnostics, "deny_gateway_approval", lambda aid: payload)
        result = runner.invoke(app, ["gateway", "approvals", "deny", "a-1", "--json"])
        assert result.exit_code == 0

    def test_error(self, monkeypatch):
        monkeypatch.setattr(
            _gw_diagnostics,
            "deny_gateway_approval",
            lambda aid: (_ for _ in ()).throw(LookupError("nope")),
        )
        result = runner.invoke(app, ["gateway", "approvals", "deny", "bogus"])
        assert result.exit_code != 0


class TestApprovalsCleanupCommand:
    def test_json(self, monkeypatch):
        payload = {"archived_count": 2, "remaining_pending": 1}
        monkeypatch.setattr(_gw_diagnostics, "archive_stale_gateway_approvals", lambda: payload)
        result = runner.invoke(app, ["gateway", "approvals", "cleanup", "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["archived_count"] == 2

    def test_text(self, monkeypatch):
        payload = {"archived_count": 0, "remaining_pending": 0}
        monkeypatch.setattr(_gw_diagnostics, "archive_stale_gateway_approvals", lambda: payload)
        result = runner.invoke(app, ["gateway", "approvals", "cleanup"])
        assert result.exit_code == 0
        assert "Archived" in _strip(result.output)


class TestChannelSseDoctorCheck:
    """_run_gateway_doctor emits channel_sse check for claude_code_channel agents."""

    def _make_channel_entry(self, tmp_path, *, sse_connected=None, connected=True):
        token_file = tmp_path / "token"
        token_file.write_text("axp_a_agent.secret")
        entry = {
            "name": "claude-channel",
            "agent_id": "agent-cc",
            "space_id": "space-1",
            "base_url": "https://paxai.app",
            "runtime_type": "claude_code_channel",
            "template_id": "claude_code_channel",
            "desired_state": "running",
            "effective_state": "running",
            "last_seen_at": gateway_core._now_iso(),
            "token_file": str(token_file),
            "transport": "gateway",
            "credential_source": "gateway",
        }
        if sse_connected is not None:
            entry["sse_connected"] = sse_connected
        if connected:
            entry["connected"] = True
        return entry

    def test_channel_sse_passed_when_connected(self, tmp_path):
        gateway_core.save_gateway_session(
            {"token": "axp_u_test.token", "base_url": "https://paxai.app", "space_id": "space-1", "username": "u"}
        )
        registry = gateway_core.load_gateway_registry()
        registry["agents"] = [self._make_channel_entry(tmp_path, sse_connected=True, connected=True)]
        gateway_core.save_gateway_registry(registry)

        result = _gw_diagnostics._run_gateway_doctor("claude-channel")
        check_names = {c["name"]: c for c in result["checks"]}
        assert "channel_sse" in check_names
        assert check_names["channel_sse"]["status"] == "passed"

    def test_channel_sse_failed_when_disconnected(self, tmp_path):
        gateway_core.save_gateway_session(
            {"token": "axp_u_test.token", "base_url": "https://paxai.app", "space_id": "space-1", "username": "u"}
        )
        registry = gateway_core.load_gateway_registry()
        registry["agents"] = [self._make_channel_entry(tmp_path, sse_connected=False, connected=True)]
        gateway_core.save_gateway_registry(registry)

        result = _gw_diagnostics._run_gateway_doctor("claude-channel")
        check_names = {c["name"]: c for c in result["checks"]}
        assert "channel_sse" in check_names
        assert check_names["channel_sse"]["status"] == "failed"
        assert "SSE subscription is down" in check_names["channel_sse"]["detail"]

    def test_claude_code_session_passed_even_when_sse_disconnected(self, tmp_path):
        gateway_core.save_gateway_session(
            {"token": "axp_u_test.token", "base_url": "https://paxai.app", "space_id": "space-1", "username": "u"}
        )
        registry = gateway_core.load_gateway_registry()
        registry["agents"] = [self._make_channel_entry(tmp_path, sse_connected=False, connected=True)]
        gateway_core.save_gateway_registry(registry)

        result = _gw_diagnostics._run_gateway_doctor("claude-channel")
        check_names = {c["name"]: c for c in result["checks"]}
        assert check_names.get("claude_code_session", {}).get("status") == "passed"


class TestRuntimeLaunchDoctorCheck:
    """runtime_launch must not fail Gateway-supervised runtimes that carry no exec_command (issue #359)."""

    def _make_supervised_entry(self, tmp_path, *, runtime_type, template_id):
        token_file = tmp_path / "token"
        token_file.write_text("axp_a_agent.secret")
        return {
            "name": "supervised-agent",
            "agent_id": "agent-sup",
            "space_id": "space-1",
            "base_url": "https://paxai.app",
            "runtime_type": runtime_type,
            "template_id": template_id,
            "desired_state": "running",
            "effective_state": "running",
            "last_seen_at": gateway_core._now_iso(),
            "token_file": str(token_file),
            "transport": "gateway",
            "credential_source": "gateway",
            "connected": True,
            # No exec_command on purpose: Gateway supervises the launch implicitly.
        }

    def _seed(self, tmp_path, entry):
        gateway_core.save_gateway_session(
            {"token": "axp_u_test.token", "base_url": "https://paxai.app", "space_id": "space-1", "username": "u"}
        )
        registry = gateway_core.load_gateway_registry()
        registry["agents"] = [entry]
        gateway_core.save_gateway_registry(registry)

    def test_hermes_plugin_runtime_launch_passes_without_exec_command(self, tmp_path):
        entry = self._make_supervised_entry(tmp_path, runtime_type="hermes_plugin", template_id="hermes")
        self._seed(tmp_path, entry)

        result = _gw_diagnostics._run_gateway_doctor("supervised-agent")
        check_names = {c["name"]: c for c in result["checks"]}
        assert check_names.get("runtime_launch", {}).get("status") == "passed"
        assert "hermes gateway run" in check_names["runtime_launch"]["detail"]
        # The bug surfaced as a doctor-level failure summarizing the missing launch command.
        assert "does not have a launch command" not in (result.get("summary") or "")

    def test_sentinel_inference_sdk_runtime_launch_passes_without_exec_command(self, tmp_path):
        entry = self._make_supervised_entry(
            tmp_path, runtime_type="sentinel_inference_sdk", template_id="sentinel_cli"
        )
        self._seed(tmp_path, entry)

        result = _gw_diagnostics._run_gateway_doctor("supervised-agent")
        check_names = {c["name"]: c for c in result["checks"]}
        assert check_names.get("runtime_launch", {}).get("status") == "passed"

    def test_non_supervised_runtime_without_exec_command_still_fails(self, tmp_path):
        # Guard against over-broadening: a runtime outside the supervised-subprocess
        # set (sentinel_cli is per-prompt, not a long-running supervised child)
        # missing its exec_command must still be reported as a failure — its
        # behavior is unchanged by this fix.
        entry = self._make_supervised_entry(tmp_path, runtime_type="sentinel_cli", template_id="sentinel_cli")
        self._seed(tmp_path, entry)

        result = _gw_diagnostics._run_gateway_doctor("supervised-agent")
        check_names = {c["name"]: c for c in result["checks"]}
        assert check_names.get("runtime_launch", {}).get("status") == "failed"


class TestStatusTextRendering:
    def test_status_with_agents_and_alerts(self, monkeypatch):
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
            "recent_activity": [
                {"ts": "2026-01-01", "event": "test", "agent_name": "bot1", "message_id": "m1", "reply_preview": "ok"},
            ],
            "summary": {
                "managed_agents": 1,
                "live_agents": 1,
                "on_demand_agents": 0,
                "inbox_agents": 0,
                "hidden_agents": 1,
                "system_agents": 1,
                "alert_count": 1,
                "pending_approvals": 0,
            },
            "alerts": [
                {"severity": "warn", "title": "test alert", "agent_name": "bot1", "detail": "check it"},
            ],
        }
        monkeypatch.setattr(_gw_diagnostics, "_status_payload", lambda **kw: payload)
        result = runner.invoke(app, ["gateway", "status"])
        assert result.exit_code == 0

    def test_status_with_all_flag(self, monkeypatch):
        payload = {
            "gateway_dir": "/tmp/gw",
            "connected": True,
            "daemon": {"running": True, "pid": 100},
            "ui": {"running": True, "pid": 200, "url": "http://localhost:8765"},
            "base_url": "https://paxai.app",
            "space_id": "sp-1",
            "space_name": "Test",
            "user": "tester",
            "agents": [],
            "recent_activity": [],
            "summary": {
                "managed_agents": 0,
                "live_agents": 0,
                "on_demand_agents": 0,
                "inbox_agents": 0,
                "hidden_agents": 2,
                "system_agents": 1,
                "alert_count": 0,
                "pending_approvals": 0,
            },
            "alerts": [],
        }
        monkeypatch.setattr(_gw_diagnostics, "_status_payload", lambda **kw: payload)
        result = runner.invoke(app, ["gateway", "status", "--all"])
        assert result.exit_code == 0


def test_status_shows_offline_indicator_when_lock_present(monkeypatch, tmp_path):
    """When the daemon was started in offline mode (marker file present),
    the status output renders a prominent OFFLINE line at the top of the
    human-mode output."""
    monkeypatch.setenv("AX_CONFIG_DIR", str(tmp_path / "ax_config"))
    _seed_running_daemon_status(monkeypatch, offline=True)

    result = runner.invoke(app, ["gateway", "status"])

    assert result.exit_code == 0, result.output
    assert "MODE" in result.output
    assert "OFFLINE" in result.output
    assert "AX_OFFLINE=1" in result.output
    assert "no platform calls" in result.output


def test_status_no_offline_indicator_when_lock_absent(monkeypatch, tmp_path):
    """When the daemon was not started in offline mode, the status output
    does not show the OFFLINE indicator."""
    monkeypatch.setenv("AX_CONFIG_DIR", str(tmp_path / "ax_config"))
    _seed_running_daemon_status(monkeypatch, offline=False)

    result = runner.invoke(app, ["gateway", "status"])

    assert result.exit_code == 0, result.output
    assert "OFFLINE" not in result.output
    assert "MODE" not in result.output


def test_status_json_includes_offline_mode_field(monkeypatch, tmp_path):
    """The `--json` payload exposes `offline_mode` for scripted consumers
    (operator tooling, CI checks, dashboards)."""
    import json

    monkeypatch.setenv("AX_CONFIG_DIR", str(tmp_path / "ax_config"))
    _seed_running_daemon_status(monkeypatch, offline=True)

    result = runner.invoke(app, ["gateway", "status", "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["offline_mode"] is True
