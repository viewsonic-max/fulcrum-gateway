"""Tests for AxClient auth and token class selection."""

from unittest.mock import MagicMock, patch

import httpx
import pytest

from ax_cli.client import (
    RATE_LIMIT_INTERACTIVE_LOW_WATER,
    RATE_LIMIT_LOW_WATER,
    AxClient,
    RateLimitPreemptedError,
    _build_fingerprint,
    _check_honeypot,
    _mime_from_ext,
    _mime_from_filename,
    _normalize_rate_limit_reset,
    _RateLimitState,
    _RetryOnAuthClient,
)


class TestTokenClassSelection:
    """Verify correct token class is requested based on PAT prefix + agent_id."""

    def test_user_pat_with_agent_id_is_blocked(self, tmp_path, monkeypatch, mock_exchange):
        """User PATs exchange to user JWTs, so an agent-bound profile must not use one."""
        mock_post = mock_exchange()
        monkeypatch.chdir(tmp_path)
        (tmp_path / ".ax").mkdir()
        (tmp_path / ".ax" / "config.toml").write_text("")

        client = AxClient(
            "https://example.com",
            "axp_u_UserKey.UserSecret",
            agent_id="some-agent-uuid",
        )
        with pytest.raises(SystemExit):
            client._get_jwt()

        mock_post.assert_not_called()

    def test_user_pat_with_agent_name_is_blocked(self, tmp_path, monkeypatch, mock_exchange):
        """Agent-name config plus user PAT is also an attribution boundary violation."""
        mock_post = mock_exchange()
        monkeypatch.chdir(tmp_path)
        (tmp_path / ".ax").mkdir()
        (tmp_path / ".ax" / "config.toml").write_text("")

        client = AxClient(
            "https://example.com",
            "axp_u_UserKey.UserSecret",
            agent_name="some-agent",
        )
        with pytest.raises(SystemExit):
            client._get_jwt()

        mock_post.assert_not_called()

    def test_agent_pat_with_agent_id_uses_agent_access(self, tmp_path, monkeypatch, mock_exchange):
        """Agent-bound PATs (axp_a_) with agent_id should use agent_access."""
        mock_post = mock_exchange()
        monkeypatch.chdir(tmp_path)
        (tmp_path / ".ax").mkdir()
        (tmp_path / ".ax" / "config.toml").write_text("")

        client = AxClient(
            "https://example.com",
            "axp_a_AgentKey.AgentSecret",
            agent_id="some-agent-uuid",
        )
        client._get_jwt()

        call_body = mock_post.call_args[1]["json"]
        assert call_body["requested_token_class"] == "agent_access"
        assert call_body["agent_id"] == "some-agent-uuid"

    def test_agent_pat_without_agent_id_falls_back_to_user_access(self, tmp_path, monkeypatch, mock_exchange):
        """Agent-bound PATs need configured agent_id before requesting agent_access."""
        mock_post = mock_exchange()
        monkeypatch.chdir(tmp_path)
        (tmp_path / ".ax").mkdir()
        (tmp_path / ".ax" / "config.toml").write_text("")

        client = AxClient(
            "https://example.com",
            "axp_a_AgentKey.AgentSecret",
        )
        client._get_jwt()

        call_body = mock_post.call_args[1]["json"]
        assert call_body["requested_token_class"] == "user_access"
        assert "agent_id" not in call_body

    def test_user_pat_without_agent_id_uses_user_access(self, tmp_path, monkeypatch, mock_exchange):
        """User PAT without agent_id → user_access."""
        mock_post = mock_exchange()
        monkeypatch.chdir(tmp_path)
        (tmp_path / ".ax").mkdir()
        (tmp_path / ".ax" / "config.toml").write_text("")

        client = AxClient(
            "https://example.com",
            "axp_u_UserKey.UserSecret",
        )
        client._get_jwt()

        call_body = mock_post.call_args[1]["json"]
        assert call_body["requested_token_class"] == "user_access"


def test_cli_mime_overrides_normalize_common_source_artifacts_to_safe_text():
    assert _mime_from_ext(".java") == "text/plain"
    assert _mime_from_ext(".go") == "text/plain"
    assert _mime_from_ext(".rs") == "text/plain"
    assert _mime_from_ext(".yaml") == "text/plain"
    assert _mime_from_ext(".sh") == "text/plain"
    assert _mime_from_filename("Dockerfile") == "text/plain"
    assert _mime_from_filename("Makefile") == "text/plain"


def test_connect_sse_uses_v1_route_and_explicit_space_id():
    client = AxClient("https://example.com", "legacy-token")
    client._http.stream = MagicMock(return_value="stream-response")

    result = client.connect_sse(space_id="space-123")

    assert result == "stream-response"
    call = client._http.stream.call_args
    assert call.args[:2] == ("GET", "/api/v1/sse/messages")
    assert call.kwargs["params"] == {"token": "legacy-token", "space_id": "space-123"}


def test_list_messages_passes_explicit_space_id():
    client = AxClient("https://example.com", "legacy-token")
    response = httpx.Response(
        200,
        json={"messages": []},
        request=httpx.Request("GET", "https://example.com/api/v1/messages"),
    )
    client._http.get = MagicMock(return_value=response)

    client.list_messages(limit=5, channel="main", space_id="space-123")

    assert client._http.get.call_args.args[0] == "/api/v1/messages"
    assert client._http.get.call_args.kwargs["params"] == {
        "limit": 5,
        "channel": "main",
        "space_id": "space-123",
    }


def test_list_messages_can_request_unread_and_mark_read():
    client = AxClient("https://example.com", "legacy-token")
    response = httpx.Response(
        200,
        json={"messages": [], "unread_count": 0},
        request=httpx.Request("GET", "https://example.com/api/v1/messages"),
    )
    client._http.get = MagicMock(return_value=response)

    client.list_messages(
        limit=5,
        channel="main",
        space_id="space-123",
        unread_only=True,
        mark_read=True,
    )

    assert client._http.get.call_args.kwargs["params"] == {
        "limit": 5,
        "channel": "main",
        "space_id": "space-123",
        "unread_only": "true",
        "mark_read": "true",
    }


def test_send_message_allows_metadata_and_message_type():
    client = AxClient("https://example.com", "legacy-token")
    response = httpx.Response(
        200,
        json={"id": "msg-1"},
        request=httpx.Request("POST", "https://example.com/api/v1/messages"),
    )
    client._http.post = MagicMock(return_value=response)

    client.send_message(
        "space-123",
        "context signal",
        channel="automation-alerts",
        metadata={"ui": {"widget": {"resource_uri": "ui://context/explorer"}}},
        message_type="system",
    )

    assert client._http.post.call_args.args[0] == "/api/v1/messages"
    assert client._http.post.call_args.kwargs["json"] == {
        "content": "context signal",
        "space_id": "space-123",
        "channel": "automation-alerts",
        "message_type": "system",
        "metadata": {"ui": {"widget": {"resource_uri": "ui://context/explorer"}}},
    }


def test_mark_message_read_calls_backend_read_endpoint():
    client = AxClient("https://example.com", "legacy-token")
    response = httpx.Response(
        200,
        json={"status": "success", "message_id": "msg-1"},
        request=httpx.Request("POST", "https://example.com/api/v1/messages/msg-1/read"),
    )
    client._http.post = MagicMock(return_value=response)

    assert client.mark_message_read("msg-1")["status"] == "success"
    assert client._http.post.call_args.args[0] == "/api/v1/messages/msg-1/read"


def test_mark_all_messages_read_calls_backend_endpoint():
    client = AxClient("https://example.com", "legacy-token")
    response = httpx.Response(
        200,
        json={"status": "success", "marked_read": 2},
        request=httpx.Request("POST", "https://example.com/api/v1/messages/mark-all-read"),
    )
    client._http.post = MagicMock(return_value=response)

    assert client.mark_all_messages_read()["marked_read"] == 2
    assert client._http.post.call_args.args[0] == "/api/v1/messages/mark-all-read"


def test_parse_json_names_agent_create_html_shell_as_api_contract_failure():
    client = AxClient("https://example.com", "legacy-token")
    response = httpx.Response(
        200,
        text="<!DOCTYPE html><html></html>",
        headers={"content-type": "text/html"},
        request=httpx.Request("POST", "https://example.com/api/v1/agents"),
    )

    with pytest.raises(httpx.HTTPStatusError) as exc:
        client._parse_json(response)

    message = str(exc.value)
    assert "Agent create returned HTML instead of JSON" in message
    assert "quota" in message
    assert "name conflict" in message


def test_parse_json_names_send_message_html_shell_as_routing_failure():
    """Parallel of the agents-create case: when the hosted SPA captures
    POST /api/v1/messages, the CLI cannot post reply metadata, so the
    error message names that consequence rather than the generic
    'frontend may be catching this route' hint."""
    client = AxClient("https://example.com", "legacy-token")
    response = httpx.Response(
        200,
        text="<!DOCTYPE html><html></html>",
        headers={"content-type": "text/html"},
        request=httpx.Request("POST", "https://example.com/api/v1/messages"),
    )

    with pytest.raises(httpx.HTTPStatusError) as exc:
        client._parse_json(response)

    message = str(exc.value)
    assert "Send-message returned HTML instead of JSON" in message
    assert "parent_id" in message
    assert "agent-to-agent reply routing" in message


def test_record_tool_call_posts_audit_payload():
    client = AxClient("https://example.com", "legacy-token", agent_id="agent-123", agent_name="codex")
    response = httpx.Response(
        202,
        json={"ok": True, "tool_call_id": "tool-1"},
        request=httpx.Request("POST", "https://example.com/api/v1/tool-calls"),
    )
    client._http.post = MagicMock(return_value=response)

    result = client.record_tool_call(
        tool_name="shell",
        tool_call_id="tool-1",
        space_id="space-123",
        tool_action="wc -c README.md",
        arguments={"command": "wc -c README.md"},
        initial_data={"output": "28358 README.md"},
        status="success",
        message_id="msg-1",
        correlation_id="msg-1",
    )

    assert result["tool_call_id"] == "tool-1"
    assert client._http.post.call_args.args[0] == "/api/v1/tool-calls"
    assert client._http.post.call_args.kwargs["json"] == {
        "tool_name": "shell",
        "tool_call_id": "tool-1",
        "status": "success",
        "space_id": "space-123",
        "tool_action": "wc -c README.md",
        "arguments": {"command": "wc -c README.md"},
        "initial_data": {"output": "28358 README.md"},
        "message_id": "msg-1",
        "correlation_id": "msg-1",
    }


def test_set_agent_processing_status_includes_optional_fields():
    client = AxClient("https://example.com", "legacy-token", agent_id="agent-123", agent_name="codex")
    response = httpx.Response(
        200,
        json={"ok": True, "event": "agent_processing", "status": "processing"},
        request=httpx.Request("POST", "https://example.com/api/v1/agents/processing-status"),
    )
    client._http.post = MagicMock(return_value=response)

    result = client.set_agent_processing_status(
        "msg-1",
        "processing",
        agent_name="codex",
        space_id="space-123",
        activity="Running command",
        tool_name="shell",
        progress={"current": 1, "total": 3, "unit": "steps"},
        detail={"command": "pwd"},
        reason="gateway_runtime",
        error_message=None,
        retry_after_seconds=5,
        parent_message_id="parent-1",
    )

    assert result["status"] == "processing"
    assert client._http.post.call_args.args[0] == "/api/v1/agents/processing-status"
    assert client._http.post.call_args.kwargs["json"] == {
        "message_id": "msg-1",
        "status": "processing",
        "agent_name": "codex",
        "activity": "Running command",
        "tool_name": "shell",
        "progress": {"current": 1, "total": 3, "unit": "steps"},
        "detail": {"command": "pwd"},
        "reason": "gateway_runtime",
        "retry_after_seconds": 5,
        "parent_message_id": "parent-1",
    }


def test_set_agent_processing_status_posts_rich_payload():
    client = AxClient("https://example.com", "legacy-token", agent_id="agent-123", agent_name="codex")
    response = httpx.Response(
        202,
        json={"ok": True},
        request=httpx.Request("POST", "https://example.com/api/v1/agents/processing-status"),
    )
    client._http.post = MagicMock(return_value=response)

    result = client.set_agent_processing_status(
        "msg-1",
        "tool_call",
        agent_name="codex",
        space_id="space-123",
        activity="Running tests",
        tool_name="shell",
        progress={"current": 1, "total": 3, "unit": "steps"},
        detail={"command": "pytest tests/test_gateway_commands.py"},
        reason="tool started",
        error_message="",
        retry_after_seconds=5,
        parent_message_id="parent-1",
    )

    assert result["ok"] is True
    assert client._http.post.call_args.args[0] == "/api/v1/agents/processing-status"
    assert client._http.post.call_args.kwargs["json"] == {
        "message_id": "msg-1",
        "status": "tool_call",
        "agent_name": "codex",
        "activity": "Running tests",
        "tool_name": "shell",
        "progress": {"current": 1, "total": 3, "unit": "steps"},
        "detail": {"command": "pytest tests/test_gateway_commands.py"},
        "reason": "tool started",
        "error_message": "",
        "retry_after_seconds": 5,
        "parent_message_id": "parent-1",
    }


def test_list_tasks_passes_explicit_space_id():
    client = AxClient("https://example.com", "legacy-token")
    response = httpx.Response(
        200,
        json={"tasks": []},
        request=httpx.Request("GET", "https://example.com/api/v1/tasks"),
    )
    client._http.get = MagicMock(return_value=response)

    client.list_tasks(limit=7, space_id="space-123")

    assert client._http.get.call_args.args[0] == "/api/v1/tasks"
    assert client._http.get.call_args.kwargs["params"] == {
        "limit": 7,
        "space_id": "space-123",
    }


def test_list_agents_passes_explicit_space_id_and_limit():
    client = AxClient("https://example.com", "legacy-token")
    response = httpx.Response(
        200,
        json={"agents": []},
        request=httpx.Request("GET", "https://example.com/api/v1/agents"),
    )
    client._http.get = MagicMock(return_value=response)

    client.list_agents(space_id="space-123", limit=500)

    assert client._http.get.call_args.args[0] == "/api/v1/agents"
    assert client._http.get.call_args.kwargs["params"] == {
        "space_id": "space-123",
        "limit": 500,
    }


class TestCredentialManagement:
    """Verify credential management request payloads."""

    def _response(self, method: str, url: str, status_code: int, *, json=None, text: str | None = None):
        request = httpx.Request(method, url)
        if text is not None:
            return httpx.Response(
                status_code,
                text=text,
                headers={"content-type": "text/html; charset=utf-8"},
                request=request,
            )
        return httpx.Response(status_code, json=json or {}, request=request)

    def test_create_key_with_allowed_agents_sets_agent_scope(self):
        client = AxClient("https://example.com", "axp_u_UserKey.UserSecret")
        response = httpx.Response(
            201,
            json={"ok": True},
            request=httpx.Request("POST", "https://example.com/api/v1/keys"),
        )
        client._http.post = MagicMock(return_value=response)

        client.create_key("agent-key", allowed_agent_ids=["agent-123"])

        body = client._http.post.call_args.kwargs["json"]
        assert body["agent_scope"] == "agents"
        assert body["allowed_agent_ids"] == ["agent-123"]

    def test_create_key_with_bound_agent_id(self):
        client = AxClient("https://example.com", "axp_u_UserKey.UserSecret")
        response = httpx.Response(
            201,
            json={"credential_id": "cred-1", "token": "axp_a_…"},
            request=httpx.Request("POST", "https://example.com/api/v1/keys"),
        )
        client._http.post = MagicMock(return_value=response)

        client.create_key("bound-key", bound_agent_id="a0eebc99-9c0b-4ef8-bb6d-6bb9bd380a11")

        body = client._http.post.call_args.kwargs["json"]
        assert body["name"] == "bound-key"
        assert body["bound_agent_id"] == "a0eebc99-9c0b-4ef8-bb6d-6bb9bd380a11"
        assert "agent_scope" not in body

    def test_create_key_with_bound_agent_id_and_scope(self):
        client = AxClient("https://example.com", "axp_u_UserKey.UserSecret")
        response = httpx.Response(
            201,
            json={},
            request=httpx.Request("POST", "https://example.com/api/v1/keys"),
        )
        client._http.post = MagicMock(return_value=response)

        agent_uuid = "b1eebc99-9c0b-4ef8-bb6d-6bb9bd380a22"
        client.create_key(
            "combo",
            allowed_agent_ids=[agent_uuid],
            bound_agent_id=agent_uuid,
        )

        body = client._http.post.call_args.kwargs["json"]
        assert body["agent_scope"] == "agents"
        assert body["allowed_agent_ids"] == [agent_uuid]
        assert body["bound_agent_id"] == agent_uuid

    def test_create_task_sends_assignee_id_in_body(self):
        client = AxClient("https://example.com", "legacy-token", agent_id="creator-agent")
        response = httpx.Response(
            201,
            json={"id": "task-123", "space_id": "space-123", "assignee_id": "target-agent"},
            request=httpx.Request("POST", "https://example.com/api/v1/tasks"),
        )
        client._http.post = MagicMock(return_value=response)

        client.create_task(
            "space-123",
            "Review the spec",
            priority="medium",
            assignee_id="target-agent",
        )

        body = client._http.post.call_args.kwargs["json"]
        assert body["space_id"] == "space-123"
        assert body["assignee_id"] == "target-agent"

    def test_create_task_falls_back_from_hosted_html_and_verifies_space(self):
        client = AxClient("https://example.com", "legacy-token", agent_id="creator-agent")
        html_response = httpx.Response(
            200,
            text="<!doctype html><html></html>",
            headers={"content-type": "text/html; charset=utf-8"},
            request=httpx.Request("POST", "https://example.com/api/v1/tasks"),
        )
        create_response = httpx.Response(
            201,
            json={"id": "task-123", "title": "Review the spec"},
            request=httpx.Request("POST", "https://example.com/api/tasks"),
        )
        whoami_response = httpx.Response(
            200,
            json={"resolved_space_id": "space-123"},
            request=httpx.Request("GET", "https://example.com/auth/me"),
        )
        list_response = httpx.Response(
            200,
            json={"tasks": [{"id": "task-123", "space_id": "space-123"}]},
            request=httpx.Request("GET", "https://example.com/api/v1/tasks?limit=100&space_id=space-123"),
        )
        client._http.post = MagicMock(side_effect=[html_response, create_response])
        client._http.get = MagicMock(side_effect=[whoami_response, list_response])

        data = client.create_task("space-123", "Review the spec", priority="high")

        assert data["id"] == "task-123"
        assert client._http.post.call_args_list[0].args[0] == "/api/v1/tasks"
        assert client._http.post.call_args_list[1].args[0] == "/api/tasks"
        assert client._http.get.call_args_list[0].args[0] == "/auth/me"
        assert client._http.get.call_args_list[1].kwargs["params"] == {"limit": 100, "space_id": "space-123"}

    def test_create_task_fallback_refuses_when_session_space_differs(self):
        client = AxClient("https://example.com", "legacy-token", agent_id="creator-agent")
        html_response = httpx.Response(
            200,
            text="<!doctype html><html></html>",
            headers={"content-type": "text/html; charset=utf-8"},
            request=httpx.Request("POST", "https://example.com/api/v1/tasks"),
        )
        whoami_response = httpx.Response(
            200,
            json={"resolved_space_id": "madtank-space"},
            request=httpx.Request("GET", "https://example.com/auth/me"),
        )
        client._http.post = MagicMock(return_value=html_response)
        client._http.get = MagicMock(return_value=whoami_response)

        with pytest.raises(RuntimeError, match="Refusing to create the task"):
            client.create_task("ax-cli-dev-space", "Review the spec", priority="high")

        assert client._http.post.call_count == 1

    def test_create_task_fallback_rejects_unverified_space(self):
        client = AxClient("https://example.com", "legacy-token", agent_id="creator-agent")
        html_response = httpx.Response(
            200,
            text="<!doctype html><html></html>",
            headers={"content-type": "text/html; charset=utf-8"},
            request=httpx.Request("POST", "https://example.com/api/v1/tasks"),
        )
        create_response = httpx.Response(
            201,
            json={"id": "task-123", "title": "Review the spec"},
            request=httpx.Request("POST", "https://example.com/api/tasks"),
        )
        whoami_response = httpx.Response(
            200,
            json={"bound_agent": {"default_space_id": "space-123"}},
            request=httpx.Request("GET", "https://example.com/auth/me"),
        )
        list_response = httpx.Response(
            200,
            json={"tasks": [{"id": "other-task", "space_id": "space-123"}]},
            request=httpx.Request("GET", "https://example.com/api/v1/tasks?limit=100&space_id=space-123"),
        )
        client._http.post = MagicMock(side_effect=[html_response, create_response])
        client._http.get = MagicMock(side_effect=[whoami_response, list_response])

        with pytest.raises(RuntimeError, match="not visible in requested space"):
            client.create_task("space-123", "Review the spec", priority="high")

    def test_gateway_auth_contract_task_create_exchanges_then_posts_api_tasks(self, monkeypatch):
        import httpx

        exchange_calls = []

        def fake_exchange(url, *, json=None, headers=None, timeout=None):
            exchange_calls.append({"url": url, "json": json, "headers": headers, "timeout": timeout})
            return httpx.Response(
                200,
                json={
                    "access_token": "exchanged.jwt",
                    "token_type": "Bearer",
                    "expires_in": 3600,
                    "scope": "tasks:read tasks:write",
                    "token_class": "agent_access",
                    "agent_id": "agent-123",
                    "agent_name": "cli-sentinel-local",
                },
                request=httpx.Request("POST", url),
            )

        monkeypatch.setattr(httpx, "post", fake_exchange)
        client = AxClient("https://example.com", "axp_a_AgentKey.AgentSecret", agent_name="cli-sentinel-local")
        task_response = httpx.Response(
            201,
            json={
                "id": "task-123",
                "task_display_id": "a1b2c3",
                "title": "Land gateway stub",
                "status": "not_started",
                "priority": "high",
                "posted_by": {"id": "agent-123", "type": "agent"},
                # Backend echoes space_id so the client can verify the task
                # actually landed in the requested space (acceptance criterion
                # for ax-cli-dev tasks 97e2f06c / cbb8f887 / 7fbd5d0f).
                "space_id": "space-hint",
            },
            request=httpx.Request("POST", "https://example.com/api/tasks"),
        )
        client._http.post = MagicMock(return_value=task_response)

        data = client.create_task("space-hint", "Land gateway stub", description="Contract draft", priority="high")

        assert data["id"] == "task-123"
        assert exchange_calls == [
            {
                "url": "https://example.com/auth/exchange",
                "json": {
                    "requested_token_class": "agent_access",
                    "audience": "ax-api",
                    "scope": "tasks:read tasks:write messages:read messages:write agents:read",
                    "agent_name": "cli-sentinel-local",
                },
                "headers": {
                    "Authorization": "Bearer axp_a_AgentKey.AgentSecret",
                    "Content-Type": "application/json",
                },
                "timeout": 10.0,
            }
        ]
        assert client._http.post.call_args.args[0] == "/api/tasks"
        assert client._http.post.call_args.kwargs["headers"]["Authorization"] == "Bearer exchanged.jwt"
        body = client._http.post.call_args.kwargs["json"]
        assert body == {
            "title": "Land gateway stub",
            "description": "Contract draft",
            "requirements": {
                "source": "gateway-first-cli",
                "space_id_hint": "space-hint",
                "fingerprint": client._base_headers["X-AX-FP"],
            },
            "priority": "high",
            "deadline": None,
        }
        assert "space_id" not in body
        assert "assigned_agent_id" not in body
        assert "assignee_id" not in body

    def test_gateway_auth_contract_task_create_refuses_when_response_space_mismatches(self, monkeypatch):
        """Regression for ax-cli-dev 97e2f06c / 7fbd5d0f: the auth-contract path
        used to silently land tasks in the credential's default space when it
        differed from --space-id. Verify that a mismatched response space_id
        now raises RuntimeError instead of returning a false success.
        """
        import httpx

        def fake_exchange(url, *, json=None, headers=None, timeout=None):
            return httpx.Response(
                200,
                json={
                    "access_token": "exchanged.jwt",
                    "expires_in": 3600,
                    "token_class": "agent_access",
                    "agent_id": "agent-123",
                    "agent_name": "cli-sentinel-local",
                },
                request=httpx.Request("POST", url),
            )

        monkeypatch.setattr(httpx, "post", fake_exchange)
        client = AxClient("https://example.com", "axp_a_AgentKey.AgentSecret", agent_name="cli-sentinel-local")
        task_response = httpx.Response(
            201,
            json={
                "id": "task-123",
                "title": "Land gateway stub",
                # Backend filed it in madtank's default workspace despite
                # space_id_hint=ax-cli-dev-space — this is the silent-misfile
                # scenario the bug reports describe.
                "space_id": "madtank-space",
            },
            request=httpx.Request("POST", "https://example.com/api/tasks"),
        )
        client._http.post = MagicMock(return_value=task_response)

        with pytest.raises(RuntimeError, match="created in the wrong space"):
            client.create_task("ax-cli-dev-space", "Land gateway stub", priority="high")

    def test_gateway_auth_contract_task_create_verifies_via_list_when_response_omits_space_id(self, monkeypatch):
        """Backend doesn't always echo space_id today; client falls back to a
        list_tasks probe in the requested space to confirm the new task is
        actually there before returning success.
        """
        import httpx

        def fake_exchange(url, *, json=None, headers=None, timeout=None):
            return httpx.Response(
                200,
                json={
                    "access_token": "exchanged.jwt",
                    "expires_in": 3600,
                    "token_class": "agent_access",
                    "agent_id": "agent-123",
                    "agent_name": "cli-sentinel-local",
                },
                request=httpx.Request("POST", url),
            )

        monkeypatch.setattr(httpx, "post", fake_exchange)
        client = AxClient("https://example.com", "axp_a_AgentKey.AgentSecret", agent_name="cli-sentinel-local")
        task_response = httpx.Response(
            201,
            json={"id": "task-123", "title": "Land gateway stub"},
            request=httpx.Request("POST", "https://example.com/api/tasks"),
        )
        list_response = httpx.Response(
            200,
            json={"tasks": [{"id": "task-123", "space_id": "ax-cli-dev-space"}]},
            request=httpx.Request("GET", "https://example.com/api/v1/tasks"),
        )
        client._http.post = MagicMock(return_value=task_response)
        client._http.get = MagicMock(return_value=list_response)

        data = client.create_task("ax-cli-dev-space", "Land gateway stub", priority="high")

        assert data["id"] == "task-123"
        # Verification round-trip used the requested space, not the default.
        assert client._http.get.call_args.kwargs["params"]["space_id"] == "ax-cli-dev-space"

    def test_gateway_auth_contract_task_create_refuses_when_list_misses(self, monkeypatch):
        """If the response omits space_id and the new task isn't visible in
        the requested space, surface a clear failure instead of pretending."""
        import httpx

        def fake_exchange(url, *, json=None, headers=None, timeout=None):
            return httpx.Response(
                200,
                json={
                    "access_token": "exchanged.jwt",
                    "expires_in": 3600,
                    "token_class": "agent_access",
                    "agent_id": "agent-123",
                    "agent_name": "cli-sentinel-local",
                },
                request=httpx.Request("POST", url),
            )

        monkeypatch.setattr(httpx, "post", fake_exchange)
        client = AxClient("https://example.com", "axp_a_AgentKey.AgentSecret", agent_name="cli-sentinel-local")
        task_response = httpx.Response(
            201,
            json={"id": "task-123", "title": "Land gateway stub"},
            request=httpx.Request("POST", "https://example.com/api/tasks"),
        )
        list_response = httpx.Response(
            200,
            json={"tasks": [{"id": "some-other-task", "space_id": "ax-cli-dev-space"}]},
            request=httpx.Request("GET", "https://example.com/api/v1/tasks"),
        )
        client._http.post = MagicMock(return_value=task_response)
        client._http.get = MagicMock(return_value=list_response)

        with pytest.raises(RuntimeError, match="not visible in requested space"):
            client.create_task("ax-cli-dev-space", "Land gateway stub", priority="high")

    def test_issue_agent_pat_sends_requested_audience(self):
        client = AxClient("https://example.com", "axp_u_UserKey.UserSecret")
        client._admin_headers = MagicMock(return_value={"Authorization": "Bearer admin"})
        response = httpx.Response(
            201,
            json={"ok": True},
            request=httpx.Request("POST", "https://example.com/credentials/agent-pat"),
        )
        client._http.post = MagicMock(return_value=response)

        client.mgmt_issue_agent_pat("agent-123", audience="mcp")

        body = client._http.post.call_args.kwargs["json"]
        assert body["audience"] == "mcp"

    def test_issue_enrollment_sends_requested_audience(self):
        client = AxClient("https://example.com", "axp_u_UserKey.UserSecret")
        client._admin_headers = MagicMock(return_value={"Authorization": "Bearer admin"})
        response = httpx.Response(
            201,
            json={"ok": True},
            request=httpx.Request("POST", "https://example.com/credentials/enrollment"),
        )
        client._http.post = MagicMock(return_value=response)

        client.mgmt_issue_enrollment(audience="both")

        body = client._http.post.call_args.kwargs["json"]
        assert body["audience"] == "both"

    def test_mgmt_create_agent_prefers_api_v1_route(self):
        client = AxClient("https://example.com", "axp_u_UserKey.UserSecret")
        client._admin_headers = MagicMock(return_value={"Authorization": "Bearer admin"})
        client._http.post = MagicMock(
            return_value=self._response(
                "POST",
                "https://example.com/api/v1/agents/manage/create",
                201,
                json={"agent": {"id": "agent-123", "name": "new-agent"}},
            )
        )

        result = client.mgmt_create_agent("new-agent")

        assert result["agent"]["id"] == "agent-123"
        assert client._http.post.call_args.args[0] == "/api/v1/agents/manage/create"

    def test_mgmt_create_agent_falls_back_to_legacy_route_on_route_miss(self):
        client = AxClient("https://example.com", "axp_u_UserKey.UserSecret")
        client._admin_headers = MagicMock(return_value={"Authorization": "Bearer admin"})
        client._http.post = MagicMock(
            side_effect=[
                self._response(
                    "POST",
                    "https://example.com/api/v1/agents/manage/create",
                    404,
                    json={"detail": "Not Found"},
                ),
                self._response(
                    "POST",
                    "https://example.com/agents/manage/create",
                    201,
                    json={"agent": {"id": "agent-123", "name": "new-agent"}},
                ),
            ]
        )

        result = client.mgmt_create_agent("new-agent")

        assert result["agent"]["id"] == "agent-123"
        assert [call.args[0] for call in client._http.post.call_args_list] == [
            "/api/v1/agents/manage/create",
            "/agents/manage/create",
        ]

    def test_mgmt_create_agent_falls_back_when_frontend_catches_route(self):
        client = AxClient("https://example.com", "axp_u_UserKey.UserSecret")
        client._admin_headers = MagicMock(return_value={"Authorization": "Bearer admin"})
        client._http.post = MagicMock(
            side_effect=[
                self._response(
                    "POST",
                    "https://example.com/api/v1/agents/manage/create",
                    200,
                    text="<!DOCTYPE html><html></html>",
                ),
                self._response(
                    "POST",
                    "https://example.com/agents/manage/create",
                    201,
                    json={"agent": {"id": "agent-123", "name": "new-agent"}},
                ),
            ]
        )

        result = client.mgmt_create_agent("new-agent")

        assert result["agent"]["id"] == "agent-123"
        assert [call.args[0] for call in client._http.post.call_args_list] == [
            "/api/v1/agents/manage/create",
            "/agents/manage/create",
        ]

    def test_mgmt_create_agent_does_not_fallback_on_auth_failure(self):
        client = AxClient("https://example.com", "axp_u_UserKey.UserSecret")
        client._admin_headers = MagicMock(return_value={"Authorization": "Bearer admin"})
        client._http.post = MagicMock(
            return_value=self._response(
                "POST",
                "https://example.com/api/v1/agents/manage/create",
                401,
                json={"detail": "Not authenticated"},
            )
        )

        with pytest.raises(httpx.HTTPStatusError):
            client.mgmt_create_agent("new-agent")

        assert client._http.post.call_count == 1

    def test_mgmt_list_agents_falls_back_to_legacy_route_on_route_miss(self):
        client = AxClient("https://example.com", "axp_u_UserKey.UserSecret")
        client._admin_headers = MagicMock(return_value={"Authorization": "Bearer admin"})
        client._http.get = MagicMock(
            side_effect=[
                self._response(
                    "GET",
                    "https://example.com/api/v1/agents/manage/list",
                    405,
                    json={"detail": "Method Not Allowed"},
                ),
                self._response(
                    "GET",
                    "https://example.com/agents/manage/list",
                    200,
                    json=[{"id": "agent-123", "name": "new-agent"}],
                ),
            ]
        )

        result = client.mgmt_list_agents()

        assert result == [{"id": "agent-123", "name": "new-agent"}]
        assert [call.args[0] for call in client._http.get.call_args_list] == [
            "/api/v1/agents/manage/list",
            "/agents/manage/list",
        ]


# ---------------------------------------------------------------------------
# Helper for building mock responses
# ---------------------------------------------------------------------------


def _make_response(method, url, status_code, *, json_body=None, text=None, content_type=None):
    """Build an httpx.Response for testing."""
    request = httpx.Request(method, url)
    headers = {}
    if content_type:
        headers["content-type"] = content_type
    if text is not None:
        headers.setdefault("content-type", "text/html; charset=utf-8")
        return httpx.Response(status_code, text=text, headers=headers, request=request)
    return httpx.Response(status_code, json=json_body or {}, request=request)


# ---------------------------------------------------------------------------
# Honeypot detection
# ---------------------------------------------------------------------------


class TestHoneypotDetection:
    """Verify _check_honeypot fires an alert for known key prefixes."""

    def test_honeypot_fires_alert_for_aws_key(self):
        with patch("ax_cli.client.httpx.post") as mock_post:
            _check_honeypot("AKIA1234567890EXAMPLE", "https://example.com")
            mock_post.assert_called_once()
            call_kwargs = mock_post.call_args
            assert "/api/v1/security/honeypot" in call_kwargs.args[0]
            body = call_kwargs.kwargs["json"]
            assert body["event"] == "honeypot_triggered"
            assert body["provider_pattern"] == "aws-iam"
            assert body["prefix"] == "AKIA"

    def test_honeypot_fires_for_github_pat(self):
        with patch("ax_cli.client.httpx.post") as mock_post:
            _check_honeypot("ghp_abc123def456", "https://example.com")
            mock_post.assert_called_once()
            body = mock_post.call_args.kwargs["json"]
            assert body["provider_pattern"] == "github-pat"

    def test_honeypot_silent_on_network_error(self):
        """Alert failures are best-effort — should not raise."""
        with patch("ax_cli.client.httpx.post", side_effect=Exception("network down")):
            _check_honeypot("AKIA1234567890EXAMPLE", "https://example.com")

    def test_honeypot_noop_for_legitimate_ax_token(self):
        with patch("ax_cli.client.httpx.post") as mock_post:
            _check_honeypot("axp_u_SomeKey.SomeSecret", "https://example.com")
            mock_post.assert_not_called()


# ---------------------------------------------------------------------------
# _RetryOnAuthClient
# ---------------------------------------------------------------------------


class TestRetryOnAuthClient:
    """Verify 401-retry wrapper with exponential backoff."""

    def _mock_inner(self):
        inner = MagicMock(spec=httpx.Client)
        return inner

    def test_retry_on_401_calls_get_fresh_jwt(self):
        inner = self._mock_inner()
        ok_response = _make_response("GET", "https://x.com/test", 200, json_body={"ok": True})
        inner.get.side_effect = [
            _make_response("GET", "https://x.com/test", 401, json_body={"detail": "expired"}),
            ok_response,
        ]
        get_fresh = MagicMock(return_value="fresh.jwt")
        client = _RetryOnAuthClient(inner, get_fresh)

        with patch("time.sleep"):
            r = client.get("/test")

        assert r.status_code == 200
        get_fresh.assert_called_once()

    def test_no_retry_when_get_fresh_jwt_is_none(self):
        inner = self._mock_inner()
        inner.get.return_value = _make_response("GET", "https://x.com/test", 401, json_body={"detail": "expired"})
        client = _RetryOnAuthClient(inner, None)

        r = client.get("/test")

        assert r.status_code == 401
        assert inner.get.call_count == 1

    def test_gives_up_after_max_retries(self):
        inner = self._mock_inner()
        inner.post.return_value = _make_response("POST", "https://x.com/test", 401, json_body={"detail": "expired"})
        get_fresh = MagicMock(return_value="fresh.jwt")
        client = _RetryOnAuthClient(inner, get_fresh)

        with patch("time.sleep"):
            r = client.post("/test")

        assert r.status_code == 401
        # 1 initial + 3 retries
        assert inner.post.call_count == 4

    def test_put_delegates_to_inner(self):
        inner = self._mock_inner()
        inner.put.return_value = _make_response("PUT", "https://x.com/test", 200, json_body={})
        client = _RetryOnAuthClient(inner, None)
        r = client.put("/test", json={})
        assert r.status_code == 200

    def test_patch_delegates_to_inner(self):
        inner = self._mock_inner()
        inner.patch.return_value = _make_response("PATCH", "https://x.com/test", 200, json_body={})
        client = _RetryOnAuthClient(inner, None)
        r = client.patch("/test", json={})
        assert r.status_code == 200

    def test_delete_delegates_to_inner(self):
        inner = self._mock_inner()
        inner.delete.return_value = _make_response("DELETE", "https://x.com/test", 204, json_body={})
        client = _RetryOnAuthClient(inner, None)
        r = client.delete("/test")
        assert r.status_code == 204

    def test_stream_delegates_directly(self):
        from contextlib import contextmanager

        inner = self._mock_inner()
        response = MagicMock()
        response.headers = {}

        @contextmanager
        def _fake_stream(*args, **kwargs):
            yield response

        inner.stream = _fake_stream
        client = _RetryOnAuthClient(inner, None)
        with client.stream("GET", "/sse") as result:
            assert result is response

    def test_close_delegates(self):
        inner = self._mock_inner()
        client = _RetryOnAuthClient(inner, None)
        client.close()
        inner.close.assert_called_once()


# ---------------------------------------------------------------------------
# Build fingerprint
# ---------------------------------------------------------------------------


def test_build_fingerprint_returns_expected_keys():
    fp = _build_fingerprint("test-token")
    assert "X-AX-FP" in fp
    assert "X-AX-FP-Token" in fp
    assert "X-AX-FP-OS" in fp
    assert "X-AX-FP-Arch" in fp
    assert len(fp["X-AX-FP"]) == 24
    assert len(fp["X-AX-FP-Token"]) == 16


# ---------------------------------------------------------------------------
# _parse_json — generic HTML fallback
# ---------------------------------------------------------------------------


def test_parse_json_generic_html_error_for_unknown_route():
    """Non-agents, non-messages HTML response gets the generic error message."""
    client = AxClient("https://example.com", "legacy-token")
    response = httpx.Response(
        200,
        text="<!DOCTYPE html><html></html>",
        headers={"content-type": "text/html"},
        request=httpx.Request("GET", "https://example.com/api/v1/spaces"),
    )
    with pytest.raises(httpx.HTTPStatusError, match="Expected JSON but got HTML"):
        client._parse_json(response)


# ---------------------------------------------------------------------------
# _management_json_with_fallback — no paths
# ---------------------------------------------------------------------------


def test_management_json_with_fallback_raises_on_empty_paths():
    client = AxClient("https://example.com", "legacy-token")
    with pytest.raises(RuntimeError, match="No management route paths provided"):
        client._management_json_with_fallback("get", [])


# ---------------------------------------------------------------------------
# Space methods
# ---------------------------------------------------------------------------


class TestSpaceMethods:
    def test_list_spaces(self):
        client = AxClient("https://example.com", "legacy-token")
        response = _make_response("GET", "https://example.com/api/v1/spaces", 200, json_body=[{"id": "s1"}])
        client._http.get = MagicMock(return_value=response)
        result = client.list_spaces()
        assert result == [{"id": "s1"}]
        assert client._http.get.call_args.args[0] == "/api/v1/spaces"

    def test_get_space(self):
        client = AxClient("https://example.com", "legacy-token")
        response = _make_response(
            "GET", "https://example.com/api/v1/spaces/s1", 200, json_body={"id": "s1", "name": "Test"}
        )
        client._http.get = MagicMock(return_value=response)
        result = client.get_space("s1")
        assert result["id"] == "s1"
        assert client._http.get.call_args.args[0] == "/api/v1/spaces/s1"

    def test_create_space_with_description(self):
        client = AxClient("https://example.com", "legacy-token")
        response = _make_response(
            "POST", "https://example.com/api/spaces/create", 201, json_body={"id": "s1", "name": "NewSpace"}
        )
        client._http.post = MagicMock(return_value=response)
        result = client.create_space("NewSpace", description="A test space")
        assert result["name"] == "NewSpace"
        body = client._http.post.call_args.kwargs["json"]
        assert body["name"] == "NewSpace"
        assert body["description"] == "A test space"
        assert body["visibility"] == "private"

    def test_create_space_without_description(self):
        client = AxClient("https://example.com", "legacy-token")
        response = _make_response("POST", "https://example.com/api/spaces/create", 201, json_body={"id": "s1"})
        client._http.post = MagicMock(return_value=response)
        client.create_space("Minimal")
        body = client._http.post.call_args.kwargs["json"]
        assert "description" not in body

    def test_list_space_members(self):
        client = AxClient("https://example.com", "legacy-token")
        response = _make_response(
            "GET", "https://example.com/api/v1/spaces/s1/members", 200, json_body=[{"user_id": "u1"}]
        )
        client._http.get = MagicMock(return_value=response)
        result = client.list_space_members("s1")
        assert result == [{"user_id": "u1"}]


# ---------------------------------------------------------------------------
# Heartbeat
# ---------------------------------------------------------------------------


class TestHeartbeat:
    def test_send_heartbeat_minimal(self):
        client = AxClient("https://example.com", "legacy-token")
        response = _make_response("POST", "https://example.com/api/v1/agents/heartbeat", 200, json_body={"ok": True})
        client._http.post = MagicMock(return_value=response)
        result = client.send_heartbeat()
        assert result["ok"] is True
        body = client._http.post.call_args.kwargs["json"]
        assert body == {}

    def test_send_heartbeat_with_all_fields(self):
        client = AxClient("https://example.com", "legacy-token")
        response = _make_response("POST", "https://example.com/api/v1/agents/heartbeat", 200, json_body={"ok": True})
        client._http.post = MagicMock(return_value=response)
        client.send_heartbeat(
            agent_id="agent-1",
            status="active",
            note="Processing task",
            cadence_seconds=30,
        )
        body = client._http.post.call_args.kwargs["json"]
        assert body["status"] == "active"
        assert body["note"] == "Processing task"
        assert body["cadence_seconds"] == 30


# ---------------------------------------------------------------------------
# send_message — branches for parent_id and attachments
# ---------------------------------------------------------------------------


class TestSendMessageBranches:
    def test_send_message_with_parent_id(self):
        client = AxClient("https://example.com", "legacy-token")
        response = _make_response("POST", "https://example.com/api/v1/messages", 200, json_body={"id": "msg-2"})
        client._http.post = MagicMock(return_value=response)
        client.send_message("space-1", "reply content", parent_id="msg-1")
        body = client._http.post.call_args.kwargs["json"]
        assert body["parent_id"] == "msg-1"

    def test_send_message_with_attachments(self):
        client = AxClient("https://example.com", "legacy-token")
        response = _make_response("POST", "https://example.com/api/v1/messages", 200, json_body={"id": "msg-3"})
        client._http.post = MagicMock(return_value=response)
        attachments = [{"file_id": "f1", "filename": "test.txt"}]
        client.send_message("space-1", "see attached", attachments=attachments)
        body = client._http.post.call_args.kwargs["json"]
        assert body["attachments"] == attachments
        assert body["metadata"]["accepted_attachments"] == attachments

    def test_send_message_with_attachments_merges_existing_metadata(self):
        client = AxClient("https://example.com", "legacy-token")
        response = _make_response("POST", "https://example.com/api/v1/messages", 200, json_body={"id": "msg-4"})
        client._http.post = MagicMock(return_value=response)
        attachments = [{"file_id": "f1"}]
        metadata = {"custom_key": "custom_value"}
        client.send_message("space-1", "with both", attachments=attachments, metadata=metadata)
        body = client._http.post.call_args.kwargs["json"]
        assert body["metadata"]["custom_key"] == "custom_value"
        assert body["metadata"]["accepted_attachments"] == attachments


# ---------------------------------------------------------------------------
# upload_file
# ---------------------------------------------------------------------------


class TestUploadFile:
    def test_upload_file_not_found_raises(self, tmp_path):
        client = AxClient("https://example.com", "legacy-token")
        with pytest.raises(FileNotFoundError, match="File not found"):
            client.upload_file(str(tmp_path / "nonexistent.txt"))

    def test_upload_file_directory_raises(self, tmp_path):
        client = AxClient("https://example.com", "legacy-token")
        d = tmp_path / "subdir"
        d.mkdir()
        with pytest.raises(FileNotFoundError, match="File not found"):
            client.upload_file(str(d))

    def test_upload_file_posts_multipart(self, tmp_path):
        client = AxClient("https://example.com", "legacy-token")
        test_file = tmp_path / "test.txt"
        test_file.write_text("hello world")

        mock_response = _make_response("POST", "https://example.com/api/v1/uploads/", 200, json_body={"file_id": "f1"})

        with patch("ax_cli.client.httpx.Client") as MockClient:
            mock_upload_http = MagicMock()
            mock_upload_http.__enter__ = MagicMock(return_value=mock_upload_http)
            mock_upload_http.__exit__ = MagicMock(return_value=False)
            mock_upload_http.post.return_value = mock_response
            MockClient.return_value = mock_upload_http

            result = client.upload_file(str(test_file), space_id="space-1")

        assert result["file_id"] == "f1"
        # Verify space_id was passed as form data
        call_kwargs = mock_upload_http.post.call_args.kwargs
        assert call_kwargs["data"] == {"space_id": "space-1"}

    def test_upload_file_without_space_id(self, tmp_path):
        client = AxClient("https://example.com", "legacy-token")
        test_file = tmp_path / "test.md"
        test_file.write_text("# hello")

        mock_response = _make_response("POST", "https://example.com/api/v1/uploads/", 200, json_body={"file_id": "f2"})

        with patch("ax_cli.client.httpx.Client") as MockClient:
            mock_upload_http = MagicMock()
            mock_upload_http.__enter__ = MagicMock(return_value=mock_upload_http)
            mock_upload_http.__exit__ = MagicMock(return_value=False)
            mock_upload_http.post.return_value = mock_response
            MockClient.return_value = mock_upload_http

            client.upload_file(str(test_file))

        call_kwargs = mock_upload_http.post.call_args.kwargs
        assert call_kwargs["data"] is None


# ---------------------------------------------------------------------------
# Message CRUD: get, edit, delete, reactions, replies
# ---------------------------------------------------------------------------


class TestMessageCRUD:
    def test_get_message(self):
        client = AxClient("https://example.com", "legacy-token")
        response = _make_response(
            "GET", "https://example.com/api/v1/messages/msg-1", 200, json_body={"id": "msg-1", "content": "hi"}
        )
        client._http.get = MagicMock(return_value=response)
        result = client.get_message("msg-1")
        assert result["id"] == "msg-1"

    def test_edit_message(self):
        client = AxClient("https://example.com", "legacy-token")
        response = _make_response(
            "PATCH", "https://example.com/api/v1/messages/msg-1", 200, json_body={"id": "msg-1", "content": "edited"}
        )
        client._http.patch = MagicMock(return_value=response)
        result = client.edit_message("msg-1", "edited")
        assert result["content"] == "edited"
        assert client._http.patch.call_args.kwargs["json"] == {"content": "edited"}

    def test_delete_message(self):
        client = AxClient("https://example.com", "legacy-token")
        response = _make_response("DELETE", "https://example.com/api/v1/messages/msg-1", 204)
        client._http.delete = MagicMock(return_value=response)
        status = client.delete_message("msg-1")
        assert status == 204

    def test_add_reaction(self):
        client = AxClient("https://example.com", "legacy-token")
        response = _make_response(
            "POST", "https://example.com/api/v1/messages/msg-1/reactions", 200, json_body={"ok": True}
        )
        client._http.post = MagicMock(return_value=response)
        result = client.add_reaction("msg-1", "thumbsup")
        assert result["ok"] is True
        assert client._http.post.call_args.kwargs["json"] == {"emoji": "thumbsup"}

    def test_list_replies(self):
        client = AxClient("https://example.com", "legacy-token")
        response = _make_response(
            "GET", "https://example.com/api/v1/messages/msg-1/replies", 200, json_body={"replies": [{"id": "r1"}]}
        )
        client._http.get = MagicMock(return_value=response)
        result = client.list_replies("msg-1")
        assert result["replies"][0]["id"] == "r1"


# ---------------------------------------------------------------------------
# Task methods — get_task, update_task
# ---------------------------------------------------------------------------


class TestTaskMethods:
    def test_get_task(self):
        client = AxClient("https://example.com", "legacy-token")
        response = _make_response(
            "GET", "https://example.com/api/v1/tasks/t1", 200, json_body={"id": "t1", "title": "Do it"}
        )
        client._http.get = MagicMock(return_value=response)
        result = client.get_task("t1")
        assert result["title"] == "Do it"

    def test_update_task(self):
        client = AxClient("https://example.com", "legacy-token")
        response = _make_response(
            "PATCH", "https://example.com/api/v1/tasks/t1", 200, json_body={"id": "t1", "status": "done"}
        )
        client._http.patch = MagicMock(return_value=response)
        result = client.update_task("t1", status="done", priority="high")
        assert result["status"] == "done"
        assert client._http.patch.call_args.kwargs["json"] == {"status": "done", "priority": "high"}

    def test_create_task_with_description(self):
        client = AxClient("https://example.com", "legacy-token")
        response = _make_response(
            "POST",
            "https://example.com/api/v1/tasks",
            201,
            json_body={"id": "t2", "space_id": "s1", "description": "details"},
        )
        client._http.post = MagicMock(return_value=response)
        result = client.create_task("s1", "New task", description="details")
        body = client._http.post.call_args.kwargs["json"]
        assert body["description"] == "details"
        assert result["id"] == "t2"


# ---------------------------------------------------------------------------
# _task_from_create_response branches
# ---------------------------------------------------------------------------


class TestTaskFromCreateResponse:
    def test_extracts_task_subobject(self):
        client = AxClient("https://example.com", "legacy-token")
        data = {"task": {"id": "t1", "space_id": "s1"}}
        result = client._task_from_create_response(data)
        assert result["id"] == "t1"

    def test_returns_dict_directly_when_no_task_key(self):
        client = AxClient("https://example.com", "legacy-token")
        data = {"id": "t1", "space_id": "s1"}
        result = client._task_from_create_response(data)
        assert result["id"] == "t1"

    def test_returns_empty_dict_for_non_dict(self):
        client = AxClient("https://example.com", "legacy-token")
        result = client._task_from_create_response([{"id": "t1"}])
        assert result == {}


# ---------------------------------------------------------------------------
# _whoami_space_id branches
# ---------------------------------------------------------------------------


class TestWhoamiSpaceId:
    def test_returns_resolved_space_id(self):
        client = AxClient("https://example.com", "legacy-token")
        assert client._whoami_space_id({"resolved_space_id": "s1"}) == "s1"

    def test_returns_space_id_fallback(self):
        client = AxClient("https://example.com", "legacy-token")
        assert client._whoami_space_id({"space_id": "s2"}) == "s2"

    def test_returns_bound_agent_default_space(self):
        client = AxClient("https://example.com", "legacy-token")
        assert client._whoami_space_id({"bound_agent": {"default_space_id": "s3"}}) == "s3"

    def test_returns_none_when_no_space_found(self):
        client = AxClient("https://example.com", "legacy-token")
        assert client._whoami_space_id({}) is None

    def test_returns_none_for_empty_bound_agent(self):
        client = AxClient("https://example.com", "legacy-token")
        assert client._whoami_space_id({"bound_agent": {}}) is None


# ---------------------------------------------------------------------------
# _verify_session_space_for_task_fallback — exception branch
# ---------------------------------------------------------------------------


class TestVerifySessionSpaceFallback:
    def test_raises_on_whoami_failure(self):
        client = AxClient("https://example.com", "legacy-token")
        client._http.get = MagicMock(side_effect=ConnectionError("network down"))
        with pytest.raises(RuntimeError, match="could not verify the active session space"):
            client._verify_session_space_for_task_fallback("space-1")

    def test_raises_on_http_status_error(self):
        """HTTPStatusError from whoami should re-raise directly, not wrap."""
        client = AxClient("https://example.com", "legacy-token")
        error_response = _make_response("GET", "https://example.com/auth/me", 403, json_body={"detail": "forbidden"})
        client._http.get = MagicMock(return_value=error_response)
        # whoami calls raise_for_status which raises HTTPStatusError on 403
        with pytest.raises(httpx.HTTPStatusError):
            client._verify_session_space_for_task_fallback("space-1")


# ---------------------------------------------------------------------------
# _verify_created_task_space — no id/space_id
# ---------------------------------------------------------------------------


def test_verify_created_task_space_no_id_or_space_id():
    client = AxClient("https://example.com", "legacy-token")
    with pytest.raises(RuntimeError, match="did not include an id or space_id"):
        client._verify_created_task_space({}, "expected-space")


# ---------------------------------------------------------------------------
# Agent CRUD methods
# ---------------------------------------------------------------------------


class TestAgentMethods:
    def test_get_agents_presence(self):
        client = AxClient("https://example.com", "legacy-token")
        response = _make_response("GET", "https://example.com/api/v1/agents/presence", 200, json_body={"agents": []})
        client._http.get = MagicMock(return_value=response)
        result = client.get_agents_presence()
        assert result == {"agents": []}

    def test_create_agent(self):
        client = AxClient("https://example.com", "legacy-token")
        response = _make_response(
            "POST", "https://example.com/api/v1/agents", 201, json_body={"id": "a1", "name": "bot"}
        )
        client._http.post = MagicMock(return_value=response)
        result = client.create_agent("bot", description="A bot", model="claude-3", space_id="s1")
        assert result["name"] == "bot"
        body = client._http.post.call_args.kwargs["json"]
        assert body["name"] == "bot"
        assert body["description"] == "A bot"
        assert body["model"] == "claude-3"
        assert body["space_id"] == "s1"

    def test_create_agent_ignores_none_kwargs(self):
        client = AxClient("https://example.com", "legacy-token")
        response = _make_response("POST", "https://example.com/api/v1/agents", 201, json_body={"id": "a1"})
        client._http.post = MagicMock(return_value=response)
        client.create_agent("minimal-bot", description=None)
        body = client._http.post.call_args.kwargs["json"]
        assert body == {"name": "minimal-bot"}

    def test_get_agent(self):
        client = AxClient("https://example.com", "legacy-token")
        response = _make_response(
            "GET", "https://example.com/api/v1/agents/manage/my-bot", 200, json_body={"id": "a1", "name": "my-bot"}
        )
        client._http.get = MagicMock(return_value=response)
        result = client.get_agent("my-bot")
        assert result["name"] == "my-bot"

    def test_update_agent(self):
        client = AxClient("https://example.com", "legacy-token")
        response = _make_response(
            "PUT", "https://example.com/api/v1/agents/manage/a1", 200, json_body={"id": "a1", "description": "updated"}
        )
        client._http.put = MagicMock(return_value=response)
        result = client.update_agent("a1", description="updated")
        assert result["description"] == "updated"

    def test_delete_agent(self):
        client = AxClient("https://example.com", "legacy-token")
        response = _make_response(
            "DELETE", "https://example.com/api/v1/agents/manage/a1", 200, json_body={"deleted": True}
        )
        client._http.delete = MagicMock(return_value=response)
        result = client.delete_agent("a1")
        assert result["deleted"] is True


# ---------------------------------------------------------------------------
# get_agent_tools
# ---------------------------------------------------------------------------


class TestGetAgentTools:
    def test_found_in_roster(self):
        client = AxClient("https://example.com", "legacy-token")
        response = _make_response(
            "GET",
            "https://example.com/api/v1/organizations/s1/roster",
            200,
            json_body={
                "entries": [
                    {"id": "a1", "name": "bot1", "enabled_tools": ["shell"], "capabilities_list": ["code"]},
                    {"id": "a2", "name": "bot2", "enabled_tools": []},
                ],
            },
        )
        client._http.get = MagicMock(return_value=response)
        result = client.get_agent_tools("s1", "a1")
        assert result["agent_id"] == "a1"
        assert result["enabled_tools"] == ["shell"]
        assert result["capabilities"] == ["code"]

    def test_not_found_in_roster(self):
        client = AxClient("https://example.com", "legacy-token")
        response = _make_response(
            "GET",
            "https://example.com/api/v1/organizations/s1/roster",
            200,
            json_body={"entries": [{"id": "other", "name": "other-bot"}]},
        )
        client._http.get = MagicMock(return_value=response)
        result = client.get_agent_tools("s1", "a1")
        assert result["error"] == "not_found"

    def test_roster_as_list(self):
        """When roster response is a plain list (no entries key)."""
        client = AxClient("https://example.com", "legacy-token")
        response = _make_response(
            "GET",
            "https://example.com/api/v1/organizations/s1/roster",
            200,
            json_body=[{"id": "a1", "name": "bot", "enabled_tools": ["x"]}],
        )
        client._http.get = MagicMock(return_value=response)
        result = client.get_agent_tools("s1", "a1")
        assert result["enabled_tools"] == ["x"]


# ---------------------------------------------------------------------------
# get_agent_presence — single-agent with /state fallback to /presence
# ---------------------------------------------------------------------------


class TestGetAgentPresence:
    def test_state_endpoint_unwraps_envelope(self):
        client = AxClient("https://example.com", "legacy-token")
        state_response = _make_response(
            "GET",
            "https://example.com/api/v1/agents/a0eebc99-9c0b-4ef8-bb6d-6bb9bd380a11/state",
            200,
            json_body={
                "agent_state": {"badge": "live", "connection_path": "gateway"},
                "raw_presence": {"last_seen": "2026-05-11"},
                "control": {"enabled": True},
            },
        )
        client._http.get = MagicMock(return_value=state_response)
        result = client.get_agent_presence("a0eebc99-9c0b-4ef8-bb6d-6bb9bd380a11")
        assert result["badge"] == "live"
        assert result["_raw_presence"] == {"last_seen": "2026-05-11"}
        assert result["_control"] == {"enabled": True}

    def test_state_endpoint_returns_raw_when_no_agent_state_key(self):
        client = AxClient("https://example.com", "legacy-token")
        state_response = _make_response(
            "GET",
            "https://example.com/api/v1/agents/a0eebc99-9c0b-4ef8-bb6d-6bb9bd380a11/state",
            200,
            json_body={"badge": "live"},
        )
        client._http.get = MagicMock(return_value=state_response)
        result = client.get_agent_presence("a0eebc99-9c0b-4ef8-bb6d-6bb9bd380a11")
        assert result["badge"] == "live"

    def test_falls_back_to_presence_on_404(self):
        client = AxClient("https://example.com", "legacy-token")
        state_404 = _make_response(
            "GET",
            "https://example.com/api/v1/agents/a0eebc99-9c0b-4ef8-bb6d-6bb9bd380a11/state",
            404,
            json_body={"detail": "not found"},
        )
        presence_ok = _make_response(
            "GET",
            "https://example.com/api/v1/agents/a0eebc99-9c0b-4ef8-bb6d-6bb9bd380a11/presence",
            200,
            json_body={"status": "online"},
        )
        client._http.get = MagicMock(side_effect=[state_404, presence_ok])
        result = client.get_agent_presence("a0eebc99-9c0b-4ef8-bb6d-6bb9bd380a11")
        assert result["status"] == "online"

    def test_state_non_404_error_raises(self):
        client = AxClient("https://example.com", "legacy-token")
        state_500 = _make_response(
            "GET",
            "https://example.com/api/v1/agents/a0eebc99-9c0b-4ef8-bb6d-6bb9bd380a11/state",
            500,
            json_body={"detail": "server error"},
        )
        client._http.get = MagicMock(return_value=state_500)
        with pytest.raises(httpx.HTTPStatusError):
            client.get_agent_presence("a0eebc99-9c0b-4ef8-bb6d-6bb9bd380a11")

    def test_falls_back_to_presence_on_200_html_spa_shell(self):
        """Regression for #60: on SPA-fallback deployments (current paxai.app
        prod, see #59), missing API paths return 200 HTML instead of 404. The
        fallback to /presence must still fire — same "endpoint isn't there"
        semantics, just a different status code shape."""
        client = AxClient("https://example.com", "legacy-token")
        state_spa = _make_response(
            "GET",
            "https://example.com/api/v1/agents/a0eebc99-9c0b-4ef8-bb6d-6bb9bd380a11/state",
            200,
            text='<!DOCTYPE html>\n<html lang="en">\n  <head>\n    <title>aX Platform</title>\n  </head>\n</html>',
        )
        presence_ok = _make_response(
            "GET",
            "https://example.com/api/v1/agents/a0eebc99-9c0b-4ef8-bb6d-6bb9bd380a11/presence",
            200,
            json_body={"status": "online", "responsive": True},
        )
        client._http.get = MagicMock(side_effect=[state_spa, presence_ok])
        result = client.get_agent_presence("a0eebc99-9c0b-4ef8-bb6d-6bb9bd380a11")
        assert result["status"] == "online"
        assert result["responsive"] is True
        # Both endpoints were tried, in order: /state then /presence.
        assert client._http.get.call_count == 2
        first_call_url, second_call_url = (
            client._http.get.call_args_list[0][0][0],
            client._http.get.call_args_list[1][0][0],
        )
        assert first_call_url.endswith("/state")
        assert second_call_url.endswith("/presence")

    def test_falls_back_to_presence_on_text_html_content_type(self):
        """Sibling guard for #60: the SPA-fallback detection should also fire
        when the response has Content-Type: text/html but the body doesn't
        start with <!DOCTYPE (e.g. unusual edge frameworks). Relies on
        _is_html_response's content-type branch, not just the body sniff."""
        client = AxClient("https://example.com", "legacy-token")
        # text="..." in _make_response sets content-type: text/html. Body
        # deliberately does NOT start with <! so the only HTML signal is the
        # content-type header.
        state_html = _make_response(
            "GET",
            "https://example.com/api/v1/agents/a0eebc99-9c0b-4ef8-bb6d-6bb9bd380a11/state",
            200,
            text="some non-doctype html body",
        )
        presence_ok = _make_response(
            "GET",
            "https://example.com/api/v1/agents/a0eebc99-9c0b-4ef8-bb6d-6bb9bd380a11/presence",
            200,
            json_body={"status": "online"},
        )
        client._http.get = MagicMock(side_effect=[state_html, presence_ok])
        result = client.get_agent_presence("a0eebc99-9c0b-4ef8-bb6d-6bb9bd380a11")
        assert result["status"] == "online"

    def test_state_non_404_json_error_still_raises(self):
        """Gating test for #60: a real 5xx with a JSON body must still raise,
        not be silently fallen back over. The HTML-fallback exemption is
        specifically for "endpoint not implemented" cases, not for "server
        error on the implemented endpoint."""
        client = AxClient("https://example.com", "legacy-token")
        state_500_json = _make_response(
            "GET",
            "https://example.com/api/v1/agents/a0eebc99-9c0b-4ef8-bb6d-6bb9bd380a11/state",
            500,
            json_body={"detail": "internal server error"},
        )
        client._http.get = MagicMock(return_value=state_500_json)
        with pytest.raises(httpx.HTTPStatusError):
            client.get_agent_presence("a0eebc99-9c0b-4ef8-bb6d-6bb9bd380a11")

    def test_resolves_name_to_uuid(self):
        client = AxClient("https://example.com", "legacy-token")
        list_response = _make_response(
            "GET",
            "https://example.com/api/v1/agents",
            200,
            json_body={
                "agents": [
                    {"name": "my-bot", "id": "a0eebc99-9c0b-4ef8-bb6d-6bb9bd380a11"},
                ]
            },
        )
        state_response = _make_response(
            "GET",
            "https://example.com/api/v1/agents/a0eebc99-9c0b-4ef8-bb6d-6bb9bd380a11/state",
            200,
            json_body={"agent_state": {"badge": "live"}},
        )
        client._http.get = MagicMock(side_effect=[list_response, state_response])
        result = client.get_agent_presence("my-bot")
        assert result["badge"] == "live"

    def test_raises_when_agent_name_not_found(self):
        client = AxClient("https://example.com", "legacy-token")
        list_response = _make_response(
            "GET",
            "https://example.com/api/v1/agents",
            200,
            json_body={"agents": [{"name": "other-bot", "id": "x"}]},
        )
        client._http.get = MagicMock(return_value=list_response)
        with pytest.raises(RuntimeError, match="agent not found"):
            client.get_agent_presence("nonexistent-bot")

    def test_raises_when_agent_has_no_id(self):
        client = AxClient("https://example.com", "legacy-token")
        list_response = _make_response(
            "GET",
            "https://example.com/api/v1/agents",
            200,
            json_body={"agents": [{"name": "no-id-bot"}]},
        )
        client._http.get = MagicMock(return_value=list_response)
        with pytest.raises(RuntimeError, match="agent has no id field"):
            client.get_agent_presence("no-id-bot")


# ---------------------------------------------------------------------------
# get_agent_placement
# ---------------------------------------------------------------------------


class TestGetAgentPlacement:
    def test_returns_placement_fields(self):
        client = AxClient("https://example.com", "legacy-token")
        agent_response = _make_response(
            "GET",
            "https://example.com/api/v1/agents/manage/my-bot",
            200,
            json_body={
                "agent": {
                    "id": "a1",
                    "name": "my-bot",
                    "space_id": "s1",
                    "pinned": True,
                    "allowed_spaces": ["s1", "s2"],
                },
            },
        )
        client._http.get = MagicMock(return_value=agent_response)
        result = client.get_agent_placement("my-bot")
        assert result["agent_id"] == "a1"
        assert result["space_id"] == "s1"
        assert result["pinned"] is True
        assert result["allowed_spaces"] == ["s1", "s2"]

    def test_placement_with_uuid(self):
        client = AxClient("https://example.com", "legacy-token")
        uuid = "a0eebc99-9c0b-4ef8-bb6d-6bb9bd380a11"
        agent_response = _make_response(
            "GET",
            f"https://example.com/api/v1/agents/manage/{uuid}",
            200,
            json_body={"id": "a1", "name": "bot", "space_id": "s1"},
        )
        client._http.get = MagicMock(return_value=agent_response)
        result = client.get_agent_placement(uuid)
        assert result["agent_id"] == "a1"


# ---------------------------------------------------------------------------
# list_agents_availability — badge_state branch
# ---------------------------------------------------------------------------


def test_list_agents_availability_with_all_filters():
    client = AxClient("https://example.com", "legacy-token")
    response = _make_response("GET", "https://example.com/api/v1/agents/availability", 200, json_body=[])
    client._http.get = MagicMock(return_value=response)
    client.list_agents_availability(
        space_id="s1",
        connection_path="gateway_managed",
        badge_state="live",
        filter_="available_now",
    )
    params = client._http.get.call_args.kwargs["params"]
    assert params["space_id"] == "s1"
    assert params["connection_path"] == "gateway_managed"
    assert params["badge_state"] == "live"
    assert params["filter"] == "available_now"


# ---------------------------------------------------------------------------
# Context methods
# ---------------------------------------------------------------------------


class TestContextMethods:
    def test_set_context(self):
        client = AxClient("https://example.com", "legacy-token")
        response = _make_response("POST", "https://example.com/api/v1/context", 200, json_body={"ok": True})
        client._http.post = MagicMock(return_value=response)
        result = client.set_context("s1", "my-key", "my-value", ttl=300)
        body = client._http.post.call_args.kwargs["json"]
        assert body == {"key": "my-key", "value": "my-value", "space_id": "s1", "ttl": 300}
        assert result["ok"] is True

    def test_set_context_without_ttl(self):
        client = AxClient("https://example.com", "legacy-token")
        response = _make_response("POST", "https://example.com/api/v1/context", 200, json_body={"ok": True})
        client._http.post = MagicMock(return_value=response)
        client.set_context("s1", "key", "val")
        body = client._http.post.call_args.kwargs["json"]
        assert "ttl" not in body

    def test_get_context(self):
        client = AxClient("https://example.com", "legacy-token")
        response = _make_response(
            "GET", "https://example.com/api/v1/context/my-key", 200, json_body={"key": "my-key", "value": "v"}
        )
        client._http.get = MagicMock(return_value=response)
        result = client.get_context("my-key", space_id="s1")
        assert result["key"] == "my-key"
        assert client._http.get.call_args.kwargs["params"] == {"space_id": "s1"}

    def test_get_context_without_space(self):
        client = AxClient("https://example.com", "legacy-token")
        response = _make_response("GET", "https://example.com/api/v1/context/k", 200, json_body={"key": "k"})
        client._http.get = MagicMock(return_value=response)
        client.get_context("k")
        assert client._http.get.call_args.kwargs["params"] is None

    def test_list_context(self):
        client = AxClient("https://example.com", "legacy-token")
        response = _make_response("GET", "https://example.com/api/v1/context", 200, json_body={"items": []})
        client._http.get = MagicMock(return_value=response)
        client.list_context(prefix="env:", space_id="s1")
        params = client._http.get.call_args.kwargs["params"]
        assert params == {"prefix": "env:", "space_id": "s1"}

    def test_list_context_no_filters(self):
        client = AxClient("https://example.com", "legacy-token")
        response = _make_response("GET", "https://example.com/api/v1/context", 200, json_body={"items": []})
        client._http.get = MagicMock(return_value=response)
        client.list_context()
        params = client._http.get.call_args.kwargs["params"]
        assert params == {}

    def test_delete_context(self):
        client = AxClient("https://example.com", "legacy-token")
        response = _make_response("DELETE", "https://example.com/api/v1/context/my-key", 204)
        client._http.delete = MagicMock(return_value=response)
        status = client.delete_context("my-key", space_id="s1")
        assert status == 204
        assert client._http.delete.call_args.kwargs["params"] == {"space_id": "s1"}

    def test_delete_context_without_space(self):
        client = AxClient("https://example.com", "legacy-token")
        response = _make_response("DELETE", "https://example.com/api/v1/context/k", 200)
        client._http.delete = MagicMock(return_value=response)
        client.delete_context("k")
        assert client._http.delete.call_args.kwargs["params"] is None


# ---------------------------------------------------------------------------
# search_messages
# ---------------------------------------------------------------------------


def test_search_messages():
    client = AxClient("https://example.com", "legacy-token")
    response = _make_response("POST", "https://example.com/api/v1/search/messages", 200, json_body={"results": []})
    client._http.post = MagicMock(return_value=response)
    result = client.search_messages("hello world", limit=10)
    assert result == {"results": []}
    body = client._http.post.call_args.kwargs["json"]
    assert body == {"query": "hello world", "limit": 10}


# ---------------------------------------------------------------------------
# Keys (PAT management) — list, revoke, rotate, create with extra args
# ---------------------------------------------------------------------------


class TestKeyManagement:
    def test_create_key_with_audience_and_scopes_and_space_id(self):
        client = AxClient("https://example.com", "legacy-token")
        response = _make_response("POST", "https://example.com/api/v1/keys", 201, json_body={"ok": True})
        client._http.post = MagicMock(return_value=response)
        client.create_key(
            "my-key",
            audience="mcp",
            scopes=["messages:read"],
            space_id="s1",
        )
        body = client._http.post.call_args.kwargs["json"]
        assert body["audience"] == "mcp"
        assert body["scopes"] == ["messages:read"]
        headers = client._http.post.call_args.kwargs["headers"]
        assert headers["X-Space-Id"] == "s1"

    def test_list_keys(self):
        client = AxClient("https://example.com", "legacy-token")
        response = _make_response("GET", "https://example.com/api/v1/keys", 200, json_body=[{"id": "k1"}])
        client._http.get = MagicMock(return_value=response)
        result = client.list_keys()
        assert result == [{"id": "k1"}]

    def test_revoke_key(self):
        client = AxClient("https://example.com", "legacy-token")
        response = _make_response("DELETE", "https://example.com/api/v1/keys/k1", 204)
        client._http.delete = MagicMock(return_value=response)
        status = client.revoke_key("k1")
        assert status == 204

    def test_rotate_key(self):
        client = AxClient("https://example.com", "legacy-token")
        response = _make_response(
            "POST", "https://example.com/api/v1/keys/k1/rotate", 200, json_body={"new_token": "axp_u_new"}
        )
        client._http.post = MagicMock(return_value=response)
        result = client.rotate_key("k1")
        assert result["new_token"] == "axp_u_new"


# ---------------------------------------------------------------------------
# _admin_headers
# ---------------------------------------------------------------------------


class TestAdminHeaders:
    def test_admin_headers_without_exchanger(self):
        client = AxClient("https://example.com", "legacy-token")
        headers = client._admin_headers("agents.create")
        # Without exchanger, returns base headers (no exchange happens)
        assert "Content-Type" in headers

    def test_admin_headers_with_exchanger(self, tmp_path, monkeypatch, mock_exchange):
        mock_exchange(access_token="admin.jwt")
        monkeypatch.chdir(tmp_path)
        (tmp_path / ".ax").mkdir()
        (tmp_path / ".ax" / "config.toml").write_text("")
        client = AxClient("https://example.com", "axp_u_Key.Secret")
        headers = client._admin_headers("agents.create")
        assert headers["Authorization"] == "Bearer admin.jwt"


# ---------------------------------------------------------------------------
# Management methods — update_agent, issue_agent_pat with name, issue_enrollment with name,
# revoke_credential, list_credentials, create_agent with extra kwargs
# ---------------------------------------------------------------------------


class TestManagementMethods:
    def test_mgmt_update_agent(self):
        client = AxClient("https://example.com", "legacy-token")
        client._admin_headers = MagicMock(return_value={"Authorization": "Bearer admin"})
        response = _make_response(
            "PATCH", "https://example.com/agents/manage/a1", 200, json_body={"id": "a1", "description": "updated"}
        )
        client._http.patch = MagicMock(return_value=response)
        result = client.mgmt_update_agent("a1", description="updated")
        assert result["description"] == "updated"
        assert client._http.patch.call_args.args[0] == "/agents/manage/a1"

    def test_mgmt_issue_agent_pat_with_name(self):
        client = AxClient("https://example.com", "legacy-token")
        client._admin_headers = MagicMock(return_value={"Authorization": "Bearer admin"})
        response = _make_response(
            "POST", "https://example.com/credentials/agent-pat", 201, json_body={"token": "axp_a_new"}
        )
        client._http.post = MagicMock(return_value=response)
        client.mgmt_issue_agent_pat("a1", name="my-pat")
        body = client._http.post.call_args.kwargs["json"]
        assert body["name"] == "my-pat"
        assert body["agent_id"] == "a1"

    def test_mgmt_issue_enrollment_with_name(self):
        client = AxClient("https://example.com", "legacy-token")
        client._admin_headers = MagicMock(return_value={"Authorization": "Bearer admin"})
        response = _make_response(
            "POST", "https://example.com/credentials/enrollment", 201, json_body={"code": "enroll-1"}
        )
        client._http.post = MagicMock(return_value=response)
        client.mgmt_issue_enrollment(name="bot-enrollment")
        body = client._http.post.call_args.kwargs["json"]
        assert body["name"] == "bot-enrollment"

    def test_mgmt_revoke_credential(self):
        client = AxClient("https://example.com", "legacy-token")
        client._admin_headers = MagicMock(return_value={"Authorization": "Bearer admin"})
        response = _make_response("DELETE", "https://example.com/credentials/c1", 200, json_body={"revoked": True})
        client._http.delete = MagicMock(return_value=response)
        result = client.mgmt_revoke_credential("c1")
        assert result["revoked"] is True

    def test_mgmt_list_credentials(self):
        client = AxClient("https://example.com", "legacy-token")
        client._admin_headers = MagicMock(return_value={"Authorization": "Bearer admin"})
        response = _make_response("GET", "https://example.com/credentials", 200, json_body=[{"id": "c1"}])
        client._http.get = MagicMock(return_value=response)
        result = client.mgmt_list_credentials()
        assert result == [{"id": "c1"}]

    def test_mgmt_create_agent_passes_extra_kwargs(self):
        client = AxClient("https://example.com", "legacy-token")
        client._admin_headers = MagicMock(return_value={"Authorization": "Bearer admin"})
        response = _make_response(
            "POST", "https://example.com/api/v1/agents/manage/create", 201, json_body={"id": "a1"}
        )
        client._http.post = MagicMock(return_value=response)
        client.mgmt_create_agent("bot", description="A bot", model="claude-3", space_id="s1")
        body = client._http.post.call_args.kwargs["json"]
        assert body["description"] == "A bot"
        assert body["model"] == "claude-3"
        assert body["space_id"] == "s1"


# ---------------------------------------------------------------------------
# Context manager / close
# ---------------------------------------------------------------------------


class TestClientLifecycle:
    def test_close_delegates_to_http(self):
        client = AxClient("https://example.com", "legacy-token")
        client._http.close = MagicMock()
        client.close()
        client._http.close.assert_called_once()

    def test_context_manager(self):
        with AxClient("https://example.com", "legacy-token") as client:
            assert client is not None
            client._http.close = MagicMock()
        # __exit__ should have called close
        client._http.close.assert_called_once()

    def test_enter_returns_self(self):
        client = AxClient("https://example.com", "legacy-token")
        result = client.__enter__()
        assert result is client
        client._http.close = MagicMock()
        client.__exit__(None, None, None)


# ---------------------------------------------------------------------------
# _inject_auth event hook
# ---------------------------------------------------------------------------


class TestInjectAuth:
    def test_inject_auth_adds_bearer_when_no_authorization(self, tmp_path, monkeypatch, mock_exchange):
        mock_exchange(access_token="injected.jwt")
        monkeypatch.chdir(tmp_path)
        (tmp_path / ".ax").mkdir()
        (tmp_path / ".ax" / "config.toml").write_text("")
        client = AxClient("https://example.com", "axp_u_Key.Secret")
        request = httpx.Request("GET", "https://example.com/api/v1/messages")
        client._inject_auth(request)
        assert request.headers["Authorization"] == "Bearer injected.jwt"

    def test_inject_auth_skips_when_authorization_present(self, tmp_path, monkeypatch, mock_exchange):
        mock_exchange(access_token="injected.jwt")
        monkeypatch.chdir(tmp_path)
        (tmp_path / ".ax").mkdir()
        (tmp_path / ".ax" / "config.toml").write_text("")
        client = AxClient("https://example.com", "axp_u_Key.Secret")
        request = httpx.Request(
            "GET", "https://example.com/api/v1/messages", headers={"Authorization": "Bearer existing"}
        )
        client._inject_auth(request)
        assert request.headers["Authorization"] == "Bearer existing"


# ---------------------------------------------------------------------------
# Constructor edge cases
# ---------------------------------------------------------------------------


class TestConstructorEdgeCases:
    def test_non_exchange_path_sets_authorization_directly(self):
        client = AxClient("https://example.com", "legacy-jwt-token")
        assert client._base_headers["Authorization"] == "Bearer legacy-jwt-token"
        assert client._use_exchange is False

    def test_non_exchange_path_sets_agent_name_header(self):
        client = AxClient("https://example.com", "legacy-token", agent_name="my-agent")
        assert client._base_headers["X-Agent-Name"] == "my-agent"

    def test_exchange_path_does_not_set_agent_name_header(self, tmp_path, monkeypatch, mock_exchange):
        mock_exchange()
        monkeypatch.chdir(tmp_path)
        (tmp_path / ".ax").mkdir()
        (tmp_path / ".ax" / "config.toml").write_text("")
        client = AxClient("https://example.com", "axp_u_Key.Secret", agent_name="my-agent")
        assert "X-Agent-Name" not in client._base_headers

    def test_base_url_trailing_slash_stripped(self):
        client = AxClient("https://example.com/", "legacy-token")
        assert client.base_url == "https://example.com"

    def test_honeypot_token_triggers_alert_during_construction(self):
        with patch("ax_cli.client.httpx.post") as mock_post:
            AxClient("https://example.com", "ghp_fake_github_token")
            mock_post.assert_called_once()


# ---------------------------------------------------------------------------
# promote_context
# ---------------------------------------------------------------------------


def test_promote_context():
    client = AxClient("https://example.com", "legacy-token")
    response = _make_response(
        "POST", "https://example.com/api/v1/spaces/s1/intelligence/promote", 200, json_body={"ok": True}
    )
    client._http.post = MagicMock(return_value=response)
    result = client.promote_context("s1", "my-key", artifact_type="RESEARCH", agent_id="a1")
    body = client._http.post.call_args.kwargs["json"]
    assert body == {"key": "my-key", "artifact_type": "RESEARCH", "agent_id": "a1"}
    assert result["ok"] is True


# ---------------------------------------------------------------------------
# set_agent_placement
# ---------------------------------------------------------------------------


def test_set_agent_placement():
    client = AxClient("https://example.com", "legacy-token")
    response = _make_response("POST", "https://example.com/api/v1/agents/a1/placement", 200, json_body={"ok": True})
    client._http.post = MagicMock(return_value=response)
    result = client.set_agent_placement("a1", space_id="s1", pinned=True)
    body = client._http.post.call_args.kwargs["json"]
    assert body == {"space_id": "s1", "pinned": True}
    assert result["ok"] is True


# ---------------------------------------------------------------------------
# _management_json_with_fallback — all paths miss (lines 393-394)
# ---------------------------------------------------------------------------


def test_management_fallback_raises_when_all_paths_are_route_misses():
    """When every route in paths returns a miss (404/405/HTML), the method
    should raise HTTPStatusError from the last response's raise_for_status()."""
    client = AxClient("https://example.com", "legacy-token")
    client._admin_headers = MagicMock(return_value={"Authorization": "Bearer admin"})
    client._http.get = MagicMock(
        side_effect=[
            _make_response("GET", "https://example.com/path1", 404, json_body={"detail": "Not found"}),
            _make_response("GET", "https://example.com/path2", 404, json_body={"detail": "Not found"}),
        ]
    )
    with pytest.raises(httpx.HTTPStatusError):
        client._management_json_with_fallback("get", ["/path1", "/path2"])


# ---------------------------------------------------------------------------
# create_task_auth_contract — no id in response (line 789)
# ---------------------------------------------------------------------------


def test_create_task_auth_contract_raises_when_no_id_in_response(monkeypatch):
    import httpx as _httpx

    def fake_exchange(url, *, json=None, headers=None, timeout=None):
        return _httpx.Response(
            200,
            json={
                "access_token": "exchanged.jwt",
                "expires_in": 3600,
                "token_class": "agent_access",
                "agent_id": "agent-123",
            },
            request=_httpx.Request("POST", url),
        )

    monkeypatch.setattr(_httpx, "post", fake_exchange)
    client = AxClient("https://example.com", "axp_a_AgentKey.AgentSecret", agent_name="test-agent")
    # Response has no "id" field
    task_response = _make_response(
        "POST",
        "https://example.com/api/tasks",
        201,
        json_body={"title": "Missing ID task"},
    )
    client._http.post = MagicMock(return_value=task_response)
    with pytest.raises(RuntimeError, match="did not include an id"):
        client.create_task_auth_contract("Missing ID task")


# ---------------------------------------------------------------------------
# x-ratelimit-reset normalization
# ---------------------------------------------------------------------------


class TestNormalizeRateLimitReset:
    def test_epoch_seconds_pass_through(self):
        epoch = 1_781_827_556.0
        assert _normalize_rate_limit_reset(str(int(epoch)), now=1_700_000_000.0) == epoch

    def test_delta_seconds_converted_to_epoch(self):
        now = 1_700_000_000.0
        assert _normalize_rate_limit_reset("60", now=now) == now + 60

    def test_http_date_converted_to_epoch(self):
        import email.utils

        expected = email.utils.parsedate_to_datetime("Wed, 21 Oct 2015 07:28:00 GMT").timestamp()
        assert _normalize_rate_limit_reset("Wed, 21 Oct 2015 07:28:00 GMT") == expected

    def test_missing_or_blank_returns_zero(self):
        assert _normalize_rate_limit_reset(None) == 0.0
        assert _normalize_rate_limit_reset("") == 0.0
        assert _normalize_rate_limit_reset("   ") == 0.0

    def test_unparseable_returns_zero(self):
        assert _normalize_rate_limit_reset("not-a-date") == 0.0


# ---------------------------------------------------------------------------
# _RateLimitState tests
# ---------------------------------------------------------------------------


class TestRateLimitState:
    def test_record_sets_exhausted_when_remaining_zero(self):
        state = _RateLimitState()
        state.record(remaining=0, reset_at=9999999999.0)
        assert state.exhausted_for(RATE_LIMIT_LOW_WATER) is True

    def test_record_clears_exhausted_when_remaining_positive(self):
        state = _RateLimitState()
        state.record(remaining=0, reset_at=9999999999.0)
        state.record(remaining=50, reset_at=9999999999.0)
        assert state.exhausted_for(RATE_LIMIT_LOW_WATER) is False

    def test_record_updates_reset_at(self):
        state = _RateLimitState()
        state.record(remaining=0, reset_at=1234567890.0)
        assert state.reset_at == 1234567890.0

    def test_wait_if_needed_no_op_when_not_exhausted(self, monkeypatch):
        import time as _time

        sleeps = []
        monkeypatch.setattr(_time, "sleep", lambda s: sleeps.append(s))
        state = _RateLimitState()
        state.record(remaining=50, reset_at=_time.time() + 30)
        state.wait_if_needed(120.0)
        assert sleeps == []

    def test_low_water_threshold_triggers_before_zero(self, monkeypatch):
        """Exhaustion fires at <= the caller's low-water threshold, not just at 0."""
        import time as _time

        monkeypatch.setattr(_time, "sleep", lambda s: None)
        state = _RateLimitState()
        state.record(remaining=RATE_LIMIT_LOW_WATER, reset_at=_time.time() + 30)
        assert state.exhausted_for(RATE_LIMIT_LOW_WATER) is True
        state.record(remaining=RATE_LIMIT_LOW_WATER + 1, reset_at=_time.time() + 30)
        assert state.exhausted_for(RATE_LIMIT_LOW_WATER) is False

    def test_exhaustion_is_caller_relative(self, monkeypatch):
        """One shared state, two thresholds: remaining=5 stops automated callers
        (low water 10) but lets human-initiated callers (low water 2) proceed."""
        import time as _time

        sleeps = []
        monkeypatch.setattr(_time, "sleep", lambda s: sleeps.append(s))
        state = _RateLimitState()
        state.record(remaining=5, reset_at=_time.time() + 30)
        assert state.exhausted_for(RATE_LIMIT_LOW_WATER) is True
        assert state.exhausted_for(RATE_LIMIT_INTERACTIVE_LOW_WATER) is False
        state.wait_if_needed(120.0, low_water=RATE_LIMIT_INTERACTIVE_LOW_WATER)
        assert sleeps == []  # human-threshold caller proceeds
        state.wait_if_needed(120.0, low_water=RATE_LIMIT_LOW_WATER)
        assert len(sleeps) == 1  # automated-threshold caller waits

    def test_wait_if_needed_sleeps_when_exhausted(self, monkeypatch):
        import time as _time

        sleeps = []
        monkeypatch.setattr(_time, "sleep", lambda s: sleeps.append(s))
        state = _RateLimitState()
        state.record(remaining=0, reset_at=_time.time() + 30)
        state.wait_if_needed(120.0)
        assert len(sleeps) == 1
        assert sleeps[0] > 0

    def test_wait_if_needed_calls_callback(self, monkeypatch):
        import time as _time

        monkeypatch.setattr(_time, "sleep", lambda s: None)
        calls = []
        state = _RateLimitState()
        state.record(remaining=0, reset_at=_time.time() + 30)
        state.wait_if_needed(120.0, on_wait=lambda w, r: calls.append((w, r)))
        assert len(calls) == 1
        assert calls[0][0] > 0

    def test_wait_if_needed_clears_exhausted_after_wait(self, monkeypatch):
        import time as _time

        now = [_time.time()]
        monkeypatch.setattr(_time, "sleep", lambda s: now.__setitem__(0, now[0] + s))
        monkeypatch.setattr(_time, "time", lambda: now[0])
        state = _RateLimitState()
        state.record(remaining=0, reset_at=now[0] + 30)
        state.wait_if_needed(120.0)
        assert state.exhausted_for(RATE_LIMIT_LOW_WATER) is False  # window turned over during the sleep

    def test_wait_if_needed_raises_preempted_when_wait_exceeds_max(self, monkeypatch):
        import time as _time

        sleeps = []
        monkeypatch.setattr(_time, "sleep", lambda s: sleeps.append(s))
        state = _RateLimitState()
        state.record(remaining=0, reset_at=_time.time() + 999)
        with pytest.raises(RateLimitPreemptedError) as exc_info:
            state.wait_if_needed(30.0)
        assert sleeps == []
        assert exc_info.value.retry_after_seconds > 0
        assert "try again after" in str(exc_info.value)

    def test_shared_state_coordinates_across_clients(self, monkeypatch):
        """Two _RetryOnAuthClient instances sharing state both see exhaustion."""
        import time as _time

        sleeps = []
        monkeypatch.setattr(_time, "sleep", lambda s: sleeps.append(s))

        shared = _RateLimitState()
        request = httpx.Request("GET", "https://example.com/api/v1/agents")
        ok = httpx.Response(
            200,
            headers={
                "x-ratelimit-remaining": "0",
                "x-ratelimit-reset": str(_time.time() + 30),
            },
            request=request,
        )

        inner_a = MagicMock()
        inner_a.get.return_value = ok
        client_a = _RetryOnAuthClient(inner_a, get_fresh_jwt=None, rate_limit_state=shared)

        inner_b = MagicMock()
        inner_b.get.return_value = ok
        client_b = _RetryOnAuthClient(inner_b, get_fresh_jwt=None, rate_limit_state=shared)

        client_a.get("/api/v1/agents")  # records exhaustion on shared state
        client_b.get("/api/v1/agents")  # should sleep before making its request
        assert len(sleeps) == 1  # exactly one sleep from client_b's proactive wait

    def test_wait_if_needed_does_not_clear_exhausted_if_record_fires_during_sleep(self, monkeypatch):
        """If record() sets a new low-water window during the sleep, callers must
        still see the state as exhausted afterwards (exhaustion is derived from
        the recorded facts, so a new window can never be clobbered by a waiter)."""
        import time as _time

        sleeps = []

        def fake_sleep(s):
            sleeps.append(s)
            state.record(remaining=0, reset_at=_time.time() + 60)

        monkeypatch.setattr(_time, "sleep", fake_sleep)
        state = _RateLimitState()
        state.record(remaining=0, reset_at=_time.time() + 0.1)  # just expired
        state.wait_if_needed(120.0)
        assert state.exhausted_for(RATE_LIMIT_LOW_WATER) is True  # new window set during sleep must survive

    def test_missing_rate_limit_headers_do_not_update_state(self):
        """Responses without x-ratelimit-remaining must not touch _RateLimitState."""
        import time as _time

        state = _RateLimitState()
        state.record(remaining=50, reset_at=_time.time() + 30)

        request = httpx.Request("GET", "https://example.com/api/v1/agents")
        no_headers = httpx.Response(200, request=request)

        inner = MagicMock()
        inner.get.return_value = no_headers
        client = _RetryOnAuthClient(inner, get_fresh_jwt=None, rate_limit_state=state)
        client.get("/api/v1/agents")

        assert state.remaining == 50  # unchanged
        assert state.exhausted_for(RATE_LIMIT_LOW_WATER) is False

    def test_exhausted_without_reset_header_uses_current_time(self):
        """When remaining hits low-water but reset header is absent, reset_at is set to
        now so wait_if_needed sleeps at most 0.5s instead of waiting on a stale window."""
        import time as _time

        state = _RateLimitState()
        stale_reset = _time.time() + 9999
        state.record(remaining=50, reset_at=stale_reset)

        before = _time.time()
        state.record(remaining=0, reset_at=0.0)
        after = _time.time()

        assert state.exhausted_for(RATE_LIMIT_LOW_WATER) is True
        assert before <= state.reset_at <= after + 1

    def test_missing_rate_limit_headers_pass_none_to_callback(self):
        """on_request_complete receives remaining=None when headers are absent."""
        calls = []
        request = httpx.Request("GET", "https://example.com/api/v1/agents")
        no_headers = httpx.Response(200, request=request)

        inner = MagicMock()
        inner.get.return_value = no_headers
        client = _RetryOnAuthClient(
            inner,
            get_fresh_jwt=None,
            on_request_complete=lambda method, path, status, remaining, reset_at, ct="": calls.append(remaining),
        )
        client.get("/api/v1/agents")
        assert calls == [None]


# ---------------------------------------------------------------------------
# _RetryOnAuthClient — proactive rate-limit behaviour
# ---------------------------------------------------------------------------


def _rl_response(remaining: int, reset_at: float, status: int = 200) -> httpx.Response:
    request = httpx.Request("GET", "https://paxai.app/api/v1/agents")
    return httpx.Response(
        status,
        headers={"x-ratelimit-remaining": str(remaining), "x-ratelimit-reset": str(reset_at)},
        request=request,
    )


def _rl_response_delta(remaining: int, reset_delta: int, status: int = 200) -> httpx.Response:
    request = httpx.Request("GET", "https://paxai.app/api/v1/agents")
    return httpx.Response(
        status,
        headers={"x-ratelimit-remaining": str(remaining), "x-ratelimit-reset": str(reset_delta)},
        request=request,
    )


class TestRetryOnAuthClientRateLimiting:
    """_RetryOnAuthClient proactive rate-limit sleep and preemption."""

    def test_no_wait_when_remaining_positive(self, monkeypatch):
        import time as _time

        sleeps = []
        monkeypatch.setattr(_time, "sleep", lambda s: sleeps.append(s))
        inner = MagicMock()
        inner.get.return_value = _rl_response(remaining=5, reset_at=_time.time() + 60)
        client = _RetryOnAuthClient(inner, get_fresh_jwt=None)
        client.get("/api/v1/agents")
        assert sleeps == []

    def test_waits_when_exhausted(self, monkeypatch):
        import time as _time

        sleeps = []
        monkeypatch.setattr(_time, "sleep", lambda s: sleeps.append(s))
        reset_at = _time.time() + 30
        inner = MagicMock()
        inner.get.return_value = _rl_response(remaining=0, reset_at=reset_at)
        client = _RetryOnAuthClient(inner, get_fresh_jwt=None)
        client.get("/api/v1/agents")  # records exhaustion
        client.get("/api/v1/agents")  # should sleep before this request
        assert len(sleeps) == 1
        assert sleeps[0] > 0

    def test_waits_when_reset_header_is_delta_seconds(self, monkeypatch):
        import time as _time

        sleeps = []
        now = [_time.time()]
        monkeypatch.setattr(_time, "sleep", lambda s: sleeps.append(s))
        monkeypatch.setattr(_time, "time", lambda: now[0])
        inner = MagicMock()
        inner.get.return_value = _rl_response_delta(remaining=0, reset_delta=30)
        client = _RetryOnAuthClient(inner, get_fresh_jwt=None)
        client.get("/api/v1/agents")  # records exhaustion with delta reset
        client.get("/api/v1/agents")  # should sleep before this request
        assert len(sleeps) == 1
        assert sleeps[0] > 0
        assert client._rl.reset_at == pytest.approx(now[0] + 30)

    def test_calls_callback_on_wait(self, monkeypatch):
        import time as _time

        monkeypatch.setattr(_time, "sleep", lambda s: None)
        reset_at = _time.time() + 30
        calls = []
        inner = MagicMock()
        inner.get.return_value = _rl_response(remaining=0, reset_at=reset_at)
        client = _RetryOnAuthClient(
            inner,
            get_fresh_jwt=None,
            on_rate_limit_wait=lambda w, r: calls.append((w, r)),
        )
        client.get("/api/v1/agents")
        client.get("/api/v1/agents")
        assert len(calls) == 1
        wait, reset = calls[0]
        assert wait > 0
        assert reset == reset_at

    def test_raises_preempted_when_wait_exceeds_max(self, monkeypatch):
        import time as _time

        sleeps = []
        monkeypatch.setattr(_time, "sleep", lambda s: sleeps.append(s))
        reset_at = _time.time() + 999
        inner = MagicMock()
        inner.get.return_value = _rl_response(remaining=0, reset_at=reset_at)
        client = _RetryOnAuthClient(inner, get_fresh_jwt=None, max_rate_limit_wait=30.0)
        client.get("/api/v1/agents")  # records exhaustion
        with pytest.raises(RateLimitPreemptedError) as exc_info:
            client.get("/api/v1/agents")
        assert sleeps == []
        assert exc_info.value.retry_after_seconds > 0
        assert "try again after" in str(exc_info.value)

    def test_interactive_client_proceeds_where_automated_waits(self, monkeypatch):
        """Same shared state, remaining=5: an automated client (low water 10)
        sleeps; a human-initiated client (low water 2) sends immediately."""
        import time as _time

        sleeps = []
        monkeypatch.setattr(_time, "sleep", lambda s: sleeps.append(s))
        shared = _RateLimitState()
        ok = _rl_response(remaining=5, reset_at=_time.time() + 30)

        inner_auto = MagicMock()
        inner_auto.get.return_value = ok
        automated = _RetryOnAuthClient(inner_auto, get_fresh_jwt=None, rate_limit_state=shared)

        inner_human = MagicMock()
        inner_human.get.return_value = ok
        interactive = _RetryOnAuthClient(
            inner_human,
            get_fresh_jwt=None,
            rate_limit_state=shared,
            rate_limit_low_water=RATE_LIMIT_INTERACTIVE_LOW_WATER,
        )

        automated.get("/api/v1/agents")  # records remaining=5 on shared state
        interactive.get("/api/v1/agents")  # 5 > 2 → proceeds without sleeping
        assert sleeps == []
        automated.get("/api/v1/agents")  # 5 <= 10 → waits
        assert len(sleeps) == 1
