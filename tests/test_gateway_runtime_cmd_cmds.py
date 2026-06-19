"""Per-module gateway command tests: gateway_runtime_cmd (gateway split #28 Phase 1).

Ported from skipped test_gateway_commands*.py; monkeypatches target ax_cli.commands.gateway_runtime_cmd."""

from __future__ import annotations

import json
import sys

from typer.testing import CliRunner

from ax_cli.commands import gateway_runtime_cmd as _gw_runtime
from ax_cli.main import app
from tests.gateway_cmd_testlib import _strip

runner = CliRunner()


def test_gateway_templates_command_json():
    result = runner.invoke(app, ["gateway", "templates", "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    ids = [item["id"] for item in payload["templates"]]
    assert ids[:11] == [
        "hermes",
        "ollama",
        "langgraph",
        "langgraph_composio",
        "autogen",
        "pydantic_ai",
        "strands",
        "echo_test",
        "service_account",
        "pass_through",
        "sentinel_cli",
    ]
    assert ids[11] == "claude_code_channel"
    assert "inbox" not in ids
    assert payload["count"] == 12
    ollama = next(item for item in payload["templates"] if item["id"] == "ollama")
    assert ollama["runtime_type"] == "exec"
    assert ollama["launchable"] is True
    assert ollama["asset_type_label"] == "On-Demand Agent"
    assert ollama["output_label"] == "Reply"
    assert ollama["setup_skill"] == "gateway-agent-setup"
    assert ollama["setup_skill_path"].endswith("skills/gateway-agent-setup/SKILL.md")
    pass_through = next(item for item in payload["templates"] if item["id"] == "pass_through")
    assert pass_through["runtime_type"] == "inbox"
    assert pass_through["requires_approval"] is True
    assert pass_through["intake_model"] == "polling_mailbox"
    service_account = next(item for item in payload["templates"] if item["id"] == "service_account")
    assert service_account["runtime_type"] == "inbox"
    assert service_account["asset_type_label"] == "Service Account"
    assert service_account["output_label"] == "Message"
    channel = next(item for item in payload["templates"] if item["id"] == "claude_code_channel")
    assert channel["runtime_type"] == "claude_code_channel"
    assert channel["intake_model"] == "live_listener"
    assert channel["launchable"] is True


def test_gateway_templates_command_json_includes_ollama_catalog(monkeypatch):
    monkeypatch.setattr(
        _gw_runtime,
        "ollama_setup_status",
        lambda preferred_model=None: {
            "server_reachable": True,
            "recommended_model": "gemma4:latest",
            "available_models": ["gemma4:latest", "nemotron-3-nano:latest"],
            "local_models": ["gemma4:latest", "nemotron-3-nano:latest"],
            "summary": "Ollama is reachable. Recommended model: gemma4:latest.",
        },
    )

    result = runner.invoke(app, ["gateway", "templates", "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    ollama = next(item for item in payload["templates"] if item["id"] == "ollama")
    assert ollama["defaults"]["model"] == "gemma4:latest"
    assert ollama["ollama_recommended_model"] == "gemma4:latest"
    assert ollama["ollama_available_models"] == ["gemma4:latest", "nemotron-3-nano:latest"]


def test_gateway_runtime_types_command_json():
    result = runner.invoke(app, ["gateway", "runtime-types", "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    ids = [item["id"] for item in payload["runtime_types"]]
    assert ids == [
        "echo",
        "exec",
        "hermes_plugin",
        "sentinel_hermes_sdk",
        "sentinel_inference_sdk",
        "sentinel_cli",
        "claude_code_channel",
        "inbox",
    ]
    exec_type = next(item for item in payload["runtime_types"] if item["id"] == "exec")
    assert exec_type["signals"]["activity"]
    assert exec_type["examples"]
    plugin_type = next(item for item in payload["runtime_types"] if item["id"] == "hermes_plugin")
    assert plugin_type["kind"] == "supervised_process"
    assert plugin_type.get("deprecated") is not True
    hermes_sdk_type = next(item for item in payload["runtime_types"] if item["id"] == "sentinel_hermes_sdk")
    assert hermes_sdk_type["kind"] == "supervised_process"
    assert hermes_sdk_type.get("deprecated") is not True
    vendor_sdk_type = next(item for item in payload["runtime_types"] if item["id"] == "sentinel_inference_sdk")
    assert vendor_sdk_type["kind"] == "supervised_process"
    assert vendor_sdk_type.get("deprecated") is not True
    sentinel_type = next(item for item in payload["runtime_types"] if item["id"] == "sentinel_cli")
    assert sentinel_type["signals"]["tools"]
    channel_type = next(item for item in payload["runtime_types"] if item["id"] == "claude_code_channel")
    assert channel_type["kind"] == "attached_session"


class TestRuntimeTypesCommand:
    def test_json(self, monkeypatch):
        payload = {
            "runtime_types": [
                {"id": "echo", "label": "Echo", "kind": "builtin", "signals": {"activity": "yes", "tools": "no"}},
            ]
        }
        monkeypatch.setattr(_gw_runtime, "_runtime_types_payload", lambda: payload)
        result = runner.invoke(app, ["gateway", "runtime-types", "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["runtime_types"][0]["id"] == "echo"

    def test_text(self, monkeypatch):
        payload = {
            "runtime_types": [
                {"id": "echo", "label": "Echo", "kind": "builtin", "signals": {"activity": "yes", "tools": "no"}},
            ]
        }
        monkeypatch.setattr(_gw_runtime, "_runtime_types_payload", lambda: payload)
        result = runner.invoke(app, ["gateway", "runtime-types"])
        assert result.exit_code == 0


class TestTemplatesCommand:
    def test_json(self, monkeypatch):
        payload = {
            "templates": [
                {
                    "id": "echo_test",
                    "label": "Echo Test",
                    "asset_type_label": "test",
                    "output_label": "echo",
                    "availability": "ga",
                    "operator_summary": "Just echoes",
                    "signals": {"activity": "yes"},
                },
            ]
        }
        monkeypatch.setattr(_gw_runtime, "_agent_templates_payload", lambda: payload)
        result = runner.invoke(app, ["gateway", "templates", "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["templates"][0]["id"] == "echo_test"

    def test_text(self, monkeypatch):
        payload = {
            "templates": [
                {
                    "id": "echo_test",
                    "label": "Echo Test",
                    "asset_type_label": "test",
                    "output_label": "echo",
                    "availability": "ga",
                    "operator_summary": "Just echoes",
                    "signals": {"activity": "yes"},
                },
            ]
        }
        monkeypatch.setattr(_gw_runtime, "_agent_templates_payload", lambda: payload)
        result = runner.invoke(app, ["gateway", "templates"])
        assert result.exit_code == 0


class TestRuntimeInstallCommand:
    def test_no_session(self, monkeypatch):
        monkeypatch.setattr(_gw_runtime, "load_gateway_session", lambda: None)
        result = runner.invoke(app, ["gateway", "runtime", "install", "hermes"])
        assert result.exit_code != 0
        assert "login" in _strip(result.output).lower()

    def test_install_json(self, monkeypatch):
        monkeypatch.setattr(_gw_runtime, "load_gateway_session", lambda: {"token": "axp_u_x"})
        monkeypatch.setattr(
            _gw_runtime,
            "_install_runtime_payload",
            lambda tid, **kw: {"target": "/home/hermes", "steps": [], "ready": True},
        )
        result = runner.invoke(app, ["gateway", "runtime", "install", "hermes", "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["ready"] is True

    def test_install_not_ready_exits_1(self, monkeypatch):
        monkeypatch.setattr(_gw_runtime, "load_gateway_session", lambda: {"token": "axp_u_x"})
        monkeypatch.setattr(
            _gw_runtime,
            "_install_runtime_payload",
            lambda tid, **kw: {"target": "/home/hermes", "steps": [], "ready": False},
        )
        result = runner.invoke(app, ["gateway", "runtime", "install", "hermes"])
        assert result.exit_code != 0

    def test_install_error(self, monkeypatch):
        monkeypatch.setattr(_gw_runtime, "load_gateway_session", lambda: {"token": "axp_u_x"})

        def _raise(**kw):
            raise ValueError("bad template")

        monkeypatch.setattr(_gw_runtime, "_install_runtime_payload", lambda tid, **kw: _raise(**kw))
        result = runner.invoke(app, ["gateway", "runtime", "install", "hermes"])
        assert result.exit_code != 0


class TestRuntimeStatusCommand:
    def test_unknown_template(self, monkeypatch):
        result = runner.invoke(app, ["gateway", "runtime", "status", "unknown_thing"])
        assert result.exit_code != 0
        assert "unknown" in _strip(result.output).lower()

    def test_hermes_ready_json(self, monkeypatch):
        import ax_cli.gateway as gw_core

        monkeypatch.setattr(
            gw_core, "hermes_setup_status", lambda entry: {"ready": True, "resolved_path": "/opt/hermes"}
        )
        result = runner.invoke(app, ["gateway", "runtime", "status", "hermes", "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["ready"] is True

    def test_hermes_not_ready(self, monkeypatch):
        import ax_cli.gateway as gw_core

        monkeypatch.setattr(
            gw_core,
            "hermes_setup_status",
            lambda entry: {"ready": False, "expected_path": "/home/hermes", "summary": "not installed"},
        )
        result = runner.invoke(app, ["gateway", "runtime", "status", "hermes"])
        assert result.exit_code != 0
        assert "not ready" in _strip(result.output).lower()


class TestRuntimeAuth:
    """ax gateway runtime auth write/status/clear (#263)."""

    def _redirect_codex_path(self, monkeypatch, tmp_path):
        path = tmp_path / ".ax" / "codex-token"
        monkeypatch.setitem(_gw_runtime._RUNTIME_AUTH_PROVIDERS["openai-codex"], "path", path)
        return path

    def test_write_then_status_and_clear(self, monkeypatch, tmp_path):
        path = self._redirect_codex_path(monkeypatch, tmp_path)

        # write
        result = runner.invoke(
            app, ["gateway", "runtime", "auth", "write", "openai-codex", "CODEX_TOKEN=sk-codex-123", "--json"]
        )
        assert result.exit_code == 0, result.output
        payload = json.loads(result.stdout)
        assert payload["provider"] == "openai-codex"
        assert payload["exists"] is True
        # bare-token file, trailing newline, never echoes the value in output
        assert path.read_text(encoding="utf-8") == "sk-codex-123\n"
        assert "sk-codex-123" not in result.stdout
        # credential file is owner-only on POSIX (never world/group-readable)
        if sys.platform != "win32":
            assert (path.stat().st_mode & 0o077) == 0

        # status reports presence, not the value (assert via JSON — the human
        # output soft-wraps long paths across lines under Rich's 80-col default)
        result = runner.invoke(app, ["gateway", "runtime", "auth", "status", "openai-codex", "--json"])
        assert result.exit_code == 0, result.output
        status = json.loads(result.stdout)
        assert status["exists"] is True
        assert status["path"] == str(path)
        assert "sk-codex-123" not in result.stdout

        # clear removes it
        result = runner.invoke(app, ["gateway", "runtime", "auth", "clear", "openai-codex", "--json"])
        assert result.exit_code == 0, result.output
        assert json.loads(result.stdout)["auth_removed"] is True
        assert not path.exists()

    def test_write_rejects_ax_pat(self, monkeypatch, tmp_path):
        path = self._redirect_codex_path(monkeypatch, tmp_path)
        result = runner.invoke(
            app, ["gateway", "runtime", "auth", "write", "openai-codex", "CODEX_TOKEN=axp_u_notacodextoken"]
        )
        assert result.exit_code == 1
        assert "Refusing to store" in _strip(result.output)
        assert not path.exists()

    def test_write_unknown_provider(self, monkeypatch, tmp_path):
        self._redirect_codex_path(monkeypatch, tmp_path)
        result = runner.invoke(app, ["gateway", "runtime", "auth", "write", "bogus", "CODEX_TOKEN=x"])
        assert result.exit_code == 1
        assert "Unknown runtime provider" in _strip(result.output)

    def test_write_missing_expected_key(self, monkeypatch, tmp_path):
        self._redirect_codex_path(monkeypatch, tmp_path)
        result = runner.invoke(app, ["gateway", "runtime", "auth", "write", "openai-codex", "WRONG_KEY=x"])
        assert result.exit_code == 1
        assert "expects CODEX_TOKEN" in _strip(result.output)

    def test_status_when_absent(self, monkeypatch, tmp_path):
        self._redirect_codex_path(monkeypatch, tmp_path)
        result = runner.invoke(app, ["gateway", "runtime", "auth", "status", "openai-codex"])
        assert result.exit_code == 0, result.output
        out = _strip(result.output)
        assert "No credential stored" in out
        assert "auth write openai-codex CODEX_TOKEN=" in out

    def test_status_flags_stored_ax_pat(self, monkeypatch, tmp_path):
        # Defense-in-depth: a pre-existing ~/.ax/codex-token holding an aX PAT
        # (the historical misconfig) is flagged without echoing the value.
        path = self._redirect_codex_path(monkeypatch, tmp_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("axp_u_legacy.pat\n", encoding="utf-8")
        result = runner.invoke(app, ["gateway", "runtime", "auth", "status", "openai-codex", "--json"])
        assert result.exit_code == 0, result.output
        payload = json.loads(result.stdout)
        assert payload["exists"] is True
        assert "looks_invalid" in payload

    def test_clear_when_absent(self, monkeypatch, tmp_path):
        self._redirect_codex_path(monkeypatch, tmp_path)
        result = runner.invoke(app, ["gateway", "runtime", "auth", "clear", "openai-codex"])
        assert result.exit_code == 0, result.output
        assert "No credential stored" in _strip(result.output)
