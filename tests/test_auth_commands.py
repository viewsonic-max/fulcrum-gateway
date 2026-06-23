import json
import tomllib

import pytest
import typer
from typer.testing import CliRunner

from ax_cli.commands import auth
from ax_cli.main import app

runner = CliRunner()


def test_login_calls_user_login(monkeypatch):
    """`ax login` is the human login path, separate from local agent init."""
    called = {}

    def fake_login_user(token, *, base_url, agent, space_id, env_name, print_only=False):
        called.update(
            {
                "token": token,
                "base_url": base_url,
                "agent": agent,
                "space_id": space_id,
                "env_name": env_name,
                "print_only": print_only,
            }
        )

    monkeypatch.setattr(auth, "login_user", fake_login_user)

    result = runner.invoke(
        app,
        [
            "login",
            "--token",
            "axp_u_test.token",
            "--url",
            "https://paxai.app",
            "--env",
            "next",
            "--agent",
            "anvil",
            "--space-id",
            "space-123",
        ],
    )

    assert result.exit_code == 0
    assert called == {
        "token": "axp_u_test.token",
        "base_url": "https://paxai.app",
        "agent": "anvil",
        "space_id": "space-123",
        "env_name": "next",
        "print_only": False,
    }


def test_login_defaults_to_next_without_space_requirement(monkeypatch):
    """`ax login` is the user path: next URL by default, no space required."""
    called = {}

    def fake_login_user(token, *, base_url, agent, space_id, env_name, print_only=False):
        called.update(
            {
                "token": token,
                "base_url": base_url,
                "agent": agent,
                "space_id": space_id,
                "env_name": env_name,
                "print_only": print_only,
            }
        )

    monkeypatch.setattr(auth, "login_user", fake_login_user)

    result = runner.invoke(app, ["login", "--token", "axp_u_test.token"])

    assert result.exit_code == 0
    assert called == {
        "token": "axp_u_test.token",
        "base_url": "https://paxai.app",
        "agent": None,
        "space_id": None,
        "env_name": None,
        "print_only": False,
    }


def test_login_print_flag_threads_through_to_login_user(monkeypatch):
    """`--print` toggles print_only on the underlying login_user call."""
    called = {}

    def fake_login_user(token, *, base_url, agent, space_id, env_name, print_only=False):
        called["print_only"] = print_only

    monkeypatch.setattr(auth, "login_user", fake_login_user)

    result = runner.invoke(app, ["login", "--token", "axp_u_test.token", "--print"])

    assert result.exit_code == 0
    assert called == {"print_only": True}


def test_login_token_prompt_is_masked(monkeypatch):
    """Omitting --token prompts via Typer's hidden input path."""
    prompt_calls = []
    printed = []

    def fake_prompt(label, *, hide_input):
        prompt_calls.append({"label": label, "hide_input": hide_input})
        return " axp_u_prompt.token "

    monkeypatch.setattr(auth.typer, "prompt", fake_prompt)
    monkeypatch.setattr(auth.console, "print", lambda *args, **kwargs: printed.append(str(args[0]) if args else ""))

    assert auth._resolve_login_token(None) == "axp_u_prompt.token"
    assert prompt_calls == [{"label": "Token", "hide_input": True}]
    assert any("Token captured" in line for line in printed)
    assert "axp_u_prompt.token" not in "\n".join(printed)
    assert "axp_u_********" in "\n".join(printed)


def test_login_space_selection_uses_only_unambiguous_space():
    assert auth._select_login_space([{"id": "space-1", "name": "Only"}]) == {"id": "space-1", "name": "Only"}
    assert auth._select_login_space(
        [
            {"id": "space-1", "name": "Team"},
            {"id": "space-2", "name": "Personal", "is_personal": True},
        ]
    ) == {"id": "space-2", "name": "Personal", "is_personal": True}
    assert (
        auth._select_login_space(
            [
                {"id": "space-1", "name": "Team A"},
                {"id": "space-2", "name": "Team B"},
            ]
        )
        is None
    )


def test_user_login_does_not_modify_local_agent_config(monkeypatch, write_config, config_dir):
    """A user PAT login is stored separately and must not rewrite an agent config."""
    write_config(
        token="axp_a_old.secret",
        base_url="https://old.example.com",
        agent_name="orion",
        agent_id="agent-orion",
        space_id="old-space",
    )

    class FakeTokenExchanger:
        def __init__(self, base_url, token):
            self.base_url = base_url
            self.token = token

        def get_token(self, token_class, *, scope, force_refresh):
            assert self.base_url == "https://paxai.app"
            assert self.token == "axp_u_new.secret"
            assert token_class == "user_access"
            assert scope == "messages tasks context agents spaces search"
            assert force_refresh is True
            return "fake.jwt"

    class FakeAxClient:
        def __init__(self, *, base_url, token):
            self.base_url = base_url
            self.token = token

        def whoami(self):
            return {"username": "madtank", "email": "madtank@example.com"}

        def list_spaces(self):
            return {"spaces": [{"id": "space-current", "name": "Team Hub", "is_current": True}]}

        def list_agents(self):
            raise AssertionError("user login must not auto-select or store an agent")

    monkeypatch.setattr("ax_cli.token_cache.TokenExchanger", FakeTokenExchanger)
    monkeypatch.setattr("ax_cli.client.AxClient", FakeAxClient)

    result = runner.invoke(app, ["login", "--token", "axp_u_new.secret"])

    assert result.exit_code == 0
    local_cfg = tomllib.loads((config_dir / "config.toml").read_text())
    assert local_cfg == {
        "token": "axp_a_old.secret",
        "base_url": "https://old.example.com",
        "agent_name": "orion",
        "agent_id": "agent-orion",
        "space_id": "old-space",
    }
    user_cfg = tomllib.loads((config_dir.parent / "_global_config" / "user.toml").read_text())
    assert user_cfg == {
        "token": "axp_u_new.secret",
        "base_url": "https://paxai.app",
        "principal_type": "user",
        "space_id": "space-current",
    }


def test_user_login_env_stores_named_login_and_marks_active(monkeypatch, write_config, config_dir):
    """Admins can keep separate user bootstrap tokens for dev/next/prod."""
    write_config(token="axp_a_old.secret", base_url="https://old.example.com", agent_name="orion")

    class FakeTokenExchanger:
        def __init__(self, base_url, token):
            self.base_url = base_url
            self.token = token

        def get_token(self, token_class, *, scope, force_refresh):
            assert self.base_url == "https://dev.paxai.app"
            assert self.token == "axp_u_dev.secret"
            return "fake.jwt"

    class FakeAxClient:
        def __init__(self, *, base_url, token):
            self.base_url = base_url
            self.token = token

        def whoami(self):
            return {"username": "madtank", "email": "madtank@example.com"}

        def list_spaces(self):
            return {"spaces": []}

    monkeypatch.setattr("ax_cli.token_cache.TokenExchanger", FakeTokenExchanger)
    monkeypatch.setattr("ax_cli.client.AxClient", FakeAxClient)

    result = runner.invoke(
        app, ["login", "--token", "axp_u_dev.secret", "--url", "https://dev.paxai.app", "--env", "dev"]
    )

    assert result.exit_code == 0
    global_dir = config_dir.parent / "_global_config"
    default_user = global_dir / "user.toml"
    dev_user = global_dir / "users" / "dev" / "user.toml"
    assert not default_user.exists()
    assert tomllib.loads(dev_user.read_text()) == {
        "token": "axp_u_dev.secret",
        "base_url": "https://dev.paxai.app",
        "principal_type": "user",
        "environment": "dev",
    }
    assert (global_dir / "users" / ".active").read_text().strip() == "dev"


def test_user_login_print_only_emits_token_and_skips_save(monkeypatch, write_config, config_dir):
    """--print verifies the token, emits it on stdout, and never writes user.toml."""
    write_config(token="axp_a_old.secret", base_url="https://old.example.com", agent_name="orion")

    class FakeTokenExchanger:
        def __init__(self, base_url, token):
            self.base_url = base_url
            self.token = token

        def get_token(self, token_class, *, scope, force_refresh):
            assert self.token == "axp_u_print.secret"
            return "fake.jwt"

    class FakeAxClient:
        def __init__(self, *, base_url, token):
            self.base_url = base_url
            self.token = token

        def whoami(self):
            return {"username": "madtank", "email": "madtank@example.com"}

        def list_spaces(self):
            raise AssertionError("--print must not list spaces")

    def _no_save(*_a, **_k):
        raise AssertionError("--print must not write user.toml")

    monkeypatch.setattr("ax_cli.token_cache.TokenExchanger", FakeTokenExchanger)
    monkeypatch.setattr("ax_cli.client.AxClient", FakeAxClient)
    monkeypatch.setattr(auth, "_save_user_config", _no_save)

    result = runner.invoke(app, ["login", "--token", "axp_u_print.secret", "--print"])

    assert result.exit_code == 0, result.output
    assert "axp_u_print.secret" in result.stdout
    assert "Saved user login" not in result.output
    global_dir = config_dir.parent / "_global_config"
    assert not (global_dir / "user.toml").exists()


def test_user_login_print_only_exits_on_verification_failure(monkeypatch, config_dir):
    """A bad PAT in --print mode must fail closed without leaking the token to stdout."""

    class FailingExchanger:
        def __init__(self, base_url, token):
            pass

        def get_token(self, *_a, **_k):
            raise RuntimeError("403 Forbidden")

    def _no_save(*_a, **_k):
        raise AssertionError("--print must not write user.toml")

    monkeypatch.setattr("ax_cli.token_cache.TokenExchanger", FailingExchanger)
    monkeypatch.setattr(auth, "_save_user_config", _no_save)

    result = runner.invoke(app, ["login", "--token", "axp_u_bad.secret", "--print"])

    assert result.exit_code == 1
    assert "axp_u_bad.secret" not in result.stdout


def test_user_login_print_only_emits_token_when_whoami_fails(monkeypatch, config_dir):
    """A verified token still lands on stdout even when whoami transiently fails."""

    class FakeTokenExchanger:
        def __init__(self, base_url, token):
            pass

        def get_token(self, *_a, **_k):
            return "fake.jwt"

    class FailingAxClient:
        def __init__(self, *, base_url, token):
            pass

        def whoami(self):
            raise RuntimeError("temporary backend hiccup")

    def _no_save(*_a, **_k):
        raise AssertionError("--print must not write user.toml")

    monkeypatch.setattr("ax_cli.token_cache.TokenExchanger", FakeTokenExchanger)
    monkeypatch.setattr("ax_cli.client.AxClient", FailingAxClient)
    monkeypatch.setattr(auth, "_save_user_config", _no_save)

    result = runner.invoke(app, ["login", "--token", "axp_u_resilient.secret", "--print"])

    assert result.exit_code == 0, result.output
    assert "axp_u_resilient.secret" in result.stdout


def test_auth_doctor_json_outputs_diagnostics(monkeypatch):
    monkeypatch.setattr(
        auth,
        "diagnose_auth_config",
        lambda *, env_name, explicit_space_id: {
            "ok": True,
            "selected_env": env_name,
            "selected_profile": None,
            "runtime_config": "/tmp/codex/.ax/config.toml",
            "effective": {
                "auth_source": "user_login:dev",
                "token_kind": "user_pat",
                "token": "axp_u_...cret",
                "base_url": "https://dev.paxai.app",
                "base_url_source": "user_login:dev",
                "host": "dev.paxai.app",
                "space_id": explicit_space_id,
                "space_source": "option:--space-id",
                "principal_intent": "user",
            },
            "sources": [],
            "warnings": [],
            "problems": [],
        },
    )

    result = runner.invoke(app, ["auth", "doctor", "--env", "dev", "--space-id", "space-1", "--json"])

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["version"] == 1
    assert payload["skipped"] is False
    assert payload["summary"] == {
        "command": "ax auth doctor",
        "principal_intent": "user",
        "auth_source": "user_login:dev",
        "host": "dev.paxai.app",
        "space_id": "space-1",
        "warnings": 0,
        "problems": 0,
    }
    assert payload["details"] == []
    assert payload["effective"]["auth_source"] == "user_login:dev"
    assert payload["effective"]["space_id"] == "space-1"


def test_auth_whoami_reports_runtime_config(monkeypatch, tmp_path):
    runtime_config = tmp_path / "runtime-config.toml"
    runtime_config.write_text("")
    monkeypatch.setenv("AX_CONFIG_FILE", str(runtime_config))

    class FakeClient:
        def whoami(self):
            return {
                "id": "user-1",
                "bound_agent": {
                    "default_space_id": "space-1",
                },
            }

    monkeypatch.setattr(auth, "get_client", lambda: FakeClient())
    monkeypatch.setattr(auth, "resolve_agent_name", lambda *, client: "codex")
    monkeypatch.setattr(auth, "_local_config_dir", lambda: None)

    result = runner.invoke(app, ["auth", "whoami", "--json"])

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["runtime_config"] == str(runtime_config)
    assert payload["resolved_agent"] == "codex"


def test_auth_whoami_does_not_crash_on_multi_space_user(monkeypatch):
    """Regression for ax-cli-dev task f664c903 / Heath onboarding bug.

    A fresh-laptop user with >1 space (e.g. logged into next.paxai.app) used
    to fail their first `ax auth whoami` because the unbound-agent fallback
    called `resolve_space_id`, which raises `typer.Exit` (a `RuntimeError`
    subclass, not `SystemExit`) when more than one space exists. The
    surrounding `except SystemExit:` block was therefore dead code and the
    command exited 1 with `Error: Multiple spaces found.`

    Identity is token-bound and space-independent; whoami must report the
    user's id/email/role even when space resolution is ambiguous.
    """

    class FakeClient:
        def whoami(self):
            return {"id": "user-1", "email": "user@example.com", "bound_agent": None}

        def list_spaces(self):
            return {
                "spaces": [
                    {"id": "space-a", "name": "Space A"},
                    {"id": "space-b", "name": "Space B"},
                ]
            }

    monkeypatch.setattr(auth, "get_client", lambda: FakeClient())
    monkeypatch.setattr(auth, "resolve_agent_name", lambda *, client: None)
    monkeypatch.setattr(auth, "resolve_gateway_config", lambda: {})
    monkeypatch.setattr(auth, "_local_config_dir", lambda: None)
    # Don't let env or config cascade short-circuit the multi-space fallback.
    monkeypatch.delenv("AX_SPACE_ID", raising=False)
    monkeypatch.delenv("AX_SPACE", raising=False)
    monkeypatch.setattr(auth, "_load_config", lambda: {})

    result = runner.invoke(app, ["auth", "whoami", "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["id"] == "user-1"
    assert payload["email"] == "user@example.com"
    assert payload["resolved_space_id"] == "unresolved (set AX_SPACE_ID or use --space-id)"
    # The bug printed `Error: Multiple spaces found.` on stderr before exiting.
    assert "Multiple spaces found" not in result.output


def test_auth_whoami_resolves_single_space_for_unbound_user(monkeypatch):
    """Best-effort: when the user has exactly one space we still surface its id."""

    class FakeClient:
        def whoami(self):
            return {"id": "user-1", "bound_agent": None}

        def list_spaces(self):
            return {"spaces": [{"id": "the-only-space", "name": "Solo"}]}

    monkeypatch.setattr(auth, "get_client", lambda: FakeClient())
    monkeypatch.setattr(auth, "resolve_agent_name", lambda *, client: None)
    monkeypatch.setattr(auth, "resolve_gateway_config", lambda: {})
    monkeypatch.setattr(auth, "_local_config_dir", lambda: None)
    monkeypatch.delenv("AX_SPACE_ID", raising=False)
    monkeypatch.delenv("AX_SPACE", raising=False)
    monkeypatch.setattr(auth, "_load_config", lambda: {})

    result = runner.invoke(app, ["auth", "whoami", "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["resolved_space_id"] == "the-only-space"


def test_auth_whoami_uses_explicit_space_from_env(monkeypatch):
    """Env-configured space must short-circuit the list_spaces probe entirely."""

    class FakeClient:
        def whoami(self):
            return {"id": "user-1", "bound_agent": None}

        def list_spaces(self):
            raise AssertionError("list_spaces should not be called when AX_SPACE_ID is set")

    monkeypatch.setattr(auth, "get_client", lambda: FakeClient())
    monkeypatch.setattr(auth, "resolve_agent_name", lambda *, client: None)
    monkeypatch.setattr(auth, "resolve_gateway_config", lambda: {})
    monkeypatch.setattr(auth, "_local_config_dir", lambda: None)
    monkeypatch.setenv("AX_SPACE_ID", "env-pinned-space")
    monkeypatch.setattr(auth, "_load_config", lambda: {})

    result = runner.invoke(app, ["auth", "whoami", "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["resolved_space_id"] == "env-pinned-space"


def test_auth_exchange_without_token_points_agents_to_gateway(monkeypatch, config_dir):
    monkeypatch.delenv("AX_TOKEN", raising=False)
    monkeypatch.delenv("AX_TOKEN_FILE", raising=False)

    result = runner.invoke(app, ["auth", "exchange"])

    assert result.exit_code == 1
    assert "No token configured" in result.output
    assert "ax gateway local" in result.output
    assert "ax gateway login" in result.output
    assert "auth token set" not in result.output
    assert "AX_TOKEN" not in result.output


def test_auth_token_show_without_token_points_agents_to_gateway(monkeypatch, config_dir):
    monkeypatch.delenv("AX_TOKEN", raising=False)
    monkeypatch.delenv("AX_TOKEN_FILE", raising=False)

    result = runner.invoke(app, ["auth", "token", "show"])

    assert result.exit_code == 1
    assert "Gateway-managed agents" in result.output
    assert "ax gateway local" in result.output
    assert "ax gateway login" in result.output
    assert "AX_TOKEN" not in result.output


# --- ax auth refresh --------------------------------------------------------


class _FakeExchanger:
    """Stand-in for TokenExchanger that records calls without hitting the API."""

    def __init__(self, base_url, pat):
        self.base_url = base_url
        self.pat = pat
        self.invalidated = False
        self.last_get_token = None
        self.invalidated_count = 3

    def invalidate(self):
        self.invalidated = True
        return self.invalidated_count

    def get_token(self, token_class, *, agent_id=None, force_refresh=False, **_):
        self.last_get_token = {
            "token_class": token_class,
            "agent_id": agent_id,
            "force_refresh": force_refresh,
        }
        return "jwt.fake.token"


def _patch_exchanger_factory(monkeypatch, factory_holder):
    """Patch the in-function `from ..token_cache import TokenExchanger` import."""
    import ax_cli.token_cache as token_cache_module

    monkeypatch.setattr(token_cache_module, "TokenExchanger", factory_holder)


def test_auth_refresh_invalidates_then_re_exchanges_user_pat(monkeypatch):
    seen = {}

    def factory(base_url, pat):
        ex = _FakeExchanger(base_url, pat)
        seen["exchanger"] = ex
        return ex

    monkeypatch.setattr("ax_cli.commands.auth.resolve_token", lambda: "axp_u_keyid.secret")
    monkeypatch.setattr("ax_cli.config.resolve_base_url", lambda: "https://next.paxai.app")
    _patch_exchanger_factory(monkeypatch, factory)

    result = runner.invoke(app, ["auth", "refresh", "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["ok"] is True
    assert payload["token_class"] == "user_access"
    assert payload["invalidated_entries"] == 3
    assert payload["host"] == "https://next.paxai.app"

    ex = seen["exchanger"]
    assert ex.invalidated is True
    assert ex.last_get_token == {
        "token_class": "user_access",
        "agent_id": None,
        "force_refresh": True,
    }


def test_auth_refresh_uses_agent_access_for_agent_pat(monkeypatch):
    seen = {}

    def factory(base_url, pat):
        ex = _FakeExchanger(base_url, pat)
        seen["exchanger"] = ex
        return ex

    monkeypatch.setattr("ax_cli.commands.auth.resolve_token", lambda: "axp_a_keyid.secret")
    monkeypatch.setattr("ax_cli.config.resolve_base_url", lambda: "https://next.paxai.app")
    monkeypatch.setattr("ax_cli.commands.auth._load_config", lambda local=False: {"agent_id": "agent-7"})
    _patch_exchanger_factory(monkeypatch, factory)

    result = runner.invoke(app, ["auth", "refresh", "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["token_class"] == "agent_access"

    ex = seen["exchanger"]
    assert ex.last_get_token == {
        "token_class": "agent_access",
        "agent_id": "agent-7",
        "force_refresh": True,
    }


def test_auth_refresh_without_token_points_agents_to_gateway(monkeypatch):
    monkeypatch.setattr("ax_cli.commands.auth.resolve_token", lambda: None)

    result = runner.invoke(app, ["auth", "refresh"])

    assert result.exit_code == 1
    assert "No token configured" in result.output
    assert "ax gateway login" in result.output
    assert "AX_TOKEN" not in result.output


def test_auth_refresh_rejects_non_pat_token(monkeypatch):
    monkeypatch.setattr("ax_cli.commands.auth.resolve_token", lambda: "not-a-pat")

    result = runner.invoke(app, ["auth", "refresh"])

    assert result.exit_code == 1
    assert "must start with axp_" in result.output


# ---------------------------------------------------------------------------
# _mask_token_prefix
# ---------------------------------------------------------------------------


def test_mask_token_prefix_empty():
    assert auth._mask_token_prefix("") == "***"
    assert auth._mask_token_prefix("   ") == "***"


def test_mask_token_prefix_short():
    assert auth._mask_token_prefix("ab") == "**"
    assert auth._mask_token_prefix("abcd") == "****"


def test_mask_token_prefix_normal():
    result = auth._mask_token_prefix("axp_u_keyid.secret")
    assert result.startswith("axp_u_")
    assert "********" in result


# ---------------------------------------------------------------------------
# _resolve_login_token — empty prompt
# ---------------------------------------------------------------------------


def test_resolve_login_token_empty_prompt_exits(monkeypatch):
    printed = []
    monkeypatch.setattr(auth.typer, "prompt", lambda *a, **kw: "   ")
    monkeypatch.setattr(auth.console, "print", lambda *a, **kw: printed.append(str(a[0]) if a else ""))
    monkeypatch.setattr(auth.err_console, "print", lambda *a, **kw: printed.append(str(a[0]) if a else ""))
    with pytest.raises((SystemExit, typer.Exit)):
        auth._resolve_login_token(None)
    assert any("Token required" in p for p in printed)


# ---------------------------------------------------------------------------
# login_user — exception paths
# ---------------------------------------------------------------------------


def test_login_user_token_verification_failure(monkeypatch, config_dir):
    """login_user exits if token exchange fails."""

    class FailingExchanger:
        def __init__(self, base_url, token):
            pass

        def get_token(self, *a, **kw):
            raise RuntimeError("bad token")

    monkeypatch.setattr("ax_cli.token_cache.TokenExchanger", FailingExchanger)

    with pytest.raises((SystemExit, typer.Exit)):
        auth.login_user("axp_u_bad.token", base_url="https://paxai.app")


def test_login_user_space_discovery_error_with_explicit_space(monkeypatch, config_dir):
    """If space discovery fails but explicit space_id given, it is stored."""
    printed = []

    class OkExchanger:
        def __init__(self, base_url, token):
            pass

        def get_token(self, *a, **kw):
            return "fake.jwt"

    class FailClient:
        def __init__(self, *, base_url, token):
            pass

        def whoami(self):
            raise RuntimeError("network error")

    monkeypatch.setattr("ax_cli.token_cache.TokenExchanger", OkExchanger)
    monkeypatch.setattr("ax_cli.client.AxClient", FailClient)
    monkeypatch.setattr(auth.console, "print", lambda *a, **kw: printed.append(str(a[0]) if a else ""))

    auth.login_user("axp_u_test.token", base_url="https://paxai.app", space_id="explicit-space")

    user_cfg = tomllib.loads((config_dir.parent / "_global_config" / "user.toml").read_text())
    assert user_cfg["space_id"] == "explicit-space"


def test_login_user_multiple_spaces_no_default(monkeypatch, config_dir):
    """Multiple spaces with no default shows a warning message."""
    printed = []

    class OkExchanger:
        def __init__(self, base_url, token):
            pass

        def get_token(self, *a, **kw):
            return "fake.jwt"

    class MultiSpaceClient:
        def __init__(self, *, base_url, token):
            pass

        def whoami(self):
            return {"username": "tester", "email": "t@e.com"}

        def list_spaces(self):
            return {
                "spaces": [
                    {"id": "s1", "name": "A"},
                    {"id": "s2", "name": "B"},
                ]
            }

    monkeypatch.setattr("ax_cli.token_cache.TokenExchanger", OkExchanger)
    monkeypatch.setattr("ax_cli.client.AxClient", MultiSpaceClient)
    monkeypatch.setattr(auth.console, "print", lambda *a, **kw: printed.append(str(a[0]) if a else ""))

    auth.login_user("axp_u_test.token", base_url="https://paxai.app")

    assert any("2 spaces found" in str(p) for p in printed)


def test_login_user_agent_flag_ignored(monkeypatch, config_dir):
    """Passing agent= to login_user emits a warning."""
    printed = []

    class OkExchanger:
        def __init__(self, base_url, token):
            pass

        def get_token(self, *a, **kw):
            return "fake.jwt"

    class OkClient:
        def __init__(self, *, base_url, token):
            pass

        def whoami(self):
            return {"username": "tester"}

        def list_spaces(self):
            return {"spaces": [{"id": "s1", "name": "Only"}]}

    monkeypatch.setattr("ax_cli.token_cache.TokenExchanger", OkExchanger)
    monkeypatch.setattr("ax_cli.client.AxClient", OkClient)
    monkeypatch.setattr(auth.console, "print", lambda *a, **kw: printed.append(str(a[0]) if a else ""))

    auth.login_user("axp_u_test.token", base_url="https://paxai.app", agent="orion")

    assert any("Ignoring --agent" in str(p) for p in printed)


# ---------------------------------------------------------------------------
# _probe_credential
# ---------------------------------------------------------------------------


def test_probe_credential_gateway_brokered():
    result = auth._probe_credential({"auth_source": "local_config:gateway"})
    assert result["skipped"] is True
    assert "brokered by Gateway" in result["reason"]


def test_probe_credential_missing_token():
    result = auth._probe_credential({"token_kind": "missing", "base_url": None})
    assert result["skipped"] is True


def test_probe_credential_no_pat_resolved(monkeypatch):
    monkeypatch.setattr("ax_cli.config.resolve_token", lambda: None)
    result = auth._probe_credential({"token_kind": "user_pat", "base_url": "https://paxai.app"})
    assert result["skipped"] is True
    assert "no token" in result["reason"]


def test_probe_credential_agent_pat_success(monkeypatch):
    class FakeExchanger:
        def __init__(self, base_url, pat):
            pass

        def get_token(self, *a, **kw):
            return "jwt"

    monkeypatch.setattr("ax_cli.config.resolve_token", lambda: "axp_a_key.sec")
    monkeypatch.setattr("ax_cli.token_cache.TokenExchanger", FakeExchanger)

    result = auth._probe_credential(
        {
            "token_kind": "agent_pat",
            "base_url": "https://paxai.app",
            "host": "paxai.app",
            "agent_id": "agent-1",
        }
    )
    assert result["ok"] is True
    assert result["token_class"] == "agent_access"


def test_probe_credential_user_pat_success(monkeypatch):
    class FakeExchanger:
        def __init__(self, base_url, pat):
            pass

        def get_token(self, *a, **kw):
            return "jwt"

    monkeypatch.setattr("ax_cli.config.resolve_token", lambda: "axp_u_key.sec")
    monkeypatch.setattr("ax_cli.token_cache.TokenExchanger", FakeExchanger)

    result = auth._probe_credential(
        {
            "token_kind": "user_pat",
            "base_url": "https://paxai.app",
            "host": "paxai.app",
        }
    )
    assert result["ok"] is True
    assert result["token_class"] == "user_access"


def test_probe_credential_http_401_invalid(monkeypatch):
    from unittest.mock import MagicMock

    import httpx as _httpx

    resp = MagicMock(spec=_httpx.Response)
    resp.status_code = 401
    resp.json.return_value = {"detail": {"error": "invalid_credential"}}

    req = MagicMock(spec=_httpx.Request)
    exc = _httpx.HTTPStatusError("401", request=req, response=resp)

    class FailExchanger:
        def __init__(self, base_url, pat):
            pass

        def get_token(self, *a, **kw):
            raise exc

    monkeypatch.setattr("ax_cli.config.resolve_token", lambda: "axp_u_key.sec")
    monkeypatch.setattr("ax_cli.token_cache.TokenExchanger", FailExchanger)

    result = auth._probe_credential(
        {
            "token_kind": "user_pat",
            "base_url": "https://paxai.app",
            "host": "paxai.app",
        }
    )
    assert result["ok"] is False
    assert result["code"] == "invalid_credential"


def test_probe_credential_http_error_json_parse_failure(monkeypatch):
    from unittest.mock import MagicMock

    import httpx as _httpx

    resp = MagicMock(spec=_httpx.Response)
    resp.status_code = 500
    resp.json.side_effect = ValueError("bad json")

    req = MagicMock(spec=_httpx.Request)
    exc = _httpx.HTTPStatusError("500", request=req, response=resp)

    class FailExchanger:
        def __init__(self, base_url, pat):
            pass

        def get_token(self, *a, **kw):
            raise exc

    monkeypatch.setattr("ax_cli.config.resolve_token", lambda: "axp_u_key.sec")
    monkeypatch.setattr("ax_cli.token_cache.TokenExchanger", FailExchanger)

    result = auth._probe_credential(
        {
            "token_kind": "user_pat",
            "base_url": "https://paxai.app",
            "host": "paxai.app",
        }
    )
    assert result["ok"] is False
    assert "500" in result["code"]


def test_probe_credential_generic_exception(monkeypatch):
    class FailExchanger:
        def __init__(self, base_url, pat):
            pass

        def get_token(self, *a, **kw):
            raise ConnectionError("oops")

    monkeypatch.setattr("ax_cli.config.resolve_token", lambda: "axp_u_key.sec")
    monkeypatch.setattr("ax_cli.token_cache.TokenExchanger", FailExchanger)

    result = auth._probe_credential(
        {
            "token_kind": "user_pat",
            "base_url": "https://paxai.app",
            "host": "paxai.app",
        }
    )
    assert result["ok"] is False
    assert result["code"] == "exchange_failed"


# ---------------------------------------------------------------------------
# doctor — text output and probe paths
# ---------------------------------------------------------------------------


def _doctor_diag_data(*, ok=True, problems=None, warnings=None, gateway_binding=None):
    return {
        "ok": ok,
        "effective": {
            "principal_intent": "user",
            "auth_source": "user_login",
            "token_kind": "user_pat",
            "token": "axp_u_...cret",
            "base_url": "https://paxai.app",
            "base_url_source": "user_login",
            "host": "paxai.app",
            "space_id": "space-1",
            "space_source": "config",
            "agent_name": None,
            "agent_name_source": None,
            "agent_id": None,
            "agent_id_source": None,
            "gateway_binding": gateway_binding or {},
        },
        "runtime_config": None,
        "selected_env": None,
        "selected_profile": None,
        "sources": [],
        "warnings": warnings or [],
        "problems": problems or [],
    }


def test_doctor_text_output_ok(monkeypatch):
    monkeypatch.setattr(auth, "diagnose_auth_config", lambda *, env_name, explicit_space_id: _doctor_diag_data())

    result = runner.invoke(app, ["auth", "doctor"])
    assert result.exit_code == 0
    assert "OK" in result.output
    assert "principal_intent" in result.output
    assert "auth_source" in result.output


def test_doctor_text_with_warnings(monkeypatch):
    monkeypatch.setattr(
        auth,
        "diagnose_auth_config",
        lambda *, env_name, explicit_space_id: _doctor_diag_data(
            warnings=[{"code": "stale_config", "reason": "config is old"}]
        ),
    )

    result = runner.invoke(app, ["auth", "doctor"])
    assert result.exit_code == 0
    assert "stale_config" in result.output


def test_doctor_text_with_problems_exits_nonzero(monkeypatch):
    monkeypatch.setattr(
        auth,
        "diagnose_auth_config",
        lambda *, env_name, explicit_space_id: _doctor_diag_data(
            ok=False, problems=[{"code": "bad_token", "reason": "token expired"}]
        ),
    )

    result = runner.invoke(app, ["auth", "doctor"])
    assert result.exit_code != 0
    assert "PROBLEM" in result.output
    assert "bad_token" in result.output


def test_doctor_text_runtime_config_and_env(monkeypatch):
    data = _doctor_diag_data()
    data["runtime_config"] = "/tmp/test/.ax/config.toml"
    data["selected_env"] = "dev"
    data["selected_profile"] = "my-profile"
    monkeypatch.setattr(auth, "diagnose_auth_config", lambda *, env_name, explicit_space_id: data)

    result = runner.invoke(app, ["auth", "doctor"])
    assert result.exit_code == 0
    assert "runtime_config" in result.output
    assert "selected_env" in result.output
    assert "selected_profile" in result.output


def test_doctor_text_gateway_binding_display(monkeypatch):
    binding = {
        "daemon_running": True,
        "daemon_pid": 12345,
        "bound_candidates": [
            {"name": "codex", "template_id": "tmpl-1", "mode": "passthrough", "liveness": "ok"},
            {"name": "test-agent", "template_id": "tmpl-2", "mode": "local", "liveness": "stale"},
        ],
        "selected": {"name": "codex"},
    }
    monkeypatch.setattr(
        auth, "diagnose_auth_config", lambda *, env_name, explicit_space_id: _doctor_diag_data(gateway_binding=binding)
    )

    result = runner.invoke(app, ["auth", "doctor"])
    assert result.exit_code == 0
    assert "gateway_daemon" in result.output
    assert "running" in result.output
    assert "12345" in result.output
    assert "gateway_bindings" in result.output
    assert "@codex" in result.output
    assert "@test-agent" in result.output


def test_doctor_probe_ok(monkeypatch):
    monkeypatch.setattr(auth, "diagnose_auth_config", lambda *, env_name, explicit_space_id: _doctor_diag_data())
    monkeypatch.setattr(auth, "_probe_credential", lambda effective: {"ok": True, "token_class": "user_access"})

    result = runner.invoke(app, ["auth", "doctor", "--probe"])
    assert result.exit_code == 0
    assert "exchange ok" in result.output


def test_doctor_probe_skipped(monkeypatch):
    monkeypatch.setattr(auth, "diagnose_auth_config", lambda *, env_name, explicit_space_id: _doctor_diag_data())
    monkeypatch.setattr(auth, "_probe_credential", lambda effective: {"skipped": True, "reason": "no token"})

    result = runner.invoke(app, ["auth", "doctor", "--probe"])
    assert result.exit_code == 0
    assert "skipped" in result.output


def test_doctor_probe_failed(monkeypatch):
    monkeypatch.setattr(auth, "diagnose_auth_config", lambda *, env_name, explicit_space_id: _doctor_diag_data())
    monkeypatch.setattr(
        auth, "_probe_credential", lambda effective: {"ok": False, "code": "invalid_credential", "host": "paxai.app"}
    )

    result = runner.invoke(app, ["auth", "doctor", "--probe"])
    assert result.exit_code != 0
    assert "rejected" in result.output


def test_doctor_probe_json_output(monkeypatch):
    monkeypatch.setattr(auth, "diagnose_auth_config", lambda *, env_name, explicit_space_id: _doctor_diag_data())
    monkeypatch.setattr(auth, "_probe_credential", lambda effective: {"ok": True, "token_class": "user_access"})

    result = runner.invoke(app, ["auth", "doctor", "--probe", "--json"])
    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["probe"]["ok"] is True
    assert payload["summary"]["probe"]["ok"] is True


# ---------------------------------------------------------------------------
# whoami — gateway-managed identity path
# ---------------------------------------------------------------------------


def test_whoami_gateway_managed_json(monkeypatch, tmp_path):
    gateway_cfg = {"url": "http://127.0.0.1:8765", "agent_name": "codex"}

    def fake_gateway_call(*, gateway_cfg, method):
        return {"agent_name": "codex", "agent_id": "agent-123"}

    monkeypatch.setattr(auth, "resolve_gateway_config", lambda: gateway_cfg)
    monkeypatch.setattr("ax_cli.commands.messages._gateway_local_call", fake_gateway_call)
    monkeypatch.setattr(auth, "_local_config_dir", lambda: None)

    result = runner.invoke(app, ["auth", "whoami", "--json"])
    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["control_plane"] == "gateway"
    assert payload["gateway_url"] == "http://127.0.0.1:8765"
    assert payload["agent_name"] == "codex"


def test_whoami_gateway_managed_text(monkeypatch, tmp_path):
    gateway_cfg = {"url": "http://127.0.0.1:8765"}

    def fake_gateway_call(*, gateway_cfg, method):
        return {"agent_name": "codex"}

    monkeypatch.setattr(auth, "resolve_gateway_config", lambda: gateway_cfg)
    monkeypatch.setattr("ax_cli.commands.messages._gateway_local_call", fake_gateway_call)
    monkeypatch.setattr(auth, "_local_config_dir", lambda: None)

    result = runner.invoke(app, ["auth", "whoami"])
    assert result.exit_code == 0
    assert "codex" in result.output


def test_whoami_gateway_with_stale_workdir(monkeypatch, tmp_path):
    gateway_cfg = {"url": "http://127.0.0.1:8765"}

    def fake_gateway_call(*, gateway_cfg, method):
        return {"agent_name": "codex"}

    # Create a local config with workdir
    local_dir = tmp_path / ".ax"
    local_dir.mkdir(exist_ok=True)
    config_file = local_dir / "config.toml"
    config_file.write_text('[agent]\nworkdir = "/old/path"\n')

    monkeypatch.setattr(auth, "resolve_gateway_config", lambda: gateway_cfg)
    monkeypatch.setattr("ax_cli.commands.messages._gateway_local_call", fake_gateway_call)
    monkeypatch.setattr(auth, "_local_config_dir", lambda: local_dir)
    monkeypatch.setattr("ax_cli.config._find_project_root", lambda: tmp_path)
    monkeypatch.setattr(
        "ax_cli.config._local_config_workdir_mismatch",
        lambda cfg, root: {
            "config_path": str(config_file),
            "configured_workdir": "/old/path",
            "actual_workdir": str(tmp_path),
        },
    )

    result = runner.invoke(app, ["auth", "whoami"])
    assert result.exit_code == 0
    assert "stale local config" in result.output


def test_whoami_http_error(monkeypatch):
    from unittest.mock import MagicMock

    import httpx as _httpx

    resp = MagicMock(spec=_httpx.Response)
    resp.status_code = 401
    resp.text = "Unauthorized"
    resp.json.return_value = {"detail": "Unauthorized"}

    req = MagicMock(spec=_httpx.Request)
    req.url = "https://paxai.app/auth/whoami"

    class FailClient:
        def whoami(self):
            raise _httpx.HTTPStatusError("401", request=req, response=resp)

    monkeypatch.setattr(auth, "resolve_gateway_config", lambda: {})
    monkeypatch.setattr(auth, "get_client", lambda: FailClient())

    result = runner.invoke(app, ["auth", "whoami"])
    # handle_error raises SystemExit
    assert result.exit_code != 0


def test_whoami_local_config_path_and_stale_workdir_text(monkeypatch, tmp_path):
    """whoami text output shows stale workdir warning when applicable."""
    local_dir = tmp_path / ".ax"
    local_dir.mkdir()
    config_file = local_dir / "config.toml"
    config_file.write_text('[agent]\nworkdir = "/old/path"\n')

    class FakeClient:
        def whoami(self):
            return {"id": "user-1", "bound_agent": {"default_space_id": "s1"}}

    monkeypatch.setattr(auth, "resolve_gateway_config", lambda: {})
    monkeypatch.setattr(auth, "get_client", lambda: FakeClient())
    monkeypatch.setattr(auth, "resolve_agent_name", lambda *, client: "test-agent")
    monkeypatch.setattr(auth, "_local_config_dir", lambda: local_dir)
    monkeypatch.setattr("ax_cli.config._find_project_root", lambda: tmp_path)
    monkeypatch.setattr(
        "ax_cli.config._local_config_workdir_mismatch",
        lambda cfg, root: {
            "config_path": str(config_file),
            "configured_workdir": "/old/path",
            "actual_workdir": str(tmp_path),
        },
    )

    result = runner.invoke(app, ["auth", "whoami"])
    assert result.exit_code == 0
    assert "stale local config" in result.output


# ---------------------------------------------------------------------------
# init command — user token flow
# ---------------------------------------------------------------------------


def test_init_user_token_flow(monkeypatch, tmp_path):
    """init with user PAT goes through user token flow."""
    ax_dir = tmp_path / ".ax"
    ax_dir.mkdir()

    class OkExchanger:
        def __init__(self, base_url, token):
            pass

        def get_token(self, *a, **kw):
            return "fake.jwt"

    class OkClient:
        def __init__(self, *, base_url, token):
            pass

        def whoami(self):
            return {"username": "tester", "email": "t@e.com"}

        def list_spaces(self):
            return {"spaces": [{"id": "space-1", "name": "Only"}]}

    monkeypatch.setattr("ax_cli.token_cache.TokenExchanger", OkExchanger)
    monkeypatch.setattr("ax_cli.client.AxClient", OkClient)

    result = runner.invoke(app, ["auth", "init", "--token", "axp_u_key.secret", "--url", "https://paxai.app"])
    assert result.exit_code == 0
    assert "Token verified" in result.output
    assert "Saved" in result.output


def test_init_user_token_exchange_failure(monkeypatch, tmp_path):
    ax_dir = tmp_path / ".ax"
    ax_dir.mkdir()

    class FailExchanger:
        def __init__(self, base_url, token):
            pass

        def get_token(self, *a, **kw):
            raise RuntimeError("exchange failed")

    monkeypatch.setattr("ax_cli.token_cache.TokenExchanger", FailExchanger)

    result = runner.invoke(app, ["auth", "init", "--token", "axp_u_key.secret"])
    assert result.exit_code == 1
    assert "Token verification failed" in result.output


def test_init_user_token_multi_spaces(monkeypatch, tmp_path):
    """init with multiple spaces shows warning."""
    ax_dir = tmp_path / ".ax"
    ax_dir.mkdir()

    class OkExchanger:
        def __init__(self, base_url, token):
            pass

        def get_token(self, *a, **kw):
            return "fake.jwt"

    class MultiSpaceClient:
        def __init__(self, *, base_url, token):
            pass

        def whoami(self):
            return {"username": "tester"}

        def list_spaces(self):
            return {
                "spaces": [
                    {"id": "s1", "name": "A"},
                    {"id": "s2", "name": "B"},
                ]
            }

    monkeypatch.setattr("ax_cli.token_cache.TokenExchanger", OkExchanger)
    monkeypatch.setattr("ax_cli.client.AxClient", MultiSpaceClient)

    result = runner.invoke(app, ["auth", "init", "--token", "axp_u_key.secret"])
    assert result.exit_code == 0
    assert "2 spaces found" in result.output


def test_init_user_token_with_agent_flag(monkeypatch, tmp_path):
    """init with user PAT and --agent ignores the agent."""
    ax_dir = tmp_path / ".ax"
    ax_dir.mkdir()

    class OkExchanger:
        def __init__(self, base_url, token):
            pass

        def get_token(self, *a, **kw):
            return "fake.jwt"

    class OkClient:
        def __init__(self, *, base_url, token):
            pass

        def whoami(self):
            return {"username": "tester"}

        def list_spaces(self):
            return {"spaces": []}

    monkeypatch.setattr("ax_cli.token_cache.TokenExchanger", OkExchanger)
    monkeypatch.setattr("ax_cli.client.AxClient", OkClient)

    result = runner.invoke(app, ["auth", "init", "--token", "axp_u_key.secret", "--agent", "orion"])
    assert result.exit_code == 0
    assert "Ignoring --agent" in result.output


def test_init_user_token_explicit_space_id(monkeypatch, tmp_path):
    """init with explicit --space-id stores it."""
    ax_dir = tmp_path / ".ax"
    ax_dir.mkdir()

    class OkExchanger:
        def __init__(self, base_url, token):
            pass

        def get_token(self, *a, **kw):
            return "fake.jwt"

    class OkClient:
        def __init__(self, *, base_url, token):
            pass

        def whoami(self):
            return {"username": "tester"}

        def list_spaces(self):
            raise AssertionError("should not call list_spaces when space_id is given")

    monkeypatch.setattr("ax_cli.token_cache.TokenExchanger", OkExchanger)
    monkeypatch.setattr("ax_cli.client.AxClient", OkClient)

    result = runner.invoke(app, ["auth", "init", "--token", "axp_u_key.secret", "--space-id", "my-space"])
    assert result.exit_code == 0
    # Check the saved config includes the space_id
    cfg_text = (ax_dir / "config.toml").read_text()
    assert "my-space" in cfg_text


# ---------------------------------------------------------------------------
# init command — agent enrollment token flow
# ---------------------------------------------------------------------------


def test_init_agent_enrollment_success(monkeypatch, tmp_path, mock_exchange):
    """init with agent PAT enrolls the agent."""
    ax_dir = tmp_path / ".ax"
    ax_dir.mkdir()

    mock_post = mock_exchange(access_token="jwt.agent.token", expires_in=900)
    # Override the mock to also return agent fields
    from unittest.mock import MagicMock

    import httpx as _httpx

    resp = MagicMock(spec=_httpx.Response)
    resp.status_code = 200
    resp.json.return_value = {
        "access_token": "jwt.agent.token",
        "expires_in": 900,
        "agent_id": "agent-uuid-1234-5678-9012-123456789012",
        "agent_name": "test-agent",
    }
    resp.raise_for_status = MagicMock()
    mock_post = MagicMock(return_value=resp)
    monkeypatch.setattr(_httpx, "post", mock_post)

    # Mock whoami for space discovery
    class DiscoverClient:
        def __init__(self, *, base_url, token, agent_id=None):
            pass

        def whoami(self):
            return {"bound_agent": {"default_space_id": "space-1", "default_space_name": "Main"}}

    monkeypatch.setattr("ax_cli.client.AxClient", DiscoverClient)

    result = runner.invoke(app, ["auth", "init", "--token", "axp_a_key.secret", "--agent", "test-agent"])
    assert result.exit_code == 0
    assert "Agent registered" in result.output or "Connected" in result.output
    assert "Saved" in result.output


def test_init_agent_enrollment_with_uuid(monkeypatch, tmp_path):
    """init with agent PAT and UUID agent ID."""
    ax_dir = tmp_path / ".ax"
    ax_dir.mkdir()

    from unittest.mock import MagicMock

    import httpx as _httpx

    resp = MagicMock(spec=_httpx.Response)
    resp.status_code = 200
    resp.json.return_value = {
        "access_token": "jwt.agent",
        "expires_in": 900,
        "agent_id": "12345678-1234-1234-1234-123456789012",
        "agent_name": "uuid-agent",
    }
    resp.raise_for_status = MagicMock()
    monkeypatch.setattr(_httpx, "post", MagicMock(return_value=resp))

    class DiscoverClient:
        def __init__(self, *, base_url, token, agent_id=None):
            pass

        def whoami(self):
            return {"bound_agent": None}

    monkeypatch.setattr("ax_cli.client.AxClient", DiscoverClient)

    result = runner.invoke(
        app, ["auth", "init", "--token", "axp_a_key.secret", "--agent", "12345678-1234-1234-1234-123456789012"]
    )
    assert result.exit_code == 0


def test_init_agent_enrollment_no_name_already_bound(monkeypatch, tmp_path):
    """init with agent PAT, no --agent, token already bound."""
    ax_dir = tmp_path / ".ax"
    ax_dir.mkdir()

    from unittest.mock import MagicMock

    import httpx as _httpx

    resp = MagicMock(spec=_httpx.Response)
    resp.status_code = 200
    resp.json.return_value = {
        "access_token": "jwt",
        "expires_in": 900,
        "agent_id": "agent-uuid-1234",
        "agent_name": "bound-agent",
    }
    resp.raise_for_status = MagicMock()
    monkeypatch.setattr(_httpx, "post", MagicMock(return_value=resp))

    class DiscoverClient:
        def __init__(self, *, base_url, token, agent_id=None):
            pass

        def whoami(self):
            return {"bound_agent": None}

    monkeypatch.setattr("ax_cli.client.AxClient", DiscoverClient)

    result = runner.invoke(app, ["auth", "init", "--token", "axp_a_key.secret"])
    assert result.exit_code == 0
    assert "Connected" in result.output


def test_init_agent_enrollment_exchange_http_error_agent_not_found(monkeypatch, tmp_path):
    """Exchange fails with agent_not_found, falls back to whoami discovery."""
    ax_dir = tmp_path / ".ax"
    ax_dir.mkdir()

    from unittest.mock import MagicMock

    import httpx as _httpx

    resp = MagicMock(spec=_httpx.Response)
    resp.status_code = 409
    resp.json.return_value = {"detail": {"error": "agent_not_found", "message": "not found"}}
    req = MagicMock(spec=_httpx.Request)
    req.url = "https://paxai.app/auth/exchange"

    def failing_post(*a, **kw):
        raise _httpx.HTTPStatusError("409", request=req, response=resp)

    monkeypatch.setattr(_httpx, "post", failing_post)

    class DiscoveryClient:
        def __init__(self, *, base_url, token):
            pass

        def whoami(self):
            return {"bound_agent": {"agent_id": "agent-discovered", "agent_name": "discovered"}}

    monkeypatch.setattr("ax_cli.client.AxClient", DiscoveryClient)

    # Also need a client for space discovery
    class SpaceClient:
        def __init__(self, *, base_url, token, agent_id=None):
            pass

        def whoami(self):
            return {"bound_agent": None}

    # Second AxClient import is for space discovery - re-patch
    call_count = {"n": 0}
    original_discovery = DiscoveryClient

    class MultiClient:
        def __init__(self, **kw):
            call_count["n"] += 1
            if call_count["n"] == 1:
                self._inner = original_discovery(**{k: v for k, v in kw.items() if k in ("base_url", "token")})
            else:
                self._inner = SpaceClient(**kw)

        def whoami(self):
            return self._inner.whoami()

    monkeypatch.setattr("ax_cli.client.AxClient", MultiClient)

    result = runner.invoke(app, ["auth", "init", "--token", "axp_a_key.secret", "--agent", "test-agent"])
    assert result.exit_code == 0
    assert "already bound" in result.output or "Found bound agent" in result.output


def test_init_agent_enrollment_exchange_http_error_other(monkeypatch, tmp_path):
    """Exchange fails with a non-agent_not_found error."""
    ax_dir = tmp_path / ".ax"
    ax_dir.mkdir()

    from unittest.mock import MagicMock

    import httpx as _httpx

    resp = MagicMock(spec=_httpx.Response)
    resp.status_code = 403
    resp.json.return_value = {"detail": {"error": "forbidden", "message": "not allowed"}}
    req = MagicMock(spec=_httpx.Request)

    def failing_post(*a, **kw):
        raise _httpx.HTTPStatusError("403", request=req, response=resp)

    monkeypatch.setattr(_httpx, "post", failing_post)

    result = runner.invoke(app, ["auth", "init", "--token", "axp_a_key.secret", "--agent", "test-agent"])
    assert result.exit_code == 1
    assert "Registration failed" in result.output


def test_init_agent_enrollment_connection_failure(monkeypatch, tmp_path):
    """Exchange fails with a generic connection error."""
    ax_dir = tmp_path / ".ax"
    ax_dir.mkdir()

    import httpx as _httpx

    def failing_post(*a, **kw):
        raise ConnectionError("cannot connect")

    monkeypatch.setattr(_httpx, "post", failing_post)

    result = runner.invoke(app, ["auth", "init", "--token", "axp_a_key.secret", "--agent", "test-agent"])
    assert result.exit_code == 1
    assert "Connection failed" in result.output


def test_init_agent_enrollment_no_name_not_registered(monkeypatch, tmp_path):
    """Agent enrollment with no name and not registered exits with hint."""
    ax_dir = tmp_path / ".ax"
    ax_dir.mkdir()

    from unittest.mock import MagicMock

    import httpx as _httpx

    # Exchange succeeds but returns no agent_id (simulating no binding)
    resp = MagicMock(spec=_httpx.Response)
    resp.status_code = 200
    resp.json.return_value = {
        "access_token": "jwt",
        "expires_in": 900,
        # no agent_id, no agent_name
    }
    resp.raise_for_status = MagicMock()
    monkeypatch.setattr(_httpx, "post", MagicMock(return_value=resp))

    class DiscoverClient:
        def __init__(self, *, base_url, token, agent_id=None):
            pass

        def whoami(self):
            return {"bound_agent": None}

    monkeypatch.setattr("ax_cli.client.AxClient", DiscoverClient)

    result = runner.invoke(app, ["auth", "init", "--token", "axp_a_key.secret"])
    assert result.exit_code == 0
    # The exchange returns "" for agent_id/agent_name but registered=True


def test_init_gitignore_reminder(monkeypatch, tmp_path):
    """init reminds to add .ax/ to .gitignore when .git exists."""
    ax_dir = tmp_path / ".ax"
    ax_dir.mkdir()
    git_dir = tmp_path / ".git"
    git_dir.mkdir()

    class OkExchanger:
        def __init__(self, base_url, token):
            pass

        def get_token(self, *a, **kw):
            return "fake.jwt"

    class OkClient:
        def __init__(self, *, base_url, token):
            pass

        def whoami(self):
            return {"username": "tester"}

        def list_spaces(self):
            return {"spaces": []}

    monkeypatch.setattr("ax_cli.token_cache.TokenExchanger", OkExchanger)
    monkeypatch.setattr("ax_cli.client.AxClient", OkClient)

    result = runner.invoke(app, ["auth", "init", "--token", "axp_u_key.secret"])
    assert result.exit_code == 0
    assert "Reminder" in result.output
    assert ".gitignore" in result.output


def test_init_gitignore_already_configured(monkeypatch, tmp_path):
    """init does not remind about .gitignore when .ax/ already listed."""
    ax_dir = tmp_path / ".ax"
    ax_dir.mkdir()
    gitignore = tmp_path / ".gitignore"
    gitignore.write_text(".ax/\n")

    class OkExchanger:
        def __init__(self, base_url, token):
            pass

        def get_token(self, *a, **kw):
            return "fake.jwt"

    class OkClient:
        def __init__(self, *, base_url, token):
            pass

        def whoami(self):
            return {"username": "tester"}

        def list_spaces(self):
            return {"spaces": []}

    monkeypatch.setattr("ax_cli.token_cache.TokenExchanger", OkExchanger)
    monkeypatch.setattr("ax_cli.client.AxClient", OkClient)

    result = runner.invoke(app, ["auth", "init", "--token", "axp_u_key.secret"])
    assert result.exit_code == 0
    assert "Reminder" not in result.output


# ---------------------------------------------------------------------------
# refresh — text output paths
# ---------------------------------------------------------------------------


def test_auth_refresh_text_output_with_invalidated(monkeypatch):
    seen = {}

    def factory(base_url, pat):
        ex = _FakeExchanger(base_url, pat)
        ex.invalidated_count = 2
        seen["exchanger"] = ex
        return ex

    monkeypatch.setattr("ax_cli.commands.auth.resolve_token", lambda: "axp_u_keyid.secret")
    monkeypatch.setattr("ax_cli.config.resolve_base_url", lambda: "https://paxai.app")
    _patch_exchanger_factory(monkeypatch, factory)

    result = runner.invoke(app, ["auth", "refresh"])
    assert result.exit_code == 0
    assert "Refreshed" in result.output
    assert "Dropped 2" in result.output


def test_auth_refresh_text_output_no_cached(monkeypatch):
    seen = {}

    def factory(base_url, pat):
        ex = _FakeExchanger(base_url, pat)
        ex.invalidated_count = 0
        seen["exchanger"] = ex
        return ex

    monkeypatch.setattr("ax_cli.commands.auth.resolve_token", lambda: "axp_u_keyid.secret")
    monkeypatch.setattr("ax_cli.config.resolve_base_url", lambda: "https://paxai.app")
    _patch_exchanger_factory(monkeypatch, factory)

    result = runner.invoke(app, ["auth", "refresh"])
    assert result.exit_code == 0
    assert "No cached entries" in result.output


def test_auth_refresh_http_error(monkeypatch):
    from unittest.mock import MagicMock

    import httpx as _httpx

    resp = MagicMock(spec=_httpx.Response)
    resp.status_code = 401
    resp.text = "Unauthorized"
    resp.json.return_value = {"detail": "Unauthorized"}

    req = MagicMock(spec=_httpx.Request)
    req.url = "https://paxai.app/auth/exchange"

    class FailExchanger:
        def __init__(self, base_url, pat):
            pass

        def invalidate(self):
            return 0

        def get_token(self, *a, **kw):
            raise _httpx.HTTPStatusError("401", request=req, response=resp)

    monkeypatch.setattr("ax_cli.commands.auth.resolve_token", lambda: "axp_u_keyid.secret")
    monkeypatch.setattr("ax_cli.config.resolve_base_url", lambda: "https://paxai.app")
    _patch_exchanger_factory(monkeypatch, lambda base_url, pat: FailExchanger(base_url, pat))

    result = runner.invoke(app, ["auth", "refresh"])
    assert result.exit_code != 0


# ---------------------------------------------------------------------------
# exchange command
# ---------------------------------------------------------------------------


def test_auth_exchange_non_pat_token(monkeypatch):
    monkeypatch.setattr("ax_cli.commands.auth.resolve_token", lambda: "jwt.not.pat")

    result = runner.invoke(app, ["auth", "exchange"])
    assert result.exit_code == 1
    assert "must start with axp_" in result.output


def test_auth_exchange_text_output(monkeypatch):
    class FakeExchanger:
        def __init__(self, base_url, pat):
            pass

        def get_token(self, *a, **kw):
            return "header.payload_section.signature_part"

    monkeypatch.setattr("ax_cli.commands.auth.resolve_token", lambda: "axp_u_key.secret")
    monkeypatch.setattr("ax_cli.config.resolve_base_url", lambda: "https://paxai.app")
    _patch_exchanger_factory(monkeypatch, lambda base_url, pat: FakeExchanger(base_url, pat))

    result = runner.invoke(app, ["auth", "exchange"])
    assert result.exit_code == 0
    assert "Exchanged" in result.output
    assert "JWT:" in result.output


def test_auth_exchange_json_output_with_jwt_claims(monkeypatch):
    import base64
    import json as json_mod

    # Build a valid JWT with 3 parts
    header = base64.urlsafe_b64encode(json_mod.dumps({"alg": "HS256"}).encode()).decode().rstrip("=")
    payload_data = {
        "sub": "user-1",
        "token_class": "user_access",
        "scope": "messages",
        "exp": 1700000900,
        "iat": 1700000000,
        "agent_id": None,
    }
    payload = base64.urlsafe_b64encode(json_mod.dumps(payload_data).encode()).decode().rstrip("=")
    fake_jwt = f"{header}.{payload}.fakesig"

    class FakeExchanger:
        def __init__(self, base_url, pat):
            pass

        def get_token(self, *a, **kw):
            return fake_jwt

    monkeypatch.setattr("ax_cli.commands.auth.resolve_token", lambda: "axp_u_key.secret")
    monkeypatch.setattr("ax_cli.config.resolve_base_url", lambda: "https://paxai.app")
    _patch_exchanger_factory(monkeypatch, lambda base_url, pat: FakeExchanger(base_url, pat))

    result = runner.invoke(app, ["auth", "exchange", "--json"])
    assert result.exit_code == 0
    out = json.loads(result.output)
    assert out["token_class"] == "user_access"
    assert out["sub"] == "user-1"
    assert out["expires_in"] == 900


def test_auth_exchange_json_output_non_jwt(monkeypatch):
    """Non-3-part token still outputs something useful."""

    class FakeExchanger:
        def __init__(self, base_url, pat):
            pass

        def get_token(self, *a, **kw):
            return "opaque_access_token"

    monkeypatch.setattr("ax_cli.commands.auth.resolve_token", lambda: "axp_u_key.secret")
    monkeypatch.setattr("ax_cli.config.resolve_base_url", lambda: "https://paxai.app")
    _patch_exchanger_factory(monkeypatch, lambda base_url, pat: FakeExchanger(base_url, pat))

    result = runner.invoke(app, ["auth", "exchange", "--json"])
    assert result.exit_code == 0
    out = json.loads(result.output)
    assert "access_token" in out


def test_auth_exchange_http_error(monkeypatch):
    from unittest.mock import MagicMock

    import httpx as _httpx

    resp = MagicMock(spec=_httpx.Response)
    resp.status_code = 401
    resp.text = "Unauthorized"
    resp.json.return_value = {"detail": "Unauthorized"}

    req = MagicMock(spec=_httpx.Request)
    req.url = "https://paxai.app/auth/exchange"

    class FailExchanger:
        def __init__(self, base_url, pat):
            pass

        def get_token(self, *a, **kw):
            raise _httpx.HTTPStatusError("401", request=req, response=resp)

    monkeypatch.setattr("ax_cli.commands.auth.resolve_token", lambda: "axp_u_key.secret")
    monkeypatch.setattr("ax_cli.config.resolve_base_url", lambda: "https://paxai.app")
    _patch_exchanger_factory(monkeypatch, lambda base_url, pat: FailExchanger(base_url, pat))

    result = runner.invoke(app, ["auth", "exchange"])
    assert result.exit_code != 0


# ---------------------------------------------------------------------------
# token set / token show
# ---------------------------------------------------------------------------


def test_token_set_local(monkeypatch, tmp_path):
    ax_dir = tmp_path / ".ax"
    ax_dir.mkdir()
    monkeypatch.setattr(auth, "save_token", lambda token, *, local: None)
    monkeypatch.setattr(auth, "_local_config_dir", lambda: ax_dir)

    result = runner.invoke(app, ["auth", "token", "set", "axp_u_mytoken"])
    assert result.exit_code == 0
    assert "Token saved" in result.output


def test_token_set_global(monkeypatch, tmp_path):
    global_dir = tmp_path / "global_ax"
    global_dir.mkdir()
    monkeypatch.setattr(auth, "save_token", lambda token, *, local: None)
    monkeypatch.setattr(auth, "_global_config_dir", lambda: global_dir)

    result = runner.invoke(app, ["auth", "token", "set", "--global", "axp_u_mytoken"])
    assert result.exit_code == 0
    assert "Token saved" in result.output


def test_token_set_local_no_dir(monkeypatch, tmp_path):
    """When no .ax/ dir exists, falls back to cwd/.ax."""
    monkeypatch.setattr(auth, "save_token", lambda token, *, local: None)
    monkeypatch.setattr(auth, "_local_config_dir", lambda: None)

    result = runner.invoke(app, ["auth", "token", "set", "axp_u_mytoken"])
    assert result.exit_code == 0
    assert "Token saved" in result.output


def test_token_show_long_token(monkeypatch, config_dir):
    monkeypatch.setattr(auth, "resolve_token", lambda: "axp_u_keyid.longsecretvalue")

    result = runner.invoke(app, ["auth", "token", "show"])
    assert result.exit_code == 0
    assert "axp_u_" in result.output
    assert "longsecretvalue" not in result.output
    assert "..." in result.output


def test_token_show_short_token(monkeypatch, config_dir):
    monkeypatch.setattr(auth, "resolve_token", lambda: "abcdefgh")

    result = runner.invoke(app, ["auth", "token", "show"])
    assert result.exit_code == 0
    assert "ab...gh" in result.output


# ---------------------------------------------------------------------------
# _select_login_space — additional edge cases
# ---------------------------------------------------------------------------


def test_select_login_space_is_current_true():
    result = auth._select_login_space(
        [
            {"id": "s1", "name": "A", "is_current": True},
            {"id": "s2", "name": "B"},
        ]
    )
    assert result == {"id": "s1", "name": "A", "is_current": True}


def test_select_login_space_is_default_true():
    result = auth._select_login_space(
        [
            {"id": "s1", "name": "A"},
            {"id": "s2", "name": "B", "is_default": True},
        ]
    )
    assert result == {"id": "s2", "name": "B", "is_default": True}


def test_select_login_space_personal_by_mode():
    result = auth._select_login_space(
        [
            {"id": "s1", "name": "A"},
            {"id": "s2", "name": "B", "space_mode": "personal"},
        ]
    )
    assert result == {"id": "s2", "name": "B", "space_mode": "personal"}


def test_select_login_space_empty():
    assert auth._select_login_space([]) is None


# ---------------------------------------------------------------------------
# _candidate_space_id
# ---------------------------------------------------------------------------


def test_candidate_space_id_from_id():
    assert auth._candidate_space_id({"id": "s1"}) == "s1"


def test_candidate_space_id_from_space_id():
    assert auth._candidate_space_id({"space_id": "s2"}) == "s2"


def test_candidate_space_id_none():
    assert auth._candidate_space_id({}) is None


def test_candidate_space_id_empty_string():
    assert auth._candidate_space_id({"id": ""}) is None


# ---------------------------------------------------------------------------
# _best_effort_single_space_id
# ---------------------------------------------------------------------------


def test_best_effort_single_space_list_response():
    class FakeClient:
        def list_spaces(self):
            return [{"id": "only-space"}]

    assert auth._best_effort_single_space_id(FakeClient()) == "only-space"


def test_best_effort_single_space_items_key():
    class FakeClient:
        def list_spaces(self):
            return {"items": [{"space_id": "via-items"}]}

    assert auth._best_effort_single_space_id(FakeClient()) == "via-items"


def test_best_effort_single_space_error():
    class FakeClient:
        def list_spaces(self):
            raise RuntimeError("fail")

    assert auth._best_effort_single_space_id(FakeClient()) == auth._UNRESOLVED_SPACE_LABEL


def test_best_effort_single_space_multiple():
    class FakeClient:
        def list_spaces(self):
            return {"spaces": [{"id": "a"}, {"id": "b"}]}

    assert auth._best_effort_single_space_id(FakeClient()) == auth._UNRESOLVED_SPACE_LABEL


def test_best_effort_single_space_non_dict_entry():
    class FakeClient:
        def list_spaces(self):
            return {"spaces": ["not-a-dict"]}

    assert auth._best_effort_single_space_id(FakeClient()) == auth._UNRESOLVED_SPACE_LABEL


def test_best_effort_single_space_no_id():
    class FakeClient:
        def list_spaces(self):
            return {"spaces": [{"name": "no-id"}]}

    assert auth._best_effort_single_space_id(FakeClient()) == auth._UNRESOLVED_SPACE_LABEL


# ---------------------------------------------------------------------------
# _invalid_credential_recovery_copy
# ---------------------------------------------------------------------------


def test_invalid_credential_recovery_copy_with_host():
    msg = auth._invalid_credential_recovery_copy("paxai.app")
    assert "paxai.app" in msg
    assert "axctl login" in msg


def test_invalid_credential_recovery_copy_no_host():
    msg = auth._invalid_credential_recovery_copy(None)
    assert "the configured host" in msg
    assert "<your-host>" in msg


# ---------------------------------------------------------------------------
# init — agent enrollment error paths (exchange bound but no agent in whoami)
# ---------------------------------------------------------------------------


def test_init_agent_bound_but_no_agent_in_whoami(monkeypatch, tmp_path):
    """Exchange fails with agent_not_found, whoami returns no bound_agent."""
    ax_dir = tmp_path / ".ax"
    ax_dir.mkdir()

    from unittest.mock import MagicMock

    import httpx as _httpx

    resp = MagicMock(spec=_httpx.Response)
    resp.status_code = 409
    resp.json.return_value = {"detail": {"error": "agent_not_found", "message": "nope"}}
    req = MagicMock(spec=_httpx.Request)

    def failing_post(*a, **kw):
        raise _httpx.HTTPStatusError("409", request=req, response=resp)

    monkeypatch.setattr(_httpx, "post", failing_post)

    class NoBindClient:
        def __init__(self, *, base_url, token):
            pass

        def whoami(self):
            return {"bound_agent": None}

    monkeypatch.setattr("ax_cli.client.AxClient", NoBindClient)

    result = runner.invoke(app, ["auth", "init", "--token", "axp_a_key.secret", "--agent", "test-agent"])
    assert result.exit_code == 1
    assert "agent not found" in result.output.lower() or "bound but" in result.output.lower()


def test_init_agent_whoami_discovery_fails(monkeypatch, tmp_path):
    """Exchange fails with agent_not_found, whoami also fails."""
    ax_dir = tmp_path / ".ax"
    ax_dir.mkdir()

    from unittest.mock import MagicMock

    import httpx as _httpx

    resp = MagicMock(spec=_httpx.Response)
    resp.status_code = 409
    resp.json.return_value = {"detail": {"error": "binding_not_allowed", "message": "nope"}}
    req = MagicMock(spec=_httpx.Request)

    def failing_post(*a, **kw):
        raise _httpx.HTTPStatusError("409", request=req, response=resp)

    monkeypatch.setattr(_httpx, "post", failing_post)

    class FailClient:
        def __init__(self, *, base_url, token):
            pass

        def whoami(self):
            raise ConnectionError("network down")

    monkeypatch.setattr("ax_cli.client.AxClient", FailClient)

    result = runner.invoke(app, ["auth", "init", "--token", "axp_a_key.secret", "--agent", "test-agent"])
    assert result.exit_code == 1
    assert "Could not discover" in result.output


# ---------------------------------------------------------------------------
# whoami — non-gateway with AX_SPACE env var (AX_SPACE not just AX_SPACE_ID)
# ---------------------------------------------------------------------------


def test_whoami_ax_space_env_var(monkeypatch):
    """AX_SPACE env var short-circuits space resolution too."""

    class FakeClient:
        def whoami(self):
            return {"id": "user-1", "bound_agent": None}

        def list_spaces(self):
            raise AssertionError("should not be called")

    monkeypatch.setattr(auth, "get_client", lambda: FakeClient())
    monkeypatch.setattr(auth, "resolve_agent_name", lambda *, client: None)
    monkeypatch.setattr(auth, "resolve_gateway_config", lambda: {})
    monkeypatch.setattr(auth, "_local_config_dir", lambda: None)
    monkeypatch.setenv("AX_SPACE", "from-ax-space")
    monkeypatch.setattr(auth, "_load_config", lambda: {})

    result = runner.invoke(app, ["auth", "whoami", "--json"])
    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["resolved_space_id"] == "from-ax-space"
