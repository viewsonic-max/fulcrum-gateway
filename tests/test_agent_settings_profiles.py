"""Tests for ax_cli.agent_settings_profiles — business logic (no network, no daemon)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ax_cli.agent_settings_profiles import (
    SUPPORTED_CLIENTS,
    _deep_merge,
    _gateway_runtime_to_client,
    apply,
    current_profile_list,
    diff,
    list_all,
    list_available,
    resolve,
    write_model,
)

# ---------------------------------------------------------------------------
# list_available
# ---------------------------------------------------------------------------


def test_list_available_returns_base(tmp_path, monkeypatch):
    monkeypatch.setattr("ax_cli.agent_settings_profiles._PROFILES_DIR", tmp_path)
    (tmp_path / "claude_cli").mkdir()
    (tmp_path / "claude_cli" / "base.json").write_text('{"permissions": {"allow": []}}')
    (tmp_path / "claude_cli" / "extra.json").write_text('{"permissions": {"allow": []}}')
    assert list_available("claude_cli") == ["base", "extra"]


def test_list_available_unknown_client_returns_empty(tmp_path, monkeypatch):
    monkeypatch.setattr("ax_cli.agent_settings_profiles._PROFILES_DIR", tmp_path)
    assert list_available("unknown") == []


def test_list_all_returns_all_clients():
    result = list_all()
    assert "claude_cli" in result
    assert "base" in result["claude_cli"]


def test_list_all_excludes_unsupported_clients():
    """The `echo/test.json` fixture ships in the package tree for tests, but
    `echo` isn't in SUPPORTED_CLIENTS — it can never be applied, so it
    shouldn't show up in `ax agents profiles list`."""
    result = list_all()
    assert "echo" not in result


def test_list_all_empty_when_no_profiles_dir(tmp_path, monkeypatch):
    monkeypatch.setattr("ax_cli.agent_settings_profiles._PROFILES_DIR", tmp_path / "nonexistent")
    assert list_all() == {}


# ---------------------------------------------------------------------------
# SUPPORTED_CLIENTS / unsupported client guard
# ---------------------------------------------------------------------------


def test_supported_clients_includes_claude():
    assert "claude_cli" in SUPPORTED_CLIENTS


def test_supported_clients_excludes_echo():
    assert "echo" not in SUPPORTED_CLIENTS


def test_apply_raises_for_unsupported_client(tmp_path):
    with pytest.raises(ValueError, match="echo"):
        apply(["test"], "echo", tmp_path)


def test_diff_raises_for_unsupported_client(tmp_path):
    with pytest.raises(ValueError, match="echo"):
        diff(["test"], "echo", tmp_path)


# ---------------------------------------------------------------------------
# _gateway_runtime_to_client
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("runtime_type", ["claude_code_channel", "sentinel_cli"])
def test_gateway_runtime_to_client_maps_claude_backed_runtimes(runtime_type):
    assert _gateway_runtime_to_client(runtime_type) == "claude_cli"


def test_gateway_runtime_to_client_is_case_and_whitespace_insensitive():
    assert _gateway_runtime_to_client(" Claude_Code_Channel ") == "claude_cli"


@pytest.mark.parametrize(
    "runtime_type",
    [
        "hermes_plugin",  # runs Hermes's own AIAgent — no .claude/settings.local.json
        "hermes_sentinel",  # multi-vendor SDK runtime, not Claude-specific
        "ollama",
        "echo",
        "exec",
        "inbox",
        "unknown_future_runtime",
        None,
        "",
    ],
)
def test_gateway_runtime_to_client_returns_none_for_unsupported(runtime_type):
    assert _gateway_runtime_to_client(runtime_type) is None


# ---------------------------------------------------------------------------
# _deep_merge
# ---------------------------------------------------------------------------


def test_deep_merge_list_union():
    base = {"permissions": {"allow": ["a", "b"]}}
    overlay = {"permissions": {"allow": ["b", "c"]}}
    _deep_merge(base, overlay)
    assert set(base["permissions"]["allow"]) == {"a", "b", "c"}


def test_deep_merge_scalar_last_wins():
    base = {"key": "old"}
    overlay = {"key": "new"}
    _deep_merge(base, overlay)
    assert base["key"] == "new"


def test_deep_merge_new_key_added():
    base: dict = {}
    overlay = {"permissions": {"allow": ["mcp__ax-channel__*"]}}
    _deep_merge(base, overlay)
    assert base == {"permissions": {"allow": ["mcp__ax-channel__*"]}}


# ---------------------------------------------------------------------------
# resolve
# ---------------------------------------------------------------------------


def test_resolve_base_profile(tmp_path, monkeypatch):
    monkeypatch.setattr("ax_cli.agent_settings_profiles._PROFILES_DIR", tmp_path)
    (tmp_path / "claude_cli").mkdir()
    (tmp_path / "claude_cli" / "base.json").write_text(json.dumps({"permissions": {"allow": ["mcp__ax-channel__*"]}}))
    result = resolve(["base"], "claude_cli")
    assert result == {"permissions": {"allow": ["mcp__ax-channel__*"]}}


def test_resolve_missing_profile_raises(tmp_path, monkeypatch):
    monkeypatch.setattr("ax_cli.agent_settings_profiles._PROFILES_DIR", tmp_path)
    (tmp_path / "claude_cli").mkdir()
    with pytest.raises(FileNotFoundError, match="missing"):
        resolve(["missing"], "claude_cli")


def test_resolve_raises_clear_error_for_unmergeable_list(tmp_path, monkeypatch):
    """A profile fragment with a list-of-dicts (e.g. ``hooks``) can't be
    list-unioned via set() — raise a clear ValueError naming the offending
    key instead of letting an opaque TypeError escape."""
    monkeypatch.setattr("ax_cli.agent_settings_profiles._PROFILES_DIR", tmp_path)
    (tmp_path / "claude_cli").mkdir()
    (tmp_path / "claude_cli" / "base.json").write_text(json.dumps({"hooks": [{"matcher": "a"}]}))
    (tmp_path / "claude_cli" / "extra.json").write_text(json.dumps({"hooks": [{"matcher": "b"}]}))

    with pytest.raises(ValueError, match="hooks"):
        resolve(["base", "extra"], "claude_cli")


def test_resolve_multiple_profiles_merged(tmp_path, monkeypatch):
    monkeypatch.setattr("ax_cli.agent_settings_profiles._PROFILES_DIR", tmp_path)
    (tmp_path / "claude_cli").mkdir()
    (tmp_path / "claude_cli" / "base.json").write_text(json.dumps({"permissions": {"allow": ["mcp__ax-channel__*"]}}))
    (tmp_path / "claude_cli" / "extra.json").write_text(json.dumps({"permissions": {"allow": ["Bash(echo:*)"]}}))
    result = resolve(["base", "extra"], "claude_cli")
    assert set(result["permissions"]["allow"]) == {"mcp__ax-channel__*", "Bash(echo:*)"}


# ---------------------------------------------------------------------------
# apply
# ---------------------------------------------------------------------------


def _write_settings(workdir: Path, content: dict) -> None:
    (workdir / ".claude").mkdir(parents=True, exist_ok=True)
    (workdir / ".claude" / "settings.local.json").write_text(json.dumps(content))


def _read_settings(workdir: Path) -> dict:
    return json.loads((workdir / ".claude" / "settings.local.json").read_text())


def _base_profile_dir(tmp_path: Path) -> Path:
    d = tmp_path / "profiles" / "claude_cli"
    d.mkdir(parents=True)
    (d / "base.json").write_text(json.dumps({"permissions": {"allow": ["mcp__ax-channel__*"]}}))
    return tmp_path / "profiles"


def test_apply_creates_settings_file(tmp_path, monkeypatch):
    profiles_root = _base_profile_dir(tmp_path)
    monkeypatch.setattr("ax_cli.agent_settings_profiles._PROFILES_DIR", profiles_root)
    workdir = tmp_path / "agent"
    workdir.mkdir()

    written = apply(["base"], "claude_cli", workdir)

    assert written == workdir / ".claude" / "settings.local.json"
    result = _read_settings(workdir)
    assert result["permissions"]["allow"] == ["mcp__ax-channel__*"]
    assert result["_axProfiles"] == ["base"]


def test_apply_merges_into_existing(tmp_path, monkeypatch):
    profiles_root = _base_profile_dir(tmp_path)
    monkeypatch.setattr("ax_cli.agent_settings_profiles._PROFILES_DIR", profiles_root)
    workdir = tmp_path / "agent"
    workdir.mkdir()
    _write_settings(workdir, {"enabledMcpjsonServers": ["ax-channel"], "permissions": {"allow": ["existing"]}})

    apply(["base"], "claude_cli", workdir)

    result = _read_settings(workdir)
    assert "existing" in result["permissions"]["allow"]
    assert "mcp__ax-channel__*" in result["permissions"]["allow"]
    assert result["enabledMcpjsonServers"] == ["ax-channel"]


def test_apply_merges_lists_outside_permissions_across_profiles(tmp_path, monkeypatch):
    """_deep_merge is generic — it unions *any* list it finds, not just
    permissions.allow. Profile fragments can also carry MCP server enablement
    (enabledMcpjsonServers) and deny rules (permissions.deny); applying two
    such fragments should union each of those lists too, the same way
    test_resolve_multiple_profiles_merged proves it for permissions.allow."""
    d = tmp_path / "profiles" / "claude_cli"
    d.mkdir(parents=True)
    (d / "base.json").write_text(
        json.dumps(
            {
                "enabledMcpjsonServers": ["ax-channel"],
                "permissions": {"allow": ["mcp__ax-channel__*"], "deny": ["Bash(rm:*)"]},
            }
        )
    )
    (d / "extra.json").write_text(
        json.dumps(
            {
                "enabledMcpjsonServers": ["other-channel"],
                "permissions": {"allow": ["Bash(echo:*)"], "deny": ["Bash(curl:*)"]},
            }
        )
    )
    monkeypatch.setattr("ax_cli.agent_settings_profiles._PROFILES_DIR", tmp_path / "profiles")
    workdir = tmp_path / "agent"
    workdir.mkdir()

    apply(["base", "extra"], "claude_cli", workdir)

    result = _read_settings(workdir)
    assert set(result["enabledMcpjsonServers"]) == {"ax-channel", "other-channel"}
    assert set(result["permissions"]["allow"]) == {"mcp__ax-channel__*", "Bash(echo:*)"}
    assert set(result["permissions"]["deny"]) == {"Bash(rm:*)", "Bash(curl:*)"}


def test_apply_reset_replaces_existing(tmp_path, monkeypatch):
    profiles_root = _base_profile_dir(tmp_path)
    monkeypatch.setattr("ax_cli.agent_settings_profiles._PROFILES_DIR", profiles_root)
    workdir = tmp_path / "agent"
    workdir.mkdir()
    _write_settings(workdir, {"permissions": {"allow": ["should-be-gone"]}, "other": "value"})

    apply(["base"], "claude_cli", workdir, reset=True)

    result = _read_settings(workdir)
    assert result["permissions"]["allow"] == ["mcp__ax-channel__*"]
    assert "other" not in result
    assert "should-be-gone" not in result["permissions"]["allow"]


def test_apply_raises_on_unparseable_existing_settings(tmp_path, monkeypatch):
    """An existing-but-invalid settings.local.json must not be silently
    treated as empty and overwritten — fail closed instead."""
    profiles_root = _base_profile_dir(tmp_path)
    monkeypatch.setattr("ax_cli.agent_settings_profiles._PROFILES_DIR", profiles_root)
    workdir = tmp_path / "agent"
    (workdir / ".claude").mkdir(parents=True)
    settings_path = workdir / ".claude" / "settings.local.json"
    settings_path.write_text("{not valid json")

    with pytest.raises(ValueError, match="settings.local.json"):
        apply(["base"], "claude_cli", workdir)

    # File must be untouched — no silent overwrite.
    assert settings_path.read_text() == "{not valid json"


def test_diff_raises_on_unparseable_existing_settings(tmp_path, monkeypatch):
    profiles_root = _base_profile_dir(tmp_path)
    monkeypatch.setattr("ax_cli.agent_settings_profiles._PROFILES_DIR", profiles_root)
    workdir = tmp_path / "agent"
    (workdir / ".claude").mkdir(parents=True)
    (workdir / ".claude" / "settings.local.json").write_text("{not valid json")

    with pytest.raises(ValueError, match="settings.local.json"):
        diff(["base"], "claude_cli", workdir)


def test_apply_records_ax_profiles_key(tmp_path, monkeypatch):
    profiles_root = _base_profile_dir(tmp_path)
    monkeypatch.setattr("ax_cli.agent_settings_profiles._PROFILES_DIR", profiles_root)
    workdir = tmp_path / "agent"
    workdir.mkdir()

    apply(["base"], "claude_cli", workdir)

    assert _read_settings(workdir)["_axProfiles"] == ["base"]


# ---------------------------------------------------------------------------
# current_profile_list
# ---------------------------------------------------------------------------


def test_current_profile_list_empty_when_no_file(tmp_path):
    assert current_profile_list(tmp_path / "nodir", "claude_cli") == []


def test_current_profile_list_returns_stored_profiles(tmp_path):
    (tmp_path / ".claude").mkdir()
    (tmp_path / ".claude" / "settings.local.json").write_text(json.dumps({"_axProfiles": ["base", "extra"]}))
    assert current_profile_list(tmp_path, "claude_cli") == ["base", "extra"]


def test_current_profile_list_rejects_unsupported_client(tmp_path):
    with pytest.raises(ValueError, match="echo"):
        current_profile_list(tmp_path, "echo")


# ---------------------------------------------------------------------------
# diff
# ---------------------------------------------------------------------------


def test_diff_shows_additions(tmp_path, monkeypatch):
    profiles_root = _base_profile_dir(tmp_path)
    monkeypatch.setattr("ax_cli.agent_settings_profiles._PROFILES_DIR", profiles_root)
    workdir = tmp_path / "agent"
    workdir.mkdir()

    result = diff(["base"], "claude_cli", workdir)

    assert "permissions.allow: mcp__ax-channel__*" in result["add"]
    assert result["remove"] == []
    assert result["profiles_before"] == []
    assert result["profiles_after"] == ["base"]


def test_diff_shows_removals_on_reset(tmp_path, monkeypatch):
    profiles_root = _base_profile_dir(tmp_path)
    monkeypatch.setattr("ax_cli.agent_settings_profiles._PROFILES_DIR", profiles_root)
    workdir = tmp_path / "agent"
    workdir.mkdir()
    _write_settings(workdir, {"permissions": {"allow": ["old-permission"]}})

    result = diff(["base"], "claude_cli", workdir, reset=True)

    assert "permissions.allow: old-permission" in result["remove"]
    assert "permissions.allow: mcp__ax-channel__*" in result["add"]


def test_diff_default_does_not_show_removals(tmp_path, monkeypatch):
    """Without --reset, apply() merges (nothing is removed), so the default
    diff must not claim existing entries will be removed."""
    profiles_root = _base_profile_dir(tmp_path)
    monkeypatch.setattr("ax_cli.agent_settings_profiles._PROFILES_DIR", profiles_root)
    workdir = tmp_path / "agent"
    workdir.mkdir()
    _write_settings(workdir, {"permissions": {"allow": ["old-permission"]}})

    result = diff(["base"], "claude_cli", workdir)

    assert result["remove"] == []
    assert "permissions.allow: mcp__ax-channel__*" in result["add"]
    assert "permissions.allow: old-permission" not in result["add"]


def test_diff_no_change_when_already_applied(tmp_path, monkeypatch):
    profiles_root = _base_profile_dir(tmp_path)
    monkeypatch.setattr("ax_cli.agent_settings_profiles._PROFILES_DIR", profiles_root)
    workdir = tmp_path / "agent"
    workdir.mkdir()
    _write_settings(workdir, {"permissions": {"allow": ["mcp__ax-channel__*"]}, "_axProfiles": ["base"]})

    result = diff(["base"], "claude_cli", workdir)

    assert result["add"] == []
    assert result["remove"] == []


def test_diff_reports_changes_outside_permissions_allow(tmp_path, monkeypatch):
    """diff() must walk the whole settings tree, not just permissions.allow —
    e.g. enabledMcpjsonServers (MCP server enablement) and permissions.deny
    are merged by apply() via the same generic _deep_merge, so a profile
    that touches them must show up in the +/- summary too."""
    profiles_root = tmp_path / "profiles"
    d = profiles_root / "claude_cli"
    d.mkdir(parents=True)
    (d / "base.json").write_text(
        json.dumps(
            {
                "enabledMcpjsonServers": ["ax-channel"],
                "permissions": {"allow": ["mcp__ax-channel__*"], "deny": ["Bash(rm:*)"]},
            }
        )
    )
    monkeypatch.setattr("ax_cli.agent_settings_profiles._PROFILES_DIR", profiles_root)
    workdir = tmp_path / "agent"
    workdir.mkdir()

    result = diff(["base"], "claude_cli", workdir)

    assert "enabledMcpjsonServers: ax-channel" in result["add"]
    assert "permissions.deny: Bash(rm:*)" in result["add"]
    assert "permissions.allow: mcp__ax-channel__*" in result["add"]


def test_diff_ignores_ax_profiles_bookkeeping_key(tmp_path, monkeypatch):
    """_axProfiles records which profiles produced the current settings — it's
    bookkeeping that apply() always overwrites, not applied-settings content,
    so switching from one profile to another shouldn't surface it as a
    spurious permission change."""
    profiles_root = _base_profile_dir(tmp_path)
    monkeypatch.setattr("ax_cli.agent_settings_profiles._PROFILES_DIR", profiles_root)
    workdir = tmp_path / "agent"
    workdir.mkdir()
    _write_settings(workdir, {"_axProfiles": ["other-profile"]})

    result = diff(["base"], "claude_cli", workdir)

    assert not any("_axProfiles" in entry for entry in result["add"] + result["remove"])


# ---------------------------------------------------------------------------
# agent_info_from_registry
# ---------------------------------------------------------------------------


def _gateway_registry(tmp_path: Path, monkeypatch, payload: dict | None = None) -> Path:
    """Point AX_GATEWAY_DIR at a tmp gateway dir; write registry.json if *payload* given."""
    gw_dir = tmp_path / "gateway"
    gw_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("AX_GATEWAY_DIR", str(gw_dir))
    registry_file = gw_dir / "registry.json"
    if payload is not None:
        registry_file.write_text(json.dumps(payload))
    return registry_file


def test_agent_info_from_registry_derives_client_from_runtime_type(tmp_path, monkeypatch):
    from ax_cli import agent_settings_profiles as mod

    _gateway_registry(
        tmp_path,
        monkeypatch,
        {"agents": [{"name": "agent-maker", "workdir": "/tmp/agent-maker", "runtime_type": "claude_code_channel"}]},
    )

    info = mod.agent_info_from_registry("agent-maker")

    assert info == {"workdir": "/tmp/agent-maker", "runtime_type": "claude_code_channel", "client": "claude_cli"}


def test_agent_info_from_registry_client_none_for_unsupported_runtime(tmp_path, monkeypatch):
    from ax_cli import agent_settings_profiles as mod

    _gateway_registry(
        tmp_path,
        monkeypatch,
        {"agents": [{"name": "wiki", "workdir": "/tmp/wiki", "runtime_type": "hermes_plugin"}]},
    )

    info = mod.agent_info_from_registry("wiki")

    assert info == {"workdir": "/tmp/wiki", "runtime_type": "hermes_plugin", "client": None}


def test_agent_info_from_registry_returns_none_for_unknown_agent(tmp_path, monkeypatch):
    from ax_cli import agent_settings_profiles as mod

    _gateway_registry(tmp_path, monkeypatch, {"agents": [{"name": "other", "runtime_type": "claude_code_channel"}]})

    assert mod.agent_info_from_registry("ghost") is None


def test_agent_info_from_registry_raises_when_registry_missing(tmp_path, monkeypatch):
    from ax_cli import agent_settings_profiles as mod

    _gateway_registry(tmp_path, monkeypatch, payload=None)

    with pytest.raises(mod.RegistryLookupError, match="Is the Gateway running"):
        mod.agent_info_from_registry("agent-maker")


def test_agent_info_from_registry_raises_on_malformed_registry(tmp_path, monkeypatch):
    from ax_cli import agent_settings_profiles as mod

    registry_file = _gateway_registry(tmp_path, monkeypatch)
    registry_file.write_text("{not valid json")

    with pytest.raises(mod.RegistryLookupError, match="Could not read the Gateway registry") as excinfo:
        mod.agent_info_from_registry("agent-maker")
    assert "registry.json" in str(excinfo.value)


# ---------------------------------------------------------------------------
# write_model
# ---------------------------------------------------------------------------


def test_write_model_creates_file_with_model(tmp_path):
    workdir = tmp_path / "agent"
    workdir.mkdir()

    written = write_model(workdir, "claude_cli", "claude-sonnet-4-6")

    assert written == workdir / ".claude" / "settings.local.json"
    result = json.loads(written.read_text())
    assert result["model"] == "claude-sonnet-4-6"


def test_write_model_preserves_existing_keys(tmp_path):
    workdir = tmp_path / "agent"
    _write_settings(workdir, {"permissions": {"allow": ["mcp__ax-channel__*"]}, "_axProfiles": ["base"]})

    write_model(workdir, "claude_cli", "claude-haiku-4-5")

    result = _read_settings(workdir)
    assert result["model"] == "claude-haiku-4-5"
    assert result["permissions"]["allow"] == ["mcp__ax-channel__*"]
    assert result["_axProfiles"] == ["base"]


def test_write_model_removes_key_when_none(tmp_path):
    workdir = tmp_path / "agent"
    _write_settings(workdir, {"model": "claude-sonnet-4-6", "_axProfiles": ["base"]})

    write_model(workdir, "claude_cli", None)

    result = _read_settings(workdir)
    assert "model" not in result
    assert result["_axProfiles"] == ["base"]


def test_write_model_removes_key_when_empty_string(tmp_path):
    workdir = tmp_path / "agent"
    _write_settings(workdir, {"model": "claude-sonnet-4-6"})

    write_model(workdir, "claude_cli", "")

    result = _read_settings(workdir)
    assert "model" not in result


def test_write_model_creates_parent_dirs(tmp_path):
    workdir = tmp_path / "deep" / "agent"

    write_model(workdir, "claude_cli", "claude-opus-4-8")

    result = json.loads((workdir / ".claude" / "settings.local.json").read_text())
    assert result["model"] == "claude-opus-4-8"


def test_write_model_rejects_unsupported_client(tmp_path):
    with pytest.raises(ValueError, match="not supported"):
        write_model(tmp_path, "hermes_plugin", "some-model")
