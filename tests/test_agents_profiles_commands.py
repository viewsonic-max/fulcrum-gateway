"""Tests for ``ax agents profiles`` CLI commands."""

from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from ax_cli.agent_settings_profiles import RegistryLookupError
from ax_cli.main import app

runner = CliRunner()


def _flat(text: str) -> str:
    """Collapse whitespace so asserts survive Rich's console wrapping."""
    return " ".join(text.split())


def _profiles_root(tmp_path: Path) -> Path:
    d = tmp_path / "profiles" / "claude_cli"
    d.mkdir(parents=True)
    (d / "base.json").write_text(json.dumps({"permissions": {"allow": ["mcp__ax-channel__*"]}}))
    return tmp_path / "profiles"


def _mock_claude_registry(monkeypatch, workdir: Path) -> None:
    """Mock the registry so the agent resolves to workdir + client 'claude'.

    `diff`/`apply` always derive `client` from the registry now (it's a fact
    about the agent's runtime, not a pickable parameter — see
    `_resolve_client`), so any test exercising those commands needs this.
    """
    monkeypatch.setattr(
        "ax_cli.commands.agent_profiles.agent_info_from_registry",
        lambda name: {"workdir": str(workdir), "runtime_type": "claude_code_channel", "client": "claude_cli"},
    )


# ---------------------------------------------------------------------------
# list
# ---------------------------------------------------------------------------


def test_profiles_list_table_all_clients(tmp_path, monkeypatch):
    monkeypatch.setattr("ax_cli.agent_settings_profiles._PROFILES_DIR", _profiles_root(tmp_path))
    result = runner.invoke(app, ["agents", "profiles", "list"])
    assert result.exit_code == 0
    assert "base" in result.output
    assert "claude_cli" in result.output


def test_profiles_list_table_filtered(tmp_path, monkeypatch):
    monkeypatch.setattr("ax_cli.agent_settings_profiles._PROFILES_DIR", _profiles_root(tmp_path))
    result = runner.invoke(app, ["agents", "profiles", "list", "--client", "claude_cli"])
    assert result.exit_code == 0
    assert "base" in result.output
    assert "claude_cli" in result.output


def test_profiles_list_json_all_clients(tmp_path, monkeypatch):
    monkeypatch.setattr("ax_cli.agent_settings_profiles._PROFILES_DIR", _profiles_root(tmp_path))
    result = runner.invoke(app, ["agents", "profiles", "list", "--json"])
    assert result.exit_code == 0
    data = json.loads(result.output)
    assert data == {"claude_cli": ["base"]}


def test_profiles_list_json_filtered(tmp_path, monkeypatch):
    monkeypatch.setattr("ax_cli.agent_settings_profiles._PROFILES_DIR", _profiles_root(tmp_path))
    result = runner.invoke(app, ["agents", "profiles", "list", "--client", "claude_cli", "--json"])
    assert result.exit_code == 0
    data = json.loads(result.output)
    assert data == {"claude_cli": ["base"]}


def test_profiles_list_empty_client(tmp_path, monkeypatch):
    monkeypatch.setattr("ax_cli.agent_settings_profiles._PROFILES_DIR", _profiles_root(tmp_path))
    result = runner.invoke(app, ["agents", "profiles", "list", "--client", "unknown"])
    assert result.exit_code == 0
    assert "No profiles" in result.output


# ---------------------------------------------------------------------------
# apply
# ---------------------------------------------------------------------------


def test_profiles_apply_with_workdir(tmp_path, monkeypatch):
    monkeypatch.setattr("ax_cli.agent_settings_profiles._PROFILES_DIR", _profiles_root(tmp_path))
    workdir = tmp_path / "agent"
    workdir.mkdir()
    _mock_claude_registry(monkeypatch, workdir)

    result = runner.invoke(
        app,
        ["agents", "profiles", "apply", "agent-maker", "--profile", "base", "--workdir", str(workdir)],
    )
    assert result.exit_code == 0, result.output
    assert "Applied" in result.output

    settings = json.loads((workdir / ".claude" / "settings.local.json").read_text())
    assert "mcp__ax-channel__*" in settings["permissions"]["allow"]
    assert settings["_axProfiles"] == ["base"]


def test_profiles_apply_fails_when_client_cannot_be_derived(tmp_path, monkeypatch):
    monkeypatch.setattr("ax_cli.agent_settings_profiles._PROFILES_DIR", _profiles_root(tmp_path))
    workdir = tmp_path / "agent"
    workdir.mkdir()
    monkeypatch.setattr(
        "ax_cli.commands.agent_profiles.agent_info_from_registry",
        lambda name: {"workdir": str(workdir), "runtime_type": "hermes_plugin", "client": None},
    )

    result = runner.invoke(
        app, ["agents", "profiles", "apply", "agent-maker", "--profile", "base", "--workdir", str(workdir)]
    )
    assert result.exit_code == 1
    assert "hermes_plugin" in result.output
    assert "does not support profiles" in result.output


def test_profiles_apply_requires_profile(tmp_path, monkeypatch):
    monkeypatch.setattr("ax_cli.agent_settings_profiles._PROFILES_DIR", _profiles_root(tmp_path))
    workdir = tmp_path / "agent"
    workdir.mkdir()
    _mock_claude_registry(monkeypatch, workdir)

    result = runner.invoke(app, ["agents", "profiles", "apply", "agent-maker", "--workdir", str(workdir)])
    assert result.exit_code == 1
    assert "--profile" in result.output or "profile" in result.output.lower()


def test_profiles_apply_json_output(tmp_path, monkeypatch):
    monkeypatch.setattr("ax_cli.agent_settings_profiles._PROFILES_DIR", _profiles_root(tmp_path))
    workdir = tmp_path / "agent"
    workdir.mkdir()
    _mock_claude_registry(monkeypatch, workdir)

    result = runner.invoke(
        app,
        [
            "agents",
            "profiles",
            "apply",
            "my-agent",
            "--profile",
            "base",
            "--workdir",
            str(workdir),
            "--json",
        ],
    )
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert data["agent"] == "my-agent"
    assert data["profiles"] == ["base"]
    assert data["client"] == "claude_cli"
    assert data["reset"] is False


def test_profiles_apply_reset_flag(tmp_path, monkeypatch):
    monkeypatch.setattr("ax_cli.agent_settings_profiles._PROFILES_DIR", _profiles_root(tmp_path))
    workdir = tmp_path / "agent"
    workdir.mkdir()
    (workdir / ".claude").mkdir()
    (workdir / ".claude" / "settings.local.json").write_text(json.dumps({"permissions": {"allow": ["should-be-gone"]}}))
    _mock_claude_registry(monkeypatch, workdir)

    result = runner.invoke(
        app,
        [
            "agents",
            "profiles",
            "apply",
            "agent-maker",
            "--profile",
            "base",
            "--workdir",
            str(workdir),
            "--reset",
        ],
    )
    assert result.exit_code == 0, result.output

    settings = json.loads((workdir / ".claude" / "settings.local.json").read_text())
    assert "should-be-gone" not in settings["permissions"]["allow"]
    assert "mcp__ax-channel__*" in settings["permissions"]["allow"]


def test_profiles_apply_falls_back_to_gateway_registry(tmp_path, monkeypatch):
    monkeypatch.setattr("ax_cli.agent_settings_profiles._PROFILES_DIR", _profiles_root(tmp_path))
    workdir = tmp_path / "agent"
    workdir.mkdir()
    _mock_claude_registry(monkeypatch, workdir)

    result = runner.invoke(app, ["agents", "profiles", "apply", "agent-maker", "--profile", "base"])
    assert result.exit_code == 0, result.output
    assert (workdir / ".claude" / "settings.local.json").exists()


def test_profiles_apply_exits_when_agent_has_no_workdir(tmp_path, monkeypatch):
    monkeypatch.setattr("ax_cli.agent_settings_profiles._PROFILES_DIR", _profiles_root(tmp_path))
    monkeypatch.setattr(
        "ax_cli.commands.agent_profiles.agent_info_from_registry",
        lambda name: {"workdir": None, "runtime_type": "claude_code_channel", "client": "claude_cli"},
    )

    result = runner.invoke(app, ["agents", "profiles", "apply", "ghost-agent", "--profile", "base"])
    assert result.exit_code == 1
    assert "workdir" in result.output.lower() or "workdir" in (result.stderr or "").lower()


def test_profiles_apply_agent_not_found_no_daemon_hint(tmp_path, monkeypatch):
    monkeypatch.setattr("ax_cli.agent_settings_profiles._PROFILES_DIR", _profiles_root(tmp_path))
    monkeypatch.setattr("ax_cli.commands.agent_profiles.agent_info_from_registry", lambda name: None)

    result = runner.invoke(app, ["agents", "profiles", "apply", "ghost-agent", "--profile", "base"])
    assert result.exit_code == 1
    output = _flat(result.output)
    assert "not found in the local Gateway registry" in output
    assert "Is the Gateway running" not in output


def test_profiles_apply_registry_unreadable_message(tmp_path, monkeypatch):
    monkeypatch.setattr("ax_cli.agent_settings_profiles._PROFILES_DIR", _profiles_root(tmp_path))

    def _boom(name):
        raise RegistryLookupError("Could not read the Gateway registry at /tmp/registry.json: Expecting value")

    monkeypatch.setattr("ax_cli.commands.agent_profiles.agent_info_from_registry", _boom)

    result = runner.invoke(app, ["agents", "profiles", "apply", "agent-maker", "--profile", "base"])
    assert result.exit_code == 1
    output = _flat(result.output)
    assert "Could not read the Gateway registry" in output
    assert "Is the Gateway running" not in output


# ---------------------------------------------------------------------------
# diff
# ---------------------------------------------------------------------------


def test_profiles_diff_shows_additions(tmp_path, monkeypatch):
    monkeypatch.setattr("ax_cli.agent_settings_profiles._PROFILES_DIR", _profiles_root(tmp_path))
    workdir = tmp_path / "agent"
    workdir.mkdir()
    _mock_claude_registry(monkeypatch, workdir)

    result = runner.invoke(
        app,
        ["agents", "profiles", "diff", "agent-maker", "--profile", "base", "--workdir", str(workdir)],
    )
    assert result.exit_code == 0, result.output
    assert "mcp__ax-channel__*" in result.output


def test_profiles_diff_fails_when_client_cannot_be_derived(tmp_path, monkeypatch):
    monkeypatch.setattr("ax_cli.agent_settings_profiles._PROFILES_DIR", _profiles_root(tmp_path))
    workdir = tmp_path / "agent"
    workdir.mkdir()
    monkeypatch.setattr(
        "ax_cli.commands.agent_profiles.agent_info_from_registry",
        lambda name: {"workdir": str(workdir), "runtime_type": "hermes_plugin", "client": None},
    )

    result = runner.invoke(
        app, ["agents", "profiles", "diff", "agent-maker", "--profile", "base", "--workdir", str(workdir)]
    )
    assert result.exit_code == 1
    assert "hermes_plugin" in result.output
    assert "does not support profiles" in result.output


def test_profiles_diff_requires_profile(tmp_path, monkeypatch):
    monkeypatch.setattr("ax_cli.agent_settings_profiles._PROFILES_DIR", _profiles_root(tmp_path))
    workdir = tmp_path / "agent"
    workdir.mkdir()
    _mock_claude_registry(monkeypatch, workdir)

    result = runner.invoke(app, ["agents", "profiles", "diff", "agent-maker", "--workdir", str(workdir)])
    assert result.exit_code == 1
    assert "--profile" in result.output or "profile" in result.output.lower()


def test_profiles_diff_json(tmp_path, monkeypatch):
    monkeypatch.setattr("ax_cli.agent_settings_profiles._PROFILES_DIR", _profiles_root(tmp_path))
    workdir = tmp_path / "agent"
    workdir.mkdir()
    _mock_claude_registry(monkeypatch, workdir)

    result = runner.invoke(
        app,
        [
            "agents",
            "profiles",
            "diff",
            "agent-maker",
            "--profile",
            "base",
            "--workdir",
            str(workdir),
            "--json",
        ],
    )
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert "permissions.allow: mcp__ax-channel__*" in data["add"]
    assert data["remove"] == []


# ---------------------------------------------------------------------------
# show
# ---------------------------------------------------------------------------


def test_profiles_show_none_applied(tmp_path, monkeypatch):
    workdir = tmp_path / "agent"
    workdir.mkdir()
    _mock_claude_registry(monkeypatch, workdir)

    result = runner.invoke(app, ["agents", "profiles", "show", "agent-maker"])
    assert result.exit_code == 0
    assert "no profiles" in result.output.lower()


def test_profiles_show_applied(tmp_path, monkeypatch):
    workdir = tmp_path / "agent"
    workdir.mkdir()
    (workdir / ".claude").mkdir()
    (workdir / ".claude" / "settings.local.json").write_text(json.dumps({"_axProfiles": ["base"]}))
    _mock_claude_registry(monkeypatch, workdir)

    result = runner.invoke(app, ["agents", "profiles", "show", "agent-maker"])
    assert result.exit_code == 0
    assert "base" in result.output


def test_profiles_show_agent_not_found(monkeypatch):
    monkeypatch.setattr("ax_cli.commands.agent_profiles.agent_info_from_registry", lambda name: None)
    result = runner.invoke(app, ["agents", "profiles", "show", "ghost-agent"])
    assert result.exit_code == 1
    output = _flat(result.output)
    assert "ghost-agent" in output
    assert "not found in the local Gateway registry" in output
    assert "Is the Gateway running" not in output


def test_profiles_show_gateway_not_running_hint(monkeypatch):
    def _missing(name):
        raise RegistryLookupError("No local Gateway registry found at /tmp/registry.json. Is the Gateway running?")

    monkeypatch.setattr("ax_cli.commands.agent_profiles.agent_info_from_registry", _missing)
    result = runner.invoke(app, ["agents", "profiles", "show", "agent-maker"])
    assert result.exit_code == 1
    assert "Is the Gateway running" in _flat(result.output)


def test_profiles_show_unsupported_client(tmp_path, monkeypatch):
    workdir = tmp_path / "agent"
    workdir.mkdir()
    monkeypatch.setattr(
        "ax_cli.commands.agent_profiles.agent_info_from_registry",
        lambda name: {"workdir": str(workdir), "runtime_type": "hermes_plugin", "client": None},
    )
    result = runner.invoke(app, ["agents", "profiles", "show", "agent-maker"])
    assert result.exit_code == 1
    assert "hermes_plugin" in result.output
    assert "does not support profiles" in result.output
