"""Per-module gateway command tests: gateway_session (gateway split #28 Phase 1).

Ported from skipped test_gateway_commands*.py; monkeypatches target ax_cli.commands.gateway_session."""

from __future__ import annotations

import sys

import httpx
import pytest
from typer.testing import CliRunner

from ax_cli import gateway as gateway_core
from ax_cli.commands import gateway_agents as _gw_agents
from ax_cli.commands import gateway_auth as _gw_auth
from ax_cli.commands import gateway_session as _gw_session
from tests.gateway_cmd_testlib import _FakeUserClient, _seed_local_session_for_challenge

runner = CliRunner()


def test_session_challenge_disabled_by_default(monkeypatch, tmp_path):
    """Flag off → send returns normal payload, no challenge surface."""
    monkeypatch.delenv("AX_GATEWAY_SESSION_CHALLENGE", raising=False)
    token = _seed_local_session_for_challenge(tmp_path, monkeypatch)

    payload = _gw_session._send_local_session_message(
        session_token=token,
        body={"content": "hello", "space_id": "space-1"},
    )

    assert payload["agent"] == "challenge-agent"
    assert "next_session_proof" not in payload
    # Registry session record stays clean — no challenge state written.
    record = _gw_session._find_local_session_record(
        gateway_core.load_gateway_registry(), payload["session"]["session_id"]
    )
    assert "challenge_code" not in record


def test_session_challenge_first_send_issues_code_and_rejects(monkeypatch, tmp_path):
    """Flag on, no proof → raise with structured `session_challenge_required: <code>`
    and persist the code on the session record so the next send can verify."""
    monkeypatch.setenv("AX_GATEWAY_SESSION_CHALLENGE", "1")
    token = _seed_local_session_for_challenge(tmp_path, monkeypatch)

    with pytest.raises(ValueError) as excinfo:
        _gw_session._send_local_session_message(
            session_token=token,
            body={"content": "hello", "space_id": "space-1"},
        )
    msg = str(excinfo.value)
    assert msg.startswith("session_challenge_required:")
    # Code from the message ("session_challenge_required: ABCD. ...").
    issued_code = msg.split(":", 1)[1].strip().split(".", 1)[0].strip()
    assert issued_code, "challenge code must appear in the error"
    # Stored on the session record for the next send to verify against.
    registry_after = gateway_core.load_gateway_registry()
    record = registry_after["local_sessions"][0]
    assert record["challenge_code"] == issued_code
    assert "challenge_issued_at" in record


def test_session_challenge_valid_proof_rotates_and_returns_next_code(monkeypatch, tmp_path):
    """Flag on, second send with the matching proof → succeeds, response carries
    a fresh `next_session_proof` so the caller can present it on the next send."""
    monkeypatch.setenv("AX_GATEWAY_SESSION_CHALLENGE", "1")
    token = _seed_local_session_for_challenge(tmp_path, monkeypatch)

    # First call issues the challenge.
    with pytest.raises(ValueError) as first:
        _gw_session._send_local_session_message(session_token=token, body={"content": "first", "space_id": "space-1"})
    issued = str(first.value).split(":", 1)[1].strip().split(".", 1)[0].strip()

    # Second call with the matching proof succeeds and rotates.
    payload = _gw_session._send_local_session_message(
        session_token=token,
        body={"content": "second", "space_id": "space-1", "session_proof": issued},
    )
    assert payload["agent"] == "challenge-agent"
    next_code = payload["next_session_proof"]
    assert next_code, "rotated challenge code missing from response"
    assert next_code != issued, "code must rotate on every successful send"

    # Stored code matches the rotated one.
    record = gateway_core.load_gateway_registry()["local_sessions"][0]
    assert record["challenge_code"] == next_code


def test_session_challenge_wrong_proof_rejected(monkeypatch, tmp_path):
    """Flag on, mismatched proof → structured `invalid_session_proof: expected <code>`."""
    monkeypatch.setenv("AX_GATEWAY_SESSION_CHALLENGE", "1")
    token = _seed_local_session_for_challenge(tmp_path, monkeypatch)

    # Issue a challenge first.
    with pytest.raises(ValueError) as first:
        _gw_session._send_local_session_message(session_token=token, body={"content": "first", "space_id": "space-1"})
    issued = str(first.value).split(":", 1)[1].strip().split(".", 1)[0].strip()

    with pytest.raises(ValueError) as wrong:
        _gw_session._send_local_session_message(
            session_token=token,
            body={"content": "second", "space_id": "space-1", "session_proof": "WRONG-CODE"},
        )
    msg = str(wrong.value)
    assert msg.startswith("invalid_session_proof:")
    assert issued in msg, "error must surface the expected code so the operator can recover"
    # The stored code must NOT have rotated — a wrong proof doesn't burn the
    # current challenge.
    record = gateway_core.load_gateway_registry()["local_sessions"][0]
    assert record["challenge_code"] == issued


def test_gateway_local_connect_requests_approval_then_issues_session(monkeypatch, tmp_path):
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
    monkeypatch.setattr(_gw_auth, "_load_gateway_user_client", lambda: _FakeUserClient())
    monkeypatch.setattr(_gw_agents, "_find_agent_in_space", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        _gw_agents,
        "_create_agent_in_space",
        lambda *args, **kwargs: {"id": "agent-local-1", "name": "codex-local"},
    )
    monkeypatch.setattr(_gw_agents, "_polish_metadata", lambda *args, **kwargs: None)
    monkeypatch.setattr(_gw_agents, "_mint_agent_pat", lambda *args, **kwargs: ("axp_a_agent.secret", "mgmt"))
    fingerprint = {
        "agent_name": "codex-local",
        "pid": 999999,
        "parent_pid": 1,
        "cwd": str(tmp_path),
        "exe_path": sys.executable,
        "user": "madtank",
    }

    first = _gw_session._connect_local_pass_through_agent(agent_name="codex-local", fingerprint=fingerprint)

    assert first["status"] == "pending"
    assert first["approval_id"]
    registry = gateway_core.load_gateway_registry()
    entry = gateway_core.find_agent_entry(registry, "codex-local")
    assert entry is not None
    assert entry["template_id"] == "pass_through"
    assert entry["local_connection_mode"] == "pass_through"
    assert registry["approvals"][0]["status"] == "pending"

    gateway_core.approve_gateway_approval(first["approval_id"])
    second = _gw_session._connect_local_pass_through_agent(agent_name="codex-local", fingerprint=fingerprint)

    assert second["status"] == "approved"
    assert second["session_token"].startswith("axgw_s_")
    stored = gateway_core.load_gateway_registry()
    session = gateway_core.verify_local_session_token(stored, second["session_token"])
    assert session["agent_name"] == "codex-local"
    queued_entry = gateway_core.find_agent_entry(stored, "codex-local")
    assert queued_entry is not None
    queued_entry["backlog_depth"] = 1
    queued_entry["queue_depth"] = 1
    queued_entry["current_status"] = "queued"
    queued_entry["current_activity"] = "Queued in Gateway"
    queued_entry["last_received_message_id"] = "queued-local-1"
    gateway_core.save_gateway_registry(stored)
    gateway_core.save_agent_pending_messages(
        "codex-local",
        [
            {
                "message_id": "queued-local-1",
                "content": "@codex-local please check this",
                "display_name": "madtank",
                "created_at": "2026-04-25T11:59:00Z",
                "queued_at": "2026-04-25T12:00:00Z",
            }
        ],
    )

    third = _gw_session._connect_local_pass_through_agent(registry_ref="#1", fingerprint=fingerprint)

    assert third["status"] == "approved"
    assert third["registry_ref"] == "#1"
    assert third["agent"]["name"] == "codex-local"
    assert third["session_token"].startswith("axgw_s_")

    calls = {}

    class FakeManagedClient:
        def __init__(self):
            self.sent = []

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
            self.sent.append(
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
            return {
                "message": {
                    "id": "local-send-1",
                    "sender_type": "agent",
                    "display_name": "codex-local",
                    "agent_id": agent_id,
                    "metadata": metadata,
                }
            }

        def list_messages(
            self,
            limit=20,
            channel="main",
            *,
            space_id=None,
            agent_id=None,
            unread_only=False,
            mark_read=False,
        ):
            calls["list"] = {
                "limit": limit,
                "channel": channel,
                "space_id": space_id,
                "agent_id": agent_id,
                "unread_only": unread_only,
                "mark_read": mark_read,
            }
            return {
                "messages": [
                    {
                        "id": "msg-1",
                        "content": "approve this deployment",
                        "display_name": "orion",
                        "created_at": "2026-04-25T12:00:00Z",
                    }
                ],
                "unread_count": 1,
                "marked_read_count": 1,
            }

    managed_client = FakeManagedClient()
    monkeypatch.setattr(_gw_session, "_load_managed_agent_client", lambda entry: managed_client)

    sent = _gw_session._send_local_session_message(
        session_token=second["session_token"],
        body={
            "space_id": "space-1",
            "content": "@night_owl please review",
            "parent_id": "parent-1",
            "metadata": {"purpose": "review"},
        },
    )

    assert sent["agent"] == "codex-local"
    assert sent["message"]["message"]["sender_type"] == "agent"
    assert sent["message"]["message"]["display_name"] == "codex-local"
    assert managed_client.sent == [
        {
            "space_id": "space-1",
            "content": "@night_owl please review",
            "agent_id": "agent-local-1",
            "channel": "main",
            "parent_id": "parent-1",
            "metadata": {
                "purpose": "review",
                "gateway_local_session_id": session["session_id"],
                "gateway_pass_through_agent": "codex-local",
                "gateway_pass_through_agent_id": "agent-local-1",
                "gateway_pass_through_fingerprint_signature": session["fingerprint_signature"],
                # Reply metadata is preserved through Gateway:
                # parent_id flips routing_intent on, and the @handle in content
                # is extracted so the backend can fan the reply out to night_owl.
                "routing_intent": "reply_with_mentions",
                "mentions": ["night_owl"],
            },
            "message_type": "text",
            "attachments": None,
        }
    ]

    inbox = _gw_session._local_session_inbox(session_token=second["session_token"], limit=5)

    assert inbox["agent"] == "codex-local"
    assert inbox["messages"][0]["content"] == "approve this deployment"
    assert gateway_core.load_agent_pending_messages("codex-local") == []
    updated_entry = gateway_core.find_agent_entry(gateway_core.load_gateway_registry(), "codex-local")
    assert updated_entry["backlog_depth"] == 0
    assert updated_entry["queue_depth"] == 0
    assert updated_entry["current_status"] is None
    assert updated_entry["current_activity"] is None
    assert calls["list"] == {
        "limit": 5,
        "channel": "main",
        "space_id": "space-1",
        "agent_id": "agent-local-1",
        "unread_only": True,
        "mark_read": True,
    }


def test_local_process_fingerprint_resolves_executable_symlink(tmp_path):
    exe_target = tmp_path / "python3.12"
    exe_target.write_text("fake python")
    exe_link = tmp_path / "python3"
    exe_link.symlink_to(exe_target)

    fingerprint = _gw_session._local_process_fingerprint(
        agent_name="codex-local",
        cwd=str(tmp_path),
        exe_path=str(exe_link),
    )

    assert fingerprint["exe_path"] == str(exe_target.resolve())


def test_gateway_local_connect_rejects_registry_ref_for_managed_runtime(monkeypatch, tmp_path):
    config_dir = tmp_path / "config"
    monkeypatch.setenv("AX_CONFIG_DIR", str(config_dir))
    registry = gateway_core.load_gateway_registry()
    registry["agents"] = [
        {
            "name": "demo-hermes",
            "agent_id": "agent-hermes-1",
            "space_id": "space-1",
            "template_id": "hermes",
            "runtime_type": "sentinel_inference_sdk",
            "install_id": "install-hermes-1",
        }
    ]
    gateway_core.save_gateway_registry(registry)
    fingerprint = {
        "agent_name": "codex-local",
        "pid": 999999,
        "cwd": str(tmp_path),
        "exe_path": sys.executable,
        "user": "madtank",
    }

    with pytest.raises(ValueError, match="registry_ref_not_attachable"):
        _gw_session._connect_local_pass_through_agent(registry_ref="#1", fingerprint=fingerprint)


def test_gateway_local_connect_rejects_second_identity_from_same_origin(monkeypatch, tmp_path):
    config_dir = tmp_path / "config"
    monkeypatch.setenv("AX_CONFIG_DIR", str(config_dir))
    fingerprint = {
        "agent_name": "mac_frontend",
        "pid": 999999,
        "parent_pid": 1,
        "cwd": str(tmp_path),
        "exe_path": sys.executable,
        "user": "madtank",
    }
    registry = gateway_core.load_gateway_registry()
    registry["agents"] = [
        {
            "name": "mac_frontend",
            "agent_id": "agent-mac-frontend-1",
            "space_id": "space-1",
            "template_id": "pass_through",
            "runtime_type": "inbox",
            "local_fingerprint": dict(fingerprint),
        }
    ]
    gateway_core.save_gateway_registry(registry)
    changed_name = dict(fingerprint)
    changed_name["agent_name"] = "frontend_sentinel"

    with pytest.raises(ValueError, match="already registered as @mac_frontend"):
        _gw_session._connect_local_pass_through_agent(agent_name="frontend_sentinel", fingerprint=changed_name)


def test_gateway_local_connect_still_blocks_fresh_name_when_workdir_is_owned(monkeypatch, tmp_path):
    """The fresh-name protection must still fire when registering a brand-new
    agent at a workdir already owned by a different agent.

    This is the same shape as the existing
    ``rejects_second_identity_from_same_origin`` test but explicitly framed as
    the "after the fix, the protection still exists" guard so a future
    refactor can't quietly silence it.
    """
    config_dir = tmp_path / "config"
    monkeypatch.setenv("AX_CONFIG_DIR", str(config_dir))
    fingerprint = {
        "agent_name": "owner",
        "pid": 999999,
        "parent_pid": 1,
        "cwd": str(tmp_path),
        "exe_path": sys.executable,
        "user": "anyone",
    }
    registry = gateway_core.load_gateway_registry()
    registry["agents"] = [
        {
            "name": "owner",
            "agent_id": "agent-owner",
            "space_id": "space-1",
            "template_id": "pass_through",
            "runtime_type": "inbox",
            "local_fingerprint": dict(fingerprint),
        }
    ]
    gateway_core.save_gateway_registry(registry)

    fresh_attempt = {**fingerprint, "agent_name": "newbie"}
    with pytest.raises(ValueError, match="already registered as @owner"):
        _gw_session._connect_local_pass_through_agent(agent_name="newbie", fingerprint=fresh_attempt)


def test_local_proxy_allowlist_includes_upload_file():
    """The /local/proxy method allowlist must expose upload_file so agents
    on the gateway-native path can attach files to messages without holding
    the user PAT."""
    spec = _gw_session._LOCAL_PROXY_METHODS.get("upload_file")
    assert spec is not None, "upload_file should be on the local proxy allowlist"
    # file_path is positional, space_id is keyword — matches AxClient.upload_file.
    assert "file_path" in spec.get("args", [])
    assert "space_id" in spec.get("kwargs", [])


def test_proxy_upload_file_rejects_path_outside_workdir(monkeypatch, tmp_path):
    """The proxy handler must reject upload_file requests where file_path
    resolves outside the agent's registered workdir.

    Operational finding: /tmp/gateway-security-test.md (completely outside
    any agent workdir) was successfully uploaded to paxai.app through the
    agent's managed PAT. No path restriction was enforced.
    """
    config_dir = tmp_path / "config"
    monkeypatch.setenv("AX_CONFIG_DIR", str(config_dir))

    agent_workdir = tmp_path / "agent-home"
    agent_workdir.mkdir()
    outside_file = tmp_path / "sensitive.md"
    outside_file.write_text("secret content")

    token_file = tmp_path / "agent.token"
    token_file.write_text("axp_a_test.token")

    gateway_core.save_gateway_session(
        {
            "token": "axp_u_test.token",
            "base_url": "https://paxai.app",
            "space_id": "space-1",
            "username": "tester",
        }
    )
    registry = gateway_core.load_gateway_registry()
    registry["agents"] = [
        {
            "name": "sandboxed-agent",
            "agent_id": "agent-1",
            "space_id": "space-1",
            "base_url": "https://paxai.app",
            "runtime_type": "hermes",
            "template_id": "hermes",
            "desired_state": "running",
            "effective_state": "running",
            "approval_state": "approved",
            "token_file": str(token_file),
            "transport": "gateway",
            "credential_source": "gateway",
            "workdir": str(agent_workdir),
        }
    ]
    gateway_core.save_gateway_registry(registry)

    entry = registry["agents"][0]
    session = gateway_core.issue_local_session(registry, entry)
    gateway_core.save_gateway_registry(registry)
    session_token = session["session_token"]

    uploaded = False

    class _SpyClient:
        def __init__(self, **kw):
            pass

        def upload_file(self, file_path, *, space_id=None):
            nonlocal uploaded
            uploaded = True
            return {"id": "file-1", "filename": "sensitive.md"}

        def close(self):
            pass

    monkeypatch.setattr(_gw_auth, "AxClient", _SpyClient)

    # Attempt to upload a file outside the agent's workdir
    try:
        _gw_session._proxy_local_session_call(
            session_token=session_token,
            body={"method": "upload_file", "args": {"file_path": str(outside_file)}},
        )
        # If we get here without an error, the path was not validated.
        # This test documents the expected fix — it will FAIL until
        # path sandboxing is implemented.
        assert not uploaded, (
            f"upload_file accepted path outside workdir: {outside_file} "
            f"(workdir={agent_workdir}). The proxy must reject this."
        )
    except (ValueError, PermissionError) as exc:
        # Expected after the fix: proxy should raise on path traversal
        assert "workdir" in str(exc).lower() or "path" in str(exc).lower() or "outside" in str(exc).lower()


def _stale_space_error(space_id: str) -> httpx.HTTPStatusError:
    """Build the 404 the platform raises for a vanished space (require_space)."""
    request = httpx.Request("POST", "http://platform/api/v1/messages")
    response = httpx.Response(404, json={"detail": f"Space not found: '{space_id}'"}, request=request)
    return httpx.HTTPStatusError("404 Not Found", request=request, response=response)


def test_platform_error_detail_extracts_server_detail():
    exc = _stale_space_error("abc")
    assert _gw_session._platform_error_detail(exc) == "Space not found: 'abc'"
    assert _gw_session._is_stale_space_error(exc) is True


def test_is_stale_space_error_false_for_non_space_failures():
    request = httpx.Request("POST", "http://platform/api/v1/messages")
    forbidden = httpx.HTTPStatusError(
        "403", request=request, response=httpx.Response(403, json={"detail": "forbidden"}, request=request)
    )
    not_found_other = httpx.HTTPStatusError(
        "404", request=request, response=httpx.Response(404, json={"detail": "Agent not found"}, request=request)
    )
    assert _gw_session._is_stale_space_error(forbidden) is False
    assert _gw_session._is_stale_space_error(not_found_other) is False


def test_resolve_live_fallback_prefers_live_session_space(monkeypatch):
    class _C:
        def list_spaces(self):
            return {"spaces": [{"id": "live-a"}, {"id": "live-b"}]}

    monkeypatch.setattr(_gw_session, "load_gateway_session", lambda: {"space_id": "live-b"})
    assert _gw_session._resolve_live_fallback_space(_C(), "dead") == "live-b"


def test_resolve_live_fallback_first_live_when_session_space_also_dead(monkeypatch):
    class _C:
        def list_spaces(self):
            return {"spaces": [{"id": "live-a"}, {"id": "live-b"}]}

    monkeypatch.setattr(_gw_session, "load_gateway_session", lambda: {"space_id": "also-dead"})
    assert _gw_session._resolve_live_fallback_space(_C(), "dead") == "live-a"


def test_resolve_live_fallback_none_when_attempted_space_is_live(monkeypatch):
    class _C:
        def list_spaces(self):
            return {"spaces": [{"id": "live-a"}]}

    monkeypatch.setattr(_gw_session, "load_gateway_session", lambda: {})
    assert _gw_session._resolve_live_fallback_space(_C(), "live-a") is None


def test_send_local_session_falls_back_to_live_space_on_stale_404(monkeypatch, tmp_path):
    """A stale pinned space_id (404 Space not found) is retried into a live space."""
    monkeypatch.delenv("AX_GATEWAY_SESSION_CHALLENGE", raising=False)
    token = _seed_local_session_for_challenge(tmp_path, monkeypatch)

    class _StaleThenLiveClient:
        def __init__(self):
            self.sent = []

        def list_spaces(self):
            return {"spaces": [{"id": "space-live", "name": "Live"}]}

        def send_message(self, space_id, content, **kwargs):
            self.sent.append(space_id)
            if space_id != "space-live":
                raise _stale_space_error(space_id)
            return {"message": {"id": "ok-1", "space_id": space_id, "content": content}}

    client = _StaleThenLiveClient()
    monkeypatch.setattr(_gw_session, "_load_managed_agent_client", lambda entry: client)

    payload = _gw_session._send_local_session_message(
        session_token=token,
        body={"content": "hello", "space_id": "space-stale"},
    )

    # First attempt hits the stale space, then retries into the live space.
    assert client.sent == ["space-stale", "space-live"]
    assert payload["message"]["message"]["id"] == "ok-1"


def test_send_local_session_stale_space_without_live_fallback_raises_actionable(monkeypatch, tmp_path):
    """No live space to fall back to → actionable error naming the move command."""
    monkeypatch.delenv("AX_GATEWAY_SESSION_CHALLENGE", raising=False)
    token = _seed_local_session_for_challenge(tmp_path, monkeypatch)

    class _NoLiveSpaceClient:
        def list_spaces(self):
            return {"spaces": []}

        def send_message(self, space_id, content, **kwargs):
            raise _stale_space_error(space_id)

    monkeypatch.setattr(_gw_session, "_load_managed_agent_client", lambda entry: _NoLiveSpaceClient())

    with pytest.raises(ValueError) as excinfo:
        _gw_session._send_local_session_message(
            session_token=token,
            body={"content": "hello", "space_id": "space-stale"},
        )
    msg = str(excinfo.value)
    assert "no longer exists" in msg.lower()
    assert "ax gateway agents move" in msg
    assert "space-stale" in msg
