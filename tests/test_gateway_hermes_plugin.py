"""Tests for the Gateway-supervised Hermes plugin runtime.

Covers the scaffolding + spawn helpers (no live subprocess). Specifically
asserts the trust-boundary property: the raw AX_TOKEN never lands in any
file under the workspace; it only appears in the subprocess env that
Gateway builds at spawn time from the Gateway-owned token file.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ax_cli import gateway as gateway_core
from ax_cli.gateway_runtime_types import (
    agent_template_definition,
    runtime_type_definition,
    runtime_type_list,
)


def _base_entry(tmp_path: Path) -> dict:
    workdir = tmp_path / "wiki"
    return {
        "name": "mnemo",
        "agent_id": "11111111-1111-1111-1111-111111111111",
        "space_id": "22222222-2222-2222-2222-222222222222",
        "base_url": "https://paxai.app",
        "workdir": str(workdir),
        "runtime_type": "hermes_plugin",
        "template_id": "hermes",
    }


def test_hermes_plugin_runtime_in_catalog():
    plugin_def = runtime_type_definition("hermes_plugin")
    assert plugin_def["id"] == "hermes_plugin"
    assert plugin_def["kind"] == "supervised_process"
    assert plugin_def.get("deprecated") is not True
    ids = [r["id"] for r in runtime_type_list()]
    assert "hermes_plugin" in ids
    # hermes template now defaults to plugin runtime
    hermes_template = agent_template_definition("hermes")
    assert hermes_template["runtime_type"] == "hermes_plugin"
    assert hermes_template["defaults"]["runtime_type"] == "hermes_plugin"


def test_is_predicates():
    assert gateway_core._is_hermes_plugin_runtime("hermes_plugin")
    assert not gateway_core._is_hermes_plugin_runtime("sentinel_inference_sdk")
    assert gateway_core._is_supervised_subprocess_runtime("hermes_plugin")
    assert gateway_core._is_supervised_subprocess_runtime("sentinel_hermes_sdk")
    assert not gateway_core._is_supervised_subprocess_runtime("echo")
    assert not gateway_core._is_supervised_subprocess_runtime("exec")


def test_hermes_plugin_workdir_and_home(tmp_path):
    entry = _base_entry(tmp_path)
    workdir = gateway_core._hermes_plugin_workdir(entry)
    assert workdir == tmp_path / "wiki"
    home = gateway_core._hermes_plugin_home(entry)
    assert home == workdir / ".hermes"
    entry["hermes_home"] = str(tmp_path / "elsewhere")
    assert gateway_core._hermes_plugin_home(entry) == tmp_path / "elsewhere"


def test_scaffold_creates_dir_plugin_link_and_dotenv(tmp_path):
    entry = _base_entry(tmp_path)
    home = gateway_core._scaffold_hermes_plugin_home(entry)
    assert home.is_dir()
    plugin_link = home / "plugins" / "ax"
    assert plugin_link.is_symlink() or (plugin_link / "plugin.yaml").exists()
    assert plugin_link.resolve() == gateway_core._plugin_source_dir().resolve()
    dotenv = (home / ".env").read_text()
    assert "AX_AGENT_NAME=mnemo" in dotenv
    assert entry["agent_id"] in dotenv
    assert entry["space_id"] in dotenv
    assert "AX_BASE_URL=https://paxai.app" in dotenv
    # The .env can mention AX_TOKEN in a comment, but must never assign it.
    assert not any(line.strip().startswith("AX_TOKEN=") for line in dotenv.splitlines()), (
        "Identity .env must not assign AX_TOKEN"
    )


def test_scaffold_writes_allow_all_users_when_opted_in(tmp_path):
    entry = _base_entry(tmp_path)
    entry["allow_all_users"] = True
    home = gateway_core._scaffold_hermes_plugin_home(entry)
    dotenv = (home / ".env").read_text()
    assert "AX_ALLOW_ALL_USERS=1" in dotenv
    assert "GATEWAY_ALLOW_ALL_USERS=true" in dotenv


def test_scaffold_omits_allow_all_users_by_default(tmp_path):
    entry = _base_entry(tmp_path)
    home = gateway_core._scaffold_hermes_plugin_home(entry)
    dotenv = (home / ".env").read_text()
    assert "AX_ALLOW_ALL_USERS=1" not in dotenv
    assert "GATEWAY_ALLOW_ALL_USERS=true" not in dotenv


def test_scaffold_writes_allowed_users(tmp_path):
    entry = _base_entry(tmp_path)
    entry["allowed_users"] = "alice,bob"
    home = gateway_core._scaffold_hermes_plugin_home(entry)
    dotenv = (home / ".env").read_text()
    assert "AX_ALLOWED_USERS=alice,bob" in dotenv


def test_scaffold_is_idempotent(tmp_path):
    entry = _base_entry(tmp_path)
    home_first = gateway_core._scaffold_hermes_plugin_home(entry)
    plugin_link = home_first / "plugins" / "ax"
    first_target = plugin_link.resolve()
    home_second = gateway_core._scaffold_hermes_plugin_home(entry)
    assert home_first == home_second
    assert (home_second / "plugins" / "ax").resolve() == first_target


def test_scaffold_inherits_operator_auth_when_present(tmp_path, monkeypatch):
    fake_home = tmp_path / "operator-home"
    operator_hermes = fake_home / ".hermes"
    operator_hermes.mkdir(parents=True)
    (operator_hermes / "auth.json").write_text("{}")
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))

    entry = _base_entry(tmp_path)
    home = gateway_core._scaffold_hermes_plugin_home(entry)
    auth = home / "auth.json"
    assert auth.is_symlink()
    assert auth.resolve() == (operator_hermes / "auth.json").resolve()


def test_scaffold_renders_config_yaml_with_pinned_terminal_cwd(tmp_path, monkeypatch):
    """config.yaml is rendered (not symlinked) so operator's terminal.cwd
    can't bleed through. The agent's workdir wins for terminal.cwd while
    other operator defaults (model, providers, etc.) are still seeded.
    """
    yaml = pytest.importorskip("yaml")
    fake_home = tmp_path / "operator-home"
    operator_hermes = fake_home / ".hermes"
    operator_hermes.mkdir(parents=True)
    operator_config = {
        "model": "gpt-5.5",
        "providers": {"openai-codex": {"default_model": "gpt-5.5"}},
        # Operator pointed terminal.cwd at a *different* agent's tree —
        # exactly the bleed that mis-identifies agents in production.
        "terminal": {"backend": "local", "cwd": str(tmp_path / "some-other-agent")},
    }
    (operator_hermes / "config.yaml").write_text(yaml.safe_dump(operator_config))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))

    entry = _base_entry(tmp_path)
    home = gateway_core._scaffold_hermes_plugin_home(entry)
    cfg_path = home / "config.yaml"

    assert cfg_path.is_file() and not cfg_path.is_symlink()
    rendered = yaml.safe_load(cfg_path.read_text())
    # terminal.cwd must point at the agent's own workdir, never the
    # operator's pinned path.
    assert rendered["terminal"]["cwd"] == str(tmp_path / "wiki")
    # Operator's other terminal fields and top-level defaults pass through.
    assert rendered["terminal"]["backend"] == "local"
    assert rendered["model"] == "gpt-5.5"
    assert rendered["providers"]["openai-codex"]["default_model"] == "gpt-5.5"


def test_scaffold_replaces_stale_config_symlink(tmp_path, monkeypatch):
    """Upgrading from the old symlink-based scaffold must not leave a
    stale symlink in place — otherwise the identity bleed survives.
    """
    yaml = pytest.importorskip("yaml")
    fake_home = tmp_path / "operator-home"
    operator_hermes = fake_home / ".hermes"
    operator_hermes.mkdir(parents=True)
    (operator_hermes / "config.yaml").write_text(yaml.safe_dump({"terminal": {"cwd": str(tmp_path / "stale")}}))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))

    entry = _base_entry(tmp_path)
    home = gateway_core._hermes_plugin_home(entry)
    home.mkdir(parents=True, exist_ok=True)
    stale_target = home / "config.yaml"
    stale_target.symlink_to(operator_hermes / "config.yaml")
    assert stale_target.is_symlink()

    gateway_core._scaffold_hermes_plugin_home(entry)
    assert not stale_target.is_symlink()
    assert stale_target.is_file()
    rendered = yaml.safe_load(stale_target.read_text())
    assert rendered["terminal"]["cwd"] == str(tmp_path / "wiki")


def test_scaffold_renders_minimal_config_when_operator_has_none(tmp_path, monkeypatch):
    yaml = pytest.importorskip("yaml")
    fake_home = tmp_path / "operator-home"
    (fake_home / ".hermes").mkdir(parents=True)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))

    entry = _base_entry(tmp_path)
    home = gateway_core._scaffold_hermes_plugin_home(entry)
    cfg_path = home / "config.yaml"

    assert cfg_path.is_file() and not cfg_path.is_symlink()
    rendered = yaml.safe_load(cfg_path.read_text())
    # Scaffold always pins terminal.cwd and enables the ax-platform plugin —
    # without the latter Hermes' opt-in gate silently drops the adapter.
    assert rendered == {
        "terminal": {"cwd": str(tmp_path / "wiki")},
        "plugins": {"enabled": [gateway_core.AX_PLUGIN_NAME]},
    }


def test_scaffold_enables_ax_platform_when_operator_config_has_no_plugins_key(tmp_path, monkeypatch):
    """Operator config without a `plugins` section gets one created with
    `enabled: [ax-platform]`. Otherwise fresh setups would hit Hermes'
    silent `No messaging platforms enabled` failure mode.
    """
    yaml = pytest.importorskip("yaml")
    fake_home = tmp_path / "operator-home"
    operator_hermes = fake_home / ".hermes"
    operator_hermes.mkdir(parents=True)
    (operator_hermes / "config.yaml").write_text(yaml.safe_dump({"model": "gpt-5.5"}))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))

    entry = _base_entry(tmp_path)
    home = gateway_core._scaffold_hermes_plugin_home(entry)
    rendered = yaml.safe_load((home / "config.yaml").read_text())
    assert rendered["plugins"]["enabled"] == [gateway_core.AX_PLUGIN_NAME]
    assert rendered["model"] == "gpt-5.5"


def test_scaffold_appends_ax_platform_alongside_existing_enabled_plugins(tmp_path, monkeypatch):
    """Operator already has other plugins enabled — scaffold must append
    ax-platform without dropping the existing entries.
    """
    yaml = pytest.importorskip("yaml")
    fake_home = tmp_path / "operator-home"
    operator_hermes = fake_home / ".hermes"
    operator_hermes.mkdir(parents=True)
    (operator_hermes / "config.yaml").write_text(yaml.safe_dump({"plugins": {"enabled": ["disk-cleanup", "spotify"]}}))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))

    entry = _base_entry(tmp_path)
    home = gateway_core._scaffold_hermes_plugin_home(entry)
    rendered = yaml.safe_load((home / "config.yaml").read_text())
    assert set(rendered["plugins"]["enabled"]) == {
        "disk-cleanup",
        "spotify",
        gateway_core.AX_PLUGIN_NAME,
    }


def test_scaffold_does_not_duplicate_ax_platform_when_already_enabled(tmp_path, monkeypatch):
    """Idempotent: running the scaffold twice (or against a config that
    already enables ax-platform) does not produce duplicate entries.
    """
    yaml = pytest.importorskip("yaml")
    fake_home = tmp_path / "operator-home"
    operator_hermes = fake_home / ".hermes"
    operator_hermes.mkdir(parents=True)
    (operator_hermes / "config.yaml").write_text(
        yaml.safe_dump({"plugins": {"enabled": [gateway_core.AX_PLUGIN_NAME]}})
    )
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))

    entry = _base_entry(tmp_path)
    gateway_core._scaffold_hermes_plugin_home(entry)
    # Second scaffold against the previously-rendered per-agent config.
    home = gateway_core._scaffold_hermes_plugin_home(entry)
    rendered = yaml.safe_load((home / "config.yaml").read_text())
    assert rendered["plugins"]["enabled"].count(gateway_core.AX_PLUGIN_NAME) == 1


def test_scaffold_scrubs_stale_disable_of_ax_platform(tmp_path, monkeypatch):
    """If the operator's `plugins.disabled` lists ax-platform, the
    scaffold removes it — a stale disable would override the enable and
    re-create the silent-drop failure mode.
    """
    yaml = pytest.importorskip("yaml")
    fake_home = tmp_path / "operator-home"
    operator_hermes = fake_home / ".hermes"
    operator_hermes.mkdir(parents=True)
    (operator_hermes / "config.yaml").write_text(
        yaml.safe_dump(
            {
                "plugins": {
                    "enabled": ["disk-cleanup"],
                    "disabled": [gateway_core.AX_PLUGIN_NAME, "google_meet"],
                }
            }
        )
    )
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))

    entry = _base_entry(tmp_path)
    home = gateway_core._scaffold_hermes_plugin_home(entry)
    rendered = yaml.safe_load((home / "config.yaml").read_text())
    assert gateway_core.AX_PLUGIN_NAME in rendered["plugins"]["enabled"]
    assert gateway_core.AX_PLUGIN_NAME not in rendered["plugins"].get("disabled", [])
    # Unrelated disables stay put.
    assert "google_meet" in rendered["plugins"]["disabled"]


def test_hermes_setup_status_does_not_gate_plugin_runtime(tmp_path, monkeypatch):
    """hermes_plugin must not be gated on the presence of a hermes-agent
    git checkout — the binary is resolved via _hermes_bin (HERMES_BIN /
    $PATH / fallback) and the aX plugin source ships in this repo.
    """
    # Make sure no candidate path exists so the legacy gate would fail.
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "nope"))
    monkeypatch.delenv("HERMES_REPO_PATH", raising=False)
    entry = _base_entry(tmp_path)
    status = gateway_core.hermes_setup_status(entry)
    assert status["ready"] is True, status

    # sentinel_inference_sdk no longer requires a hermes-agent checkout —
    # it uses a gateway-owned venv under ~/.ax/runtimes/sentinel_inference_sdk.
    sentinel_entry = dict(entry, runtime_type="sentinel_inference_sdk")
    sentinel_status = gateway_core.hermes_setup_status(sentinel_entry)
    assert sentinel_status["ready"] is True


def test_build_cmd_uses_hermes_gateway_run(tmp_path, monkeypatch):
    entry = _base_entry(tmp_path)
    monkeypatch.setenv("HERMES_BIN", "/usr/local/bin/hermes-test")
    cmd = gateway_core._build_hermes_plugin_cmd(entry)
    assert cmd == ["/usr/local/bin/hermes-test", "gateway", "run"]


def test_build_env_injects_token_from_gateway_file(tmp_path, monkeypatch):
    entry = _base_entry(tmp_path)
    token_file = tmp_path / "token"
    token_file.write_text("axp_a_TEST_TOKEN_NOT_REAL_xxxxx")
    entry["token_file"] = str(token_file)

    # Strip any inherited AX_TOKEN that would mask the per-agent value.
    for var in ("AX_TOKEN", "AX_AGENT_NAME", "AX_AGENT_ID", "AX_SPACE_ID"):
        monkeypatch.delenv(var, raising=False)

    env = gateway_core._build_hermes_plugin_env(entry)
    assert env["AX_TOKEN"] == "axp_a_TEST_TOKEN_NOT_REAL_xxxxx"
    assert env["AX_AGENT_NAME"] == "mnemo"
    assert env["AX_AGENT_ID"] == entry["agent_id"]
    assert env["AX_SPACE_ID"] == entry["space_id"]
    assert env["AX_BASE_URL"] == "https://paxai.app"
    home = gateway_core._hermes_plugin_home(entry)
    assert env["HERMES_HOME"] == str(home)
    # ENV_DENYLIST stripping: callers should not leak Gateway's own AX_TOKEN
    # if it happened to be set when Gateway started.
    monkeypatch.setenv("AX_TOKEN", "axp_u_GATEWAY_USER_TOKEN_LEAK")
    env_again = gateway_core._build_hermes_plugin_env(entry)
    assert env_again["AX_TOKEN"] == "axp_a_TEST_TOKEN_NOT_REAL_xxxxx"


def test_hermes_bin_prefers_entry_override(tmp_path, monkeypatch):
    entry = _base_entry(tmp_path)
    entry["hermes_bin"] = "/custom/bin/hermes"
    monkeypatch.setenv("HERMES_BIN", "/env/bin/hermes")
    assert gateway_core._hermes_bin(entry) == "/custom/bin/hermes"


def test_hermes_bin_falls_back_to_env(tmp_path, monkeypatch):
    entry = _base_entry(tmp_path)
    monkeypatch.setenv("HERMES_BIN", "/env/bin/hermes")
    assert gateway_core._hermes_bin(entry) == "/env/bin/hermes"


def test_hermes_bin_raises_when_unresolvable(tmp_path, monkeypatch):
    entry = _base_entry(tmp_path)
    monkeypatch.delenv("HERMES_BIN", raising=False)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "nope"))
    monkeypatch.setattr(gateway_core.shutil, "which", lambda _name: None)
    with pytest.raises(RuntimeError, match="hermes CLI not found"):
        gateway_core._hermes_bin(entry)
