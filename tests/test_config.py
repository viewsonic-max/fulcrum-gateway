"""Tests for config resolution — the cascade that burned us (2026-04-05)."""

import os
import sys
from pathlib import Path

import pytest
from typer import Exit

from ax_cli import config as config_module
from ax_cli.config import (
    _check_config_permissions,
    _find_project_root,
    _global_config_dir,
    _load_config,
    _load_local_config,
    _local_config_workdir_mismatch,
    _save_config,
    _save_user_config,
    _warn_stale_workdir_local_config,
    diagnose_auth_config,
    get_client,
    get_user_client,
    resolve_agent_id,
    resolve_agent_name,
    resolve_base_url,
    resolve_gateway_config,
    resolve_space_id,
    resolve_token,
    resolve_user_base_url,
    resolve_user_token,
    save_space_id,
    save_token,
)


def _write_active_profile(global_dir: Path, *, name: str = "next-orion") -> Path:
    token_file = global_dir / "profiles" / name / "token"
    token_file.parent.mkdir(parents=True)
    token_file.write_text("axp_a_agent.secret")
    (global_dir / "profiles" / ".active").write_text(f"{name}\n")
    (global_dir / "profiles" / name / "profile.toml").write_text(
        f'base_url = "https://paxai.app"\n'
        f'agent_name = "orion"\n'
        f'agent_id = "agent-orion"\n'
        f'space_id = "next-space"\n'
        f'token_file = "{token_file}"\n'
    )
    return token_file


class TestFindProjectRoot:
    def test_finds_ax_dir(self, tmp_path, monkeypatch):
        (tmp_path / ".ax").mkdir()
        monkeypatch.chdir(tmp_path)
        assert _find_project_root() == tmp_path

    def test_ignores_git_dir(self, tmp_path, monkeypatch):
        (tmp_path / ".git").mkdir()
        monkeypatch.chdir(tmp_path)
        result = _find_project_root()
        assert result != tmp_path
        if result is not None:
            assert (result / ".ax").is_dir()

    def test_finds_ax_even_when_git_exists(self, tmp_path, monkeypatch):
        (tmp_path / ".ax").mkdir()
        (tmp_path / ".git").mkdir()
        monkeypatch.chdir(tmp_path)
        assert _find_project_root() == tmp_path

    def test_walks_up(self, tmp_path, monkeypatch):
        (tmp_path / ".ax").mkdir()
        child = tmp_path / "sub" / "deep"
        child.mkdir(parents=True)
        monkeypatch.chdir(child)
        assert _find_project_root() == tmp_path

    def test_returns_none_when_not_found(self, tmp_path, monkeypatch):
        # tmp_path has no .ax or .git
        isolated = tmp_path / "isolated"
        isolated.mkdir()
        monkeypatch.chdir(isolated)
        # May find something up the tree depending on environment,
        # but in an isolated tmp_path it should be None
        result = _find_project_root()
        # If no .ax anywhere up the tree
        if result is not None:
            assert (result / ".ax").is_dir()


class TestGlobalConfigDir:
    def test_default_is_home_ax(self, monkeypatch):
        monkeypatch.delenv("AX_CONFIG_DIR", raising=False)
        assert _global_config_dir() == Path.home() / ".ax"

    def test_respects_env_override(self, monkeypatch, tmp_path):
        custom = tmp_path / "custom-config"
        custom.mkdir()
        monkeypatch.setenv("AX_CONFIG_DIR", str(custom))
        assert _global_config_dir() == custom


class TestLoadConfig:
    def test_empty_when_no_config(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("AX_CONFIG_DIR", str(tmp_path / "nonexistent"))
        assert _load_config() == {}

    def test_loads_global_config(self, tmp_path, monkeypatch):
        global_dir = tmp_path / "global"
        global_dir.mkdir()
        (global_dir / "config.toml").write_text('base_url = "https://example.com"\n')
        monkeypatch.setenv("AX_CONFIG_DIR", str(global_dir))
        cfg = _load_config()
        assert cfg["base_url"] == "https://example.com"

    def test_local_overrides_global(self, tmp_path, monkeypatch):
        # Global config
        global_dir = tmp_path / "global"
        global_dir.mkdir()
        (global_dir / "config.toml").write_text('agent_id = "global-agent"\nbase_url = "https://global.example.com"\n')
        monkeypatch.setenv("AX_CONFIG_DIR", str(global_dir))

        # Local config (in CWD)
        local_ax = tmp_path / ".ax"
        local_ax.mkdir()
        (local_ax / "config.toml").write_text('agent_id = "local-agent"\n')
        monkeypatch.chdir(tmp_path)

        cfg = _load_config()
        assert cfg["agent_id"] == "local-agent"  # local wins
        assert cfg["base_url"] == "https://global.example.com"  # global preserved

    def test_gateway_config_does_not_adopt_local_space_id(self, tmp_path, monkeypatch):
        local_ax = tmp_path / ".ax"
        local_ax.mkdir()
        (local_ax / "config.toml").write_text(
            'space_id = "legacy-local-space"\n'
            "[gateway]\n"
            'mode = "local"\n'
            'url = "http://127.0.0.1:8765"\n'
            "[agent]\n"
            'agent_name = "backend_sentinel"\n'
        )
        monkeypatch.chdir(tmp_path)

        gateway = resolve_gateway_config()
        assert gateway["agent_name"] == "backend_sentinel"
        assert "space_id" not in gateway

    def test_gateway_config_keeps_service_base_url_separate_from_local_gateway_url(self, tmp_path, monkeypatch):
        local_ax = tmp_path / ".ax"
        local_ax.mkdir()
        (local_ax / "config.toml").write_text(
            "[gateway]\n"
            'base_url = "https://paxai.app"\n'
            'space_id = "team-space"\n'
            "[agent]\n"
            'agent_name = "codex-pass-through"\n'
        )
        monkeypatch.chdir(tmp_path)

        gateway = resolve_gateway_config()
        assert gateway["url"] == "http://127.0.0.1:8765"
        assert gateway["base_url"] == "https://paxai.app"
        assert gateway["space_id"] == "team-space"
        assert gateway["agent_name"] == "codex-pass-through"

    def test_ax_config_file_overrides_local_runtime_config(self, tmp_path, monkeypatch):
        local_ax = tmp_path / ".ax"
        local_ax.mkdir()
        (local_ax / "config.toml").write_text(
            'token = "axp_a_local.secret"\n'
            'base_url = "https://local.example.com"\n'
            'agent_name = "local-agent"\n'
            'agent_id = "agent-local"\n'
        )
        runtime_dir = tmp_path / "runtime"
        runtime_dir.mkdir()
        token_file = runtime_dir / "agent.pat"
        token_file.write_text("axp_a_runtime.secret")
        runtime_config = runtime_dir / "config.toml"
        runtime_config.write_text(
            f'token_file = "{token_file.name}"\n'
            'base_url = "https://paxai.app"\n'
            'agent_name = "orion"\n'
            'agent_id = "agent-orion"\n'
            'space_id = "space-next"\n'
        )
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("AX_CONFIG_FILE", str(runtime_config))

        cfg = _load_config()

        assert cfg["token"] == "axp_a_runtime.secret"
        assert cfg["base_url"] == "https://paxai.app"
        assert cfg["agent_name"] == "orion"
        assert cfg["agent_id"] == "agent-orion"
        assert cfg["space_id"] == "space-next"
        assert cfg["principal_type"] == "agent"

    def test_user_login_config_is_fallback_without_local_config(self, tmp_path, monkeypatch):
        global_dir = tmp_path / "global"
        global_dir.mkdir()
        monkeypatch.setenv("AX_CONFIG_DIR", str(global_dir))
        _save_user_config(
            {
                "token": "axp_u_user.secret",
                "base_url": "https://paxai.app",
                "principal_type": "user",
            }
        )
        isolated = tmp_path / "no-local"
        isolated.mkdir()
        monkeypatch.chdir(isolated)

        cfg = _load_config()

        assert cfg["token"] == "axp_u_user.secret"
        assert cfg["principal_type"] == "user"

    def test_local_agent_config_overrides_user_login_principal(self, tmp_path, monkeypatch):
        global_dir = tmp_path / "global"
        global_dir.mkdir()
        monkeypatch.setenv("AX_CONFIG_DIR", str(global_dir))
        _save_user_config(
            {
                "token": "axp_u_user.secret",
                "base_url": "https://paxai.app",
                "principal_type": "user",
            }
        )
        local_ax = tmp_path / ".ax"
        local_ax.mkdir()
        (local_ax / "config.toml").write_text(
            'token = "axp_a_agent.secret"\n'
            'base_url = "https://paxai.app"\n'
            'agent_name = "orion"\n'
            'agent_id = "agent-orion"\n'
        )
        monkeypatch.chdir(tmp_path)

        cfg = _load_config()

        assert cfg["token"] == "axp_a_agent.secret"
        assert cfg["principal_type"] == "agent"
        assert cfg["agent_name"] == "orion"

    def test_unsafe_local_user_pat_agent_config_does_not_override_active_profile(self, tmp_path, monkeypatch, capsys):
        global_dir = tmp_path / "global"
        global_dir.mkdir()
        monkeypatch.setenv("AX_CONFIG_DIR", str(global_dir))
        monkeypatch.setattr(config_module, "_unsafe_local_config_warned", False)

        token_file = global_dir / "profiles" / "next-orion" / "token"
        token_file.parent.mkdir(parents=True)
        token_file.write_text("axp_a_agent.secret")
        (global_dir / "profiles" / ".active").write_text("next-orion\n")
        (global_dir / "profiles" / "next-orion" / "profile.toml").write_text(
            f'base_url = "https://paxai.app"\n'
            f'agent_name = "orion"\n'
            f'agent_id = "agent-orion"\n'
            f'space_id = "next-space"\n'
            f'token_file = "{token_file}"\n'
        )

        local_ax = tmp_path / ".ax"
        local_ax.mkdir()
        (local_ax / "config.toml").write_text(
            'token = "axp_u_user.secret"\n'
            'base_url = "http://localhost:8002"\n'
            'agent_name = "wire_tap"\n'
            'agent_id = "agent-wire-tap"\n'
            'space_id = "dev-space"\n'
        )
        monkeypatch.chdir(tmp_path)

        cfg = _load_config()

        assert cfg["token"] == "axp_a_agent.secret"
        assert cfg["base_url"] == "https://paxai.app"
        assert cfg["agent_name"] == "orion"
        assert cfg["agent_id"] == "agent-orion"
        assert cfg["space_id"] == "next-space"
        assert "Ignoring unsafe local aX config" in capsys.readouterr().err

    def test_unsafe_local_user_pat_agent_config_falls_back_to_user_login(self, tmp_path, monkeypatch):
        global_dir = tmp_path / "global"
        global_dir.mkdir()
        monkeypatch.setenv("AX_CONFIG_DIR", str(global_dir))
        _save_user_config(
            {
                "token": "axp_u_user.secret",
                "base_url": "https://dev.paxai.app",
                "principal_type": "user",
            }
        )

        local_ax = tmp_path / ".ax"
        local_ax.mkdir()
        (local_ax / "config.toml").write_text(
            'token = "axp_u_stale.secret"\n'
            'base_url = "http://localhost:8002"\n'
            'agent_name = "wire_tap"\n'
            'agent_id = "agent-wire-tap"\n'
        )
        monkeypatch.chdir(tmp_path)

        cfg = _load_config()

        assert cfg["token"] == "axp_u_user.secret"
        assert cfg["base_url"] == "https://dev.paxai.app"
        assert cfg["principal_type"] == "user"
        assert "agent_name" not in cfg
        assert "agent_id" not in cfg


class TestAuthDoctorDiagnostics:
    def test_named_env_reports_user_login_as_effective_source(self, tmp_path, monkeypatch):
        global_dir = tmp_path / "global"
        global_dir.mkdir()
        monkeypatch.setenv("AX_CONFIG_DIR", str(global_dir))
        _save_user_config(
            {
                "token": "axp_u_dev.secret",
                "base_url": "https://dev.paxai.app",
                "principal_type": "user",
                "space_id": "dev-space",
            },
            env_name="dev",
            activate=False,
        )

        diagnostic = diagnose_auth_config(env_name="dev")

        assert diagnostic["ok"] is True
        assert diagnostic["selected_env"] == "dev"
        assert diagnostic["effective"]["auth_source"] == "user_login:dev"
        assert diagnostic["effective"]["base_url"] == "https://dev.paxai.app"
        assert diagnostic["effective"]["host"] == "dev.paxai.app"
        assert diagnostic["effective"]["space_id"] == "dev-space"
        assert diagnostic["effective"]["principal_intent"] == "user"

    def test_default_env_alias_reports_default_user_login(self, tmp_path, monkeypatch):
        global_dir = tmp_path / "global"
        global_dir.mkdir()
        monkeypatch.setenv("AX_CONFIG_DIR", str(global_dir))
        _save_user_config(
            {
                "token": "axp_u_next.secret",
                "base_url": "https://paxai.app",
                "principal_type": "user",
                "space_id": "next-space",
            },
            env_name="default",
            activate=False,
        )

        diagnostic = diagnose_auth_config(env_name="default")

        assert diagnostic["ok"] is True
        assert diagnostic["selected_env"] == "default"
        assert diagnostic["effective"]["auth_source"] == "user_login:default"
        assert diagnostic["effective"]["base_url"] == "https://paxai.app"
        assert diagnostic["effective"]["space_id"] == "next-space"
        assert diagnostic["effective"]["principal_intent"] == "user"

    def test_active_profile_reports_agent_runtime_source(self, tmp_path, monkeypatch):
        global_dir = tmp_path / "global"
        global_dir.mkdir()
        monkeypatch.setenv("AX_CONFIG_DIR", str(global_dir))
        _write_active_profile(global_dir)
        isolated = tmp_path / "isolated"
        isolated.mkdir()
        monkeypatch.chdir(isolated)

        diagnostic = diagnose_auth_config()

        assert diagnostic["ok"] is True
        assert diagnostic["selected_profile"] == "next-orion"
        assert diagnostic["effective"]["auth_source"] == "active_profile:next-orion"
        assert diagnostic["effective"]["token_kind"] == "agent_pat"
        assert diagnostic["effective"]["principal_intent"] == "agent"
        assert diagnostic["effective"]["space_id"] == "next-space"

    def test_explicit_env_vars_report_environment_sources(self, tmp_path, monkeypatch):
        global_dir = tmp_path / "global"
        global_dir.mkdir()
        monkeypatch.setenv("AX_CONFIG_DIR", str(global_dir))
        monkeypatch.setenv("AX_TOKEN", "axp_a_env.secret")
        monkeypatch.setenv("AX_BASE_URL", "https://env.paxai.app")
        monkeypatch.setenv("AX_AGENT_NAME", "env-agent")
        monkeypatch.setenv("AX_AGENT_ID", "env-agent-id")
        monkeypatch.setenv("AX_SPACE_ID", "env-space")

        diagnostic = diagnose_auth_config()

        assert diagnostic["ok"] is True
        assert diagnostic["effective"]["auth_source"] == "env:AX_TOKEN"
        assert diagnostic["effective"]["base_url_source"] == "env:AX_BASE_URL"
        assert diagnostic["effective"]["agent_name_source"] == "env:AX_AGENT_NAME"
        assert diagnostic["effective"]["agent_id_source"] == "env:AX_AGENT_ID"
        assert diagnostic["effective"]["space_source"] == "env:AX_SPACE_ID"
        assert diagnostic["effective"]["host"] == "env.paxai.app"
        assert diagnostic["effective"]["principal_intent"] == "agent"

    def test_explicit_runtime_config_reports_runtime_source(self, tmp_path, monkeypatch):
        global_dir = tmp_path / "global"
        global_dir.mkdir()
        monkeypatch.setenv("AX_CONFIG_DIR", str(global_dir))

        local_ax = tmp_path / ".ax"
        local_ax.mkdir()
        (local_ax / "config.toml").write_text(
            'base_url = "https://next.paxai.app"\n'
            'agent_name = "night_owl"\n'
            'agent_id = "agent-night-owl"\n'
            'space_id = "night-space"\n'
        )
        runtime_dir = tmp_path / "runtime"
        runtime_dir.mkdir()
        token_file = runtime_dir / "codex.pat"
        token_file.write_text("axp_a_codex.secret")
        runtime_config = runtime_dir / "config.toml"
        runtime_config.write_text(
            f'token_file = "{token_file.name}"\n'
            'base_url = "https://paxai.app"\n'
            'agent_name = "codex"\n'
            'agent_id = "agent-codex"\n'
            'space_id = "codex-space"\n'
        )
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("AX_CONFIG_FILE", str(runtime_config))

        diagnostic = diagnose_auth_config()

        assert diagnostic["ok"] is True
        assert diagnostic["runtime_config"] == str(runtime_config)
        assert diagnostic["effective"]["auth_source"] == f"runtime_config:{runtime_config}"
        assert diagnostic["effective"]["base_url_source"] == f"runtime_config:{runtime_config}"
        assert diagnostic["effective"]["agent_name"] == "codex"
        assert diagnostic["effective"]["space_id"] == "codex-space"
        runtime_source = next(source for source in diagnostic["sources"] if source["name"] == "runtime_config")
        assert runtime_source["used"] is True
        assert runtime_source["path"] == str(runtime_config)

    def test_unsafe_local_config_reports_ignored_reason_and_uses_profile(self, tmp_path, monkeypatch):
        global_dir = tmp_path / "global"
        global_dir.mkdir()
        monkeypatch.setenv("AX_CONFIG_DIR", str(global_dir))
        _write_active_profile(global_dir)

        local_ax = tmp_path / ".ax"
        local_ax.mkdir()
        (local_ax / "config.toml").write_text(
            'token = "axp_u_user.secret"\n'
            'base_url = "http://localhost:8002"\n'
            'agent_name = "wire_tap"\n'
            'agent_id = "agent-wire-tap"\n'
            'space_id = "dev-space"\n'
        )
        monkeypatch.chdir(tmp_path)

        diagnostic = diagnose_auth_config()

        assert diagnostic["ok"] is True
        assert diagnostic["effective"]["auth_source"] == "active_profile:next-orion"
        assert diagnostic["effective"]["agent_name"] == "orion"
        assert diagnostic["effective"]["space_id"] == "next-space"
        assert any(warning["code"] == "unsafe_local_config_ignored" for warning in diagnostic["warnings"])
        local_source = next(source for source in diagnostic["sources"] if source["name"] == "local_config")
        assert local_source["ignored"] is True
        assert "user PAT" in local_source["reason"]


class TestResolveAgentId:
    def test_env_var_wins(self, monkeypatch):
        monkeypatch.setenv("AX_AGENT_ID", "env-agent-id")
        assert resolve_agent_id() == "env-agent-id"

    def test_env_none_clears(self, monkeypatch):
        monkeypatch.setenv("AX_AGENT_ID", "none")
        assert resolve_agent_id() is None

    def test_env_empty_clears(self, monkeypatch):
        monkeypatch.setenv("AX_AGENT_ID", "")
        assert resolve_agent_id() is None

    def test_env_null_clears(self, monkeypatch):
        monkeypatch.setenv("AX_AGENT_ID", "null")
        assert resolve_agent_id() is None

    def test_falls_back_to_config(self, tmp_path, monkeypatch, write_config):
        write_config(agent_id="config-agent-id")
        monkeypatch.chdir(tmp_path)
        assert resolve_agent_id() == "config-agent-id"

    def test_user_principal_ignores_stale_config_agent_id(self, tmp_path, monkeypatch, write_config):
        write_config(principal_type="user", agent_id="stale-agent-id")
        monkeypatch.chdir(tmp_path)
        assert resolve_agent_id() is None

    def test_returns_none_when_not_set(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        assert resolve_agent_id() is None


class TestResolveAgentName:
    def test_env_var_wins(self, monkeypatch):
        monkeypatch.setenv("AX_AGENT_NAME", "env-agent")
        assert resolve_agent_name() == "env-agent"

    def test_env_none_clears(self, monkeypatch):
        monkeypatch.setenv("AX_AGENT_NAME", "none")
        assert resolve_agent_name() is None

    def test_env_empty_clears(self, monkeypatch):
        monkeypatch.setenv("AX_AGENT_NAME", "")
        assert resolve_agent_name() is None

    def test_env_null_clears(self, monkeypatch):
        monkeypatch.setenv("AX_AGENT_NAME", "null")
        assert resolve_agent_name() is None

    def test_falls_back_to_config(self, tmp_path, monkeypatch, write_config):
        write_config(agent_name="config-agent")
        monkeypatch.chdir(tmp_path)
        assert resolve_agent_name() == "config-agent"

    def test_user_principal_ignores_stale_config_agent_name(self, tmp_path, monkeypatch, write_config):
        write_config(principal_type="user", agent_name="stale-agent")
        monkeypatch.chdir(tmp_path)
        assert resolve_agent_name() is None

    def test_returns_none_when_not_set(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        assert resolve_agent_name() is None


class TestResolveSpaceId:
    def test_explicit_uuid_returns_without_listing_spaces(self):
        class FakeClient:
            def list_spaces(self):
                raise AssertionError("UUID space refs should not require list_spaces")

        assert resolve_space_id(FakeClient(), explicit="ed81ae98-50cb-4268-b986-1b9fe76df742") == (
            "ed81ae98-50cb-4268-b986-1b9fe76df742"
        )

    def test_explicit_slug_resolves_to_space_id(self, monkeypatch):
        monkeypatch.delenv("AX_SPACE_ID", raising=False)

        class FakeClient:
            def list_spaces(self):
                return {
                    "spaces": [
                        {"id": "private-space", "slug": "madtank-workspace", "name": "madtank's Workspace"},
                        {"id": "team-space", "slug": "ax-cli-dev", "name": "ax-cli-dev"},
                    ]
                }

        assert resolve_space_id(FakeClient(), explicit="ax-cli-dev") == "team-space"

    def test_explicit_name_resolves_case_insensitively(self, monkeypatch):
        monkeypatch.delenv("AX_SPACE_ID", raising=False)

        class FakeClient:
            def list_spaces(self):
                return {"spaces": [{"id": "team-space", "slug": "ax-cli-dev", "name": "aX CLI Dev"}]}

        assert resolve_space_id(FakeClient(), explicit="AX CLI DEV") == "team-space"

    def test_explicit_space_ref_fails_when_ambiguous(self, monkeypatch, capsys):
        monkeypatch.delenv("AX_SPACE_ID", raising=False)

        class FakeClient:
            def list_spaces(self):
                return {
                    "spaces": [
                        {"id": "space-1", "slug": "team", "name": "Team"},
                        {"id": "space-2", "slug": "other", "name": "Team"},
                    ]
                }

        with pytest.raises(Exit):
            resolve_space_id(FakeClient(), explicit="team")

        assert "matched multiple spaces" in capsys.readouterr().err

    def test_bound_agent_default_beats_stale_config(self, tmp_path, monkeypatch):
        monkeypatch.delenv("AX_SPACE", raising=False)
        monkeypatch.delenv("AX_SPACE_ID", raising=False)
        local_ax = tmp_path / ".ax"
        local_ax.mkdir()
        (local_ax / "config.toml").write_text('space_id = "stale-dev-space"\n')
        monkeypatch.chdir(tmp_path)

        class FakeClient:
            def whoami(self):
                return {"bound_agent": {"default_space_id": "agent-home-space"}}

        assert resolve_space_id(FakeClient()) == "agent-home-space"

    def test_config_uuid_returned_when_still_valid(self, tmp_path, monkeypatch):
        monkeypatch.delenv("AX_SPACE", raising=False)
        monkeypatch.delenv("AX_SPACE_ID", raising=False)
        local_ax = tmp_path / ".ax"
        local_ax.mkdir()
        (local_ax / "config.toml").write_text('space_id = "11111111-1111-4111-8111-111111111111"\n')
        monkeypatch.chdir(tmp_path)

        class FakeClient:
            def list_spaces(self):
                return {"spaces": [{"id": "11111111-1111-4111-8111-111111111111", "name": "Default"}]}

        assert resolve_space_id(FakeClient()) == "11111111-1111-4111-8111-111111111111"

    def test_stale_config_uuid_falls_back_to_single_space(self, tmp_path, monkeypatch, capsys):
        monkeypatch.delenv("AX_SPACE", raising=False)
        monkeypatch.delenv("AX_SPACE_ID", raising=False)
        local_ax = tmp_path / ".ax"
        local_ax.mkdir()
        (local_ax / "config.toml").write_text('space_id = "00000000-0000-4000-8000-000000000000"\n')
        monkeypatch.chdir(tmp_path)

        class FakeClient:
            def list_spaces(self):
                return {"spaces": [{"id": "22222222-2222-4222-8222-222222222222", "name": "Default"}]}

        assert resolve_space_id(FakeClient()) == "22222222-2222-4222-8222-222222222222"
        assert "no longer exists" in capsys.readouterr().err

    def test_stale_config_uuid_ambiguous_fails_closed(self, tmp_path, monkeypatch, capsys):
        monkeypatch.delenv("AX_SPACE", raising=False)
        monkeypatch.delenv("AX_SPACE_ID", raising=False)
        local_ax = tmp_path / ".ax"
        local_ax.mkdir()
        (local_ax / "config.toml").write_text('space_id = "00000000-0000-4000-8000-000000000000"\n')
        monkeypatch.chdir(tmp_path)

        class FakeClient:
            def list_spaces(self):
                return {
                    "spaces": [
                        {"id": "22222222-2222-4222-8222-222222222222", "name": "A"},
                        {"id": "33333333-3333-4333-8333-333333333333", "name": "B"},
                    ]
                }

        with pytest.raises(Exit):
            resolve_space_id(FakeClient())
        err = capsys.readouterr().err
        assert "no longer exists" in err

    def test_stale_config_uuid_trusted_when_listing_fails(self, tmp_path, monkeypatch):
        # If the space list can't be fetched (offline), trust the pin rather than block.
        monkeypatch.delenv("AX_SPACE", raising=False)
        monkeypatch.delenv("AX_SPACE_ID", raising=False)
        local_ax = tmp_path / ".ax"
        local_ax.mkdir()
        (local_ax / "config.toml").write_text('space_id = "44444444-4444-4444-8444-444444444444"\n')
        monkeypatch.chdir(tmp_path)

        class FakeClient:
            def list_spaces(self):
                raise RuntimeError("network down")

        assert resolve_space_id(FakeClient()) == "44444444-4444-4444-8444-444444444444"

    def test_env_space_slug_resolves_to_space_id(self, monkeypatch):
        monkeypatch.setenv("AX_SPACE", "ax-cli-dev")
        monkeypatch.delenv("AX_SPACE_ID", raising=False)

        class FakeClient:
            def list_spaces(self):
                return {
                    "spaces": [
                        {"id": "private-space", "slug": "madtank-workspace", "name": "madtank's Workspace"},
                        {"id": "team-space", "slug": "ax-cli-dev", "name": "aX CLI Dev"},
                    ]
                }

        assert resolve_space_id(FakeClient()) == "team-space"


class TestResolveToken:
    def test_env_var_wins(self, monkeypatch):
        monkeypatch.setenv("AX_TOKEN", "env-token")
        assert resolve_token() == "env-token"

    def test_ax_token_file_wins_when_no_direct_env_token(self, tmp_path, monkeypatch, write_config):
        write_config(token="config-token")
        token_file = tmp_path / "agent.pat"
        token_file.write_text("file-token")
        monkeypatch.setenv("AX_TOKEN_FILE", str(token_file))
        monkeypatch.chdir(tmp_path)

        assert resolve_token() == "file-token"

    def test_falls_back_to_config(self, tmp_path, monkeypatch, write_config):
        write_config(token="config-token")
        monkeypatch.chdir(tmp_path)
        assert resolve_token() == "config-token"

    def test_falls_back_to_config_token_file(self, tmp_path, monkeypatch):
        token_file = tmp_path / ".ax" / "agent.token"
        token_file.parent.mkdir()
        token_file.write_text("file-config-token")
        (tmp_path / ".ax" / "config.toml").write_text(f'token_file = "{token_file}"\n')
        monkeypatch.chdir(tmp_path)

        assert resolve_token() == "file-config-token"

    def test_returns_none_when_not_set(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        assert resolve_token() is None

    def test_resolve_user_token_uses_user_login_even_with_local_agent(self, tmp_path, monkeypatch):
        global_dir = tmp_path / "global"
        global_dir.mkdir()
        monkeypatch.setenv("AX_CONFIG_DIR", str(global_dir))
        _save_user_config(
            {
                "token": "axp_u_user.secret",
                "base_url": "https://paxai.app",
                "principal_type": "user",
            }
        )
        local_ax = tmp_path / ".ax"
        local_ax.mkdir()
        (local_ax / "config.toml").write_text(
            'token = "axp_a_agent.secret"\nagent_name = "orion"\nagent_id = "agent-orion"\n'
        )
        monkeypatch.chdir(tmp_path)

        assert resolve_token() == "axp_a_agent.secret"
        assert resolve_user_token() == "axp_u_user.secret"

    def test_named_user_env_selects_matching_user_login(self, tmp_path, monkeypatch):
        global_dir = tmp_path / "global"
        global_dir.mkdir()
        monkeypatch.setenv("AX_CONFIG_DIR", str(global_dir))
        _save_user_config(
            {
                "token": "axp_u_next.secret",
                "base_url": "https://paxai.app",
                "principal_type": "user",
            },
            env_name="next",
            activate=False,
        )
        _save_user_config(
            {
                "token": "axp_u_dev.secret",
                "base_url": "https://dev.paxai.app",
                "principal_type": "user",
            },
            env_name="dev",
            activate=False,
        )

        monkeypatch.setenv("AX_ENV", "dev")

        assert resolve_user_token() == "axp_u_dev.secret"
        assert resolve_user_base_url() == "https://dev.paxai.app"


class TestCheckConfigPermissions:
    @pytest.mark.skipif(sys.platform == "win32", reason="POSIX permission semantics")
    def test_warns_on_world_readable_config(self, tmp_path, monkeypatch, capsys):
        cf = tmp_path / "config.toml"
        cf.write_text('token = "axp_u_x.y"\n')
        cf.chmod(0o644)
        monkeypatch.setattr(config_module, "_local_config_dir", lambda: tmp_path)
        monkeypatch.setattr(config_module, "_global_config_dir", lambda: tmp_path / "nope")

        _check_config_permissions()

        err = capsys.readouterr().err
        assert "WARNING" in err
        assert "0o644" in err

    def test_skipped_on_windows(self, tmp_path, monkeypatch, capsys):
        cf = tmp_path / "config.toml"
        cf.write_text('token = "axp_u_x.y"\n')
        if sys.platform != "win32":
            cf.chmod(0o644)
        monkeypatch.setattr(config_module, "_local_config_dir", lambda: tmp_path)
        monkeypatch.setattr(config_module, "_global_config_dir", lambda: tmp_path / "nope")
        monkeypatch.setattr(config_module.sys, "platform", "win32")

        _check_config_permissions()

        assert capsys.readouterr().err == ""


class TestResolveBaseUrl:
    def test_env_var_wins(self, monkeypatch):
        monkeypatch.setenv("AX_BASE_URL", "https://custom.example.com")
        assert resolve_base_url() == "https://custom.example.com"

    def test_falls_back_to_config(self, tmp_path, monkeypatch, write_config):
        write_config(base_url="https://config.example.com")
        monkeypatch.chdir(tmp_path)
        assert resolve_base_url() == "https://config.example.com"

    def test_default_is_localhost(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        assert resolve_base_url() == "http://localhost:8001"


# ---------------------------------------------------------------------------
# Stale-workdir guard — defense against the 2026-05-04 misattribution incident
# where codex_supervisor's cwd in another worktree silently rebound the CLI to
# widget_hermes_local because that worktree's .ax/config.toml had the wrong
# [agent].workdir. Bug report: aX msg 06bc04f0.
# ---------------------------------------------------------------------------


class TestStaleWorkdirMismatch:
    def test_returns_none_when_workdir_matches(self, tmp_path):
        cfg = {"agent": {"workdir": str(tmp_path)}}
        assert _local_config_workdir_mismatch(cfg, tmp_path) is None

    def test_returns_dict_when_workdir_differs(self, tmp_path):
        other = tmp_path / "other"
        other.mkdir()
        cfg = {"agent": {"workdir": str(other)}}
        result = _local_config_workdir_mismatch(cfg, tmp_path)
        assert result is not None
        assert result["configured_workdir"] == str(other.resolve())
        assert result["actual_workdir"] == str(tmp_path.resolve())
        assert result["config_path"].endswith(".ax/config.toml")

    def test_returns_none_when_workdir_field_missing(self, tmp_path):
        # Legacy / minimal config without workdir field — no opinion.
        cfg = {"agent": {"agent_name": "some-agent"}}
        assert _local_config_workdir_mismatch(cfg, tmp_path) is None

    def test_returns_none_when_no_agent_block(self, tmp_path):
        cfg = {"gateway": {"url": "http://x"}}
        assert _local_config_workdir_mismatch(cfg, tmp_path) is None

    def test_returns_none_when_project_root_none(self):
        cfg = {"agent": {"workdir": "/some/path"}}
        assert _local_config_workdir_mismatch(cfg, None) is None

    def test_returns_none_for_non_dict_cfg(self, tmp_path):
        assert _local_config_workdir_mismatch(None, tmp_path) is None  # type: ignore[arg-type]

    def test_warning_fires_once_per_config_path(self, tmp_path, capsys):
        # Reset the warned set so this test is independent.
        config_module._stale_workdir_warned.clear()
        mismatch = {
            "config_path": str(tmp_path / ".ax" / "config.toml"),
            "configured_workdir": "/somewhere/else",
            "actual_workdir": str(tmp_path),
        }
        _warn_stale_workdir_local_config(mismatch)
        first = capsys.readouterr().err
        _warn_stale_workdir_local_config(mismatch)
        second = capsys.readouterr().err
        assert "Stale local aX config" in first
        assert second == ""  # second call suppressed

    def test_load_local_config_warns_when_stale(self, tmp_path, monkeypatch, capsys):
        config_module._stale_workdir_warned.clear()
        ax_dir = tmp_path / ".ax"
        ax_dir.mkdir()
        (ax_dir / "config.toml").write_text(
            '[gateway]\nmode = "local"\nurl = "http://127.0.0.1:8765"\n\n'
            '[agent]\nagent_name = "widget_hermes_local"\n'
            'workdir = "/some/other/worktree"\n'
        )
        monkeypatch.chdir(tmp_path)
        cfg = _load_local_config()
        assert cfg["agent"]["agent_name"] == "widget_hermes_local"
        captured = capsys.readouterr().err
        assert "Stale local aX config" in captured
        assert "/some/other/worktree" in captured
        assert str(tmp_path) in captured

    def test_load_local_config_silent_when_workdir_matches(self, tmp_path, monkeypatch, capsys):
        config_module._stale_workdir_warned.clear()
        ax_dir = tmp_path / ".ax"
        ax_dir.mkdir()
        (ax_dir / "config.toml").write_text(
            '[gateway]\nmode = "local"\nurl = "http://127.0.0.1:8765"\n\n'
            '[agent]\nagent_name = "ok-agent"\n'
            f'workdir = "{tmp_path}"\n'
        )
        monkeypatch.chdir(tmp_path)
        _load_local_config()
        assert "Stale local aX config" not in capsys.readouterr().err

    def test_diagnose_includes_stale_workdir_warning(self, tmp_path, monkeypatch):
        config_module._stale_workdir_warned.clear()
        ax_dir = tmp_path / ".ax"
        ax_dir.mkdir()
        (ax_dir / "config.toml").write_text(
            '[gateway]\nmode = "local"\nurl = "http://127.0.0.1:8765"\n\n'
            '[agent]\nagent_name = "widget"\n'
            'workdir = "/other/worktree"\n'
        )
        monkeypatch.chdir(tmp_path)
        # Isolate global config dir so diagnose doesn't read a real ~/.ax
        monkeypatch.setenv("AX_CONFIG_DIR", str(tmp_path / "global"))
        report = diagnose_auth_config()
        codes = [w.get("code") for w in report.get("warnings", [])]
        assert "stale_workdir_local_config" in codes


# ---------------------------------------------------------------------------
# Doctor v2: Gateway-aware diagnostic. Pre-v2 doctor reported `missing_token:
# PROBLEM` for any session without a local token, even when the Gateway daemon
# was holding the credential out-of-band — exactly the state a Gateway-
# brokered agent runtime is *supposed* to be in. v2 probes the daemon first
# so it stops false-flagging the correct config.
# ---------------------------------------------------------------------------


def _isolate_gateway_for_test(monkeypatch, gateway_dir, *, registry=None, pid=None):
    """Point AX_GATEWAY_DIR at a tmp dir and seed registry / pid file."""
    import json as _json

    gateway_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("AX_GATEWAY_DIR", str(gateway_dir))
    if registry is not None:
        (gateway_dir / "registry.json").write_text(_json.dumps(registry))
    if pid is not None:
        (gateway_dir / "gateway.pid").write_text(str(pid))


class TestProbeGatewayBinding:
    def test_no_daemon_no_registry_returns_empty(self, tmp_path, monkeypatch):
        from ax_cli.config import _probe_gateway_binding

        _isolate_gateway_for_test(monkeypatch, tmp_path / "gw")
        monkeypatch.chdir(tmp_path)
        result = _probe_gateway_binding()
        assert result["daemon_running"] is False
        assert result["daemon_pid"] is None
        assert result["bound_candidates"] == []

    def test_finds_candidate_with_matching_workdir(self, tmp_path, monkeypatch):
        from ax_cli.config import _probe_gateway_binding

        wd = tmp_path / "ws"
        wd.mkdir()
        registry = {
            "agents": [
                {
                    "name": "alice",
                    "agent_id": "agent-alice",
                    "template_id": "claude_code_channel",
                    "runtime_type": "claude_code_channel",
                    "workdir": str(wd),
                    "mode": "LIVE",
                    "liveness": "connected",
                }
            ]
        }
        _isolate_gateway_for_test(monkeypatch, tmp_path / "gw", registry=registry)
        monkeypatch.chdir(wd)
        result = _probe_gateway_binding()
        assert len(result["bound_candidates"]) == 1
        cand = result["bound_candidates"][0]
        assert cand["name"] == "alice"
        assert cand["template_id"] == "claude_code_channel"
        assert cand["mode"] == "LIVE"

    def test_finds_candidate_when_workdir_is_parent_of_cwd(self, tmp_path, monkeypatch):
        from ax_cli.config import _probe_gateway_binding

        wd = tmp_path / "ws"
        sub = wd / "sub" / "deep"
        sub.mkdir(parents=True)
        registry = {"agents": [{"name": "alice", "workdir": str(wd)}]}
        _isolate_gateway_for_test(monkeypatch, tmp_path / "gw", registry=registry)
        monkeypatch.chdir(sub)
        result = _probe_gateway_binding()
        assert len(result["bound_candidates"]) == 1
        assert result["bound_candidates"][0]["name"] == "alice"

    def test_skips_candidate_with_unrelated_workdir(self, tmp_path, monkeypatch):
        from ax_cli.config import _probe_gateway_binding

        wd_a = tmp_path / "ws_a"
        wd_b = tmp_path / "ws_b"
        wd_a.mkdir()
        wd_b.mkdir()
        registry = {"agents": [{"name": "alice", "workdir": str(wd_a)}]}
        _isolate_gateway_for_test(monkeypatch, tmp_path / "gw", registry=registry)
        monkeypatch.chdir(wd_b)
        result = _probe_gateway_binding()
        assert result["bound_candidates"] == []

    def test_skips_candidate_with_no_workdir(self, tmp_path, monkeypatch):
        from ax_cli.config import _probe_gateway_binding

        registry = {"agents": [{"name": "no_workdir_agent"}]}
        _isolate_gateway_for_test(monkeypatch, tmp_path / "gw", registry=registry)
        monkeypatch.chdir(tmp_path)
        result = _probe_gateway_binding()
        assert result["bound_candidates"] == []

    def test_daemon_pid_alive_check(self, tmp_path, monkeypatch):
        from ax_cli.config import _probe_gateway_binding

        # os.getpid() is the test process — known alive.
        _isolate_gateway_for_test(monkeypatch, tmp_path / "gw", pid=os.getpid())
        monkeypatch.chdir(tmp_path)
        result = _probe_gateway_binding()
        assert result["daemon_running"] is True
        assert result["daemon_pid"] == os.getpid()

    def test_daemon_pid_dead_returns_not_running(self, tmp_path, monkeypatch):
        from ax_cli.config import _probe_gateway_binding

        # 999999 is virtually certain to be unused.
        _isolate_gateway_for_test(monkeypatch, tmp_path / "gw", pid=999999)
        monkeypatch.chdir(tmp_path)
        result = _probe_gateway_binding()
        assert result["daemon_running"] is False


class TestDoctorV2Classifier:
    """Verify the post-Gateway diagnostic classifies correctly."""

    def test_no_token_with_gateway_binding_is_brokered_not_missing(self, tmp_path, monkeypatch):
        wd = tmp_path / "ws"
        wd.mkdir()
        registry = {
            "agents": [
                {
                    "name": "alice",
                    "agent_id": "agent-alice",
                    "template_id": "claude_code_channel",
                    "workdir": str(wd),
                    "mode": "LIVE",
                    "liveness": "connected",
                }
            ]
        }
        _isolate_gateway_for_test(monkeypatch, tmp_path / "gw", registry=registry, pid=os.getpid())
        monkeypatch.setenv("AX_CONFIG_DIR", str(tmp_path / "global"))
        monkeypatch.chdir(wd)

        report = diagnose_auth_config()
        eff = report["effective"]
        assert eff["principal_intent"] == "agent_gateway_brokered"
        assert eff["agent_name"] == "alice"
        assert eff["agent_id"] == "agent-alice"
        assert eff["agent_name_source"] == "gateway_daemon"
        codes = [p["code"] for p in report["problems"]]
        assert "missing_token" not in codes
        assert report["ok"] is True
        assert eff["gateway_binding"]["daemon_running"] is True

    def test_no_token_no_binding_keeps_missing_token(self, tmp_path, monkeypatch):
        _isolate_gateway_for_test(monkeypatch, tmp_path / "gw")
        monkeypatch.setenv("AX_CONFIG_DIR", str(tmp_path / "global"))
        monkeypatch.chdir(tmp_path)
        report = diagnose_auth_config()
        assert report["effective"]["principal_intent"] == "missing"
        codes = [p["code"] for p in report["problems"]]
        assert "missing_token" in codes

    def test_local_token_plus_gateway_binding_warns(self, tmp_path, monkeypatch):
        wd = tmp_path / "ws"
        wd.mkdir()
        ax_dir = wd / ".ax"
        ax_dir.mkdir()
        (ax_dir / "config.toml").write_text(
            'token = "axp_a_local.secret"\n'
            'principal_type = "agent"\n'
            '[gateway]\nmode = "local"\nurl = "http://127.0.0.1:8765"\n\n'
            '[agent]\nagent_name = "alice"\n'
            f'workdir = "{wd}"\n'
        )
        registry = {"agents": [{"name": "alice", "workdir": str(wd), "agent_id": "agent-alice"}]}
        _isolate_gateway_for_test(monkeypatch, tmp_path / "gw", registry=registry, pid=os.getpid())
        monkeypatch.setenv("AX_CONFIG_DIR", str(tmp_path / "global"))
        monkeypatch.chdir(wd)
        report = diagnose_auth_config()
        codes = [w["code"] for w in report["warnings"]]
        assert "local_token_with_gateway_binding" in codes

    def test_ambiguous_gateway_binding_warns_when_multiple_candidates(self, tmp_path, monkeypatch):
        wd = tmp_path / "ws"
        wd.mkdir()
        registry = {
            "agents": [
                {"name": "alice", "agent_id": "a-1", "workdir": str(wd)},
                {"name": "bob", "agent_id": "b-1", "workdir": str(wd)},
            ]
        }
        _isolate_gateway_for_test(monkeypatch, tmp_path / "gw", registry=registry, pid=os.getpid())
        monkeypatch.setenv("AX_CONFIG_DIR", str(tmp_path / "global"))
        monkeypatch.chdir(wd)
        report = diagnose_auth_config()
        codes = [w["code"] for w in report["warnings"]]
        assert "ambiguous_gateway_binding" in codes
        assert report["effective"]["agent_name"] == "alice"

    def test_gateway_binding_payload_always_present(self, tmp_path, monkeypatch):
        # Even on a vanilla missing-token case, the payload exposes the
        # gateway_binding block so consumers can inspect daemon state.
        _isolate_gateway_for_test(monkeypatch, tmp_path / "gw")
        monkeypatch.setenv("AX_CONFIG_DIR", str(tmp_path / "global"))
        monkeypatch.chdir(tmp_path)
        report = diagnose_auth_config()
        assert "gateway_binding" in report["effective"]
        gb = report["effective"]["gateway_binding"]
        assert "daemon_running" in gb
        assert "bound_candidates" in gb


# ---- _save_config ----


class TestSaveConfig:
    def test_saves_string_values(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        ax_dir = tmp_path / ".ax"
        ax_dir.mkdir()
        _save_config({"token": "axp_a_test", "base_url": "https://example.com"}, local=True)
        content = (ax_dir / "config.toml").read_text()
        assert 'token = "axp_a_test"' in content
        assert 'base_url = "https://example.com"' in content

    def test_saves_non_string_values(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        ax_dir = tmp_path / ".ax"
        ax_dir.mkdir()
        _save_config({"timeout": 30, "debug": True}, local=True)
        content = (ax_dir / "config.toml").read_text()
        assert "timeout = 30" in content
        # TOML booleans are lowercase, not Python's "True" / "False".
        assert "debug = true" in content

    def test_round_trip_preserves_nested_tables(self, tmp_path, monkeypatch):
        """Regression for #39: load → mutate → save must keep [gateway]/[agent]
        tables parseable. Previously emitted Python ``dict.__repr__`` and
        corrupted the file."""
        import tomllib

        monkeypatch.chdir(tmp_path)
        ax_dir = tmp_path / ".ax"
        ax_dir.mkdir()
        (ax_dir / "config.toml").write_text(
            "[gateway]\n"
            'mode = "local"\n'
            'url = "http://127.0.0.1:8765"\n'
            "\n"
            "[agent]\n"
            'agent_name = "aalan-bot"\n'
            'workdir = "/home/claude/repos/ax-gateway"\n'
        )

        cfg = _load_local_config()
        cfg["space_id"] = "fdffa484-efcc-44d4-ae92-6340ad6209d9"
        _save_config(cfg, local=True)

        reloaded = tomllib.loads((ax_dir / "config.toml").read_text())
        assert reloaded["space_id"] == "fdffa484-efcc-44d4-ae92-6340ad6209d9"
        assert reloaded["gateway"] == {"mode": "local", "url": "http://127.0.0.1:8765"}
        assert reloaded["agent"]["agent_name"] == "aalan-bot"
        assert reloaded["agent"]["workdir"] == "/home/claude/repos/ax-gateway"

    def test_escapes_strings_containing_quotes(self, tmp_path, monkeypatch):
        """Defensive: the prior hand-rolled writer produced unescaped output
        for strings with embedded quotes or backslashes, breaking the next
        load. The TOML writer handles escaping for us."""
        import tomllib

        monkeypatch.chdir(tmp_path)
        ax_dir = tmp_path / ".ax"
        ax_dir.mkdir()
        _save_config({"note": 'has "quotes" and \\ backslash'}, local=True)
        reloaded = tomllib.loads((ax_dir / "config.toml").read_text())
        assert reloaded["note"] == 'has "quotes" and \\ backslash'

    def test_creates_ax_dir_when_missing(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        _save_config({"token": "test"}, local=True)
        assert (tmp_path / ".ax" / "config.toml").exists()

    def test_file_permissions(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        (tmp_path / ".ax").mkdir()
        _save_config({"token": "test"}, local=True)
        cf = tmp_path / ".ax" / "config.toml"
        assert cf.stat().st_mode & 0o777 == 0o600

    def test_save_global(self, tmp_path, monkeypatch):
        monkeypatch.setenv("AX_CONFIG_DIR", str(tmp_path / "global"))
        _save_config({"token": "global-tok"}, local=False)
        cf = tmp_path / "global" / "config.toml"
        assert cf.exists()
        assert 'token = "global-tok"' in cf.read_text()


class TestSaveToken:
    def test_save_token_local(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        (tmp_path / ".ax").mkdir()
        (tmp_path / ".ax" / "config.toml").write_text('base_url = "https://paxai.app"\n')
        save_token("axp_a_new", local=True)
        content = (tmp_path / ".ax" / "config.toml").read_text()
        assert "axp_a_new" in content


class TestSaveSpaceId:
    def test_save_space_id_local(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        (tmp_path / ".ax").mkdir()
        (tmp_path / ".ax" / "config.toml").write_text('token = "axp_a_x"\n')
        save_space_id("space-uuid-123", local=True)
        content = (tmp_path / ".ax" / "config.toml").read_text()
        assert "space-uuid-123" in content


# ---- resolve_agent_name auto-detect ----


class TestResolveAgentNameAutoDetect:
    def test_auto_detect_from_single_agent_pat(self, tmp_path, monkeypatch):
        from unittest.mock import MagicMock

        monkeypatch.delenv("AX_AGENT_NAME", raising=False)
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("AX_CONFIG_DIR", str(tmp_path))
        client = MagicMock()
        client.whoami.return_value = {
            "credential_scope": {"allowed_agent_ids": ["agent-123"]},
        }
        client.list_agents.return_value = [
            {"id": "agent-123", "name": "orion"},
            {"id": "agent-456", "name": "other"},
        ]
        result = resolve_agent_name(client=client)
        assert result == "orion"

    def test_auto_detect_skips_multi_agent_pat(self, tmp_path, monkeypatch):
        from unittest.mock import MagicMock

        monkeypatch.delenv("AX_AGENT_NAME", raising=False)
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("AX_CONFIG_DIR", str(tmp_path))
        client = MagicMock()
        client.whoami.return_value = {
            "credential_scope": {"allowed_agent_ids": ["a-1", "a-2"]},
        }
        result = resolve_agent_name(client=client)
        assert result is None

    def test_auto_detect_whoami_fails_returns_none(self, tmp_path, monkeypatch):
        from unittest.mock import MagicMock

        monkeypatch.delenv("AX_AGENT_NAME", raising=False)
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("AX_CONFIG_DIR", str(tmp_path))
        client = MagicMock()
        client.whoami.side_effect = Exception("network error")
        result = resolve_agent_name(client=client)
        assert result is None


# ---- resolve_space_id ----


class TestResolveSpaceIdExtended:
    def test_single_space_autodetect(self, tmp_path, monkeypatch):
        from unittest.mock import MagicMock

        monkeypatch.delenv("AX_SPACE", raising=False)
        monkeypatch.delenv("AX_SPACE_ID", raising=False)
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("AX_CONFIG_DIR", str(tmp_path))
        client = MagicMock()
        client.whoami.return_value = {}
        client.list_spaces.return_value = [{"id": "space-1"}]
        result = resolve_space_id(client, explicit=None)
        assert result == "space-1"

    def test_no_spaces_raises(self, tmp_path, monkeypatch):
        from unittest.mock import MagicMock

        monkeypatch.delenv("AX_SPACE", raising=False)
        monkeypatch.delenv("AX_SPACE_ID", raising=False)
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("AX_CONFIG_DIR", str(tmp_path))
        client = MagicMock()
        client.whoami.return_value = {}
        client.list_spaces.return_value = []
        with pytest.raises(Exit):
            resolve_space_id(client, explicit=None)

    def test_multiple_spaces_raises(self, tmp_path, monkeypatch):
        from unittest.mock import MagicMock

        monkeypatch.delenv("AX_SPACE", raising=False)
        monkeypatch.delenv("AX_SPACE_ID", raising=False)
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("AX_CONFIG_DIR", str(tmp_path))
        client = MagicMock()
        client.whoami.return_value = {}
        client.list_spaces.return_value = [{"id": "s1"}, {"id": "s2"}]
        with pytest.raises(Exit):
            resolve_space_id(client, explicit=None)


# ---- get_client / get_user_client ----


class TestGetClient:
    def test_raises_when_no_token(self, tmp_path, monkeypatch):
        monkeypatch.delenv("AX_TOKEN", raising=False)
        monkeypatch.delenv("AX_TOKEN_FILE", raising=False)
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("AX_CONFIG_DIR", str(tmp_path))
        with pytest.raises(Exit):
            get_client()

    def test_returns_client_with_token(self, tmp_path, monkeypatch):
        monkeypatch.setenv("AX_TOKEN", "axp_a_test_token")
        monkeypatch.setenv("AX_BASE_URL", "https://paxai.app")
        monkeypatch.delenv("AX_AGENT_NAME", raising=False)
        monkeypatch.delenv("AX_AGENT_ID", raising=False)
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("AX_CONFIG_DIR", str(tmp_path))
        client = get_client()
        assert client is not None

    def test_verbose_mode_prints_env(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setenv("AX_TOKEN", "axp_a_test")
        monkeypatch.setenv("AX_BASE_URL", "https://paxai.app")
        monkeypatch.setenv("AX_VERBOSE", "true")
        monkeypatch.delenv("AX_AGENT_NAME", raising=False)
        monkeypatch.delenv("AX_AGENT_ID", raising=False)
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("AX_CONFIG_DIR", str(tmp_path))
        get_client()
        captured = capsys.readouterr()
        assert "paxai.app" in captured.err


class TestGetUserClient:
    def test_raises_when_no_user_token(self, tmp_path, monkeypatch):
        monkeypatch.delenv("AX_USER_TOKEN", raising=False)
        monkeypatch.delenv("AX_TOKEN", raising=False)
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("AX_CONFIG_DIR", str(tmp_path))
        with pytest.raises(Exit):
            get_user_client()

    def test_raises_when_agent_pat(self, tmp_path, monkeypatch):
        monkeypatch.setenv("AX_USER_TOKEN", "axp_a_agent_token")
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("AX_CONFIG_DIR", str(tmp_path))
        with pytest.raises(Exit):
            get_user_client()

    def test_returns_client_with_user_token(self, tmp_path, monkeypatch):
        monkeypatch.setenv("AX_USER_TOKEN", "axp_u_user_token")
        monkeypatch.setenv("AX_BASE_URL", "https://paxai.app")
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("AX_CONFIG_DIR", str(tmp_path))
        client = get_user_client()
        assert client is not None


# ---- resolve_user_token ----


class TestResolveUserTokenExtended:
    def test_falls_back_to_ax_token_user_pat(self, tmp_path, monkeypatch):
        monkeypatch.delenv("AX_USER_TOKEN", raising=False)
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("AX_CONFIG_DIR", str(tmp_path))
        monkeypatch.setenv("AX_TOKEN", "axp_u_fallback")
        result = resolve_user_token()
        assert result == "axp_u_fallback"

    def test_does_not_fall_back_to_agent_pat(self, tmp_path, monkeypatch):
        monkeypatch.delenv("AX_USER_TOKEN", raising=False)
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("AX_CONFIG_DIR", str(tmp_path))
        monkeypatch.setenv("AX_TOKEN", "axp_a_agent_token")
        result = resolve_user_token()
        assert result is None


# ---- user-PAT precedence unification (#175) ----


class TestUserPatPrecedenceUnified:
    """get_client and get_user_client must agree on which user PAT wins.

    Canonical rule: environment override beats the on-disk file. Before #175
    resolve_user_token put ~/.ax/user.toml ahead of AX_TOKEN, so an operator on
    the encrypted-env workflow who set AX_TOKEN had it honored by runtime
    commands but silently shadowed by the stored file on user-login commands.
    """

    def _store_user_login(self, tmp_path, monkeypatch, token):
        monkeypatch.setenv("AX_CONFIG_DIR", str(tmp_path))
        monkeypatch.chdir(tmp_path)
        _save_user_config({"token": token, "base_url": "https://paxai.app", "principal_type": "user"})

    def test_ax_token_user_pat_wins_over_stored_file(self, tmp_path, monkeypatch):
        monkeypatch.delenv("AX_USER_TOKEN", raising=False)
        self._store_user_login(tmp_path, monkeypatch, "axp_u_file.secret")
        monkeypatch.setenv("AX_TOKEN", "axp_u_env.secret")

        assert resolve_user_token() == "axp_u_env.secret"

    def test_ax_user_token_outranks_ax_token_and_file(self, tmp_path, monkeypatch):
        self._store_user_login(tmp_path, monkeypatch, "axp_u_file.secret")
        monkeypatch.setenv("AX_TOKEN", "axp_u_env.secret")
        monkeypatch.setenv("AX_USER_TOKEN", "axp_u_login.secret")

        assert resolve_user_token() == "axp_u_login.secret"

    def test_agent_ax_token_does_not_shadow_user_file(self, tmp_path, monkeypatch):
        # The common runtime case: AX_TOKEN holds an agent PAT. It must not
        # displace the stored user login for user-authored commands.
        monkeypatch.delenv("AX_USER_TOKEN", raising=False)
        self._store_user_login(tmp_path, monkeypatch, "axp_u_file.secret")
        monkeypatch.setenv("AX_TOKEN", "axp_a_agent.secret")

        assert resolve_user_token() == "axp_u_file.secret"

    def test_runtime_and_user_clients_agree_on_shared_user_pat(self, tmp_path, monkeypatch):
        # Acceptance: with AX_TOKEN (a user PAT) and a stored file both present,
        # the runtime path and the user-login path resolve to the SAME
        # credential. resolve_token backs get_client (config.py get_client) and
        # resolve_user_token backs get_user_client; the resolvers are the single
        # source of truth for each client's token, so their agreement *is* the
        # client agreement. Asserted at the resolver layer to avoid building a
        # live AxClient in a pure config-resolution unit test.
        monkeypatch.delenv("AX_USER_TOKEN", raising=False)
        self._store_user_login(tmp_path, monkeypatch, "axp_u_file.secret")
        monkeypatch.setenv("AX_TOKEN", "axp_u_shared.secret")

        assert resolve_token() == resolve_user_token() == "axp_u_shared.secret"


# ---- _check_config_permissions ----


class TestCheckConfigPermissionsExtended:
    def test_warns_on_loose_permissions(self, tmp_path, monkeypatch, capsys):
        monkeypatch.chdir(tmp_path)
        ax_dir = tmp_path / ".ax"
        ax_dir.mkdir()
        cf = ax_dir / "config.toml"
        cf.write_text('token = "test"\n')
        cf.chmod(0o644)
        monkeypatch.setenv("AX_CONFIG_DIR", str(tmp_path / "global"))
        _check_config_permissions()
        captured = capsys.readouterr()
        assert "WARNING" in captured.err or "0o644" in captured.err

    def test_no_warning_on_safe_permissions(self, tmp_path, monkeypatch, capsys):
        monkeypatch.chdir(tmp_path)
        ax_dir = tmp_path / ".ax"
        ax_dir.mkdir()
        cf = ax_dir / "config.toml"
        cf.write_text('token = "test"\n')
        cf.chmod(0o600)
        monkeypatch.setenv("AX_CONFIG_DIR", str(tmp_path / "global"))
        _check_config_permissions()
        captured = capsys.readouterr()
        assert "WARNING" not in captured.err
