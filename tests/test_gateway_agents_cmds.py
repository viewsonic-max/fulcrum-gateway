"""Per-module gateway command tests: gateway_agents (gateway split #28 Phase 1).

Ported from skipped test_gateway_commands*.py; monkeypatches target ax_cli.commands.gateway_agents."""

from __future__ import annotations

import json
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import httpx
import pytest
from typer.testing import CliRunner

from ax_cli import gateway as gateway_core
from ax_cli import gateway_runtime_types
from ax_cli.commands import gateway_agents as _gw_agents
from ax_cli.commands import gateway_messaging as _gw_messaging
from ax_cli.commands import gateway_session as _gw_session
from ax_cli.commands import gateway_spaces as _gw_spaces
from ax_cli.main import app
from tests.gateway_cmd_testlib import (
    _fake_create_agent_in_space,
    _FakeManagedSendClient,
    _FakeUserClient,
    _isolate_gateway_paths,
    _make_429_error,
    _make_registry,
    _RecordingHeartbeatClient,
    _seed_revertable_mover,
    _SharedRuntimeClient,
    _strip,
    _strip_ansi,
)

runner = CliRunner()


def test_local_session_send_hydrates_space_from_database(monkeypatch, tmp_path):
    config_dir = tmp_path / "config"
    monkeypatch.setenv("AX_CONFIG_DIR", str(config_dir))
    gateway_core.save_gateway_session(
        {
            "token": "axp_u_test.token",
            "base_url": "https://paxai.app",
            "space_id": "space-session",
            "username": "madtank",
        }
    )
    entry = {
        "name": "codex-pass-through",
        "agent_id": "agent-codex",
        "space_id": "space-stale",
        "base_url": "https://paxai.app",
        "token_file": str(tmp_path / "codex-token"),
        "approval_state": "approved",
        "attestation_state": "verified",
    }
    registry = {
        "agents": [entry],
    }
    session_payload = gateway_core.issue_local_session(registry, entry)
    registry = {
        "agents": [entry],
        "local_sessions": registry["local_sessions"],
    }
    (tmp_path / "codex-token").write_text("axp_a_test\n")
    gateway_core.save_gateway_registry(registry)

    class FakeUserClient:
        def __init__(self, *args, **kwargs):
            pass

        def list_agents(self):
            return {
                "agents": [
                    {
                        "id": "agent-codex",
                        "name": "codex-pass-through",
                        "space_id": "space-from-db",
                        "space_name": "DB Space",
                    }
                ]
            }

    sent = {}

    class FakeManagedClient:
        def __init__(self, *args, **kwargs):
            pass

        def send_message(
            self,
            space_id,
            content,
            *,
            agent_id=None,
            channel="main",
            parent_id=None,
            metadata=None,
            message_type="text",
            attachments=None,
        ):
            sent.update(
                {
                    "space_id": space_id,
                    "content": content,
                    "agent_id": agent_id,
                    "channel": channel,
                    "parent_id": parent_id,
                    "metadata": metadata,
                    "message_type": message_type,
                    "attachments": attachments,
                }
            )
            return {"message": {"id": "msg-1", "space_id": space_id}}

    # DB hydration happens in gateway_spaces._hydrate_entry_space_from_database, which builds the
    # lookup client via gateway_spaces._load_gateway_user_client; the managed send client is built
    # in gateway_session. Patch each at its resolving module.
    monkeypatch.setattr(_gw_spaces, "_load_gateway_user_client", lambda: FakeUserClient())
    monkeypatch.setattr(_gw_session, "_load_managed_agent_client", lambda entry: FakeManagedClient())

    payload = _gw_session._send_local_session_message(
        session_token=session_payload["session_token"],
        body={"content": "hello from repo", "space_id": None},
    )

    assert payload["message"]["message"]["space_id"] == "space-from-db"
    assert sent["space_id"] == "space-from-db"
    updated = gateway_core.load_gateway_registry()["agents"][0]
    assert updated["space_id"] == "space-from-db"
    assert updated["active_space_name"] == "DB Space"

    def send_message(self, space_id, content, *, agent_id=None, parent_id=None, metadata=None):
        return {
            "message": {
                "id": "gateway-test-1",
                "space_id": space_id,
                "content": content,
                "agent_id": agent_id,
                "parent_id": parent_id,
                "metadata": metadata,
            }
        }


def test_gateway_agents_add_template_help_lists_full_catalog():
    result = runner.invoke(app, ["gateway", "agents", "add", "--help"])
    assert result.exit_code == 0, result.output
    text = _strip_ansi(result.output)
    expected_ids = [t["id"] for t in gateway_runtime_types.agent_template_list()]
    # Sanity: catalog should include templates beyond the original static five
    # so this test actually exercises drift coverage.
    assert {"langgraph", "autogen"}.issubset(set(expected_ids))
    for template_id in expected_ids:
        assert template_id in text, f"--template help missing '{template_id}': {text}"


def test_gateway_agents_add_mints_token_and_writes_registry(monkeypatch, tmp_path):
    config_dir = tmp_path / "config"
    monkeypatch.setenv("AX_CONFIG_DIR", str(config_dir))
    gateway_core.save_gateway_session(
        {
            "token": "axp_u_test.token",
            "base_url": "https://paxai.app",
            "space_id": "space-1",
            "username": "madtank",
        }
    )
    monkeypatch.setattr(_gw_agents, "_load_gateway_user_client", lambda: _FakeUserClient())
    monkeypatch.setattr(_gw_agents, "_find_agent_in_space", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        _gw_agents,
        "_create_agent_in_space",
        lambda *args, **kwargs: {"id": "agent-1", "name": "echo-bot"},
    )
    monkeypatch.setattr(_gw_agents, "_polish_metadata", lambda *args, **kwargs: None)
    monkeypatch.setattr(_gw_agents, "_mint_agent_pat", lambda *args, **kwargs: ("axp_a_agent.secret", "mgmt"))

    result = runner.invoke(app, ["gateway", "agents", "add", "echo-bot", "--type", "echo", "--timeout", "42", "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["name"] == "echo-bot"
    assert payload["runtime_type"] == "echo"
    assert payload["timeout_seconds"] == 42
    assert payload["desired_state"] == "running"
    assert payload["credential_source"] == "gateway"
    assert payload["transport"] == "gateway"
    registry = gateway_core.load_gateway_registry()
    assert registry["agents"][0]["name"] == "echo-bot"
    assert registry["agents"][0]["timeout_seconds"] == 42
    assert registry["bindings"][0]["asset_id"] == "agent-1"
    assert registry["bindings"][0]["approved_state"] == "approved"
    assert registry["agents"][0]["install_id"] == registry["bindings"][0]["install_id"]
    # token_file is stored relative to gateway_dir() for portability (#89);
    # resolve it before touching the filesystem.
    assert registry["agents"][0]["token_file"] == "agents/echo-bot/token"
    token_file = gateway_core.resolve_agent_token_file(registry["agents"][0])
    assert token_file.is_absolute()
    assert token_file.exists()
    assert token_file.read_text().strip() == "axp_a_agent.secret"
    recent = gateway_core.load_recent_gateway_activity()
    assert recent[-1]["event"] == "managed_agent_added"
    assert recent[-1]["agent_name"] == "echo-bot"


def test_gateway_agents_add_pass_through_requires_fingerprint_approval(monkeypatch, tmp_path):
    config_dir = tmp_path / "config"
    monkeypatch.setenv("AX_CONFIG_DIR", str(config_dir))
    gateway_core.save_gateway_session(
        {
            "token": "axp_u_test.token",
            "base_url": "https://paxai.app",
            "space_id": "space-1",
            "username": "madtank",
        }
    )
    monkeypatch.setattr(_gw_agents, "_load_gateway_user_client", lambda: _FakeUserClient())
    monkeypatch.setattr(_gw_agents, "_find_agent_in_space", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        _gw_agents,
        "_create_agent_in_space",
        lambda *args, **kwargs: {"id": "agent-pass-1", "name": "codex-pass"},
    )
    monkeypatch.setattr(_gw_agents, "_polish_metadata", lambda *args, **kwargs: None)
    monkeypatch.setattr(_gw_agents, "_mint_agent_pat", lambda *args, **kwargs: ("axp_a_agent.secret", "mgmt"))

    result = runner.invoke(app, ["gateway", "agents", "add", "codex-pass", "--template", "pass_through", "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["name"] == "codex-pass"
    assert payload["template_id"] == "pass_through"
    assert payload["runtime_type"] == "inbox"
    assert payload["approval_state"] == "pending"
    assert payload["approval_id"]
    assert payload["attestation_state"] == "unknown"
    registry = gateway_core.load_gateway_registry()
    assert registry["bindings"] == []
    assert registry["approvals"][0]["approval_kind"] == "new_binding"
    assert registry["approvals"][0]["candidate_binding"]["path"] == str(Path(__file__).resolve().parent.parent)


def test_gateway_agents_add_claude_code_channel_registers_gateway_identity_running_by_default(monkeypatch, tmp_path):
    config_dir = tmp_path / "config"
    monkeypatch.setenv("AX_CONFIG_DIR", str(config_dir))
    gateway_core.save_gateway_session(
        {
            "token": "axp_u_test.token",
            "base_url": "https://paxai.app",
            "space_id": "space-1",
            "username": "madtank",
        }
    )
    monkeypatch.setattr(_gw_agents, "_load_gateway_user_client", lambda: _FakeUserClient())
    monkeypatch.setattr(_gw_agents, "_find_agent_in_space", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        _gw_agents,
        "_create_agent_in_space",
        lambda *args, **kwargs: {"id": "agent-channel-1", "name": "orion"},
    )
    monkeypatch.setattr(_gw_agents, "_polish_metadata", lambda *args, **kwargs: None)
    monkeypatch.setattr(_gw_agents, "_mint_agent_pat", lambda *args, **kwargs: ("axp_a_agent.secret", "mgmt"))

    result = runner.invoke(
        app,
        [
            "gateway",
            "agents",
            "add",
            "orion",
            "--template",
            "claude_code_channel",
            "--workdir",
            str(tmp_path / "orion"),
            "--json",
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["template_id"] == "claude_code_channel"
    assert payload["runtime_type"] == "claude_code_channel"
    assert payload["desired_state"] == "running"
    assert payload["credential_source"] == "gateway"
    assert payload["token_file"]
    workspace_config = tmp_path / "orion" / ".ax" / "config.toml"
    assert workspace_config.exists()
    assert 'agent_name = "orion"' in workspace_config.read_text()
    workspace_readme = tmp_path / "orion" / ".ax" / "README.md"
    assert workspace_readme.exists()
    assert "registered with the local aX Gateway" in workspace_readme.read_text()
    workspace_context = tmp_path / "orion" / ".ax" / "AGENT_CONTEXT.md"
    assert workspace_context.exists()
    assert "multi-user, multi-agent network" in workspace_context.read_text()
    assert "Do not ask the user for a PAT" in workspace_context.read_text()
    # Claude Code reads CLAUDE.md natively; the auto-generated marker
    # section lands there. AGENTS.md is the Hermes-side convention and is
    # not written for claude_code_channel agents.
    claude_md = tmp_path / "orion" / "CLAUDE.md"
    assert claude_md.exists()
    assert "BEGIN ax-gateway-agent-context" in claude_md.read_text()


def test_gateway_agents_add_autogen_scaffolds_workdir_and_copies_bridge(monkeypatch, tmp_path):
    config_dir = tmp_path / "config"
    monkeypatch.setenv("AX_CONFIG_DIR", str(config_dir))
    gateway_core.save_gateway_session(
        {
            "token": "axp_u_test.token",
            "base_url": "https://paxai.app",
            "space_id": "space-1",
            "username": "madtank",
        }
    )
    monkeypatch.setattr(_gw_agents, "_load_gateway_user_client", lambda: _FakeUserClient())
    monkeypatch.setattr(_gw_agents, "_find_agent_in_space", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        _gw_agents,
        "_create_agent_in_space",
        lambda *args, **kwargs: {"id": "agent-autogen-1", "name": "autogen-bot"},
    )
    monkeypatch.setattr(_gw_agents, "_polish_metadata", lambda *args, **kwargs: None)
    monkeypatch.setattr(_gw_agents, "_mint_agent_pat", lambda *args, **kwargs: ("axp_a_agent.secret", "mgmt"))

    agent_workdir = tmp_path / "autogen-bot"
    result = runner.invoke(
        app,
        [
            "gateway",
            "agents",
            "add",
            "autogen-bot",
            "--template",
            "autogen",
            "--workdir",
            str(agent_workdir),
            "--json",
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    # Workdir scaffolded and bridge file copied so the agent runs without a
    # manual `mkdir + cp` step (#130).
    assert agent_workdir.is_dir()
    copied_bridge = agent_workdir / "autogen_bridge.py"
    assert copied_bridge.is_file()
    # exec_command rewritten to point at the workdir-local copy, not the
    # source path under examples/.
    assert payload["exec_command"].endswith("autogen_bridge.py")
    assert str(copied_bridge.resolve()) in payload["exec_command"]
    assert "examples" not in payload["exec_command"]
    assert payload["workdir"] == str(agent_workdir.resolve())


def test_gateway_agents_add_langgraph_composio_scaffolds_workdir_and_copies_bridge(monkeypatch, tmp_path):
    # Regression guard for #149: langgraph_composio landed (PR #124) without a
    # bridge_source field, so --workdir registrations fell through
    # _scaffold_bridge_workdir and hit the manual mkdir+cp gap. With the field
    # present the composio template scaffolds like the other bridge templates.
    from ax_cli.connectors import types as connector_types

    config_dir = tmp_path / "config"
    monkeypatch.setenv("AX_CONFIG_DIR", str(config_dir))
    gateway_core.save_gateway_session(
        {
            "token": "axp_u_test.token",
            "base_url": "https://paxai.app",
            "space_id": "space-1",
            "username": "madtank",
        }
    )
    monkeypatch.setattr(_gw_agents, "_load_gateway_user_client", lambda: _FakeUserClient())
    monkeypatch.setattr(_gw_agents, "_find_agent_in_space", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        _gw_agents,
        "_create_agent_in_space",
        lambda *args, **kwargs: {"id": "agent-composio-1", "name": "composio-bot"},
    )
    monkeypatch.setattr(_gw_agents, "_polish_metadata", lambda *args, **kwargs: None)
    monkeypatch.setattr(_gw_agents, "_mint_agent_pat", lambda *args, **kwargs: ("axp_a_agent.secret", "mgmt"))
    # langgraph_composio requires --connector-ref; resolve it to an enabled row.
    monkeypatch.setattr(
        "ax_cli.connectors.find_connector",
        lambda _ref: connector_types.ConnectorRow.create("my_composio", "composio"),
    )

    agent_workdir = tmp_path / "composio-bot"
    result = runner.invoke(
        app,
        [
            "gateway",
            "agents",
            "add",
            "composio-bot",
            "--template",
            "langgraph_composio",
            "--connector-ref",
            "my_composio",
            "--workdir",
            str(agent_workdir),
            "--json",
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert agent_workdir.is_dir()
    copied_bridge = agent_workdir / "langgraph_composio_bridge.py"
    assert copied_bridge.is_file()
    assert payload["exec_command"].endswith("langgraph_composio_bridge.py")
    assert str(copied_bridge.resolve()) in payload["exec_command"]
    assert "examples" not in payload["exec_command"]
    assert payload["workdir"] == str(agent_workdir.resolve())


def test_gateway_agents_add_with_explicit_exec_skips_bridge_scaffold(monkeypatch, tmp_path):
    config_dir = tmp_path / "config"
    monkeypatch.setenv("AX_CONFIG_DIR", str(config_dir))
    gateway_core.save_gateway_session(
        {
            "token": "axp_u_test.token",
            "base_url": "https://paxai.app",
            "space_id": "space-1",
            "username": "madtank",
        }
    )
    monkeypatch.setattr(_gw_agents, "_load_gateway_user_client", lambda: _FakeUserClient())
    monkeypatch.setattr(_gw_agents, "_find_agent_in_space", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        _gw_agents,
        "_create_agent_in_space",
        lambda *args, **kwargs: {"id": "agent-custom-1", "name": "custom-bot"},
    )
    monkeypatch.setattr(_gw_agents, "_polish_metadata", lambda *args, **kwargs: None)
    monkeypatch.setattr(_gw_agents, "_mint_agent_pat", lambda *args, **kwargs: ("axp_a_agent.secret", "mgmt"))

    agent_workdir = tmp_path / "custom-bot"
    result = runner.invoke(
        app,
        [
            "gateway",
            "agents",
            "add",
            "custom-bot",
            "--template",
            "autogen",
            "--workdir",
            str(agent_workdir),
            "--exec",
            "python my_custom_bridge.py",
            "--json",
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    # Operator's --exec is the source of truth; we don't copy the template
    # bridge or rewrite their command.
    assert payload["exec_command"] == "python my_custom_bridge.py"
    assert not (agent_workdir / "autogen_bridge.py").exists()


def test_gateway_agents_remove_archives_orphaned_pending_approval(monkeypatch, tmp_path):
    config_dir = tmp_path / "config"
    monkeypatch.setenv("AX_CONFIG_DIR", str(config_dir))
    registry = gateway_core.load_gateway_registry()
    registry["agents"] = [
        {
            "name": "pending-remove-bot",
            "agent_id": "agent-pending-remove-1",
            "space_id": "space-1",
            "base_url": "https://paxai.app",
            "template_id": "pass_through",
            "runtime_type": "inbox",
            "desired_state": "running",
            "requires_approval": True,
            "install_id": "install-pending-remove-1",
            "workdir": str(tmp_path / "repo-a"),
        }
    ]
    daemon = gateway_core.GatewayDaemon(client_factory=lambda **kwargs: _SharedRuntimeClient({}))
    reconciled = daemon._reconcile_registry(registry, {"token": "axp_u_test.token", "base_url": "https://paxai.app"})
    gateway_core.save_gateway_registry(reconciled)
    approval_id = reconciled["agents"][0]["approval_id"]

    removed = _gw_agents._remove_managed_agent("pending-remove-bot")

    assert removed["name"] == "pending-remove-bot"
    stored = gateway_core.load_gateway_registry()
    assert gateway_core.find_agent_entry(stored, "pending-remove-bot") is None
    approval = next(item for item in stored["approvals"] if item["approval_id"] == approval_id)
    assert approval["status"] == "archived"
    assert approval["decision"] == "archive"


def test_gateway_move_updates_routing_for_test_messages(monkeypatch, tmp_path):
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
    mover_token = tmp_path / "mover.token"
    mover_token.write_text("axp_a_mover.secret")
    switchboard_token = tmp_path / "switchboard.token"
    switchboard_token.write_text("axp_a_switchboard.secret")
    allowed_spaces = [
        {"space_id": "space-1", "name": "Old Space", "is_default": True},
        {"space_id": "space-2", "name": "New Space", "is_default": False},
    ]
    registry["agents"] = [
        {
            "name": "mover",
            "agent_id": "agent-mover",
            "space_id": "space-1",
            "base_url": "https://paxai.app",
            "runtime_type": "echo",
            "template_id": "echo_test",
            "desired_state": "running",
            "effective_state": "running",
            "transport": "gateway",
            "credential_source": "gateway",
            "allowed_spaces": allowed_spaces,
            "token_file": str(mover_token),
        },
        {
            "name": "switchboard-space2",
            "agent_id": "agent-switchboard-space2",
            "space_id": "space-1",
            "base_url": "https://paxai.app",
            "runtime_type": "inbox",
            "template_id": "inbox",
            "desired_state": "running",
            "effective_state": "running",
            "transport": "gateway",
            "credential_source": "gateway",
            "allowed_spaces": allowed_spaces,
            "token_file": str(switchboard_token),
        },
    ]
    for entry in registry["agents"]:
        gateway_core.ensure_gateway_identity_binding(registry, entry, session=gateway_core.load_gateway_session())
    gateway_core.save_gateway_registry(registry)

    class FakePlacementClient:
        def __init__(self):
            self.calls = []
            self.space_id = "space-1"

        def set_agent_placement(self, identifier, *, space_id, pinned=False):
            self.calls.append({"identifier": identifier, "space_id": space_id, "pinned": pinned})
            self.space_id = space_id
            return {"agent_id": identifier, "space_id": space_id, "allowed_spaces": ["space-1", "space-2"]}

        def get_agent_placement(self, identifier):
            return {
                "agent_id": identifier,
                "name": "mover",
                "space_id": self.space_id,
                "allowed_spaces": ["space-1", "space-2"],
                "_record": {
                    "id": identifier,
                    "name": "mover",
                    "space_id": self.space_id,
                    "allowed_spaces": ["space-1", "space-2"],
                },
            }

        def get_agent(self, identifier):
            return {"agent": {"id": identifier, "name": "mover", "space_id": self.space_id}}

        def list_spaces(self):
            return {
                "spaces": [
                    {"id": "space-1", "name": "Old Space", "slug": "old-space"},
                    {"id": "space-2", "name": "New Space", "slug": "new-space"},
                ]
            }

    fake_user_client = FakePlacementClient()
    sent_messages = []

    class RecordingManagedClient:
        def send_message(self, space_id, content, *, agent_id=None, parent_id=None, metadata=None):
            sent_messages.append(
                {
                    "space_id": space_id,
                    "content": content,
                    "agent_id": agent_id,
                    "parent_id": parent_id,
                    "metadata": metadata,
                }
            )
            return {"message": {"id": "gateway-test-1", "space_id": space_id, "content": content}}

    monkeypatch.setattr(_gw_agents, "_load_gateway_user_client", lambda: fake_user_client)
    monkeypatch.setattr(_gw_messaging, "_load_managed_agent_client", lambda entry: RecordingManagedClient())

    moved = _gw_agents._move_managed_agent_space("mover", "new-space")

    assert fake_user_client.calls == [{"identifier": "agent-mover", "space_id": "space-2", "pinned": False}]
    assert moved["space_id"] == "space-2"
    assert moved["active_space_name"] == "New Space"
    stored = gateway_core.find_agent_entry(gateway_core.load_gateway_registry(), "mover")
    assert stored["space_id"] == "space-2"
    assert stored["active_space_name"] == "New Space"

    # Migration (Madtank/supervisor 2026-05-02): default sender is now the
    # invoking principal, never the auto-created switchboard. This test runs
    # outside a Gateway-managed workspace, so name the sender explicitly to
    # exercise the routing-after-move semantics this test is about.
    tested = _gw_messaging._send_gateway_test_to_managed_agent("mover", sender_agent="switchboard-space2")

    assert tested["target_agent"] == "mover"
    assert tested["message"]["space_id"] == "space-2"
    assert sent_messages[-1]["space_id"] == "space-2"
    assert sent_messages[-1]["content"].startswith("@mover ")


def test_gateway_move_records_previous_space_for_revert(monkeypatch, tmp_path):
    """A successful move persists previous_space_id so --revert can find its way back."""
    fake = _seed_revertable_mover(tmp_path, monkeypatch)

    moved = _gw_agents._move_managed_agent_space("mover", "new-space")

    assert moved["space_id"] == "space-2"
    assert moved["previous_space_id"] == "space-1"
    assert moved["previous_space_name"] == "Old Space"
    stored = gateway_core.find_agent_entry(gateway_core.load_gateway_registry(), "mover")
    assert stored["previous_space_id"] == "space-1"
    assert stored["previous_space_name"] == "Old Space"
    # current_status was set to "moving" mid-move and cleared once the rebind
    # window resolved (no daemon running in the test, so the wait short-circuits).
    assert stored.get("current_status") in (None, "")
    assert stored.get("current_activity") in (None, "")
    assert fake.calls[-1]["space_id"] == "space-2"


def test_gateway_move_revert_returns_to_previous_space(monkeypatch, tmp_path):
    """--revert uses the persisted previous_space_id without requiring --space."""
    _seed_revertable_mover(tmp_path, monkeypatch)

    _gw_agents._move_managed_agent_space("mover", "new-space")
    reverted = _gw_agents._move_managed_agent_space("mover", None, revert=True)

    assert reverted["space_id"] == "space-1"
    assert reverted["active_space_name"] == "Old Space"
    # After reverting, the previous-space pointer now points at the space we
    # just left ("space-2") so a second --revert would go back there again.
    assert reverted["previous_space_id"] == "space-2"
    stored = gateway_core.find_agent_entry(gateway_core.load_gateway_registry(), "mover")
    assert stored["space_id"] == "space-1"
    assert stored["previous_space_id"] == "space-2"


def test_gateway_move_revert_without_history_errors_clearly(monkeypatch, tmp_path):
    """Reverting an agent that's never been moved fails with an actionable message."""
    _seed_revertable_mover(tmp_path, monkeypatch)

    with pytest.raises(ValueError, match="no recorded previous space"):
        _gw_agents._move_managed_agent_space("mover", None, revert=True)


def test_gateway_move_revert_and_explicit_space_are_mutually_exclusive(monkeypatch, tmp_path):
    """Passing both --space and --revert is rejected before any backend call."""
    _seed_revertable_mover(tmp_path, monkeypatch)

    with pytest.raises(ValueError, match="not both"):
        _gw_agents._move_managed_agent_space("mover", "new-space", revert=True)


def test_gateway_move_cli_requires_one_of_space_or_revert(monkeypatch, tmp_path):
    """The CLI command rejects an invocation with neither --space nor --revert."""
    _seed_revertable_mover(tmp_path, monkeypatch)

    result = runner.invoke(app, ["gateway", "agents", "move", "mover"])

    assert result.exit_code == 1
    assert "Provide --space or --revert" in result.output


def test_gateway_move_no_op_does_not_overwrite_previous_space(monkeypatch, tmp_path):
    """A move-to-same-space short-circuits and must not blank the revert pointer."""
    _seed_revertable_mover(tmp_path, monkeypatch)

    # First move records space-1 as previous.
    _gw_agents._move_managed_agent_space("mover", "new-space")
    # Now move to the SAME space (no-op).
    _gw_agents._move_managed_agent_space("mover", "new-space")

    stored = gateway_core.find_agent_entry(gateway_core.load_gateway_registry(), "mover")
    assert stored["previous_space_id"] == "space-1"


def test_gateway_agents_update_changes_template_and_workdir(monkeypatch, tmp_path):
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
    token_file = tmp_path / "echo.token"
    token_file.write_text("axp_a_agent.secret")
    registry = gateway_core.load_gateway_registry()
    entry = {
        "name": "northstar",
        "agent_id": "agent-1",
        "space_id": "space-1",
        "base_url": "https://paxai.app",
        "runtime_type": "echo",
        "template_id": "echo_test",
        "template_label": "Echo (Test)",
        "desired_state": "running",
        "effective_state": "running",
        "token_file": str(token_file),
        "transport": "gateway",
        "credential_source": "gateway",
        "created_via": "cli",
    }
    registry["agents"] = [entry]
    gateway_core.ensure_local_asset_binding(registry, entry, created_via="cli", auto_approve=True)
    gateway_core.ensure_gateway_identity_binding(registry, entry, session=gateway_core.load_gateway_session())
    gateway_core.save_gateway_registry(registry)
    monkeypatch.setattr(_gw_agents, "_load_gateway_user_client", lambda: _FakeUserClient())

    result = runner.invoke(
        app,
        [
            "gateway",
            "agents",
            "update",
            "northstar",
            "--template",
            "ollama",
            "--workdir",
            str(tmp_path),
            "--exec",
            "python3 examples/gateway_ollama/ollama_bridge.py",
            "--timeout",
            "120",
            "--json",
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["template_id"] == "ollama"
    assert payload["runtime_type"] == "exec"
    assert payload["workdir"] == str(tmp_path)
    assert payload["timeout_seconds"] == 120
    stored = gateway_core.load_gateway_registry()["agents"][0]
    assert stored["template_id"] == "ollama"
    assert stored["workdir"] == str(tmp_path)
    assert stored["timeout_seconds"] == 120
    registry_after = gateway_core.load_gateway_registry()
    binding = registry_after["bindings"][0]
    assert binding["launch_spec"]["runtime_type"] == "exec"
    assert binding["launch_spec"]["workdir"] == str(tmp_path)
    assert binding["path"] == str(tmp_path)
    runtime_fingerprint = binding["runtime_fingerprint"]
    assert runtime_fingerprint["schema"] == "gateway.runtime_fingerprint.v1"
    assert runtime_fingerprint["runtime_type"] == "exec"
    assert runtime_fingerprint["template_id"] == "ollama"
    assert runtime_fingerprint["workdir"] == str(tmp_path)
    assert runtime_fingerprint["command"] == "python3 examples/gateway_ollama/ollama_bridge.py"
    assert runtime_fingerprint["runtime_fingerprint_hash"].startswith("sha256:")
    attestation = gateway_core.evaluate_runtime_attestation(registry_after, stored)
    assert attestation["attestation_state"] == "verified"


def test_gateway_agents_update_python_flag_writes_and_clears(monkeypatch, tmp_path):
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
    token_file = tmp_path / "agent.token"
    token_file.write_text("axp_a_agent.secret")
    registry = gateway_core.load_gateway_registry()
    entry = {
        "name": "northstar",
        "agent_id": "agent-2",
        "space_id": "space-1",
        "base_url": "https://paxai.app",
        "runtime_type": "sentinel_inference_sdk",
        "template_id": "sentinel_inference_sdk",
        "template_label": "Sentinel Inference SDK",
        "desired_state": "running",
        "effective_state": "running",
        "token_file": str(token_file),
        "transport": "gateway",
        "credential_source": "gateway",
        "created_via": "cli",
        "client": "openai_sdk",
    }
    registry["agents"] = [entry]
    gateway_core.ensure_local_asset_binding(registry, entry, created_via="cli", auto_approve=True)
    gateway_core.ensure_gateway_identity_binding(registry, entry, session=gateway_core.load_gateway_session())
    gateway_core.save_gateway_registry(registry)
    monkeypatch.setattr(_gw_agents, "_load_gateway_user_client", lambda: _FakeUserClient())

    result = runner.invoke(
        app,
        ["gateway", "agents", "update", "northstar", "--python", "/usr/bin/python3.11", "--json"],
    )
    assert result.exit_code == 0, result.output
    stored = gateway_core.load_gateway_registry()["agents"][0]
    assert stored["python"] == "/usr/bin/python3.11"

    result2 = runner.invoke(
        app,
        ["gateway", "agents", "update", "northstar", "--python", "", "--json"],
    )
    assert result2.exit_code == 0, result2.output
    stored2 = gateway_core.load_gateway_registry()["agents"][0]
    assert "python" not in stored2


def _seed_manifest_apply_agent(monkeypatch, tmp_path):
    """Seed a registry with one echo agent so `agents apply` exercises the
    real (unmocked) _update_managed_agent path."""
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
    token_file = tmp_path / "echo.token"
    token_file.write_text("axp_a_agent.secret")
    workdir = tmp_path / "orig-workdir"
    workdir.mkdir()
    registry = gateway_core.load_gateway_registry()
    entry = {
        "name": "northstar",
        "agent_id": "agent-1",
        "space_id": "space-1",
        "base_url": "https://paxai.app",
        "runtime_type": "echo",
        "template_id": "echo_test",
        "template_label": "Echo (Test)",
        "description": "original description",
        "workdir": str(workdir),
        "desired_state": "running",
        "effective_state": "running",
        "token_file": str(token_file),
        "transport": "gateway",
        "credential_source": "gateway",
        "created_via": "cli",
    }
    registry["agents"] = [entry]
    gateway_core.ensure_local_asset_binding(registry, entry, created_via="cli", auto_approve=True)
    gateway_core.ensure_gateway_identity_binding(registry, entry, session=gateway_core.load_gateway_session())
    gateway_core.save_gateway_registry(registry)
    monkeypatch.setattr(_gw_agents, "_load_gateway_user_client", lambda: _FakeUserClient())
    return entry


def test_gateway_agents_apply_without_template_preserves_existing_fields(monkeypatch, tmp_path):
    """Re-applying a manifest that omits template/type/description must not
    crash and must leave those fields untouched (regression: build_update_kwargs
    maps absent fields to _UNSET, and _update_managed_agent treated the truthy
    sentinel as a real template id → ValueError: Unknown template)."""
    _seed_manifest_apply_agent(monkeypatch, tmp_path)
    manifest_path = tmp_path / "northstar.agent.toml"
    manifest_path.write_text('name = "northstar"\ntimeout_seconds = 120\n')

    result = runner.invoke(
        app,
        ["gateway", "agents", "apply", str(manifest_path), "--auto-confirm", "--json"],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["applied"] is True
    assert payload["creating"] is False
    stored = gateway_core.load_gateway_registry()["agents"][0]
    assert stored["timeout_seconds"] == 120
    assert stored["template_id"] == "echo_test"
    assert stored["runtime_type"] == "echo"
    assert stored["description"] == "original description"


def test_gateway_agents_apply_resolves_relative_workdir_against_apply_cwd(monkeypatch, tmp_path):
    """workdir = "." in a manifest must resolve to the operator's cwd at apply
    time, not be stored literally (regression: the literal "." resolved against
    the daemon's cwd at launch, starting the sentinel in the wrong directory)."""
    _seed_manifest_apply_agent(monkeypatch, tmp_path)
    apply_cwd = tmp_path / "operator-cwd"
    apply_cwd.mkdir()
    monkeypatch.chdir(apply_cwd)
    manifest_path = tmp_path / "northstar.agent.toml"
    manifest_path.write_text('name = "northstar"\nworkdir = "."\n')

    result = runner.invoke(
        app,
        ["gateway", "agents", "apply", str(manifest_path), "--auto-confirm", "--json"],
    )

    assert result.exit_code == 0, result.output
    stored = gateway_core.load_gateway_registry()["agents"][0]
    assert Path(stored["workdir"]).is_absolute()
    assert stored["workdir"] == str(apply_cwd.resolve())


def test_gateway_agents_add_ollama_persists_model_override(monkeypatch, tmp_path):
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
    monkeypatch.setattr(_gw_agents, "_load_gateway_user_client", lambda: _FakeUserClient())
    monkeypatch.setattr(_gw_agents, "_find_agent_in_space", lambda *args, **kwargs: None)
    monkeypatch.setattr(_gw_agents, "_create_agent_in_space", _fake_create_agent_in_space)
    monkeypatch.setattr(_gw_agents, "_polish_metadata", lambda *args, **kwargs: None)
    monkeypatch.setattr(_gw_agents, "_mint_agent_pat", lambda *args, **kwargs: ("axp_a_agent.secret", "mgmt"))

    result = runner.invoke(
        app,
        [
            "gateway",
            "agents",
            "add",
            "ember",
            "--template",
            "ollama",
            "--model",
            "gemma4:latest",
            "--json",
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["template_id"] == "ollama"
    assert payload["model"] == "gemma4:latest"
    stored = gateway_core.load_gateway_registry()["agents"][0]
    assert stored["model"] == "gemma4:latest"


def test_gateway_agents_add_ollama_uses_recommended_model_when_unspecified(monkeypatch, tmp_path):
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
    monkeypatch.setattr(_gw_agents, "_load_gateway_user_client", lambda: _FakeUserClient())
    monkeypatch.setattr(_gw_agents, "_find_agent_in_space", lambda *args, **kwargs: None)
    monkeypatch.setattr(_gw_agents, "_create_agent_in_space", _fake_create_agent_in_space)
    monkeypatch.setattr(_gw_agents, "_polish_metadata", lambda *args, **kwargs: None)
    monkeypatch.setattr(_gw_agents, "_mint_agent_pat", lambda *args, **kwargs: ("axp_a_agent.secret", "mgmt"))
    monkeypatch.setattr(
        _gw_agents,
        "ollama_setup_status",
        lambda preferred_model=None: {
            "recommended_model": "gemma4:latest",
            "server_reachable": True,
            "available_models": ["gemma4:latest"],
            "local_models": ["gemma4:latest"],
            "summary": "Ollama is reachable. Recommended model: gemma4:latest.",
        },
    )

    result = runner.invoke(
        app,
        [
            "gateway",
            "agents",
            "add",
            "ember-default",
            "--template",
            "ollama",
            "--json",
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["template_id"] == "ollama"
    assert payload["model"] == "gemma4:latest"
    stored = gateway_core.load_gateway_registry()["agents"][0]
    assert stored["model"] == "gemma4:latest"


def test_gateway_agents_show_json_filters_activity(monkeypatch, tmp_path):
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
            "name": "echo-bot",
            "agent_id": "agent-1",
            "space_id": "space-1",
            "runtime_type": "echo",
            "desired_state": "running",
            "effective_state": "running",
            "last_seen_at": datetime.now(timezone.utc).isoformat(),
            "last_reply_preview": "Echo: ping",
            "token_file": "/tmp/echo-token",
        },
        {
            "name": "other-bot",
            "agent_id": "agent-2",
            "space_id": "space-1",
            "runtime_type": "exec",
            "desired_state": "running",
            "effective_state": "running",
            "last_seen_at": datetime.now(timezone.utc).isoformat(),
            "token_file": "/tmp/other-token",
        },
    ]
    gateway_core.save_gateway_registry(registry)
    gateway_core.record_gateway_activity("reply_sent", entry=registry["agents"][0], reply_preview="Echo: ping")
    gateway_core.record_gateway_activity("reply_sent", entry=registry["agents"][1], reply_preview="Other reply")

    result = runner.invoke(app, ["gateway", "agents", "show", "echo-bot", "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["agent"]["name"] == "echo-bot"
    assert payload["recent_activity"]
    assert all(row["agent_name"] == "echo-bot" for row in payload["recent_activity"])


def test_gateway_agents_test_sends_gateway_authored_probe(monkeypatch, tmp_path):
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
    # Migration (Madtank/supervisor 2026-05-02): default sender = invoking
    # principal. Set up a Gateway-managed workspace so the resolver returns
    # codex_supervisor as the principal for this CLI invocation.
    workspace_ax = tmp_path / ".ax"
    workspace_ax.mkdir(exist_ok=True)
    (workspace_ax / "config.toml").write_text(
        "[gateway]\n"
        'mode = "local"\n'
        'url = "http://127.0.0.1:8765"\n'
        "\n"
        "[agent]\n"
        'agent_name = "codex_supervisor"\n'
        f'workdir = "{tmp_path}"\n'
    )
    monkeypatch.chdir(tmp_path)
    invoker_token = tmp_path / "codex_supervisor.token"
    invoker_token.write_text("axp_a_codex.secret")
    registry = gateway_core.load_gateway_registry()
    registry["agents"] = [
        {
            "name": "echo-bot",
            "agent_id": "agent-1",
            "space_id": "space-1",
            "base_url": "https://paxai.app",
            "runtime_type": "echo",
            "template_id": "echo_test",
            "desired_state": "running",
            "effective_state": "running",
            "transport": "gateway",
            "credential_source": "gateway",
        },
        {
            "name": "codex_supervisor",
            "agent_id": "agent-codex",
            "space_id": "space-1",
            "active_space_id": "space-1",
            "default_space_id": "space-1",
            "base_url": "https://paxai.app",
            "runtime_type": "echo",
            "template_id": "echo_test",
            "desired_state": "running",
            "effective_state": "running",
            "transport": "gateway",
            "credential_source": "gateway",
            "token_file": str(invoker_token),
        },
    ]
    gateway_core.save_gateway_registry(registry)
    monkeypatch.setattr(_gw_agents, "_load_gateway_user_client", lambda: _FakeUserClient())
    monkeypatch.setattr(_gw_agents, "_find_agent_in_space", lambda *args, **kwargs: None)
    monkeypatch.setattr(_gw_agents, "_create_agent_in_space", _fake_create_agent_in_space)
    monkeypatch.setattr(_gw_agents, "_polish_metadata", lambda *args, **kwargs: None)
    monkeypatch.setattr(_gw_agents, "_mint_agent_pat", lambda *args, **kwargs: ("axp_a_agent.secret", "mgmt"))
    monkeypatch.setattr(_gw_agents, "AxClient", _FakeManagedSendClient)

    result = runner.invoke(app, ["gateway", "agents", "test", "echo-bot", "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["target_agent"] == "echo-bot"
    assert payload["author"] == "agent"
    assert payload["sender_agent"] == "codex_supervisor", "default sender must be invoking principal"
    assert "switchboard" not in str(payload).lower()
    assert payload["recommended_prompt"] == "gateway test ping"
    assert payload["content"] == "@echo-bot gateway test ping"
    assert payload["message"]["metadata"]["gateway"]["sent_via"] == "gateway_test"
    assert payload["message"]["metadata"]["gateway"]["test_author"] == "agent"
    assert payload["message"]["metadata"]["gateway"].get("test_sender_explicit") is False
    recent = gateway_core.load_recent_gateway_activity()
    assert recent[-1]["event"] == "gateway_test_sent"


def test_gateway_agents_test_can_send_as_user(monkeypatch, tmp_path):
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
            "name": "echo-bot",
            "agent_id": "agent-1",
            "space_id": "space-1",
            "base_url": "https://paxai.app",
            "runtime_type": "echo",
            "template_id": "echo_test",
            "desired_state": "running",
            "effective_state": "running",
            "transport": "gateway",
            "credential_source": "gateway",
        }
    ]
    gateway_core.save_gateway_registry(registry)
    monkeypatch.setattr(_gw_messaging, "_load_gateway_user_client", lambda: _FakeUserClient())

    result = runner.invoke(app, ["gateway", "agents", "test", "echo-bot", "--author", "user", "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["author"] == "user"
    assert payload["sender_agent"] is None
    assert payload["message"]["metadata"]["gateway"]["test_author"] == "user"


def test_gateway_agents_test_blocks_attached_session_until_connected(monkeypatch, tmp_path):
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
    token_file = tmp_path / "roger.token"
    token_file.write_text("axp_a_agent.secret\n")
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
    gateway_core.ensure_gateway_identity_binding(
        registry, registry["agents"][0], session=gateway_core.load_gateway_session()
    )
    gateway_core.save_gateway_registry(registry)

    result = runner.invoke(app, ["gateway", "agents", "test", "roger", "--json"])

    assert result.exit_code == 1, result.output
    assert "is stopped and cannot receive messages yet" in result.output
    assert "test_gateway_agents_test_block0/roger" in result.output.replace("\n", "")


def test_gateway_agents_doctor_persists_structured_result(monkeypatch, tmp_path):
    config_dir = tmp_path / "config"
    monkeypatch.setenv("AX_CONFIG_DIR", str(config_dir))
    monkeypatch.setenv("AX_OFFLINE", "1")  # skip upstream_existence HTTP check
    gateway_core.save_gateway_session(
        {
            "token": "axp_u_test.token",
            "base_url": "https://paxai.app",
            "space_id": "space-1",
            "username": "codex",
        }
    )
    token_file = tmp_path / "inbox.token"
    token_file.write_text("axp_a_agent.secret")
    registry = gateway_core.load_gateway_registry()
    registry["agents"] = [
        {
            "name": "docs-worker",
            "agent_id": "agent-1",
            "space_id": "space-1",
            "base_url": "https://paxai.app",
            "runtime_type": "inbox",
            "template_id": "inbox",
            "desired_state": "running",
            "effective_state": "stopped",
            "token_file": str(token_file),
            "transport": "gateway",
            "credential_source": "gateway",
        }
    ]
    gateway_core.save_gateway_registry(registry)

    result = runner.invoke(app, ["gateway", "agents", "doctor", "docs-worker", "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["status"] == "warning"
    check_names = [item["name"] for item in payload["checks"]]
    assert "gateway_auth" in check_names
    assert "queue_writable" in check_names
    assert "worker_attached" in check_names
    assert isinstance(payload["agent"]["last_doctor_result"], dict)
    assert payload["agent"]["last_doctor_result"]["status"] == "warning"
    assert payload["agent"]["last_doctor_result"]["checks"]
    assert payload["agent"]["last_successful_doctor_at"]

    stored = gateway_core.load_gateway_registry()["agents"][0]
    assert stored["last_doctor_result"]["status"] == "warning"
    assert stored["last_successful_doctor_at"]


def test_resolve_system_prompt_input_rejects_both_flags(tmp_path):
    """Operator hygiene: --system-prompt and --system-prompt-file are
    mutually exclusive."""
    prompt_file = tmp_path / "role.md"
    prompt_file.write_text("from file")
    with pytest.raises(ValueError, match="mutually exclusive"):
        _gw_agents._resolve_system_prompt_input(
            system_prompt="from cli",
            system_prompt_file=str(prompt_file),
        )


def test_resolve_system_prompt_input_reads_file(tmp_path):
    prompt_file = tmp_path / "role.md"
    prompt_file.write_text("  multi-line\n  prompt body  \n")
    resolved = _gw_agents._resolve_system_prompt_input(
        system_prompt=None,
        system_prompt_file=str(prompt_file),
    )
    # Strips whitespace.
    assert resolved == "multi-line\n  prompt body"


def test_resolve_system_prompt_input_missing_file_raises(tmp_path):
    with pytest.raises(ValueError, match="not found"):
        _gw_agents._resolve_system_prompt_input(
            system_prompt=None,
            system_prompt_file=str(tmp_path / "does-not-exist.md"),
        )


def test_agent_workspace_context_text_includes_operator_prompt():
    """When the entry has a system_prompt, the AGENT_CONTEXT.md written
    to .ax/ must surface it so the operator can inspect the persona
    without having to dig into the registry."""
    entry = {
        "name": "satellite_resilience",
        "template_id": "claude_code_channel",
        "runtime_type": "claude_code_channel",
        "system_prompt": "You harden satellite comms against jamming.",
    }
    text = _gw_agents._agent_workspace_context_text(entry, workdir="/tmp/satrez")
    assert "Operator-supplied role instructions" in text
    assert "You harden satellite comms against jamming." in text


def test_agent_workspace_context_text_without_prompt_shows_how_to_set_one():
    """Without a system_prompt, the doc points the operator at the
    `ax gateway agents update --system-prompt` command."""
    entry = {"name": "no-persona", "template_id": "hermes", "runtime_type": "sentinel_inference_sdk"}
    text = _gw_agents._agent_workspace_context_text(entry, workdir="/tmp/no-persona")
    assert "No operator-supplied system prompt is configured" in text
    assert "ax gateway agents update no-persona --system-prompt" in text


def test_operator_cleanup_hides_selected_agents(monkeypatch, tmp_path):
    _isolate_gateway_paths(monkeypatch, tmp_path)
    registry = {
        "agents": [
            {
                "name": "stale-one",
                "agent_id": "agent-stale-one",
                "template_id": "claude_code_channel",
                "runtime_type": "claude_code_channel",
                "desired_state": "running",
                "effective_state": "error",
            },
            {
                "name": "stale-two",
                "agent_id": "agent-stale-two",
                "template_id": "pass_through",
                "runtime_type": "inbox",
                "desired_state": "running",
                "effective_state": "stale",
            },
            {
                "name": "keeper",
                "agent_id": "agent-keeper",
                "template_id": "echo",
                "runtime_type": "echo",
                "desired_state": "running",
                "effective_state": "running",
            },
        ]
    }
    gateway_core.save_gateway_registry(registry)

    payload = _gw_agents._hide_managed_agents(
        ["stale-one", "stale-two"],
        reason="operator_cleanup",
    )

    assert payload["count"] == 2
    assert payload["missing"] == []
    stored = {agent["name"]: agent for agent in gateway_core.load_gateway_registry()["agents"]}
    assert stored["stale-one"]["lifecycle_phase"] == "hidden"
    assert stored["stale-one"]["desired_state"] == "stopped"
    assert stored["stale-one"]["hidden_reason"] == "operator_cleanup"
    assert stored["stale-two"]["lifecycle_phase"] == "hidden"
    assert stored["keeper"].get("lifecycle_phase", "active") == "active"

    visible_payload = _gw_agents._status_payload(activity_limit=0)
    visible_names = [agent["name"] for agent in visible_payload["agents"]]
    assert visible_names == ["keeper"]
    assert visible_payload["summary"]["hidden_agents"] == 2
    recent = gateway_core.load_recent_gateway_activity()
    assert [event["event"] for event in recent].count("managed_agent_hidden") == 2


def test_recover_managed_agents_from_evidence_restores_lost_row(monkeypatch, tmp_path):
    """Pre-race-fix damage recovery: when a managed_agent_added activity
    event exists locally but the registry row is missing (silent race
    clobber), _recover_managed_agents_from_evidence reconstructs a
    minimal row using only verified evidence — never fabricating
    credentials.
    """
    _isolate_gateway_paths(monkeypatch, tmp_path)
    # Pre-condition: registry empty, but token file + managed_agent_added
    # event exist locally — exactly the cc-backend / widget_smith state.
    gateway_core.save_gateway_registry({"agents": []})

    token_dir = gateway_core.agent_dir("ghost-agent")
    token_dir.mkdir(parents=True, exist_ok=True)
    (token_dir / "token").write_text("axp_a_ghost.evidence", encoding="utf-8")

    gateway_core.record_gateway_activity(
        "managed_agent_added",
        agent_name="ghost-agent",
        agent_id="agent-ghost-id",
        asset_id="agent-ghost-id",
        install_id="install-ghost",
        gateway_id="gateway-host",
        runtime_type="claude_code_channel",
        transport="gateway",
        space_id="49afd277-78d2-4a32-9858-3594cda684af",
        token_file=str(token_dir / "token"),
        credential_source="gateway",
    )

    payload = _gw_agents._recover_managed_agents_from_evidence(["ghost-agent", "missing-no-evidence"])

    assert payload["count"] == 1
    assert payload["already_present"] == []
    assert payload["no_evidence"] == ["missing-no-evidence"]

    stored = gateway_core.load_gateway_registry()
    row = next((a for a in stored["agents"] if a.get("name") == "ghost-agent"), None)
    assert row is not None, "recovered row missing from registry"
    assert row["agent_id"] == "agent-ghost-id"
    assert row["install_id"] == "install-ghost"
    assert row["runtime_type"] == "claude_code_channel"
    assert row["template_id"] == "claude_code_channel"
    assert row["space_id"] == "49afd277-78d2-4a32-9858-3594cda684af"
    # Recovery reconstructs the portable relative token_file, resolving to the
    # real on-disk token under gateway_dir() (#89).
    assert row["token_file"] == "agents/ghost-agent/token"
    assert gateway_core.resolve_agent_token_file(row) == token_dir / "token"
    assert row["lifecycle_phase"] == "active"
    assert row["desired_state"] == "stopped"  # safe default — operator restarts deliberately
    assert row["drift_reason"] == "registry_row_recovered_from_evidence"

    # managed_agent_recovered activity event was recorded.
    recent = gateway_core.load_recent_gateway_activity()
    events = [e for e in recent if e.get("event") == "managed_agent_recovered"]
    assert len(events) == 1
    assert events[0].get("agent_name") == "ghost-agent"


def test_recover_managed_agents_without_event_token_file(monkeypatch, tmp_path):
    """#170: the event-side ``token_file`` is dead data — recovery derives the
    token path from the agent name. A managed_agent_added event that omits
    ``token_file`` entirely (as written after #170) still recovers correctly.
    """
    _isolate_gateway_paths(monkeypatch, tmp_path)
    gateway_core.save_gateway_registry({"agents": []})

    token_dir = gateway_core.agent_dir("ghost-agent")
    token_dir.mkdir(parents=True, exist_ok=True)
    (token_dir / "token").write_text("axp_a_ghost.evidence", encoding="utf-8")

    # No token_file kwarg — mirrors managed_agent_added events written post-#170.
    gateway_core.record_gateway_activity(
        "managed_agent_added",
        agent_name="ghost-agent",
        agent_id="agent-ghost-id",
        asset_id="agent-ghost-id",
        install_id="install-ghost",
        gateway_id="gateway-host",
        runtime_type="claude_code_channel",
        transport="gateway",
        space_id="49afd277-78d2-4a32-9858-3594cda684af",
        credential_source="gateway",
    )

    payload = _gw_agents._recover_managed_agents_from_evidence(["ghost-agent"])

    assert payload["count"] == 1
    stored = gateway_core.load_gateway_registry()
    row = next((a for a in stored["agents"] if a.get("name") == "ghost-agent"), None)
    assert row is not None, "recovered row missing from registry"
    assert row["agent_id"] == "agent-ghost-id"
    # Token path is name-derived, not sourced from the (now-absent) event field.
    assert row["token_file"] == "agents/ghost-agent/token"
    assert gateway_core.resolve_agent_token_file(row) == token_dir / "token"


def test_recover_managed_agents_refuses_when_token_missing(monkeypatch, tmp_path):
    """Recovery requires BOTH the activity event AND the token file.
    Missing token → no recovery (we don't fabricate credentials).
    """
    _isolate_gateway_paths(monkeypatch, tmp_path)
    gateway_core.save_gateway_registry({"agents": []})

    # Activity event exists but no token file.
    gateway_core.record_gateway_activity(
        "managed_agent_added",
        agent_name="no-token-agent",
        agent_id="agent-id",
        asset_id="agent-id",
        install_id="install-id",
        gateway_id="gateway-host",
        runtime_type="echo",
        transport="gateway",
        space_id="space-1",
        token_file="/tmp/nonexistent-recovery-token-path",
        credential_source="gateway",
    )

    payload = _gw_agents._recover_managed_agents_from_evidence(["no-token-agent"])
    assert payload["count"] == 0
    assert payload["no_evidence"] == ["no-token-agent"]


def test_recover_managed_agents_skips_already_present_rows(monkeypatch, tmp_path):
    """Idempotent: if a row already exists, recovery is a no-op for it."""
    _isolate_gateway_paths(monkeypatch, tmp_path)
    gateway_core.save_gateway_registry(
        {"agents": [{"name": "already-there", "agent_id": "existing", "template_id": "echo"}]}
    )

    payload = _gw_agents._recover_managed_agents_from_evidence(["already-there"])
    assert payload["count"] == 0
    assert payload["already_present"] == ["already-there"]
    assert payload["no_evidence"] == []


def test_remove_managed_agent_calls_delete_agent_then_local_remove(monkeypatch, tmp_path):
    _isolate_gateway_paths(monkeypatch, tmp_path)
    token_path = tmp_path / "tok"
    token_path.write_text("axp_a_test\n")
    entry = {
        "name": "doomed-agent",
        "agent_id": "agent-doomed",
        "space_id": "space-x",
        "template_id": "hermes",
        "runtime_type": "sentinel_inference_sdk",
        "token_file": str(token_path),
    }
    gateway_core.save_gateway_registry({"agents": [entry]})
    client = _RecordingHeartbeatClient()
    removed = _gw_agents._remove_managed_agent("doomed-agent", client_factory=lambda: client)
    assert removed["name"] == "doomed-agent"
    assert client.deletes == ["agent-doomed"]
    registry_after = gateway_core.load_gateway_registry()
    assert all(a.get("name") != "doomed-agent" for a in registry_after.get("agents", []))
    assert not token_path.exists()


def test_remove_managed_agent_proceeds_on_upstream_failure(monkeypatch, tmp_path):
    _isolate_gateway_paths(monkeypatch, tmp_path)
    token_path = tmp_path / "tok"
    token_path.write_text("axp_a_test\n")
    entry = {
        "name": "doomed-agent",
        "agent_id": "agent-doomed",
        "space_id": "space-x",
        "template_id": "hermes",
        "runtime_type": "sentinel_inference_sdk",
        "token_file": str(token_path),
    }
    gateway_core.save_gateway_registry({"agents": [entry]})
    boom = RuntimeError("network unreachable")
    client = _RecordingHeartbeatClient(fail_with=boom)
    removed = _gw_agents._remove_managed_agent("doomed-agent", client_factory=lambda: client)
    assert removed["name"] == "doomed-agent"
    registry_after = gateway_core.load_gateway_registry()
    assert all(a.get("name") != "doomed-agent" for a in registry_after.get("agents", []))
    recent = gateway_core.load_recent_gateway_activity()
    assert any(r.get("event") == "managed_agent_remove_upstream_failed" for r in recent)


def test_archive_managed_agent_sets_phase_and_stops_runtime(monkeypatch, tmp_path):
    _isolate_gateway_paths(monkeypatch, tmp_path)
    entry = {
        "name": "probe-doomed",
        "agent_id": "agent-doomed",
        "template_id": "hermes",
        "runtime_type": "sentinel_inference_sdk",
        "desired_state": "running",
        "effective_state": "running",
        "liveness": "connected",
        "last_seen_age_seconds": 5.0,
    }
    gateway_core.save_gateway_registry({"agents": [entry]})
    client = _RecordingHeartbeatClient()
    result = _gw_agents._archive_managed_agent("probe-doomed", reason="cleanup", client_factory=lambda: client)
    assert result["lifecycle_phase"] == "archived"
    registry = gateway_core.load_gateway_registry()
    stored = next(a for a in registry["agents"] if a["name"] == "probe-doomed")
    assert stored["lifecycle_phase"] == "archived"
    assert stored["archived_reason"] == "cleanup"
    assert stored["desired_state"] == "stopped"
    assert stored["desired_state_before_archive"] == "running"
    assert "archived_at" in stored
    # Audit event recorded.
    recent = gateway_core.load_recent_gateway_activity()
    assert any(r.get("event") == "managed_agent_archived" for r in recent)


def test_archive_managed_agent_idempotent(monkeypatch, tmp_path):
    _isolate_gateway_paths(monkeypatch, tmp_path)
    entry = {
        "name": "probe-already",
        "agent_id": "agent-already",
        "template_id": "hermes",
        "runtime_type": "sentinel_inference_sdk",
        "lifecycle_phase": "archived",
        "archived_at": gateway_core._now_iso(),
        "archived_reason": "first call",
        "desired_state": "stopped",
        "desired_state_before_archive": "running",
    }
    gateway_core.save_gateway_registry({"agents": [entry]})
    client = _RecordingHeartbeatClient()
    _gw_agents._archive_managed_agent("probe-already", reason="second call", client_factory=lambda: client)
    stored = next(a for a in gateway_core.load_gateway_registry()["agents"] if a["name"] == "probe-already")
    # Reason not overwritten on a no-op archive — first archived_reason preserved.
    assert stored["archived_reason"] == "first call"
    assert client.heartbeats == []


def test_archive_then_restore_returns_to_prior_desired_state(monkeypatch, tmp_path):
    _isolate_gateway_paths(monkeypatch, tmp_path)
    entry = {
        "name": "probe-roundtrip",
        "agent_id": "agent-rt",
        "template_id": "hermes",
        "runtime_type": "sentinel_inference_sdk",
        "desired_state": "running",
        "effective_state": "running",
        "liveness": "connected",
        "last_seen_age_seconds": 5.0,
    }
    gateway_core.save_gateway_registry({"agents": [entry]})
    client = _RecordingHeartbeatClient()
    _gw_agents._archive_managed_agent("probe-roundtrip", client_factory=lambda: client)
    _gw_agents._restore_managed_agent("probe-roundtrip", client_factory=lambda: client)
    stored = next(a for a in gateway_core.load_gateway_registry()["agents"] if a["name"] == "probe-roundtrip")
    assert stored["lifecycle_phase"] == "active"
    assert stored["desired_state"] == "running"
    assert "archived_at" not in stored
    assert "archived_reason" not in stored
    assert "desired_state_before_archive" not in stored
    recent = gateway_core.load_recent_gateway_activity()
    assert any(r.get("event") == "managed_agent_restored" for r in recent)


def test_restore_unarchived_agent_is_noop(monkeypatch, tmp_path):
    _isolate_gateway_paths(monkeypatch, tmp_path)
    entry = {
        "name": "probe-active",
        "agent_id": "agent-active",
        "template_id": "hermes",
        "runtime_type": "sentinel_inference_sdk",
        "lifecycle_phase": "active",
        "desired_state": "running",
    }
    gateway_core.save_gateway_registry({"agents": [entry]})
    client = _RecordingHeartbeatClient()
    _gw_agents._restore_managed_agent("probe-active", client_factory=lambda: client)
    # No upstream noise on a no-op restore.
    assert client.heartbeats == []


def test_save_registry_preserves_restore_written_during_daemon_tick(monkeypatch, tmp_path):
    """Race regression (other direction): daemon's stale-archived view must
    not clobber a CLI restore that landed mid-tick.
    """
    _isolate_gateway_paths(monkeypatch, tmp_path)
    initial = {
        "agents": [
            {
                "name": "race-restore",
                "agent_id": "agent-restore",
                "template_id": "hermes",
                "runtime_type": "sentinel_inference_sdk",
                "lifecycle_phase": "archived",
                "archived_at": gateway_core._now_iso(),
                "archived_reason": "earlier",
                "desired_state": "stopped",
                "desired_state_before_archive": "running",
            }
        ]
    }
    gateway_core.save_gateway_registry(initial, merge_archive=False)

    # Daemon's stale in-memory copy: still sees the agent as archived.
    daemon_view = gateway_core.load_gateway_registry()

    # CLI restores between the daemon's load and the daemon's save.
    _gw_agents._restore_managed_agent("race-restore")

    # Daemon now saves its (stale, still-archived) copy. Bidirectional merge
    # should pull the disk's freshly-active state forward.
    gateway_core.save_gateway_registry(daemon_view)

    final = gateway_core.load_gateway_registry()
    stored = next(a for a in final["agents"] if a["name"] == "race-restore")
    assert stored["lifecycle_phase"] == "active"
    assert "archived_at" not in stored
    assert "archived_reason" not in stored


def test_save_registry_preserves_archive_written_during_daemon_tick(monkeypatch, tmp_path):
    """Race regression: daemon load → modify → save must not clobber a CLI
    archive that landed between the daemon's load and the daemon's save.
    """
    _isolate_gateway_paths(monkeypatch, tmp_path)
    # Initial state: probe is active, runtime running.
    initial = {
        "agents": [
            {
                "name": "race-probe",
                "agent_id": "agent-race",
                "template_id": "hermes",
                "runtime_type": "sentinel_inference_sdk",
                "lifecycle_phase": "active",
                "desired_state": "running",
                "liveness": "connected",
            }
        ]
    }
    gateway_core.save_gateway_registry(initial)

    # Daemon's stale in-memory copy from the start of its tick.
    daemon_view = gateway_core.load_gateway_registry()
    daemon_view["agents"][0]["effective_state"] = "running"  # daemon-side update

    # CLI archives between the daemon's load and the daemon's save.
    _gw_agents._archive_managed_agent("race-probe")

    # Daemon now saves its (stale) copy. Race-safety merge should preserve
    # the archive fields the CLI wrote.
    gateway_core.save_gateway_registry(daemon_view)

    final = gateway_core.load_gateway_registry()
    stored = next(a for a in final["agents"] if a["name"] == "race-probe")
    assert stored["lifecycle_phase"] == "archived"
    assert stored["desired_state"] == "stopped"
    assert "archived_at" in stored


def test_backend_agent_record_falls_back_to_cache_on_failure(monkeypatch, tmp_path):
    """When list_agents raises (e.g. 429), _backend_agent_record returns
    the agent from the local cache instead of None — so dashboard reads
    survive transient upstream rate limits.
    """
    _isolate_gateway_paths(monkeypatch, tmp_path)
    # Seed the cache as if a previous successful call had populated it.
    _gw_agents._save_agents_cache(
        [
            {"name": "cached_agent", "agent_id": "agent-cached", "space_id": "space-1"},
            {"name": "other_agent", "agent_id": "agent-other"},
        ]
    )

    class FailingClient:
        def list_agents(self):
            raise _make_429_error()

    found = _gw_spaces._backend_agent_record(FailingClient(), "cached_agent")
    assert found is not None
    assert found["agent_id"] == "agent-cached"


def test_backend_agent_record_seeds_cache_on_successful_upstream(monkeypatch, tmp_path):
    """Successful upstream list_agents writes to the cache so the next
    failure has data to serve.
    """
    _isolate_gateway_paths(monkeypatch, tmp_path)
    assert _gw_agents._load_agents_cache() == []  # empty pre-condition

    class StubClient:
        def list_agents(self):
            return {
                "agents": [
                    {"name": "fresh_agent", "agent_id": "agent-fresh", "space_id": "space-1"},
                ]
            }

    found = _gw_spaces._backend_agent_record(StubClient(), "fresh_agent")
    assert found is not None
    assert found["agent_id"] == "agent-fresh"

    cached = _gw_agents._load_agents_cache()
    assert any(a.get("name") == "fresh_agent" for a in cached), "upstream success should seed cache"


class TestAgentsAddCommand:
    def test_json(self, monkeypatch):
        entry = {
            "name": "echo1",
            "template_label": "Echo Test",
            "asset_type_label": "test",
            "desired_state": "running",
            "timeout_seconds": None,
            "token_file": "/tmp/tok",
        }
        monkeypatch.setattr(_gw_agents, "_register_managed_agent", lambda **kw: entry)
        monkeypatch.setattr(_gw_agents, "_resolve_system_prompt_input", lambda **kw: None)
        result = runner.invoke(app, ["gateway", "agents", "add", "echo1", "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["name"] == "echo1"

    def test_text(self, monkeypatch):
        entry = {
            "name": "echo1",
            "template_label": "Echo Test",
            "asset_type_label": "test",
            "desired_state": "running",
            "timeout_seconds": 30,
            "token_file": "/tmp/tok",
        }
        monkeypatch.setattr(_gw_agents, "_register_managed_agent", lambda **kw: entry)
        monkeypatch.setattr(_gw_agents, "_resolve_system_prompt_input", lambda **kw: None)
        result = runner.invoke(app, ["gateway", "agents", "add", "echo1"])
        assert result.exit_code == 0
        output = _strip(result.output)
        assert "echo1" in output

    def test_error_exits_1(self, monkeypatch):
        monkeypatch.setattr(
            _gw_agents,
            "_register_managed_agent",
            lambda **kw: (_ for _ in ()).throw(ValueError("bad agent")),
        )
        monkeypatch.setattr(_gw_agents, "_resolve_system_prompt_input", lambda **kw: None)
        result = runner.invoke(app, ["gateway", "agents", "add", "bad"])
        assert result.exit_code != 0

    def test_ephemeral_session_wiped_after_successful_add(self, monkeypatch, tmp_path):
        config_dir = tmp_path / "config"
        monkeypatch.setenv("AX_CONFIG_DIR", str(config_dir))
        gateway_core.save_gateway_session(
            {
                "token": "axp_u_test.token",
                "base_url": "https://paxai.app",
                "space_id": "space-1",
                "username": "madtank",
                "ephemeral": True,
            }
        )
        entry = {
            "name": "echo1",
            "template_label": "Echo Test",
            "asset_type_label": "test",
            "desired_state": "running",
            "timeout_seconds": None,
            "token_file": "/tmp/tok",
        }
        monkeypatch.setattr(_gw_agents, "_register_managed_agent", lambda **kw: entry)
        monkeypatch.setattr(_gw_agents, "_resolve_system_prompt_input", lambda **kw: None)
        session_path = gateway_core.session_path()
        assert session_path.exists()

        result = runner.invoke(app, ["gateway", "agents", "add", "echo1", "--json"])

        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert data["ephemeral_session_wiped"] == str(session_path)
        assert not session_path.exists()
        recent = gateway_core.load_recent_gateway_activity()
        assert any(item.get("event") == "gateway_session_wiped_ephemeral" for item in recent)

    def test_ephemeral_session_preserved_when_add_fails(self, monkeypatch, tmp_path):
        config_dir = tmp_path / "config"
        monkeypatch.setenv("AX_CONFIG_DIR", str(config_dir))
        gateway_core.save_gateway_session(
            {
                "token": "axp_u_test.token",
                "base_url": "https://paxai.app",
                "space_id": "space-1",
                "username": "madtank",
                "ephemeral": True,
            }
        )
        monkeypatch.setattr(
            _gw_agents,
            "_register_managed_agent",
            lambda **kw: (_ for _ in ()).throw(ValueError("bad agent")),
        )
        monkeypatch.setattr(_gw_agents, "_resolve_system_prompt_input", lambda **kw: None)
        session_path = gateway_core.session_path()

        result = runner.invoke(app, ["gateway", "agents", "add", "bad"])

        assert result.exit_code != 0
        # Failed mint must not wipe the session — operator should retry without
        # being forced to re-paste the PAT.
        assert session_path.exists()
        session = gateway_core.load_gateway_session()
        assert session.get("ephemeral") is True
        assert session.get("token") == "axp_u_test.token"

    def test_non_ephemeral_session_survives_successful_add(self, monkeypatch, tmp_path):
        config_dir = tmp_path / "config"
        monkeypatch.setenv("AX_CONFIG_DIR", str(config_dir))
        gateway_core.save_gateway_session(
            {
                "token": "axp_u_test.token",
                "base_url": "https://paxai.app",
                "space_id": "space-1",
                "username": "madtank",
            }
        )
        entry = {
            "name": "echo1",
            "template_label": "Echo Test",
            "asset_type_label": "test",
            "desired_state": "running",
            "timeout_seconds": None,
            "token_file": "/tmp/tok",
        }
        monkeypatch.setattr(_gw_agents, "_register_managed_agent", lambda **kw: entry)
        monkeypatch.setattr(_gw_agents, "_resolve_system_prompt_input", lambda **kw: None)
        session_path = gateway_core.session_path()

        result = runner.invoke(app, ["gateway", "agents", "add", "echo1", "--json"])

        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert "ephemeral_session_wiped" not in data
        assert session_path.exists()


class TestAgentsUpdateCommand:
    def test_json(self, monkeypatch):
        entry = {
            "name": "echo1",
            "template_label": "Echo Test",
            "runtime_type": "echo",
            "desired_state": "running",
            "timeout_seconds": None,
        }
        monkeypatch.setattr(_gw_agents, "_update_managed_agent", lambda **kw: entry)
        result = runner.invoke(app, ["gateway", "agents", "update", "echo1", "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["name"] == "echo1"

    def test_error_exits_1(self, monkeypatch):
        monkeypatch.setattr(
            _gw_agents,
            "_update_managed_agent",
            lambda **kw: (_ for _ in ()).throw(LookupError("not found")),
        )
        result = runner.invoke(app, ["gateway", "agents", "update", "nope"])
        assert result.exit_code != 0


class TestAgentsListCommand:
    def test_json(self, monkeypatch):
        payload = {
            "agents": [{"name": "bot1", "runtime_type": "echo", "template_id": "echo_test"}],
            "summary": {
                "managed_agents": 1,
                "live_agents": 0,
                "on_demand_agents": 0,
                "inbox_agents": 0,
                "archived_agents": 0,
                "hidden_agents": 0,
                "system_agents": 0,
            },
        }
        monkeypatch.setattr(_gw_agents, "_status_payload", lambda **kw: payload)
        result = runner.invoke(app, ["gateway", "agents", "list", "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["count"] == 1


class TestAgentsShowCommand:
    def test_json(self, monkeypatch):
        detail = {"agent": {"name": "bot1"}, "recent_activity": []}
        monkeypatch.setattr(_gw_agents, "_agent_detail_payload", lambda name, **kw: detail)
        result = runner.invoke(app, ["gateway", "agents", "show", "bot1", "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["agent"]["name"] == "bot1"

    def test_not_found(self, monkeypatch):
        monkeypatch.setattr(_gw_agents, "_agent_detail_payload", lambda name, **kw: None)
        result = runner.invoke(app, ["gateway", "agents", "show", "nope"])
        assert result.exit_code != 0


class TestAgentsTestCommand:
    def test_json(self, monkeypatch):
        payload = {
            "target_agent": "bot1",
            "recommended_prompt": "test message",
            "message": {"id": "msg-1"},
        }
        monkeypatch.setattr(_gw_agents, "_send_gateway_test_to_managed_agent", lambda name, **kw: payload)
        result = runner.invoke(app, ["gateway", "agents", "test", "bot1", "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["target_agent"] == "bot1"

    def test_error(self, monkeypatch):
        monkeypatch.setattr(
            _gw_agents,
            "_send_gateway_test_to_managed_agent",
            lambda name, **kw: (_ for _ in ()).throw(ValueError("boom")),
        )
        result = runner.invoke(app, ["gateway", "agents", "test", "nope"])
        assert result.exit_code != 0


class TestAgentsMoveCommand:
    def test_json(self, monkeypatch):
        payload = {"active_space_id": "sp-2", "active_space_name": "New Space", "previous_space_id": "sp-1"}
        monkeypatch.setattr(_gw_agents, "_move_managed_agent_space", lambda name, sid, **kw: payload)
        result = runner.invoke(app, ["gateway", "agents", "move", "bot1", "--space", "sp-2", "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["active_space_id"] == "sp-2"

    def test_no_space_no_revert(self, monkeypatch):
        result = runner.invoke(app, ["gateway", "agents", "move", "bot1"])
        assert result.exit_code != 0

    def test_error(self, monkeypatch):
        monkeypatch.setattr(
            _gw_agents,
            "_move_managed_agent_space",
            lambda name, sid, **kw: (_ for _ in ()).throw(LookupError("not found")),
        )
        result = runner.invoke(app, ["gateway", "agents", "move", "bot1", "--space", "sp-2"])
        assert result.exit_code != 0


class TestAgentsDoctorCommand:
    def test_json(self, monkeypatch):
        payload = {
            "status": "passed",
            "summary": "all ok",
            "checks": [{"name": "connectivity", "status": "passed", "detail": "ok"}],
        }
        monkeypatch.setattr(_gw_agents, "_run_gateway_doctor", lambda name, **kw: payload)
        result = runner.invoke(app, ["gateway", "agents", "doctor", "bot1", "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["status"] == "passed"

    def test_error(self, monkeypatch):
        monkeypatch.setattr(
            _gw_agents,
            "_run_gateway_doctor",
            lambda name, **kw: (_ for _ in ()).throw(LookupError("not found")),
        )
        result = runner.invoke(app, ["gateway", "agents", "doctor", "nope"])
        assert result.exit_code != 0


class TestTokenFileResolutionInRemoveAndDoctor:
    """#89 / #147: the `agents remove` and `agents doctor` flows resolve
    `token_file` through `resolve_agent_token_file`, so they work for both the
    new relative form and legacy absolute paths. The relative-form doctor case
    also pins the regression sarob flagged on PR #108 (resolving the relative
    path against CWD instead of gateway_dir reported a false agent_token
    failure)."""

    def _entry(self, name, token_file):
        return {
            "name": name,
            "agent_id": f"agent-{name}",
            "space_id": "space-1",
            "base_url": "https://paxai.app",
            "runtime_type": "hermes",
            "template_id": "hermes",
            "desired_state": "running",
            "effective_state": "running",
            "last_seen_at": gateway_core._now_iso(),
            "token_file": token_file,
            "transport": "gateway",
            "credential_source": "gateway",
        }

    def _save(self, entry):
        gateway_core.save_gateway_session(
            {"token": "axp_u_test.token", "base_url": "https://paxai.app", "space_id": "space-1", "username": "u"}
        )
        registry = gateway_core.load_gateway_registry()
        registry["agents"] = [entry]
        gateway_core.save_gateway_registry(registry)

    def test_doctor_agent_token_passes_for_relative_token_file(self):
        # The relative form resolves under gateway_dir(), not CWD (PR #108 bug).
        token_path = gateway_core.agent_token_path("nova")
        token_path.parent.mkdir(parents=True, exist_ok=True)
        token_path.write_text("axp_a_agent.secret")
        self._save(self._entry("nova", "agents/nova/token"))

        result = _gw_agents._run_gateway_doctor("nova")
        checks = {c["name"]: c for c in result["checks"]}
        assert checks["agent_token"]["status"] == "passed"

    def test_doctor_agent_token_passes_for_legacy_absolute_token_file(self, tmp_path):
        # A legacy absolute path (non-canonical shape, so the load-time
        # migration leaves it) is honored as-is by the resolver.
        legacy = tmp_path / "legacy_tokens" / "nova.token"
        legacy.parent.mkdir(parents=True, exist_ok=True)
        legacy.write_text("axp_a_agent.secret")
        self._save(self._entry("nova", str(legacy)))

        result = _gw_agents._run_gateway_doctor("nova")
        checks = {c["name"]: c for c in result["checks"]}
        assert checks["agent_token"]["status"] == "passed"

    def test_remove_unlinks_relative_token_file(self):
        token_path = gateway_core.agent_token_path("nova")
        token_path.parent.mkdir(parents=True, exist_ok=True)
        token_path.write_text("axp_a_agent.secret")
        self._save(self._entry("nova", "agents/nova/token"))

        _gw_agents._remove_managed_agent("nova", client_factory=lambda: None)
        assert not token_path.exists()

    def test_remove_unlinks_legacy_absolute_token_file(self, tmp_path):
        legacy = tmp_path / "legacy_tokens" / "nova.token"
        legacy.parent.mkdir(parents=True, exist_ok=True)
        legacy.write_text("axp_a_agent.secret")
        self._save(self._entry("nova", str(legacy)))

        _gw_agents._remove_managed_agent("nova", client_factory=lambda: None)
        assert not legacy.exists()


class TestAgentsArchiveCommand:
    def test_json_success(self, monkeypatch):
        monkeypatch.setattr(_gw_agents, "_archive_managed_agent", lambda name, **kw: {"name": name})
        result = runner.invoke(app, ["gateway", "agents", "archive", "bot1", "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["count"] == 1

    def test_not_found(self, monkeypatch):
        monkeypatch.setattr(
            _gw_agents,
            "_archive_managed_agent",
            lambda name, **kw: (_ for _ in ()).throw(LookupError("nope")),
        )
        result = runner.invoke(app, ["gateway", "agents", "archive", "nope", "--json"])
        assert result.exit_code != 0

    def test_text_success(self, monkeypatch):
        monkeypatch.setattr(_gw_agents, "_archive_managed_agent", lambda name, **kw: {"name": name})
        result = runner.invoke(app, ["gateway", "agents", "archive", "bot1"])
        assert result.exit_code == 0
        assert "Archived" in _strip(result.output)


class TestAgentsRestoreCommand:
    def test_json_success(self, monkeypatch):
        monkeypatch.setattr(
            _gw_agents, "_restore_managed_agent", lambda name: {"name": name, "desired_state": "stopped"}
        )
        result = runner.invoke(app, ["gateway", "agents", "restore", "bot1", "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["count"] == 1

    def test_not_found(self, monkeypatch):
        monkeypatch.setattr(
            _gw_agents,
            "_restore_managed_agent",
            lambda name: (_ for _ in ()).throw(LookupError("nope")),
        )
        result = runner.invoke(app, ["gateway", "agents", "restore", "nope", "--json"])
        assert result.exit_code != 0


class TestAgentsRecoverCommand:
    def test_json_success(self, monkeypatch):
        payload = {
            "recovered": [{"name": "bot1", "agent_id": "a1"}],
            "already_present": [],
            "no_evidence": [],
            "count": 1,
        }
        monkeypatch.setattr(_gw_agents, "_recover_managed_agents_from_evidence", lambda names: payload)
        result = runner.invoke(app, ["gateway", "agents", "recover", "bot1", "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["count"] == 1

    def test_no_evidence(self, monkeypatch):
        payload = {"recovered": [], "already_present": [], "no_evidence": ["bot1"], "count": 0}
        monkeypatch.setattr(_gw_agents, "_recover_managed_agents_from_evidence", lambda names: payload)
        result = runner.invoke(app, ["gateway", "agents", "recover", "bot1", "--json"])
        assert result.exit_code != 0

    def test_error(self, monkeypatch):
        monkeypatch.setattr(
            _gw_agents,
            "_recover_managed_agents_from_evidence",
            lambda names: (_ for _ in ()).throw(ValueError("broken")),
        )
        result = runner.invoke(app, ["gateway", "agents", "recover", "bot1"])
        assert result.exit_code != 0


class TestAgentsRemoveCommand:
    def test_success(self, monkeypatch):
        monkeypatch.setattr(_gw_agents, "_remove_managed_agent", lambda name: {"name": name})
        result = runner.invoke(app, ["gateway", "agents", "remove", "bot1"])
        assert result.exit_code == 0
        assert "Removed" in _strip(result.output)

    def test_not_found(self, monkeypatch):
        monkeypatch.setattr(
            _gw_agents,
            "_remove_managed_agent",
            lambda name: (_ for _ in ()).throw(LookupError("nope")),
        )
        result = runner.invoke(app, ["gateway", "agents", "remove", "nope"])
        assert result.exit_code != 0


class TestAgentsTestTextRendering:
    def test_text_output(self, monkeypatch):
        payload = {
            "target_agent": "bot1",
            "recommended_prompt": "test prompt",
            "message": {"id": "msg-1"},
        }
        monkeypatch.setattr(_gw_agents, "_send_gateway_test_to_managed_agent", lambda name, **kw: payload)
        result = runner.invoke(app, ["gateway", "agents", "test", "bot1"])
        assert result.exit_code == 0
        output = _strip(result.output)
        assert "bot1" in output
        assert "test prompt" in output


class TestAgentsMoveTextRendering:
    def test_text_with_previous(self, monkeypatch):
        payload = {
            "active_space_id": "sp-2",
            "active_space_name": "New",
            "previous_space_id": "sp-1",
            "previous_space_name": "Old",
        }
        monkeypatch.setattr(_gw_agents, "_move_managed_agent_space", lambda name, sid, **kw: payload)
        result = runner.invoke(app, ["gateway", "agents", "move", "bot1", "--space", "sp-2"])
        assert result.exit_code == 0
        output = _strip(result.output)
        assert "revert" in output.lower()


class TestAgentsDoctorTextRendering:
    def test_text_passed(self, monkeypatch):
        payload = {
            "status": "passed",
            "summary": "all good",
            "checks": [{"name": "check1", "status": "passed", "detail": "ok"}],
        }
        monkeypatch.setattr(_gw_agents, "_run_gateway_doctor", lambda name, **kw: payload)
        result = runner.invoke(app, ["gateway", "agents", "doctor", "bot1"])
        assert result.exit_code == 0

    def test_text_failed(self, monkeypatch):
        payload = {
            "status": "failed",
            "summary": "problem",
            "checks": [{"name": "check1", "status": "failed", "detail": "bad"}],
        }
        monkeypatch.setattr(_gw_agents, "_run_gateway_doctor", lambda name, **kw: payload)
        result = runner.invoke(app, ["gateway", "agents", "doctor", "bot1"])
        assert result.exit_code == 0


class TestAgentsArchiveRestoreTextRendering:
    def test_archive_text_multiple(self, monkeypatch):
        monkeypatch.setattr(_gw_agents, "_archive_managed_agent", lambda name, **kw: {"name": name})
        result = runner.invoke(app, ["gateway", "agents", "archive", "bot1", "bot2"])
        assert result.exit_code == 0
        output = _strip(result.output)
        assert "bot1" in output
        assert "bot2" in output

    def test_restore_text_multiple(self, monkeypatch):
        monkeypatch.setattr(
            _gw_agents, "_restore_managed_agent", lambda name: {"name": name, "desired_state": "stopped"}
        )
        result = runner.invoke(app, ["gateway", "agents", "restore", "bot1", "bot2"])
        assert result.exit_code == 0

    def test_archive_mixed_results(self, monkeypatch):
        call_count = {"n": 0}

        def _archive(name, **kw):
            call_count["n"] += 1
            if name == "nope":
                raise LookupError("not found")
            return {"name": name}

        monkeypatch.setattr(_gw_agents, "_archive_managed_agent", _archive)
        result = runner.invoke(app, ["gateway", "agents", "archive", "bot1", "nope"])
        assert result.exit_code == 0  # partial success
        output = _strip(result.output)
        assert "bot1" in output
        assert "not found" in output.lower()


class TestAgentsRecoverTextRendering:
    def test_text_recovered(self, monkeypatch):
        payload = {
            "recovered": [{"name": "bot1", "agent_id": "a1"}],
            "already_present": [],
            "no_evidence": [],
            "count": 1,
        }
        monkeypatch.setattr(_gw_agents, "_recover_managed_agents_from_evidence", lambda names: payload)
        result = runner.invoke(app, ["gateway", "agents", "recover", "bot1"])
        assert result.exit_code == 0
        assert "Recovered" in _strip(result.output)

    def test_text_already_present(self, monkeypatch):
        payload = {
            "recovered": [],
            "already_present": ["bot1"],
            "no_evidence": [],
            "count": 0,
        }
        monkeypatch.setattr(_gw_agents, "_recover_managed_agents_from_evidence", lambda names: payload)
        result = runner.invoke(app, ["gateway", "agents", "recover", "bot1"])
        # exit_code 0 because already_present + no no_evidence = graceful
        assert result.exit_code == 0
        assert "Already present" in _strip(result.output)

    def test_text_no_evidence_exits_1(self, monkeypatch):
        payload = {
            "recovered": [],
            "already_present": [],
            "no_evidence": ["bot1"],
            "count": 0,
        }
        monkeypatch.setattr(_gw_agents, "_recover_managed_agents_from_evidence", lambda names: payload)
        result = runner.invoke(app, ["gateway", "agents", "recover", "bot1"])
        assert result.exit_code != 0


class TestAgentsListTextRendering:
    def test_text_with_hidden_hint(self, monkeypatch):
        payload = {
            "agents": [{"name": "bot1", "runtime_type": "echo", "template_id": "echo_test"}],
            "summary": {
                "managed_agents": 1,
                "live_agents": 0,
                "on_demand_agents": 0,
                "inbox_agents": 0,
                "archived_agents": 2,
                "hidden_agents": 1,
                "system_agents": 1,
            },
        }
        monkeypatch.setattr(_gw_agents, "_status_payload", lambda **kw: payload)
        result = runner.invoke(app, ["gateway", "agents", "list"])
        assert result.exit_code == 0
        output = _strip(result.output)
        assert "--all" in output or "archived" in output.lower()

    def test_archived_only_filter(self, monkeypatch):
        payload = {
            "agents": [
                {"name": "bot1", "lifecycle_phase": "active", "runtime_type": "echo", "template_id": "echo_test"},
                {"name": "bot2", "lifecycle_phase": "archived", "runtime_type": "echo", "template_id": "echo_test"},
            ],
            "summary": {
                "managed_agents": 2,
                "live_agents": 0,
                "on_demand_agents": 0,
                "inbox_agents": 0,
                "archived_agents": 1,
                "hidden_agents": 0,
                "system_agents": 0,
            },
        }
        monkeypatch.setattr(_gw_agents, "_status_payload", lambda **kw: payload)
        result = runner.invoke(app, ["gateway", "agents", "list", "--archived", "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["count"] == 1
        assert data["agents"][0]["name"] == "bot2"


class TestAgentsAddSpaceResolution:
    def test_add_with_space_cached(self, monkeypatch):
        monkeypatch.setattr(_gw_agents, "_resolve_space_via_cache", lambda v: "sp-resolved")
        monkeypatch.setattr(_gw_agents, "_resolve_system_prompt_input", lambda **kw: None)
        monkeypatch.setattr(
            _gw_agents,
            "_register_managed_agent",
            lambda **kw: {
                "name": "bot1",
                "desired_state": "running",
                "token_file": "/tmp/t",
                "template_label": "Echo",
                "asset_type_label": "test",
                "timeout_seconds": None,
            },
        )
        result = runner.invoke(
            app,
            ["gateway", "agents", "add", "bot1", "--space", "work", "--json"],
        )
        assert result.exit_code == 0

    def test_add_with_space_cache_miss(self, monkeypatch):
        monkeypatch.setattr(_gw_agents, "_resolve_space_via_cache", lambda v: None)
        monkeypatch.setattr(_gw_agents, "_load_gateway_user_client", lambda: MagicMock())
        monkeypatch.setattr(_gw_agents, "resolve_space_id", lambda client, explicit: "sp-from-api")
        monkeypatch.setattr(_gw_agents, "_resolve_system_prompt_input", lambda **kw: None)
        monkeypatch.setattr(
            _gw_agents,
            "_register_managed_agent",
            lambda **kw: {
                "name": "bot1",
                "desired_state": "running",
                "token_file": "/tmp/t",
                "template_label": "Echo",
                "asset_type_label": "test",
                "timeout_seconds": None,
            },
        )
        result = runner.invoke(
            app,
            ["gateway", "agents", "add", "bot1", "--space", "work", "--json"],
        )
        assert result.exit_code == 0

    def test_add_space_resolution_fails(self, monkeypatch):
        monkeypatch.setattr(_gw_agents, "_resolve_space_via_cache", lambda v: None)
        monkeypatch.setattr(_gw_agents, "_load_gateway_user_client", lambda: MagicMock())

        def _raise(client, explicit):
            raise RuntimeError("space API down")

        monkeypatch.setattr(_gw_agents, "resolve_space_id", _raise)
        monkeypatch.setattr(_gw_agents, "_resolve_system_prompt_input", lambda **kw: None)
        result = runner.invoke(
            app,
            ["gateway", "agents", "add", "bot1", "--space", "bad-space"],
        )
        assert result.exit_code != 0


class TestAgentsUpdateTextRendering:
    def test_text_with_timeout(self, monkeypatch):
        entry = {
            "name": "echo1",
            "template_label": "Echo Test",
            "runtime_type": "echo",
            "desired_state": "running",
            "timeout_seconds": 60,
        }
        monkeypatch.setattr(_gw_agents, "_update_managed_agent", lambda **kw: entry)
        result = runner.invoke(app, ["gateway", "agents", "update", "echo1"])
        assert result.exit_code == 0
        output = _strip(result.output)
        assert "timeout" in output.lower()
        assert "60" in output

    def test_update_with_system_prompt_file(self, monkeypatch, tmp_path):
        prompt_file = tmp_path / "prompt.txt"
        prompt_file.write_text("You are a coder.")
        entry = {
            "name": "echo1",
            "template_label": "Echo Test",
            "runtime_type": "echo",
            "desired_state": "running",
            "timeout_seconds": None,
        }
        monkeypatch.setattr(_gw_agents, "_update_managed_agent", lambda **kw: entry)
        result = runner.invoke(
            app,
            ["gateway", "agents", "update", "echo1", "--system-prompt-file", str(prompt_file), "--json"],
        )
        assert result.exit_code == 0


class TestAgentsShowTextRendering:
    def test_text_output(self, monkeypatch):
        detail = {
            "agent": {
                "name": "bot1",
                "template_id": "echo_test",
                "system_prompt": "Be helpful",
            },
            "recent_activity": [],
        }
        monkeypatch.setattr(_gw_agents, "_agent_detail_payload", lambda name, **kw: detail)
        result = runner.invoke(app, ["gateway", "agents", "show", "bot1"])
        assert result.exit_code == 0


def test_smoke_echo_returns_echo_response(monkeypatch, tmp_path):
    monkeypatch.setenv("AX_OFFLINE", "1")
    monkeypatch.setenv("AX_CONFIG_DIR", str(tmp_path / "ax_config"))
    entry = _make_registry(tmp_path, name="echo-bot", runtime_type="echo")
    gateway_core.save_gateway_registry({"agents": [entry]})
    gateway_core.save_gateway_session({"token": "offline", "base_url": "http://localhost:8765", "space_id": "s1"})

    from typer.testing import CliRunner

    from ax_cli.main import app

    runner = CliRunner()
    result = runner.invoke(app, ["gateway", "agents", "smoke", "echo-bot", "--message", "hello"])
    assert result.exit_code == 0
    assert "Echo: hello" in result.output


def test_smoke_echo_uses_recommended_test_message(monkeypatch, tmp_path):
    """Without --message, smoke uses the template's recommended_test_message."""
    monkeypatch.setenv("AX_OFFLINE", "1")
    monkeypatch.setenv("AX_CONFIG_DIR", str(tmp_path / "ax_config"))
    entry = _make_registry(tmp_path, name="echo-bot", runtime_type="echo")
    entry["template_id"] = "echo_test"  # recommended_test_message = "gateway test ping"
    gateway_core.save_gateway_registry({"agents": [entry]})
    gateway_core.save_gateway_session({"token": "offline", "base_url": "http://localhost:8765", "space_id": "s1"})

    from typer.testing import CliRunner

    from ax_cli.main import app

    runner = CliRunner()
    result = runner.invoke(app, ["gateway", "agents", "smoke", "echo-bot"])
    assert result.exit_code == 0
    assert "Echo: gateway test ping" in result.output


def test_smoke_channel_not_connected_when_no_subscriber(monkeypatch, tmp_path):
    monkeypatch.setenv("AX_OFFLINE", "1")
    monkeypatch.setenv("AX_CONFIG_DIR", str(tmp_path / "ax_config"))
    entry = _make_registry(tmp_path, name="my-channel", runtime_type="claude_code_channel")
    gateway_core.save_gateway_registry({"agents": [entry]})
    gateway_core.save_gateway_session({"token": "offline", "base_url": "http://localhost:8765", "space_id": "s1"})

    # Gateway returns empty delivered_to — agent not subscribed

    fake_resp = MagicMock(spec=httpx.Response)
    fake_resp.status_code = 201
    fake_resp.json.return_value = {"id": "msg-1", "delivered_to": [], "message": {}}
    fake_resp.raise_for_status = MagicMock()

    from typer.testing import CliRunner

    from ax_cli.main import app

    runner = CliRunner()
    with patch("httpx.post", return_value=fake_resp):
        result = runner.invoke(app, ["gateway", "agents", "smoke", "my-channel"])
    assert result.exit_code == 1
    assert "not connected" in result.output


def test_smoke_channel_shows_reply_from_log(monkeypatch, tmp_path):
    monkeypatch.setenv("AX_OFFLINE", "1")
    monkeypatch.setenv("AX_CONFIG_DIR", str(tmp_path / "ax_config"))
    entry = _make_registry(tmp_path, name="my-channel", runtime_type="claude_code_channel")
    gateway_core.save_gateway_registry({"agents": [entry]})
    gateway_core.save_gateway_session({"token": "offline", "base_url": "http://localhost:8765", "space_id": "s1"})

    fake_resp = MagicMock(spec=httpx.Response)
    fake_resp.status_code = 201
    fake_resp.json.return_value = {"id": "sent-msg-1", "delivered_to": ["my-channel"], "message": {"id": "sent-msg-1"}}
    fake_resp.raise_for_status = MagicMock()

    replies_path = tmp_path / "ax_config" / "gateway" / "offline-replies.jsonl"
    replies_path.parent.mkdir(parents=True, exist_ok=True)

    # Write the reply in a background thread after a short delay so it lands
    # AFTER the smoke command records start_pos (which happens post-send).
    def _write_reply():

        time.sleep(0.2)
        with replies_path.open("a") as f:
            f.write(json.dumps({"id": "reply-1", "content": "pong", "author": "my-channel"}) + "\n")

    t = threading.Thread(target=_write_reply, daemon=True)

    from typer.testing import CliRunner

    from ax_cli.main import app

    runner = CliRunner()

    def start_reply_thread(*args, **kwargs):
        t.start()
        return fake_resp

    with patch("httpx.post", side_effect=start_reply_thread):
        with patch.object(_gw_agents, "_offline_replies_path", return_value=replies_path):
            result = runner.invoke(app, ["gateway", "agents", "smoke", "my-channel", "--message", "ping"])
    assert result.exit_code == 0
    assert "pong" in result.output


# ── agents remove: workdir config warning (#219) ──────────────────────────


def test_remove_agent_warns_when_workdir_config_references_removed_agent(monkeypatch, tmp_path):
    _isolate_gateway_paths(monkeypatch, tmp_path)
    workdir = tmp_path / "myworkdir"
    ax_dir = workdir / ".ax"
    ax_dir.mkdir(parents=True)
    (ax_dir / "config.toml").write_text('agent_name = "warn-bot"\n')
    entry = {
        "name": "warn-bot",
        "agent_id": "agent-warn",
        "space_id": "space-x",
        "template_id": "pass_through",
        "runtime_type": "pass_through",
        "workdir": str(workdir),
    }
    gateway_core.save_gateway_registry({"agents": [entry]})
    result = runner.invoke(app, ["gateway", "agents", "remove", "warn-bot"])
    assert result.exit_code == 0
    assert "Warning" in result.output
    assert "warn-bot" in result.output
    assert (ax_dir / "config.toml").exists()


def test_remove_agent_no_warn_when_workdir_config_has_different_agent(monkeypatch, tmp_path):
    _isolate_gateway_paths(monkeypatch, tmp_path)
    workdir = tmp_path / "myworkdir"
    ax_dir = workdir / ".ax"
    ax_dir.mkdir(parents=True)
    (ax_dir / "config.toml").write_text('agent_name = "other-bot"\n')
    entry = {
        "name": "no-warn-bot",
        "agent_id": "agent-no-warn",
        "space_id": "space-x",
        "template_id": "pass_through",
        "runtime_type": "pass_through",
        "workdir": str(workdir),
    }
    gateway_core.save_gateway_registry({"agents": [entry]})
    result = runner.invoke(app, ["gateway", "agents", "remove", "no-warn-bot"])
    assert result.exit_code == 0
    assert "Warning" not in result.output


def test_remove_agent_no_warn_when_no_workdir(monkeypatch, tmp_path):
    _isolate_gateway_paths(monkeypatch, tmp_path)
    entry = {
        "name": "no-workdir-bot",
        "agent_id": "agent-no-workdir",
        "space_id": "space-x",
        "template_id": "pass_through",
        "runtime_type": "pass_through",
    }
    gateway_core.save_gateway_registry({"agents": [entry]})
    result = runner.invoke(app, ["gateway", "agents", "remove", "no-workdir-bot"])
    assert result.exit_code == 0
    assert "Warning" not in result.output


# ---------------------------------------------------------------------------
# upstream_existence doctor check (#245)
# ---------------------------------------------------------------------------


def _seed_doctor_agent(monkeypatch, tmp_path):
    config_dir = tmp_path / "config"
    monkeypatch.setenv("AX_CONFIG_DIR", str(config_dir))
    gateway_core.save_gateway_session(
        {"token": "axp_u_test.token", "base_url": "https://paxai.app", "space_id": "space-1", "username": "codex"}
    )
    token_file = tmp_path / "agent.token"
    token_file.write_text("axp_a_agent.secret")
    registry = gateway_core.load_gateway_registry()
    registry["agents"] = [
        {
            "name": "scout",
            "agent_id": "agent-scout",
            "space_id": "space-1",
            "base_url": "https://paxai.app",
            "runtime_type": "inbox",
            "template_id": "inbox",
            "desired_state": "running",
            "effective_state": "stopped",
            "token_file": str(token_file),
            "transport": "gateway",
            "credential_source": "gateway",
        }
    ]
    gateway_core.save_gateway_registry(registry)
    return registry


def test_doctor_upstream_existence_passes_when_agent_found(monkeypatch, tmp_path):
    _seed_doctor_agent(monkeypatch, tmp_path)

    class _FoundClient:
        def get_agent(self, identifier):
            return {"id": identifier, "name": "scout"}

    from ax_cli.commands import gateway_diagnostics as _gw_diag

    monkeypatch.setattr(_gw_diag, "_load_gateway_user_client", lambda: _FoundClient())

    result = runner.invoke(app, ["gateway", "agents", "doctor", "scout", "--json"])
    assert result.exit_code == 0, result.output
    checks = {c["name"]: c for c in json.loads(result.stdout)["checks"]}
    assert "upstream_existence" in checks
    assert checks["upstream_existence"]["status"] == "passed"


def test_doctor_upstream_existence_fails_on_404(monkeypatch, tmp_path):
    _seed_doctor_agent(monkeypatch, tmp_path)

    class _NotFoundClient:
        def get_agent(self, identifier):
            req = httpx.Request("GET", f"https://paxai.app/api/v1/agents/manage/{identifier}")
            resp = httpx.Response(404, request=req)
            raise httpx.HTTPStatusError("404", request=req, response=resp)

    from ax_cli.commands import gateway_diagnostics as _gw_diag

    monkeypatch.setattr(_gw_diag, "_load_gateway_user_client", lambda: _NotFoundClient())

    result = runner.invoke(app, ["gateway", "agents", "doctor", "scout", "--json"])
    assert result.exit_code == 0, result.output
    checks = {c["name"]: c for c in json.loads(result.stdout)["checks"]}
    assert checks["upstream_existence"]["status"] == "failed"
    assert "404" in checks["upstream_existence"]["detail"]
    assert "remove" in checks["upstream_existence"]["detail"]


def test_doctor_upstream_existence_warns_on_connection_error(monkeypatch, tmp_path):
    _seed_doctor_agent(monkeypatch, tmp_path)

    class _UnreachableClient:
        def get_agent(self, identifier):
            raise ConnectionError("network unreachable")

    from ax_cli.commands import gateway_diagnostics as _gw_diag

    monkeypatch.setattr(_gw_diag, "_load_gateway_user_client", lambda: _UnreachableClient())

    result = runner.invoke(app, ["gateway", "agents", "doctor", "scout", "--json"])
    assert result.exit_code == 0, result.output
    checks = {c["name"]: c for c in json.loads(result.stdout)["checks"]}
    assert checks["upstream_existence"]["status"] == "warning"


def test_doctor_upstream_existence_skipped_in_offline_mode(monkeypatch, tmp_path):
    _seed_doctor_agent(monkeypatch, tmp_path)
    monkeypatch.setenv("AX_OFFLINE", "1")

    result = runner.invoke(app, ["gateway", "agents", "doctor", "scout", "--json"])
    assert result.exit_code == 0, result.output
    check_names = [c["name"] for c in json.loads(result.stdout)["checks"]]
    assert "upstream_existence" not in check_names


def test_doctor_upstream_existence_warns_on_non_404_http_error(monkeypatch, tmp_path):
    _seed_doctor_agent(monkeypatch, tmp_path)

    class _ServerErrorClient:
        def get_agent(self, identifier):
            req = httpx.Request("GET", f"https://paxai.app/api/v1/agents/manage/{identifier}")
            resp = httpx.Response(500, request=req)
            raise httpx.HTTPStatusError("500", request=req, response=resp)

    from ax_cli.commands import gateway_diagnostics as _gw_diag

    monkeypatch.setattr(_gw_diag, "_load_gateway_user_client", lambda: _ServerErrorClient())

    result = runner.invoke(app, ["gateway", "agents", "doctor", "scout", "--json"])
    assert result.exit_code == 0, result.output
    checks = {c["name"]: c for c in json.loads(result.stdout)["checks"]}
    assert checks["upstream_existence"]["status"] == "warning"
    assert "500" in checks["upstream_existence"]["detail"]


def test_doctor_upstream_existence_warns_when_no_agent_id(monkeypatch, tmp_path):
    config_dir = tmp_path / "config"
    monkeypatch.setenv("AX_CONFIG_DIR", str(config_dir))
    gateway_core.save_gateway_session(
        {"token": "axp_u_test.token", "base_url": "https://paxai.app", "space_id": "space-1", "username": "codex"}
    )
    token_file = tmp_path / "agent.token"
    token_file.write_text("axp_a_agent.secret")
    registry = gateway_core.load_gateway_registry()
    registry["agents"] = [
        {
            "name": "orphan",
            "space_id": "space-1",
            "base_url": "https://paxai.app",
            "runtime_type": "inbox",
            "template_id": "inbox",
            "desired_state": "running",
            "effective_state": "stopped",
            "token_file": str(token_file),
            "transport": "gateway",
            "credential_source": "gateway",
        }
    ]
    gateway_core.save_gateway_registry(registry)

    result = runner.invoke(app, ["gateway", "agents", "doctor", "orphan", "--json"])
    assert result.exit_code == 0, result.output
    checks = {c["name"]: c for c in json.loads(result.stdout)["checks"]}
    assert checks["upstream_existence"]["status"] == "warning"
    assert "agent_id" in checks["upstream_existence"]["detail"]


# ---------------------------------------------------------------------------
# update 404 handling (#245)
# ---------------------------------------------------------------------------


def test_update_agent_404_raises_actionable_error(monkeypatch, tmp_path):
    config_dir = tmp_path / "config"
    monkeypatch.setenv("AX_CONFIG_DIR", str(config_dir))
    gateway_core.save_gateway_session(
        {"token": "axp_u_test.token", "base_url": "https://paxai.app", "space_id": "space-1", "username": "codex"}
    )
    token_file = tmp_path / "agent.token"
    token_file.write_text("axp_a_agent.secret")
    registry = gateway_core.load_gateway_registry()
    entry = {
        "name": "ghost",
        "agent_id": "agent-ghost",
        "space_id": "space-1",
        "base_url": "https://paxai.app",
        "runtime_type": "echo",
        "template_id": "echo",
        "desired_state": "running",
        "effective_state": "running",
        "token_file": str(token_file),
        "transport": "gateway",
        "credential_source": "gateway",
    }
    registry["agents"] = [entry]
    gateway_core.ensure_local_asset_binding(registry, entry, created_via="cli", auto_approve=True)
    gateway_core.ensure_gateway_identity_binding(registry, entry, session=gateway_core.load_gateway_session())
    gateway_core.save_gateway_registry(registry)

    class _NotFoundUserClient:
        def update_agent(self, identifier, **kwargs):
            req = httpx.Request("PUT", f"https://paxai.app/api/v1/agents/manage/{identifier}")
            resp = httpx.Response(404, request=req)
            raise httpx.HTTPStatusError("404", request=req, response=resp)

    monkeypatch.setattr(_gw_agents, "_load_gateway_user_client", lambda: _NotFoundUserClient())

    result = runner.invoke(app, ["gateway", "agents", "update", "ghost", "--description", "hi"])
    assert result.exit_code == 1
    assert "not found on the platform" in result.output
    assert "remove" in result.output
