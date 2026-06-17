"""Tests for the Claude Code channel bridge identity boundary."""

import asyncio
import json
import os
from pathlib import Path

import pytest
import typer
from typer.testing import CliRunner

from ax_cli import gateway as gateway_core
from ax_cli.commands import channel as channel_mod
from ax_cli.commands.channel import ChannelBridge, MentionEvent, _load_channel_env
from ax_cli.commands.listen import _is_self_authored, _remember_reply_anchor, _should_respond

runner = CliRunner()


class FakeClient:
    def __init__(self, token: str = "axp_a_AgentKey.Secret", *, agent_id: str = "agent-123"):
        self.token = token
        self.agent_id = agent_id
        self._use_exchange = token.startswith("axp_")
        self.sent = []
        self.processing_statuses = []

    def send_message(self, space_id, content, *, parent_id=None, **kwargs):
        self.sent.append({"space_id": space_id, "content": content, "parent_id": parent_id, **kwargs})
        return {"message": {"id": "msg-123"}}

    def set_agent_processing_status(self, message_id, status, *, agent_name=None, space_id=None):
        self.processing_statuses.append(
            {
                "message_id": message_id,
                "status": status,
                "agent_name": agent_name,
                "space_id": space_id,
            }
        )
        return {"ok": True, "status": status}


class CaptureBridge(ChannelBridge):
    def __init__(self, client, *, agent_id="agent-123", processing_status=True):
        super().__init__(
            client=client,
            agent_name="peer-agent",
            agent_id=agent_id,
            space_id="space-123",
            queue_size=10,
            debug=False,
            processing_status=processing_status,
        )
        self.writes = []

    async def write_message(self, payload):
        self.writes.append(payload)


class FakeSseResponse:
    status_code = 200

    def __init__(self, payload, *, event_type: str = "message"):
        self.payload = payload
        self.event_type = event_type

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def iter_lines(self):
        yield f"event: {self.event_type}"
        yield f"data: {json.dumps(self.payload)}"
        yield ""


class FakeMultiEventSseResponse:
    """SSE response that yields a scripted sequence of (event_type, payload) events."""

    status_code = 200

    def __init__(self, events: list[tuple[str, dict]]):
        self.events = events

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def iter_lines(self):
        for event_type, payload in self.events:
            yield f"event: {event_type}"
            yield f"data: {json.dumps(payload)}"
            yield ""


def test_channel_rejects_user_pat_for_agent_reply():
    client = FakeClient("axp_u_UserKey.Secret")
    bridge = CaptureBridge(client)
    bridge._last_message_id = "incoming-123"

    asyncio.run(
        bridge.handle_tool_call(
            1,
            {"name": "reply", "arguments": {"text": "hello"}},
        )
    )

    assert client.sent == []
    result = bridge.writes[0]["result"]
    assert result["isError"] is True
    assert "agent-bound PAT" in result["content"][0]["text"]


def test_channel_sends_with_agent_bound_pat():
    client = FakeClient("axp_a_AgentKey.Secret")
    bridge = CaptureBridge(client)
    bridge._last_message_id = "incoming-123"

    asyncio.run(
        bridge.handle_tool_call(
            1,
            {"name": "reply", "arguments": {"text": "hello"}},
        )
    )

    assert client.sent == [
        {
            "space_id": "space-123",
            "content": "hello",
            "parent_id": "incoming-123",
            "metadata": {
                "top_level_ingress": False,
                "routing": {"mode": "reply_target", "source": "channel_reply"},
            },
        }
    ]
    assert client.processing_statuses == [
        {
            "message_id": "incoming-123",
            "status": "completed",
            "agent_name": "peer-agent",
            "space_id": "space-123",
        }
    ]
    result = bridge.writes[0]["result"]
    assert result["content"][0]["text"] == "sent reply to incoming-123 (msg-123)"
    assert "msg-123" in bridge._reply_anchor_ids


def test_channel_reply_preserves_explicit_mentions_for_routing():
    client = FakeClient("axp_a_AgentKey.Secret")
    bridge = CaptureBridge(client)
    bridge._last_message_id = "incoming-123"

    asyncio.run(
        bridge.handle_tool_call(
            1,
            {"name": "reply", "arguments": {"text": "@nemotron can you check this with @peer-agent?"}},
        )
    )

    assert client.sent[0]["metadata"]["mentions"] == ["nemotron"]


# --- Inbox bundling on channel reply (aX task 663d9e6f) -------------------


class _InboxClient(FakeClient):
    """FakeClient that also supports list_messages so the inbox poll lands."""

    def __init__(self, *, list_messages_response, **kwargs):
        super().__init__(**kwargs)
        self._list_messages_response = list_messages_response
        self.list_messages_calls: list[dict] = []

    def list_messages(
        self, *, limit=None, space_id=None, agent_id=None, unread_only=None, mark_read=None, channel=None
    ):
        self.list_messages_calls.append(
            {
                "limit": limit,
                "space_id": space_id,
                "agent_id": agent_id,
                "unread_only": unread_only,
                "mark_read": mark_read,
                "channel": channel,
            }
        )
        return self._list_messages_response


def _send_response_for(bridge: CaptureBridge) -> dict:
    """Return the response payload the bridge wrote for the most recent reply."""
    for payload in reversed(bridge.writes):
        if isinstance(payload, dict) and payload.get("result") and "content" in payload["result"]:
            return payload["result"]
    raise AssertionError("no MCP response written")


def test_channel_reply_bundles_unread_inbox_in_response_by_default():
    """Default ON: a reply should return what arrived while the agent was drafting."""
    inbox_response = {
        "messages": [
            {"id": "m-1", "content": "@peer-agent ping from alex", "agent_name": "alex"},
            {"id": "m-2", "content": "@peer-agent follow-up", "agent_name": "alex"},
        ],
        "unread_count": 2,
    }
    client = _InboxClient(list_messages_response=inbox_response)
    bridge = CaptureBridge(client)
    bridge._last_message_id = "incoming-123"

    asyncio.run(
        bridge.handle_tool_call(
            1,
            {"name": "reply", "arguments": {"text": "thanks!", "inbox_wait": 0}},
        )
    )

    assert client.sent[0]["content"] == "thanks!"
    assert client.list_messages_calls, "list_messages must run on the post-reply inbox poll"
    assert client.list_messages_calls[0]["unread_only"] is True
    assert client.list_messages_calls[0]["mark_read"] is True
    assert client.list_messages_calls[0]["agent_id"] == "agent-123"

    response = _send_response_for(bridge)
    assert len(response["content"]) == 2  # send confirmation + inbox bundle
    inbox_text = response["content"][1]["text"]
    assert "INBOX while you were drafting" in inbox_text
    assert "2 unread message(s)" in inbox_text
    assert "@alex" in inbox_text
    assert "ping from alex" in inbox_text


def test_channel_reply_skips_inbox_when_arg_is_false():
    """`inbox: false` opts out of the post-reply poll entirely."""
    client = _InboxClient(list_messages_response={"messages": [], "unread_count": 0})
    bridge = CaptureBridge(client)
    bridge._last_message_id = "incoming-123"

    asyncio.run(
        bridge.handle_tool_call(
            1,
            {"name": "reply", "arguments": {"text": "no inbox please", "inbox": False}},
        )
    )

    assert not client.list_messages_calls, "inbox=false must skip the post-reply poll"
    response = _send_response_for(bridge)
    assert len(response["content"]) == 1  # just the send confirmation, no inbox item


def test_channel_reply_omits_inbox_section_when_no_unread():
    """No unread messages → response stays single-item, not noisy."""
    client = _InboxClient(list_messages_response={"messages": [], "unread_count": 0})
    bridge = CaptureBridge(client)
    bridge._last_message_id = "incoming-123"

    asyncio.run(
        bridge.handle_tool_call(
            1,
            {"name": "reply", "arguments": {"text": "quiet send", "inbox_wait": 0}},
        )
    )

    assert client.list_messages_calls, "poll still runs to verify quiet"
    response = _send_response_for(bridge)
    assert len(response["content"]) == 1


def test_format_inbox_bundle_for_mcp_caps_at_five():
    """The render helper truncates long inbox lists to keep the MCP item compact."""
    bundle = {
        "agent": "peer-agent",
        "messages": [{"id": f"m-{i}", "content": f"msg {i}", "agent_name": "alex"} for i in range(10)],
        "unread_count": 10,
    }
    text = channel_mod._format_inbox_bundle_for_mcp(bundle)
    assert "10 unread message(s)" in text
    # 5 visible items + 1 "and N more" line.
    assert text.count("@alex") == 5
    assert "and 5 more" in text


def test_channel_reply_shows_inbox_error_when_poll_fails():
    """If list_messages raises, the response still ships the send confirmation
    plus a `(inbox poll failed: ...)` line so the agent knows something missed."""

    class _RaisingClient(FakeClient):
        def list_messages(self, **_kwargs):
            raise RuntimeError("upstream 503")

    client = _RaisingClient()
    bridge = CaptureBridge(client)
    bridge._last_message_id = "incoming-123"

    asyncio.run(
        bridge.handle_tool_call(
            1,
            {"name": "reply", "arguments": {"text": "even on error", "inbox_wait": 0}},
        )
    )

    response = _send_response_for(bridge)
    assert len(response["content"]) == 2
    assert "inbox poll failed" in response["content"][1]["text"]
    assert "upstream 503" in response["content"][1]["text"]


def test_channel_can_publish_working_status_on_delivery():
    client = FakeClient("axp_a_AgentKey.Secret")
    bridge = CaptureBridge(client)

    asyncio.run(bridge.publish_processing_status("incoming-123", "working"))

    assert client.processing_statuses == [
        {
            "message_id": "incoming-123",
            "status": "working",
            "agent_name": "peer-agent",
            "space_id": "space-123",
        }
    ]


def test_channel_processes_idle_event_before_jwt_reconnect(monkeypatch):
    """The event that wakes an idle stream must not be dropped for reconnect."""

    class FakeSseClient(FakeClient):
        def __init__(self):
            super().__init__()
            self.connect_calls = 0

        def connect_sse(self, *, space_id):
            self.connect_calls += 1
            assert space_id == "space-123"
            return FakeSseResponse(
                {
                    "id": "incoming-123",
                    "content": "@peer-agent please check this",
                    "author": {"id": "user-123", "name": "alex", "type": "user"},
                    "mentions": ["peer-agent"],
                }
            )

        def get_message(self, message_id):
            assert message_id == "incoming-123"
            return {"message": {"metadata": {}}}

    client = FakeSseClient()
    bridge = CaptureBridge(client)
    delivered: list[MentionEvent] = []

    def capture_delivery(event):
        delivered.append(event)
        bridge.shutdown.set()

    bridge.enqueue_from_thread = capture_delivery
    ticks = iter([0, channel_mod._SSE_RECONNECT_INTERVAL + 1])
    monkeypatch.setattr(channel_mod.time, "monotonic", lambda: next(ticks, channel_mod._SSE_RECONNECT_INTERVAL + 2))

    channel_mod._sse_loop(bridge)

    assert [event.message_id for event in delivered] == ["incoming-123"]
    assert delivered[0].prompt == "please check this"


def _run_sse_loop_with_events(
    events: list[tuple[str, dict]],
    *,
    stop_after_delivery: int = 1,
    monkeypatch=None,
):
    """Drive `_sse_loop` against a scripted SSE event list and capture deliveries."""

    class ScriptedClient(FakeClient):
        def __init__(self):
            super().__init__()
            self.connect_calls = 0
            self.get_message_calls: list[str] = []

        def connect_sse(self, *, space_id):
            self.connect_calls += 1
            return FakeMultiEventSseResponse(events)

        def get_message(self, message_id):
            self.get_message_calls.append(message_id)
            return {"message": {"metadata": {}}}

    client = ScriptedClient()
    bridge = CaptureBridge(client)
    delivered: list[MentionEvent] = []
    deliveries_needed = stop_after_delivery

    def capture_delivery(event):
        delivered.append(event)
        if len(delivered) >= deliveries_needed:
            bridge.shutdown.set()

    bridge.enqueue_from_thread = capture_delivery

    # Make reconnect path inert so the bridge processes all scripted events in
    # one pass and only stops when shutdown is set (either by delivery capture
    # or because the scripted stream is exhausted).
    if monkeypatch is not None:
        monkeypatch.setattr(channel_mod.time, "monotonic", lambda: 0.0)

    # Run the loop; it exits when the scripted iter_lines generator completes
    # and ConnectionError propagates, or when shutdown is set inside capture.
    # We wrap in a bounded number of connect_sse calls to avoid infinite loop
    # if the test script doesn't produce deliveries.
    original_connect = client.connect_sse

    def limited_connect(*args, **kwargs):
        if client.connect_calls >= 2:
            bridge.shutdown.set()
            raise ConnectionError("test: exhausted scripted connects")
        return original_connect(*args, **kwargs)

    client.connect_sse = limited_connect  # type: ignore[assignment]

    channel_mod._sse_loop(bridge)
    return bridge, client, delivered


def test_channel_skips_streaming_reply_non_final(monkeypatch):
    """Placeholder/progress chunks marked non-final must not wake the session."""

    events = [
        (
            "message",
            {
                "id": "stream-1",
                "content": "Working…",
                "author": {"id": "user-1", "name": "alex", "type": "user"},
                "mentions": ["peer-agent"],
                "metadata": {
                    "streaming_reply": {"enabled": True, "final": False, "runtime": "hermes_sdk"},
                },
            },
        ),
    ]
    _, _, delivered = _run_sse_loop_with_events(events, monkeypatch=monkeypatch)
    assert delivered == []


def test_channel_skips_working_progress_message(monkeypatch):
    """Defensive regex catches progress payloads even without streaming metadata."""

    events = [
        (
            "message",
            {
                "id": "progress-1",
                "content": "@peer-agent Working…",
                "author": {"id": "user-1", "name": "alex", "type": "user"},
                "mentions": ["peer-agent"],
            },
        ),
        (
            "message",
            {
                "id": "progress-2",
                "content": "Received",
                "author": {"id": "user-1", "name": "alex", "type": "user"},
                "mentions": ["peer-agent"],
            },
        ),
        (
            "message",
            {
                "id": "progress-3",
                "content": "@peer-agent Thinking...",
                "author": {"id": "user-1", "name": "alex", "type": "user"},
                "mentions": ["peer-agent"],
            },
        ),
        (
            "message",
            {
                "id": "progress-4",
                "content": "@peer-agent No response after 5m - session may need attention.",
                "author": {"id": "user-1", "name": "alex", "type": "user"},
                "mentions": ["peer-agent"],
            },
        ),
    ]
    _, _, delivered = _run_sse_loop_with_events(events, monkeypatch=monkeypatch)
    assert delivered == []


def test_channel_delivers_prompts_that_merely_start_with_progress_words(monkeypatch):
    """A legitimate prompt like '@peer-agent Working-state cleanup proposal' must land.

    The fallback progress regex must be anchored — otherwise user messages that
    happen to start with Working/Processing/Thinking/Received would be dropped
    silently. Regression for PR #70 review (2026-04-18).
    """

    events = [
        (
            "message",
            {
                "id": "real-prompt-1",
                "content": "@peer-agent Working-state cleanup proposal",
                "author": {"id": "user-1", "name": "alex", "type": "user"},
                "mentions": ["peer-agent"],
            },
        ),
    ]
    _, _, delivered = _run_sse_loop_with_events(events, monkeypatch=monkeypatch)
    assert len(delivered) == 1
    assert "Working-state cleanup proposal" in delivered[0].prompt


def test_channel_delivers_processing_webhook_errors_prompt(monkeypatch):
    """`Processing webhook errors` is a real user prompt, not a progress marker."""

    events = [
        (
            "message",
            {
                "id": "real-prompt-2",
                "content": "@peer-agent Processing webhook errors in the dispatch queue",
                "author": {"id": "user-1", "name": "alex", "type": "user"},
                "mentions": ["peer-agent"],
            },
        ),
    ]
    _, _, delivered = _run_sse_loop_with_events(events, monkeypatch=monkeypatch)
    assert len(delivered) == 1
    assert "Processing webhook errors" in delivered[0].prompt


def test_channel_delivers_thinking_through_issue_prompt(monkeypatch):
    """`Thinking through this API issue` must be delivered, not suppressed."""

    events = [
        (
            "message",
            {
                "id": "real-prompt-3",
                "content": "@peer-agent Thinking through this API issue",
                "author": {"id": "user-1", "name": "alex", "type": "user"},
                "mentions": ["peer-agent"],
            },
        ),
    ]
    _, _, delivered = _run_sse_loop_with_events(events, monkeypatch=monkeypatch)
    assert len(delivered) == 1
    assert "Thinking through this API issue" in delivered[0].prompt


def test_channel_delivers_message_updated_final(monkeypatch):
    """When hermes streams a final payload via message_updated we deliver it."""

    placeholder = {
        "id": "hermes-1",
        "content": "Working…",
        "author": {"id": "agent-2", "name": "frontend_sentinel", "type": "agent"},
        "mentions": ["peer-agent"],
        "metadata": {
            "streaming_reply": {"enabled": True, "final": False, "runtime": "hermes_sdk"},
        },
    }
    final_update = {
        "id": "hermes-1",
        "content": "@peer-agent here is the real reply",
        "author": {"id": "agent-2", "name": "frontend_sentinel", "type": "agent"},
        "mentions": ["peer-agent"],
        "metadata": {
            "streaming_reply": {"enabled": True, "final": True, "runtime": "hermes_sdk"},
        },
    }
    events = [("message", placeholder), ("message_updated", final_update)]
    _, _, delivered = _run_sse_loop_with_events(events, monkeypatch=monkeypatch)
    assert [e.message_id for e in delivered] == ["hermes-1"]
    assert delivered[0].prompt == "here is the real reply"


def test_channel_skips_message_updated_for_already_delivered(monkeypatch):
    """Once a final payload is delivered, subsequent updates for that id do not re-wake."""

    payload = {
        "id": "msg-dup",
        "content": "@peer-agent please review",
        "author": {"id": "user-1", "name": "alex", "type": "user"},
        "mentions": ["peer-agent"],
    }
    events = [("message", payload), ("message_updated", payload)]
    _, _, delivered = _run_sse_loop_with_events(events, monkeypatch=monkeypatch)
    assert [e.message_id for e in delivered] == ["msg-dup"]


def test_channel_materializes_shared_task_metadata_for_agent_prompt(monkeypatch):
    class FakeSseClient(FakeClient):
        def connect_sse(self, *, space_id):
            assert space_id == "space-123"
            return FakeSseResponse(
                {
                    "id": "incoming-share",
                    "content": "@peer-agent can you see what I shared?",
                    "author": {"id": "user-123", "name": "alex", "type": "user"},
                    "mentions": ["peer-agent"],
                    "metadata": {
                        "forward": {
                            "intent": "share",
                            "resource_type": "task",
                            "resource_id": "task-123",
                            "task_id": "task-123",
                            "source_message_id": "source-msg-123",
                            "source_card_id": "task-signal:task-123",
                            "title": "Fix Share delivery context",
                            "summary": "The recipient should know this is a task.",
                        }
                    },
                }
            )

        def get_message(self, message_id):
            raise AssertionError("SSE metadata was already complete")

    client = FakeSseClient()
    bridge = CaptureBridge(client)
    delivered: list[MentionEvent] = []

    def capture_delivery(event):
        delivered.append(event)
        bridge.shutdown.set()

    bridge.enqueue_from_thread = capture_delivery
    monkeypatch.setattr(channel_mod.time, "monotonic", lambda: 0)

    channel_mod._sse_loop(bridge)

    assert [event.message_id for event in delivered] == ["incoming-share"]
    assert "can you see what I shared?" in delivered[0].prompt
    assert "Shared object:" in delivered[0].prompt
    assert "- resource_type: task" in delivered[0].prompt
    assert "- task_id: task-123" in delivered[0].prompt
    assert "axctl tasks get task-123 --space-id space-123 --json" in delivered[0].prompt
    assert delivered[0].metadata["forward"]["resource_type"] == "task"


def test_channel_fetches_attachment_metadata_and_adds_inspection_hint(monkeypatch):
    class FakeSseClient(FakeClient):
        def connect_sse(self, *, space_id):
            assert space_id == "space-123"
            return FakeSseResponse(
                {
                    "id": "incoming-image",
                    "content": "@peer-agent please inspect this image",
                    "author": {"id": "user-123", "name": "alex", "type": "user"},
                    "mentions": ["peer-agent"],
                    "metadata": {},
                }
            )

        def get_message(self, message_id):
            assert message_id == "incoming-image"
            attachment = {
                "id": "att-123",
                "filename": "image.png",
                "content_type": "image/png",
                "context_key": "upload:image.png:att-123",
            }
            return {"message": {"metadata": {"accepted_attachments": [attachment]}}}

    client = FakeSseClient()
    bridge = CaptureBridge(client)
    delivered: list[MentionEvent] = []

    def capture_delivery(event):
        delivered.append(event)
        bridge.shutdown.set()

    bridge.enqueue_from_thread = capture_delivery
    monkeypatch.setattr(channel_mod.time, "monotonic", lambda: 0)

    channel_mod._sse_loop(bridge)

    assert [event.message_id for event in delivered] == ["incoming-image"]
    assert "Attachments:" in delivered[0].prompt
    assert "image.png (image/png, id=att-123, context_key=upload:image.png:att-123)" in delivered[0].prompt
    assert "axctl context get 'upload:image.png:att-123' --space-id space-123 --json" in delivered[0].prompt
    assert delivered[0].attachments == [
        {
            "id": "att-123",
            "filename": "image.png",
            "content_type": "image/png",
            "context_key": "upload:image.png:att-123",
        }
    ]


def test_channel_processing_status_can_be_disabled():
    client = FakeClient("axp_a_AgentKey.Secret")
    bridge = CaptureBridge(client, processing_status=False)

    asyncio.run(bridge.publish_processing_status("incoming-123", "working"))

    assert client.processing_statuses == []


def test_channel_returns_empty_optional_mcp_lists():
    client = FakeClient("axp_a_AgentKey.Secret")
    bridge = CaptureBridge(client)

    asyncio.run(bridge.handle_request({"id": 1, "method": "resources/list"}))
    asyncio.run(bridge.handle_request({"id": 2, "method": "resources/templates/list"}))
    asyncio.run(bridge.handle_request({"id": 3, "method": "prompts/list"}))

    assert bridge.writes == [
        {"jsonrpc": "2.0", "id": 1, "result": {"resources": []}},
        {"jsonrpc": "2.0", "id": 2, "result": {"resourceTemplates": []}},
        {"jsonrpc": "2.0", "id": 3, "result": {"prompts": []}},
    ]


def test_channel_tools_include_polling_fallback():
    client = FakeClient("axp_a_AgentKey.Secret")
    bridge = CaptureBridge(client)

    asyncio.run(bridge.handle_tools_list(1))

    tools = bridge.writes[0]["result"]["tools"]
    assert {tool["name"] for tool in tools} == {"reply", "get_messages"}


def test_channel_get_messages_returns_pending_mentions():
    client = FakeClient("axp_a_AgentKey.Secret")
    bridge = CaptureBridge(client)
    bridge._pending_mentions.append(
        MentionEvent(
            message_id="incoming-123",
            parent_id=None,
            conversation_id=None,
            author="alex",
            prompt="please check this",
            raw_content="@peer-agent please check this",
            created_at="2026-04-15T23:00:00Z",
            space_id="space-123",
            attachments=[{"id": "att-1", "filename": "notes.md"}],
            metadata={"forward": {"resource_type": "context"}},
        )
    )

    asyncio.run(bridge.handle_tool_call(1, {"name": "get_messages", "arguments": {"limit": 1}}))

    result = bridge.writes[0]["result"]
    assert "incoming-123" in result["content"][0]["text"]
    assert "please check this" in result["content"][0]["text"]
    assert "notes.md" in result["content"][0]["text"]
    assert "resource_type" in result["content"][0]["text"]
    assert bridge._pending_mentions == []


def test_channel_notification_metadata_matches_claude_channel_contract():
    async def run():
        client = FakeClient("axp_a_AgentKey.Secret")
        bridge = CaptureBridge(client)
        bridge.initialized.set()
        await bridge.mention_queue.put(
            MentionEvent(
                message_id="incoming-123",
                parent_id=None,
                conversation_id="conversation-ignored",
                author="alex",
                prompt="please check this",
                raw_content="@peer-agent please check this",
                created_at=None,
                space_id="space-123",
                metadata={"forward": {"resource_type": "task", "task_id": "task-123"}},
            )
        )
        task = asyncio.create_task(bridge.emit_mentions())
        await asyncio.wait_for(bridge.mention_queue.join(), timeout=1)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        return bridge

    bridge = asyncio.run(run())

    payload = bridge.writes[0]
    assert payload["method"] == "notifications/claude/channel"
    meta = payload["params"]["meta"]
    assert meta["message_id"] == "incoming-123"
    assert isinstance(meta["ts"], str)
    assert meta["ts"]
    assert "raw_content" not in meta
    assert "conversation_id" not in meta
    assert "parent_id" not in meta
    assert meta["forward"] == {"resource_type": "task", "task_id": "task-123"}


def test_channel_notification_strips_unsafe_attachment_fields():
    """Attachment blobs (url with base64 data) must not leak into the MCP notification."""

    async def run():
        client = FakeClient("axp_a_AgentKey.Secret")
        bridge = CaptureBridge(client)
        bridge.initialized.set()
        await bridge.mention_queue.put(
            MentionEvent(
                message_id="incoming-img",
                parent_id=None,
                conversation_id=None,
                author="alex",
                prompt="check this image",
                raw_content="@peer-agent check this image",
                created_at=None,
                space_id="space-123",
                attachments=[
                    {
                        "id": "att-1",
                        "filename": "photo.jpg",
                        "content_type": "image/jpeg",
                        "size_bytes": 4_000_000,
                        "context_key": "upload:photo.jpg:att-1",
                        "url": "data:image/jpeg;base64," + "A" * 100_000,
                        "extra_field": "should-be-stripped",
                    }
                ],
            )
        )
        task = asyncio.create_task(bridge.emit_mentions())
        await asyncio.wait_for(bridge.mention_queue.join(), timeout=1)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        return bridge

    bridge = asyncio.run(run())

    meta = bridge.writes[0]["params"]["meta"]
    att = meta["attachments"][0]
    assert att["id"] == "att-1"
    assert att["filename"] == "photo.jpg"
    assert att["content_type"] == "image/jpeg"
    assert att["size_bytes"] == 4_000_000
    assert att["context_key"] == "upload:photo.jpg:att-1"
    assert "url" not in att
    assert "extra_field" not in att


def test_channel_emit_mentions_survives_write_failure():
    """A failed delivery must not kill the emit_mentions loop — next event still lands."""

    async def run():
        client = FakeClient("axp_a_AgentKey.Secret")
        bridge = CaptureBridge(client)
        bridge.initialized.set()

        call_count = 0
        original_write = bridge.write_message

        async def failing_then_ok(payload):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise RuntimeError("simulated stdout failure")
            await original_write(payload)

        bridge.write_message = failing_then_ok

        bad_event = MentionEvent(
            message_id="will-fail",
            parent_id=None,
            conversation_id=None,
            author="alex",
            prompt="this will fail",
            raw_content="@peer-agent this will fail",
            created_at=None,
            space_id="space-123",
        )
        good_event = MentionEvent(
            message_id="will-succeed",
            parent_id=None,
            conversation_id=None,
            author="alex",
            prompt="this should land",
            raw_content="@peer-agent this should land",
            created_at=None,
            space_id="space-123",
        )
        await bridge.mention_queue.put(bad_event)
        await bridge.mention_queue.put(good_event)

        task = asyncio.create_task(bridge.emit_mentions())
        await asyncio.wait_for(bridge.mention_queue.join(), timeout=2)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        return bridge

    bridge = asyncio.run(run())

    assert len(bridge.writes) == 1
    assert bridge.writes[0]["params"]["content"] == "this should land"


def test_channel_delivers_completion_update_after_progress_skip(monkeypatch):
    """Progress message skipped → subsequent message_updated with real content must land.

    Regression test for #74: previously the message_updated dedup ran before the
    progress filter, so if a progress message was delivered (e.g., regex miss),
    the completion update was dropped. With the fix, progress filtering runs
    first — the progress message never enters seen_ids, so the completion
    update for the same message id passes through.
    """

    progress = {
        "id": "sentinel-reply-1",
        "content": "@peer-agent Working…",
        "author": {"id": "agent-2", "name": "frontend_sentinel", "type": "agent"},
        "mentions": ["peer-agent"],
    }
    completion = {
        "id": "sentinel-reply-1",
        "content": "@peer-agent Here is the analysis you requested with full details.",
        "author": {"id": "agent-2", "name": "frontend_sentinel", "type": "agent"},
        "mentions": ["peer-agent"],
    }
    events = [("message", progress), ("message_updated", completion)]
    _, _, delivered = _run_sse_loop_with_events(events, monkeypatch=monkeypatch)
    assert [e.message_id for e in delivered] == ["sentinel-reply-1"]
    assert "analysis you requested" in delivered[0].prompt


def test_default_local_channel_command_resolves_relative_argv0(monkeypatch, tmp_path):
    """sys.argv[0] from a relative launcher (.venv/bin/ax) must resolve to
    an absolute path before being written into .mcp.json. Claude Code
    launches the channel bridge from the agent workdir, which has no .venv/,
    so a relative command crashes the MCP server immediately."""
    venv_bin = tmp_path / ".venv" / "bin"
    venv_bin.mkdir(parents=True)
    (venv_bin / "ax").write_text('#!/bin/sh\nexec axctl "$@"\n')
    (venv_bin / "axctl").write_text("#!/bin/sh\necho ok\n")
    (venv_bin / "ax").chmod(0o755)
    (venv_bin / "axctl").chmod(0o755)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(channel_mod.sys, "argv", [".venv/bin/ax", "channel", "setup"])

    resolved = channel_mod._default_local_channel_command()
    assert Path(resolved).is_absolute(), f"expected absolute path, got {resolved!r}"
    assert resolved.endswith("/.venv/bin/axctl")
    assert Path(resolved).exists()


def test_default_local_channel_command_falls_back_to_path_lookup(monkeypatch, tmp_path):
    """When sys.argv[0] is a process name (no sibling axctl resolves), use
    shutil.which to find axctl on PATH and return its absolute path."""
    monkeypatch.setattr(channel_mod.sys, "argv", ["python"])
    fake_axctl = tmp_path / "fake-bin" / "axctl"
    fake_axctl.parent.mkdir(parents=True)
    fake_axctl.write_text("#!/bin/sh\n")
    fake_axctl.chmod(0o755)
    monkeypatch.setenv("PATH", str(fake_axctl.parent))

    resolved = channel_mod._default_local_channel_command()
    assert resolved == str(fake_axctl.resolve())


def test_default_local_channel_command_falls_back_to_bare_when_nothing_resolves(monkeypatch, tmp_path):
    monkeypatch.setattr(channel_mod.sys, "argv", ["python"])
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    (tmp_path / "empty").mkdir()

    resolved = channel_mod._default_local_channel_command()
    assert resolved == "axctl"


def test_channel_setup_writes_persona_into_claude_md(monkeypatch, tmp_path):
    """ax channel setup must surface the operator's system_prompt in CLAUDE.md
    (the file Claude Code reads natively), not just .ax/AGENT_CONTEXT.md."""
    monkeypatch.setenv("AX_CONFIG_DIR", str(tmp_path / "config"))
    token_file = tmp_path / "gateway" / "orion.token"
    token_file.parent.mkdir(parents=True)
    token_file.write_text("axp_a_agent.secret\n")

    # Seed the registry so the persona lookup finds the operator prompt.
    gateway_core.save_gateway_registry(
        {
            "agents": [
                {
                    "name": "orion",
                    "agent_id": "agent-orion",
                    "space_id": "space-123",
                    "template_id": "claude_code_channel",
                    "runtime_type": "claude_code_channel",
                    "token_file": str(token_file),
                    "base_url": "https://paxai.app",
                    "system_prompt": "You are the orion role; coordinate the demo.",
                }
            ]
        }
    )

    workdir = tmp_path / "work-orion"
    env_path = tmp_path / "orion.env"
    result = runner.invoke(
        channel_mod.app,
        [
            "setup",
            "orion",
            "--workdir",
            str(workdir),
            "--env-path",
            str(env_path),
            "--json",
        ],
    )
    assert result.exit_code == 0, result.output

    claude_md = (workdir / "CLAUDE.md").read_text()
    assert "BEGIN ax-gateway-agent-context" in claude_md
    assert "END ax-gateway-agent-context" in claude_md
    # Operator prompt lands directly in CLAUDE.md so the Claude Code session
    # reads it on startup without an indirect AGENT_CONTEXT.md follow.
    assert "You are the orion role; coordinate the demo." in claude_md
    # Collaboration guidance also surfaces.
    assert "ax send" in claude_md
    assert "ax messages list" in claude_md


def test_marker_section_preserves_user_content_in_claude_md(tmp_path):
    """If a workdir already has CLAUDE.md, the marker writer prepends the
    auto-generated section without clobbering user content."""
    from ax_cli.commands.gateway_agents import _render_agent_persona_markdown, _write_marker_section

    workdir = tmp_path / "work"
    workdir.mkdir()
    user_content = "# User project notes\n\nDo not delete these.\n"
    (workdir / "CLAUDE.md").write_text(user_content)

    entry = {
        "name": "demo",
        "template_id": "claude_code_channel",
        "runtime_type": "claude_code_channel",
        "system_prompt": "Operator role text.",
    }
    _write_marker_section(workdir / "CLAUDE.md", body=_render_agent_persona_markdown(entry, workdir=str(workdir)))
    final = (workdir / "CLAUDE.md").read_text()
    # Marker section appears.
    assert "BEGIN ax-gateway-agent-context" in final
    assert "Operator role text." in final
    # User content preserved.
    assert "# User project notes" in final
    assert "Do not delete these." in final


def test_marker_section_replaces_in_place_on_rerun(tmp_path):
    """Re-running setup with an updated system_prompt must replace just the
    section between markers, not append a duplicate."""
    from ax_cli.commands.gateway_agents import _render_agent_persona_markdown, _write_marker_section

    workdir = tmp_path / "work"
    workdir.mkdir()
    target = workdir / "CLAUDE.md"

    entry_v1 = {
        "name": "demo",
        "template_id": "claude_code_channel",
        "runtime_type": "claude_code_channel",
        "system_prompt": "First role.",
    }
    _write_marker_section(target, body=_render_agent_persona_markdown(entry_v1, workdir=str(workdir)))

    entry_v2 = dict(entry_v1, system_prompt="Updated role.")
    _write_marker_section(target, body=_render_agent_persona_markdown(entry_v2, workdir=str(workdir)))

    final = target.read_text()
    assert final.count("BEGIN ax-gateway-agent-context") == 1
    assert "Updated role." in final
    assert "First role." not in final


def test_render_agent_persona_includes_connector_block_when_connector_ref_set(tmp_path):
    """Regression: AGENTS.md must include connector instructions when connector_ref
    is set, so sentinel agents find their connector after a restart (which regenerates
    AGENTS.md from the registry entry, discarding the --system-prompt arg)."""
    from ax_cli.commands.gateway_agents import _render_agent_persona_markdown

    entry = {
        "name": "slack-output",
        "runtime_type": "sentinel_inference_sdk",
        "system_prompt": "Send Slack messages.",
        "connector_ref": "composio-main",
    }
    output = _render_agent_persona_markdown(entry, workdir=str(tmp_path))

    assert "CONNECTORS: composio-main" in output
    assert "connector='composio-main'" in output
    assert "connector_call" in output
    assert "connect composio-main --app" in output


def test_render_agent_persona_omits_connector_block_when_no_connector_ref(tmp_path):
    """Agents without a connector_ref must not get connector instructions."""
    from ax_cli.commands.gateway_agents import _render_agent_persona_markdown

    entry = {
        "name": "echo",
        "runtime_type": "sentinel_inference_sdk",
        "system_prompt": "Echo messages.",
    }
    output = _render_agent_persona_markdown(entry, workdir=str(tmp_path))

    assert "CONNECTORS:" not in output
    assert "connector_call" not in output


def test_system_prompt_and_agents_md_render_same_connector_block(tmp_path):
    """Both renderers must emit identical connector guidance for the same agent."""
    from ax_cli.commands.gateway_agents import _render_agent_persona_markdown
    from ax_cli.connectors.guidance import render_connector_block
    from ax_cli.gateway_hermes import _gateway_environment_context

    entry = {
        "name": "slack-output",
        "runtime_type": "sentinel_inference_sdk",
        "system_prompt": "Send Slack messages.",
        "connector_ref": "composio-main",
        "space_id": "space-1",
        "base_url": "https://paxai.app",
    }
    expected = render_connector_block("composio-main")

    agents_md = _render_agent_persona_markdown(entry, workdir=str(tmp_path))
    system_prompt_env = _gateway_environment_context(entry)

    assert expected in agents_md
    assert expected in system_prompt_env
    assert "connect demo --app" not in agents_md
    assert "connect demo --app" not in system_prompt_env


def test_channel_received_signal_fires_on_enqueue():
    """_signal_received must emit 'received' processing status immediately."""

    async def run():
        client = FakeClient("axp_a_AgentKey.Secret")
        bridge = CaptureBridge(client)
        bridge.initialized.set()
        bridge.loop = asyncio.get_running_loop()

        await bridge._signal_received("incoming-456", 1)

        assert len(client.processing_statuses) == 1
        assert client.processing_statuses[0]["status"] == "received"
        assert client.processing_statuses[0]["message_id"] == "incoming-456"

    asyncio.run(run())


def test_channel_received_respects_no_processing_status():
    """--no-processing-status must suppress the received signal."""

    async def run():
        client = FakeClient("axp_a_AgentKey.Secret")
        bridge = CaptureBridge(client, processing_status=False)
        bridge.loop = asyncio.get_running_loop()

        await bridge._signal_received("incoming-456", 1)
        assert client.processing_statuses == []

    asyncio.run(run())


def test_channel_signal_sequence_received_working_completed():
    """Full lifecycle: received fires on enqueue, working on delivery, completed on reply."""

    async def run():
        client = FakeClient("axp_a_AgentKey.Secret")
        bridge = CaptureBridge(client)
        bridge.initialized.set()
        bridge.loop = asyncio.get_running_loop()

        event = MentionEvent(
            message_id="incoming-seq",
            parent_id=None,
            conversation_id=None,
            author="alex",
            prompt="check this",
            raw_content="@peer-agent check this",
            created_at=None,
            space_id="space-123",
        )

        # Step 1: received on enqueue
        await bridge._signal_received(event.message_id, 1)

        # Step 2: working via emit_mentions
        await bridge.mention_queue.put(event)
        task = asyncio.create_task(bridge.emit_mentions())
        await asyncio.wait_for(bridge.mention_queue.join(), timeout=1)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

        # Step 3: completed via reply
        await bridge.handle_tool_call(
            1,
            {"name": "reply", "arguments": {"text": "done"}},
        )

        statuses = [s["status"] for s in client.processing_statuses]
        assert statuses == ["received", "working", "completed"]

    asyncio.run(run())


def test_channel_queue_depth_in_gateway_touch(monkeypatch):
    """touch_gateway calls must include actual backlog_depth from the mention queue."""
    gateway_touches: list[dict] = []

    def capture_touch(agent_name, *, event=None, **updates):
        gateway_touches.append({"event": event, **updates})

    monkeypatch.setattr(channel_mod, "_touch_gateway_channel_entry", capture_touch)

    async def run():
        client = FakeClient("axp_a_AgentKey.Secret")
        bridge = CaptureBridge(client)
        bridge.initialized.set()
        bridge.loop = asyncio.get_running_loop()

        event = MentionEvent(
            message_id="incoming-depth",
            parent_id=None,
            conversation_id=None,
            author="alex",
            prompt="check depth",
            raw_content="@peer-agent check depth",
            created_at=None,
            space_id="space-123",
        )

        # received signal includes queue depth
        await bridge._signal_received(event.message_id, 3)

        # emit_mentions includes queue depth
        await bridge.mention_queue.put(event)
        task = asyncio.create_task(bridge.emit_mentions())
        await asyncio.wait_for(bridge.mention_queue.join(), timeout=1)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

        return bridge

    asyncio.run(run())

    received_touch = gateway_touches[0]
    assert received_touch["event"] == "channel_message_received"
    assert received_touch["backlog_depth"] == 3

    delivered_touch = gateway_touches[1]
    assert delivered_touch["event"] == "channel_message_delivered"
    assert "backlog_depth" in delivered_touch


def test_channel_env_file_sets_missing_runtime_env(monkeypatch, tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text(
        "AX_CONFIG_FILE=/tmp/agent/.ax/config.toml\nAX_SPACE_ID=space-123\nAX_AGENT_NAME=ignored-agent\n"
    )
    monkeypatch.setenv("AX_AGENT_NAME", "existing-agent")

    _load_channel_env(env_file)

    assert os.environ["AX_CONFIG_FILE"] == "/tmp/agent/.ax/config.toml"
    assert os.environ["AX_SPACE_ID"] == "space-123"
    assert os.environ["AX_AGENT_NAME"] == "existing-agent"


def test_listener_treats_parent_reply_as_delivery_signal():
    anchors = {"agent-message-1"}
    data = {
        "id": "reply-1",
        "content": "I looked at this",
        "parent_id": "agent-message-1",
        "author": {"id": "user-1", "name": "Jacob", "type": "user"},
        "mentions": [],
    }

    assert _should_respond(data, "peer-agent", "agent-123", reply_anchor_ids=anchors) is True


def test_listener_treats_conversation_reply_as_delivery_signal():
    anchors = {"agent-message-1"}
    data = {
        "id": "reply-1",
        "content": "I looked at this",
        "conversation_id": "agent-message-1",
        "author": {"id": "user-1", "name": "Jacob", "type": "user"},
        "mentions": [],
    }

    assert _should_respond(data, "peer-agent", "agent-123", reply_anchor_ids=anchors) is True


def test_listener_does_not_auto_reply_to_other_agent_thread_reply_without_mention():
    anchors = {"agent-message-1"}
    data = {
        "id": "reply-1",
        "content": "I looked at this",
        "parent_id": "agent-message-1",
        "author": {"id": "other-agent", "name": "demo-agent", "type": "agent"},
        "mentions": [],
    }

    assert _should_respond(data, "peer-agent", "agent-123", reply_anchor_ids=anchors) is False


def test_listener_still_replies_to_other_agent_thread_reply_when_explicitly_mentioned():
    anchors = {"agent-message-1"}
    data = {
        "id": "reply-1",
        "content": "@peer-agent I looked at this",
        "parent_id": "agent-message-1",
        "author": {"id": "other-agent", "name": "demo-agent", "type": "agent"},
        "mentions": ["peer-agent"],
    }

    assert _should_respond(data, "peer-agent", "agent-123", reply_anchor_ids=anchors) is True


def test_listener_ignores_thread_parent_mentions_from_other_agents():
    anchors = {"agent-message-1"}
    data = {
        "id": "reply-1",
        "content": "continuing the thread",
        "parent_id": "agent-message-1",
        "sender_type": "agent",
        "mentions": [{"agent_name": "peer-agent", "source": "thread_parent"}],
    }

    assert _should_respond(data, "peer-agent", "agent-123", reply_anchor_ids=anchors) is False


def test_listener_tracks_self_authored_messages_without_responding():
    anchors: set[str] = set()
    data = {
        "id": "agent-message-1",
        "content": "@demo-agent please check this",
        "author": {"id": "agent-123", "name": "peer-agent", "type": "agent"},
        "mentions": ["demo-agent"],
    }

    assert _is_self_authored(data, "peer-agent", "agent-123") is True
    _remember_reply_anchor(anchors, data["id"])
    assert _should_respond(data, "peer-agent", "agent-123", reply_anchor_ids=anchors) is False
    assert anchors == {"agent-message-1"}


def test_channel_setup_writes_per_agent_mcp_and_env(tmp_path):
    token_file = tmp_path / "token"
    token_file.write_text("axp_a_agent.secret\n")
    workdir = tmp_path / "work"
    env_path = tmp_path / "channel.env"

    result = runner.invoke(
        channel_mod.app,
        [
            "setup",
            "orion",
            "--workdir",
            str(workdir),
            "--space-id",
            "space-123",
            "--token-file",
            str(token_file),
            "--base-url",
            "https://paxai.app",
            "--env-path",
            str(env_path),
            "--json",
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["mcp_path"] == str(workdir / ".mcp.json")
    assert payload["cli_config_path"] == str(workdir / ".ax" / "config.toml")
    assert payload["cli_readme_path"] == str(workdir / ".ax" / "README.md")
    assert payload["agent_context_path"] == str(workdir / ".ax" / "AGENT_CONTEXT.md")
    mcp = json.loads((workdir / ".mcp.json").read_text())
    server = mcp["mcpServers"]["ax-channel"]
    # Resolves to an absolute path so Claude Code can launch the bridge
    # from inside the agent workdir (which has no .venv/). Either an
    # absolute filesystem path or, on a stripped PATH, falls back to bare
    # "axctl" (no sibling found, no PATH match).
    cmd = server["command"]
    assert cmd == "axctl" or Path(cmd).is_absolute(), cmd
    assert server["args"] == ["channel"]
    assert server["env"]["AX_CHANNEL_ENV_FILE"] == str(env_path)
    env_text = env_path.read_text()
    assert 'AX_TOKEN_FILE="' in env_text
    assert 'AX_BASE_URL="https://paxai.app"' in env_text
    assert 'AX_AGENT_NAME="orion"' in env_text
    assert 'AX_SPACE_ID="space-123"' in env_text
    cli_config = (workdir / ".ax" / "config.toml").read_text()
    assert 'url = "http://127.0.0.1:8765"' in cli_config
    assert 'agent_name = "orion"' in cli_config
    assert f'workdir = "{workdir.resolve()}"' in cli_config
    cli_readme = (workdir / ".ax" / "README.md").read_text()
    assert "aX Claude Code Channel" in cli_readme
    assert "ax gateway local connect --workdir ." in cli_readme
    agent_context = (workdir / ".ax" / "AGENT_CONTEXT.md").read_text()
    assert "multi-user, multi-agent network" in agent_context
    assert "Do not ask the user for a PAT" in agent_context
    assert (workdir / "AGENTS.md").exists()
    assert (workdir / "CLAUDE.md").exists()


def test_channel_setup_uses_gateway_registry_defaults(monkeypatch, tmp_path):
    monkeypatch.setenv("AX_CONFIG_DIR", str(tmp_path / "config"))
    token_file = tmp_path / "gateway" / "orion.token"
    token_file.parent.mkdir(parents=True)
    token_file.write_text("axp_a_agent.secret\n")
    gateway_core.save_gateway_registry(
        {
            "agents": [
                {
                    "name": "orion",
                    "agent_id": "agent-orion",
                    "space_id": "space-123",
                    "base_url": "https://paxai.app",
                    "token_file": str(token_file),
                }
            ]
        }
    )
    workdir = tmp_path / "work"
    env_path = tmp_path / "orion.env"

    result = runner.invoke(
        channel_mod.app,
        [
            "setup",
            "orion",
            "--workdir",
            str(workdir),
            "--env-path",
            str(env_path),
            "--json",
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["agent"] == "orion"
    assert payload["space_id"] == "space-123"
    assert payload["base_url"] == "https://paxai.app"
    mcp = json.loads((workdir / ".mcp.json").read_text())
    server = mcp["mcpServers"]["ax-channel"]
    assert server["args"] == ["channel"]
    env_text = env_path.read_text()
    assert f'AX_TOKEN_FILE="{token_file}"' in env_text
    assert 'AX_AGENT_ID="agent-orion"' in env_text
    cli_config = (workdir / ".ax" / "config.toml").read_text()
    assert 'agent_name = "orion"' in cli_config


def test_channel_setup_can_generate_docker_mcp_command(tmp_path):
    token_file = tmp_path / "token"
    token_file.write_text("axp_a_agent.secret\n")
    env_path = tmp_path / "orion.env"

    result = runner.invoke(
        channel_mod.app,
        [
            "setup",
            "orion",
            "--workdir",
            str(tmp_path),
            "--space-id",
            "space-123",
            "--token-file",
            str(token_file),
            "--mode",
            "docker",
            "--container-image",
            "ax-channel:demo",
            "--env-path",
            str(env_path),
            "--json",
        ],
    )

    assert result.exit_code == 0, result.output
    mcp = json.loads((tmp_path / ".mcp.json").read_text())
    server = mcp["mcpServers"]["ax-channel"]
    assert server["command"] == "docker"
    assert "ax-channel:demo" in server["args"]
    assert "-i" in server["args"]
    assert "axctl" in server["args"]
    assert env_path.exists()


# ---- Helper function unit tests ----


def test_env_line_escapes_special_chars():
    """_env_line should escape backslashes and double quotes."""
    line = channel_mod._env_line("KEY", 'value with "quotes" and \\backslash')
    assert 'KEY="value with \\"quotes\\" and \\\\backslash"' == line


def test_env_line_strips_newlines():
    """_env_line replaces newlines with spaces."""
    line = channel_mod._env_line("KEY", "line1\nline2\nline3")
    assert "\n" not in line
    assert "line1 line2 line3" in line


def test_env_line_empty_value():
    line = channel_mod._env_line("KEY", "")
    assert line == 'KEY=""'


def test_env_line_none_value():
    line = channel_mod._env_line("KEY", None)
    assert line == 'KEY=""'


def test_write_channel_env_creates_file_with_permissions(tmp_path):
    """_write_channel_env should create the file with 0o600 permissions."""
    env_path = tmp_path / "sub" / "channel.env"
    channel_mod._write_channel_env(env_path, {"AX_TOKEN": "test_tok", "AX_BASE_URL": "https://paxai.app"})
    assert env_path.exists()
    assert env_path.stat().st_mode & 0o777 == 0o600
    text = env_path.read_text()
    assert "AX_TOKEN=" in text
    assert "AX_BASE_URL=" in text
    assert "Generated by ax channel setup" in text


def test_write_channel_env_skips_empty_values(tmp_path):
    env_path = tmp_path / "channel.env"
    channel_mod._write_channel_env(env_path, {"AX_TOKEN": "tok", "AX_EMPTY": ""})
    text = env_path.read_text()
    assert "AX_TOKEN=" in text
    assert "AX_EMPTY" not in text


def test_load_channel_env_nonexistent_file(tmp_path):
    """Loading a non-existent env file should be a no-op."""
    channel_mod._load_channel_env(tmp_path / "nonexistent.env")


def test_load_channel_env_skips_comments_and_blank_lines(monkeypatch, tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text("# this is a comment\n\nAX_SPACE_ID=space-test\nNO_EQUALS_LINE\n")
    channel_mod._load_channel_env(env_file)
    assert os.environ.get("AX_SPACE_ID") == "space-test"


def test_load_channel_env_strips_quotes(monkeypatch, tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text('AX_TEST_VAR="quoted_value"\n')
    channel_mod._load_channel_env(env_file)
    assert os.environ.get("AX_TEST_VAR") == "quoted_value"


def test_load_mcp_config_returns_empty_for_missing(tmp_path):
    result = channel_mod._load_mcp_config(tmp_path / "nonexistent.json")
    assert result == {}


def test_load_mcp_config_raises_on_invalid_json(tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text("not json at all")
    with pytest.raises(typer.BadParameter, match="not valid JSON"):
        channel_mod._load_mcp_config(bad)


def test_load_mcp_config_raises_on_non_object(tmp_path):
    arr = tmp_path / "array.json"
    arr.write_text("[1, 2, 3]")
    with pytest.raises(typer.BadParameter, match="must contain a JSON object"):
        channel_mod._load_mcp_config(arr)


def test_write_mcp_server_config_creates_new_file(tmp_path):
    mcp_path = tmp_path / "sub" / ".mcp.json"
    channel_mod._write_mcp_server_config(mcp_path, "test-server", {"command": "axctl", "args": ["channel"]})
    config = json.loads(mcp_path.read_text())
    assert config["mcpServers"]["test-server"]["command"] == "axctl"


def test_write_mcp_server_config_appends_to_existing(tmp_path):
    mcp_path = tmp_path / ".mcp.json"
    mcp_path.write_text(json.dumps({"mcpServers": {"existing": {"command": "other"}}}))
    channel_mod._write_mcp_server_config(mcp_path, "new-server", {"command": "axctl", "args": ["channel"]})
    config = json.loads(mcp_path.read_text())
    assert "existing" in config["mcpServers"]
    assert "new-server" in config["mcpServers"]


def test_write_mcp_server_config_raises_on_non_dict_servers(tmp_path):
    mcp_path = tmp_path / ".mcp.json"
    mcp_path.write_text(json.dumps({"mcpServers": "not a dict"}))
    with pytest.raises(typer.BadParameter, match="non-object mcpServers"):
        channel_mod._write_mcp_server_config(mcp_path, "test", {"command": "axctl"})


def test_default_channel_env_path_uses_override(monkeypatch, tmp_path):
    monkeypatch.setenv("AX_CHANNEL_ENV_FILE", str(tmp_path / "custom.env"))
    result = channel_mod._default_channel_env_path()
    assert result == tmp_path / "custom.env"


def test_default_channel_env_path_fallback(monkeypatch):
    monkeypatch.delenv("AX_CHANNEL_ENV_FILE", raising=False)
    result = channel_mod._default_channel_env_path()
    assert result == channel_mod.CHANNEL_ENV_PATH


# ---- ChannelBridge method tests ----


def test_channel_send_error_method():
    client = FakeClient("axp_a_AgentKey.Secret")
    bridge = CaptureBridge(client)

    asyncio.run(bridge.send_error(42, -32601, "Method not found"))

    assert bridge.writes == [{"jsonrpc": "2.0", "id": 42, "error": {"code": -32601, "message": "Method not found"}}]


def test_channel_handle_unknown_tool():
    client = FakeClient("axp_a_AgentKey.Secret")
    bridge = CaptureBridge(client)

    asyncio.run(bridge.handle_tool_call(1, {"name": "unknown_tool", "arguments": {}}))

    assert bridge.writes[0]["error"]["code"] == -32601
    assert "Unknown tool" in bridge.writes[0]["error"]["message"]


def test_channel_reply_requires_text():
    client = FakeClient("axp_a_AgentKey.Secret")
    bridge = CaptureBridge(client)
    bridge._last_message_id = "incoming-123"

    asyncio.run(bridge.handle_tool_call(1, {"name": "reply", "arguments": {"text": ""}}))

    assert bridge.writes[0]["error"]["code"] == -32602
    assert "reply.text is required" in bridge.writes[0]["error"]["message"]


def test_channel_reply_requires_message_id():
    client = FakeClient("axp_a_AgentKey.Secret")
    bridge = CaptureBridge(client)
    # _last_message_id is None

    asyncio.run(bridge.handle_tool_call(1, {"name": "reply", "arguments": {"text": "hello"}}))

    assert bridge.writes[0]["error"]["code"] == -32602
    assert "reply_to is required" in bridge.writes[0]["error"]["message"]


def test_channel_reply_rejects_no_agent_id():
    client = FakeClient("axp_a_AgentKey.Secret")
    bridge = CaptureBridge(client, agent_id=None)
    bridge._last_message_id = "incoming-123"

    asyncio.run(bridge.handle_tool_call(1, {"name": "reply", "arguments": {"text": "hello"}}))

    result = bridge.writes[0]["result"]
    assert result["isError"] is True
    assert "agent_id is required" in result["content"][0]["text"]


def test_channel_reply_uses_explicit_reply_to():
    client = FakeClient("axp_a_AgentKey.Secret")
    bridge = CaptureBridge(client)
    bridge._last_message_id = "default-msg"

    asyncio.run(
        bridge.handle_tool_call(
            1,
            {"name": "reply", "arguments": {"text": "hello", "reply_to": "explicit-msg"}},
        )
    )

    assert client.sent[0]["parent_id"] == "explicit-msg"


def test_channel_handle_initialize():
    client = FakeClient("axp_a_AgentKey.Secret")
    bridge = CaptureBridge(client)

    asyncio.run(bridge.handle_initialize(1))

    result = bridge.writes[0]["result"]
    assert result["protocolVersion"] == channel_mod.PROTOCOL_VERSION
    assert result["serverInfo"]["name"] == channel_mod.SERVER_NAME
    assert "tools" in result["capabilities"]


def test_channel_handle_ping(monkeypatch):
    monkeypatch.setattr(channel_mod, "_touch_gateway_channel_entry", lambda *a, **kw: None)
    client = FakeClient("axp_a_AgentKey.Secret")
    bridge = CaptureBridge(client)

    asyncio.run(bridge.handle_request({"id": 1, "method": "ping"}))

    assert bridge.writes[0] == {"jsonrpc": "2.0", "id": 1, "result": {}}


def test_channel_handle_unknown_method():
    client = FakeClient("axp_a_AgentKey.Secret")
    bridge = CaptureBridge(client)

    asyncio.run(bridge.handle_request({"id": 1, "method": "unknown/method"}))

    assert bridge.writes[0]["error"]["code"] == -32601


def test_channel_handle_notification_initialized():
    client = FakeClient("axp_a_AgentKey.Secret")
    bridge = CaptureBridge(client)
    assert not bridge.initialized.is_set()

    asyncio.run(bridge.handle_notification({"method": "notifications/initialized"}))

    assert bridge.initialized.is_set()


def test_channel_handle_notification_cancelled():
    client = FakeClient("axp_a_AgentKey.Secret")
    bridge = CaptureBridge(client)

    # Should not raise
    asyncio.run(bridge.handle_notification({"method": "notifications/cancelled"}))


def test_channel_handle_notification_unknown():
    client = FakeClient("axp_a_AgentKey.Secret")
    bridge = CaptureBridge(client)

    # Should not raise
    asyncio.run(bridge.handle_notification({"method": "some/unknown/method"}))


def test_channel_get_messages_no_pending():
    client = FakeClient("axp_a_AgentKey.Secret")
    bridge = CaptureBridge(client)

    asyncio.run(bridge.handle_get_messages(1, {"limit": 5}))

    result = bridge.writes[0]["result"]
    assert result["content"][0]["text"] == "No pending messages."


def test_channel_get_messages_invalid_limit():
    client = FakeClient("axp_a_AgentKey.Secret")
    bridge = CaptureBridge(client)

    asyncio.run(bridge.handle_get_messages(1, {"limit": "not_a_number"}))

    # Falls back to limit=10, returns "No pending messages."
    result = bridge.writes[0]["result"]
    assert "No pending" in result["content"][0]["text"]


def test_channel_get_messages_mark_read_false():
    client = FakeClient("axp_a_AgentKey.Secret")
    bridge = CaptureBridge(client)
    bridge._pending_mentions.append(
        MentionEvent(
            message_id="m1",
            parent_id=None,
            conversation_id=None,
            author="alex",
            prompt="check",
            raw_content="@peer-agent check",
            created_at=None,
            space_id="space-123",
        )
    )

    asyncio.run(bridge.handle_get_messages(1, {"mark_read": False}))

    # Mentions should NOT be removed when mark_read=False
    assert len(bridge._pending_mentions) == 1


def test_channel_log_noop_when_not_debug():
    client = FakeClient("axp_a_AgentKey.Secret")
    bridge = CaptureBridge(client)  # debug=False
    # Should not raise
    bridge.log("test message")


def test_channel_enqueue_from_thread_drops_when_shutdown(monkeypatch):
    client = FakeClient("axp_a_AgentKey.Secret")
    bridge = CaptureBridge(client)
    bridge.shutdown.set()

    event = MentionEvent(
        message_id="m1",
        parent_id=None,
        conversation_id=None,
        author="alex",
        prompt="hello",
        raw_content="@peer-agent hello",
        created_at=None,
        space_id="space-123",
    )
    # Should not raise, just drop the event
    bridge.enqueue_from_thread(event)


# ---- string_value helper ----


def test_string_value_returns_stripped_strings():
    assert channel_mod._string_value("hello") == "hello"
    assert channel_mod._string_value("  spaced  ") == "spaced"
    assert channel_mod._string_value("") is None
    assert channel_mod._string_value(None) is None
    assert channel_mod._string_value(42) == "42"
    assert channel_mod._string_value(3.14) == "3.14"
    assert channel_mod._string_value([1, 2]) is None
    assert channel_mod._string_value({"a": 1}) is None


# ---- Format shared object ----


def test_format_shared_object_with_context_key():
    metadata = {
        "forward": {
            "resource_type": "context",
            "context_key": "upload:file.txt:att-1",
            "title": "A document",
        }
    }
    result = channel_mod._format_shared_object(metadata, space_id="space-123")
    assert "Shared object:" in result
    assert "context_key: upload:file.txt:att-1" in result
    assert "axctl context get" in result


def test_format_shared_object_returns_none_for_no_forward():
    assert channel_mod._format_shared_object({}, space_id="space-1") is None
    assert channel_mod._format_shared_object(None, space_id="space-1") is None
    assert channel_mod._format_shared_object({"forward": "not a dict"}, space_id="space-1") is None


# ---- Format attachments ----


def test_format_attachments_with_context_keys():
    attachments = [
        {
            "id": "att-1",
            "filename": "report.pdf",
            "content_type": "application/pdf",
            "context_key": "upload:report.pdf:att-1",
        }
    ]
    result = channel_mod._format_attachments(attachments, space_id="space-123")
    assert "Attachments:" in result
    assert "report.pdf" in result
    assert "application/pdf" in result
    assert "context_key=upload:report.pdf:att-1" in result
    assert "axctl context get" in result


def test_format_attachments_returns_none_for_empty():
    assert channel_mod._format_attachments([], space_id="s1") is None
    assert channel_mod._format_attachments(None, space_id="s1") is None


def test_format_attachments_skips_non_dict_entries():
    result = channel_mod._format_attachments(["not a dict"], space_id="s1")
    # Only the header line, no actual entries => still None (len(lines) <= 1)
    assert result is None


# ---- Enrich prompt for agent ----


def test_enrich_prompt_no_extra_context():
    result = channel_mod._enrich_prompt_for_agent(
        "hello",
        metadata=None,
        attachments=None,
        space_id="s1",
    )
    assert result == "hello"


def test_enrich_prompt_with_attachments():
    result = channel_mod._enrich_prompt_for_agent(
        "check this",
        metadata=None,
        attachments=[{"id": "a1", "filename": "f.txt", "content_type": "text/plain"}],
        space_id="s1",
    )
    assert "---" in result
    assert "Attachments:" in result
    assert "f.txt" in result


# ---- Format inbox bundle for MCP ----


def test_format_inbox_bundle_empty_messages():
    bundle = {"agent": "test", "messages": [], "unread_count": 0}
    text = channel_mod._format_inbox_bundle_for_mcp(bundle)
    assert "0 unread message(s)" in text


# ---- _resolve_agent_id ----


def test_resolve_agent_id_finds_match():
    class AgentsClient:
        def list_agents(self):
            return [{"name": "orion", "id": "agent-123"}, {"name": "other", "id": "agent-456"}]

    assert channel_mod._resolve_agent_id(AgentsClient(), "orion") == "agent-123"
    assert channel_mod._resolve_agent_id(AgentsClient(), "ORION") == "agent-123"  # case insensitive


def test_resolve_agent_id_returns_none_for_no_match():
    class AgentsClient:
        def list_agents(self):
            return [{"name": "other", "id": "agent-456"}]

    assert channel_mod._resolve_agent_id(AgentsClient(), "orion") is None


def test_resolve_agent_id_returns_none_on_exception():
    class FailingClient:
        def list_agents(self):
            raise RuntimeError("API down")

    assert channel_mod._resolve_agent_id(FailingClient(), "orion") is None


def test_resolve_agent_id_returns_none_for_no_name():
    assert channel_mod._resolve_agent_id(None, None) is None


def test_resolve_agent_id_handles_dict_response():
    class DictClient:
        def list_agents(self):
            return {"agents": [{"name": "orion", "id": "agent-123"}]}

    assert channel_mod._resolve_agent_id(DictClient(), "orion") == "agent-123"


# ---- write_channel_setup validation ----


def test_write_channel_setup_requires_agent_name():
    with pytest.raises(typer.BadParameter, match="Agent name is required"):
        channel_mod.write_channel_setup(agent_name="", workdir=Path("/tmp"))


def test_write_channel_setup_rejects_invalid_mode():
    with pytest.raises(typer.BadParameter, match="--mode must be local or docker"):
        channel_mod.write_channel_setup(
            agent_name="test",
            workdir=Path("/tmp"),
            mode="kubernetes",
        )


def test_write_channel_setup_requires_space_id(monkeypatch):
    """Without gateway registry, --space-id must be provided."""
    import pytest

    monkeypatch.setattr(channel_mod, "_gateway_agent_channel_defaults", lambda n: {})

    with pytest.raises(typer.BadParameter, match="--space-id is required"):
        channel_mod.write_channel_setup(
            agent_name="test",
            workdir=Path("/tmp"),
            space_id=None,
        )


def test_write_channel_setup_requires_token_file(monkeypatch):
    import pytest

    monkeypatch.setattr(channel_mod, "_gateway_agent_channel_defaults", lambda n: {})

    with pytest.raises(typer.BadParameter, match="--token-file is required"):
        channel_mod.write_channel_setup(
            agent_name="test",
            workdir=Path("/tmp"),
            space_id="space-1",
            token_file=None,
        )


# ---- write_channel_setup model propagation ----


def test_write_channel_setup_writes_model_to_settings(tmp_path, monkeypatch):
    """Registered model is written to .claude/settings.local.json during channel setup."""
    import json

    monkeypatch.setattr(
        channel_mod,
        "_gateway_agent_channel_defaults",
        lambda n: {
            "AX_TOKEN_FILE": str(tmp_path / "token"),
            "AX_BASE_URL": "https://paxai.app",
            "AX_AGENT_NAME": "orion",
            "AX_AGENT_ID": "aid-1",
            "AX_SPACE_ID": "sp-1",
            "AX_GATEWAY_WORKDIR": "",
            "model": "claude-haiku-4-5",
        },
    )
    (tmp_path / "token").write_text("tok")

    result = channel_mod.write_channel_setup(agent_name="orion", workdir=tmp_path)

    settings_path = tmp_path / ".claude" / "settings.local.json"
    assert settings_path.exists()
    settings = json.loads(settings_path.read_text())
    assert settings["model"] == "claude-haiku-4-5"
    assert result["model"] == "claude-haiku-4-5"


def test_write_channel_setup_no_model_leaves_settings_without_model_key(tmp_path, monkeypatch):
    """When registry has no model, settings.local.json is written without a model key."""
    import json

    monkeypatch.setattr(
        channel_mod,
        "_gateway_agent_channel_defaults",
        lambda n: {
            "AX_TOKEN_FILE": str(tmp_path / "token"),
            "AX_BASE_URL": "https://paxai.app",
            "AX_AGENT_NAME": "orion",
            "AX_AGENT_ID": "aid-1",
            "AX_SPACE_ID": "sp-1",
            "AX_GATEWAY_WORKDIR": "",
            "model": "",
        },
    )
    (tmp_path / "token").write_text("tok")

    result = channel_mod.write_channel_setup(agent_name="orion", workdir=tmp_path)

    settings_path = tmp_path / ".claude" / "settings.local.json"
    assert settings_path.exists()
    settings = json.loads(settings_path.read_text())
    assert "model" not in settings
    assert result["model"] is None


# ---- Docker mode MCP config ----


def test_channel_mcp_server_config_docker_requires_image():
    with pytest.raises(typer.BadParameter, match="--container-image is required"):
        channel_mod._channel_mcp_server_config(
            agent_name="test",
            space_id="s1",
            env_path=Path("/tmp/env"),
            mode="docker",
            container_image=None,
            debug=False,
        )


def test_channel_mcp_server_config_local_with_debug():
    config = channel_mod._channel_mcp_server_config(
        agent_name="test",
        space_id="s1",
        env_path=Path("/tmp/env"),
        mode="local",
        container_image=None,
        debug=True,
    )
    assert "--debug" in config["args"]
    assert config["type"] == "stdio"


# ---- Touch gateway channel entry ----


def test_touch_gateway_channel_entry_no_op_on_missing_entry(monkeypatch):
    """If agent is not in registry, touch is a no-op."""
    monkeypatch.setattr(gateway_core, "load_gateway_registry", lambda: {"agents": []})
    # Should not raise
    channel_mod._touch_gateway_channel_entry("nonexistent-agent", event="test")


def test_touch_gateway_channel_entry_no_op_on_exception(monkeypatch):
    monkeypatch.setattr(
        gateway_core, "load_gateway_registry", lambda: (_ for _ in ()).throw(RuntimeError("disk error"))
    )
    # Should not raise
    channel_mod._touch_gateway_channel_entry("test-agent", event="test")


# ---- Channel context hint ----


def test_write_channel_context_hint_no_overwrite(tmp_path):
    """_write_channel_context_hint should not overwrite existing files."""
    hint_path = tmp_path / "AGENTS.md"
    hint_path.write_text("# Existing content\n")

    channel_mod._write_channel_context_hint(hint_path, agent_name="orion", context_path=Path(".ax/AGENT_CONTEXT.md"))
    assert hint_path.read_text() == "# Existing content\n"


def test_write_channel_context_hint_creates_new(tmp_path):
    hint_path = tmp_path / "AGENTS.md"

    channel_mod._write_channel_context_hint(hint_path, agent_name="orion", context_path=Path(".ax/AGENT_CONTEXT.md"))
    text = hint_path.read_text()
    assert "orion" in text
    assert "AGENT_CONTEXT.md" in text


# ---- Gateway agent channel defaults ----


def test_gateway_agent_channel_defaults_returns_empty_on_exception(monkeypatch):
    monkeypatch.setattr(
        gateway_core,
        "load_gateway_registry",
        lambda: (_ for _ in ()).throw(RuntimeError("disk error")),
    )
    result = channel_mod._gateway_agent_channel_defaults("orion")
    assert result == {}


def test_gateway_agent_channel_defaults_returns_empty_for_unknown_agent(monkeypatch):
    monkeypatch.setattr(
        gateway_core,
        "load_gateway_registry",
        lambda: {"agents": [{"name": "other"}]},
    )
    result = channel_mod._gateway_agent_channel_defaults("nonexistent")
    assert result == {}


def test_gateway_agent_channel_defaults_returns_entry_fields(monkeypatch):
    monkeypatch.setattr(
        gateway_core,
        "load_gateway_registry",
        lambda: {
            "agents": [
                {
                    "name": "orion",
                    "agent_id": "agent-123",
                    "space_id": "space-1",
                    "base_url": "https://paxai.app",
                    "token_file": "/tmp/token",
                    "workdir": "/tmp/work",
                }
            ]
        },
    )
    result = channel_mod._gateway_agent_channel_defaults("orion")
    assert result["AX_AGENT_NAME"] == "orion"
    assert result["AX_AGENT_ID"] == "agent-123"
    assert result["AX_SPACE_ID"] == "space-1"
    assert result["AX_TOKEN_FILE"] == "/tmp/token"


# ---- _write_gateway_cli_config ----


def test_write_gateway_cli_config(tmp_path):
    config_path = tmp_path / ".ax" / "config.toml"
    channel_mod._write_gateway_cli_config(
        config_path,
        agent_name="orion",
        base_url="http://127.0.0.1:8765",
        workdir=tmp_path,
    )
    text = config_path.read_text()
    assert 'agent_name = "orion"' in text
    assert 'mode = "local"' in text
    assert config_path.stat().st_mode & 0o777 == 0o600


# ---------------------------------------------------------------------------
# SSE health reporting — _sse_loop gateway touch behaviour
# ---------------------------------------------------------------------------


class TestSseLoopGatewayTouches:
    """_sse_loop writes sse_connected to the gateway entry at the right moments."""

    def _run_loop_capture_touches(self, monkeypatch, *, connect_side_effect, payload=None):
        """Run _sse_loop against a scripted client and capture _touch_gateway_channel_entry calls."""
        touches: list[dict] = []

        def capture_touch(agent_name, *, event=None, **updates):
            touches.append({"event": event, **updates})

        monkeypatch.setattr(channel_mod, "_touch_gateway_channel_entry", capture_touch)

        class _ScriptedClient(FakeClient):
            def __init__(self):
                super().__init__()
                self.connect_calls = 0

            def connect_sse(self, *, space_id):
                self.connect_calls += 1
                return connect_side_effect(self.connect_calls)

            def get_message(self, message_id):
                return {"message": {"metadata": {}}}

        client = _ScriptedClient()
        bridge = CaptureBridge(client)
        bridge.shutdown.set()  # exit after first pass
        channel_mod._sse_loop(bridge)
        return touches

    def test_writes_sse_connected_true_on_successful_connect(self, monkeypatch):
        touches: list[dict] = []

        def capture_touch(agent_name, *, event=None, **updates):
            touches.append({"event": event, **updates})

        monkeypatch.setattr(channel_mod, "_touch_gateway_channel_entry", capture_touch)
        monkeypatch.setattr(channel_mod.time, "monotonic", lambda: 0.0)

        class _ImmediateShutdownClient(FakeClient):
            def connect_sse(self, *, space_id):
                return FakeSseResponse(
                    {
                        "id": "m1",
                        "content": "@peer-agent hi",
                        "author": {"id": "u1", "name": "u", "type": "user"},
                        "mentions": ["peer-agent"],
                    }
                )

            def get_message(self, message_id):
                return {"message": {"metadata": {}}}

        bridge = CaptureBridge(_ImmediateShutdownClient())

        delivered = []

        def capture_delivery(event):
            delivered.append(event)
            bridge.shutdown.set()

        bridge.enqueue_from_thread = capture_delivery
        channel_mod._sse_loop(bridge)

        connected_touches = [t for t in touches if t.get("sse_connected") is True]
        assert connected_touches, "expected sse_connected=True touch on successful connect"

    def test_writes_sse_connected_false_on_connect_error(self, monkeypatch):
        touches: list[dict] = []

        def capture_touch(agent_name, *, event=None, **updates):
            touches.append({"event": event, **updates})

        monkeypatch.setattr(channel_mod, "_touch_gateway_channel_entry", capture_touch)

        class _FailingClient(FakeClient):
            def connect_sse(self, *, space_id):
                raise ConnectionError("SSE failed")

        bridge = CaptureBridge(_FailingClient())

        def _sleep_and_shutdown(s):
            bridge.shutdown.set()

        monkeypatch.setattr(channel_mod.time, "sleep", _sleep_and_shutdown)
        channel_mod._sse_loop(bridge)

        disconnected_touches = [t for t in touches if t.get("sse_connected") is False]
        assert disconnected_touches, "expected sse_connected=False touch on connection failure"

    def test_startup_touch_initialises_sse_connected_false(self, monkeypatch):
        touches: list[dict] = []

        def capture_touch(agent_name, *, event=None, **updates):
            touches.append({"event": event, **updates})

        monkeypatch.setattr(channel_mod, "_touch_gateway_channel_entry", capture_touch)

        startup_touch = next(
            (t for t in touches if t.get("event") == "channel_attached"),
            None,
        )
        # Simulate the startup call directly as it happens at channel attach time
        channel_mod._touch_gateway_channel_entry(
            "peer-agent",
            event="channel_attached",
            sse_connected=False,
        )
        startup_touch = next(
            (t for t in touches if t.get("event") == "channel_attached"),
            None,
        )
        assert startup_touch is not None
        assert startup_touch.get("sse_connected") is False

    def test_mcp_ping_does_not_set_sse_connected(self, monkeypatch):
        """MCP ping updates last_seen_at but must not write sse_connected."""
        touches: list[dict] = []

        def capture_touch(agent_name, *, event=None, **updates):
            touches.append({"event": event, **updates})

        monkeypatch.setattr(channel_mod, "_touch_gateway_channel_entry", capture_touch)

        async def run():
            client = FakeClient()
            bridge = CaptureBridge(client)
            bridge.initialized.set()
            bridge.loop = __import__("asyncio").get_running_loop()
            await bridge.touch_gateway("channel_ping", current_status=None, current_activity=None)

        __import__("asyncio").run(run())

        ping_touches = [t for t in touches if t.get("event") == "channel_ping"]
        assert ping_touches, "expected a channel_ping touch"
        assert all("sse_connected" not in t for t in ping_touches), "channel_ping must not write sse_connected"
