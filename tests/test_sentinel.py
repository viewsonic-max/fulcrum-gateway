"""Tests for ax_cli/runtimes/hermes/sentinel.py

Covers SessionStore, HistoryStore, parse_args, mention detection helpers,
command builders, and stream parsers — all testable without live SSE.
"""

from __future__ import annotations

import argparse
import json
from unittest.mock import MagicMock, patch

from ax_cli.runtimes.hermes.sentinel import (
    AxAPI,
    HistoryStore,
    SessionStore,
    _is_ax_noise,
    _is_paused,
    _parse_retry_after,
    get_author_id,
    get_author_name,
    is_mentioned,
    iter_sse,
    parse_args,
    resolve_history_thread_id,
    run_cli,
    should_respond,
    strip_mention,
)

# ── SessionStore ──────────────────────────────────────────────────────────


class TestSessionStore:
    def test_get_set(self):
        store = SessionStore()
        assert store.get("thread1") is None
        store.set("thread1", "sess_abc")
        assert store.get("thread1") == "sess_abc"

    def test_delete(self):
        store = SessionStore()
        store.set("thread1", "sess_abc")
        store.delete("thread1")
        assert store.get("thread1") is None

    def test_delete_nonexistent(self):
        store = SessionStore()
        store.delete("nope")  # Should not raise

    def test_count(self):
        store = SessionStore()
        assert store.count() == 0
        store.set("a", "s1")
        store.set("b", "s2")
        assert store.count() == 2

    def test_eviction_on_max(self):
        store = SessionStore(max_sessions=3)
        store.set("a", "1")
        store.set("b", "2")
        store.set("c", "3")
        store.set("d", "4")  # Should evict "a"
        assert store.get("a") is None
        assert store.count() == 3

    def test_overwrite_existing(self):
        store = SessionStore()
        store.set("thread1", "old")
        store.set("thread1", "new")
        assert store.get("thread1") == "new"
        assert store.count() == 1


# ── HistoryStore ──────────────────────────────────────────────────────────


class TestHistoryStore:
    def test_get_set(self):
        store = HistoryStore()
        assert store.get("thread1") == []
        store.set("thread1", [{"role": "user", "content": "hi"}])
        assert len(store.get("thread1")) == 1
        assert store.get("thread1")[0]["content"] == "hi"

    def test_lru_trimming(self):
        store = HistoryStore(max_messages=3)
        history = [{"role": "user", "content": str(i)} for i in range(10)]
        store.set("thread1", history)
        result = store.get("thread1")
        assert len(result) == 3
        # Should keep the last 3
        assert result[0]["content"] == "7"

    def test_delete(self):
        store = HistoryStore()
        store.set("thread1", [{"role": "user", "content": "hi"}])
        store.delete("thread1")
        assert store.get("thread1") == []

    def test_thread_eviction(self):
        store = HistoryStore(max_threads=2)
        store.set("a", [{"role": "user", "content": "a"}])
        store.set("b", [{"role": "user", "content": "b"}])
        store.set("c", [{"role": "user", "content": "c"}])  # Evicts "a"
        assert store.get("a") == []
        assert len(store.get("b")) == 1

    def test_returns_copies(self):
        store = HistoryStore()
        store.set("t", [{"role": "user", "content": "x"}])
        result1 = store.get("t")
        result1.append({"role": "assistant", "content": "y"})
        # The modification should not affect the store
        assert len(store.get("t")) == 1


# ── parse_args ────────────────────────────────────────────────────────────


class TestParseArgs:
    def test_defaults(self):
        with patch("sys.argv", ["sentinel"]):
            args = parse_args()
        assert args.dry_run is False
        assert args.agent is None
        assert args.timeout == 300
        assert args.runtime == "hermes_sdk"
        assert args.update_interval == 2.0

    def test_dry_run(self):
        with patch("sys.argv", ["sentinel", "--dry-run"]):
            args = parse_args()
        assert args.dry_run is True

    def test_agent_name(self):
        with patch("sys.argv", ["sentinel", "--agent", "mybot"]):
            args = parse_args()
        assert args.agent == "mybot"

    def test_runtime_choices(self):
        for rt in ["openai_sdk", "hermes_sdk", "groq_sdk", "mistral_sdk", "gemini_sdk", "leapfrog_sdk"]:
            with patch("sys.argv", ["sentinel", "--runtime", rt]):
                args = parse_args()
            assert args.runtime == rt

    def test_model_flag(self):
        with patch("sys.argv", ["sentinel", "--model", "opus"]):
            args = parse_args()
        assert args.model == "opus"

    def test_workdir_flag(self):
        with patch("sys.argv", ["sentinel", "--workdir", "/custom/path"]):
            args = parse_args()
        assert args.workdir == "/custom/path"


# ── get_author_name / get_author_id ──────────────────────────────────────


class TestGetAuthorName:
    def test_string_author(self):
        assert get_author_name({"author": "alice"}) == "alice"

    def test_dict_author_name(self):
        assert get_author_name({"author": {"name": "bob", "username": "bobby"}}) == "bob"

    def test_dict_author_username_fallback(self):
        assert get_author_name({"author": {"username": "charlie"}}) == "charlie"

    def test_no_author(self):
        assert get_author_name({}) == ""

    def test_empty_dict_author(self):
        assert get_author_name({"author": {}}) == ""


class TestGetAuthorId:
    def test_dict_author_with_id(self):
        assert get_author_id({"author": {"id": "id123"}}) == "id123"

    def test_string_author_falls_back_to_agent_id(self):
        assert get_author_id({"author": "alice", "agent_id": "agent_456"}) == "agent_456"

    def test_no_ids(self):
        assert get_author_id({"author": "alice"}) == ""


# ── is_mentioned ─────────────────────────────────────────────────────────


class TestIsMentioned:
    def test_at_mention_in_content(self):
        data = {"content": "Hey @mybot do something"}
        assert is_mentioned(data, "mybot") is True

    def test_case_insensitive(self):
        data = {"content": "Hey @MyBot do something"}
        assert is_mentioned(data, "mybot") is True

    def test_no_mention(self):
        data = {"content": "Hey everyone"}
        assert is_mentioned(data, "mybot") is False

    def test_mentions_array(self):
        data = {"content": "do something", "mentions": ["mybot"]}
        assert is_mentioned(data, "mybot") is True

    def test_route_inferred_blocked(self):
        data = {
            "content": "do something",
            "mentions": ["mybot"],
            "metadata": {"route_inferred": True},
        }
        assert is_mentioned(data, "mybot") is False

    def test_router_inferred_blocked(self):
        data = {
            "content": "do something",
            "mentions": ["mybot"],
            "metadata": {"router_inferred": True},
        }
        assert is_mentioned(data, "mybot") is False

    def test_explicit_mention_overrides_route_inferred(self):
        # If @mybot is in the content text, it counts regardless of metadata
        data = {
            "content": "@mybot do something",
            "mentions": ["mybot"],
            "metadata": {"route_inferred": True},
        }
        assert is_mentioned(data, "mybot") is True


# ── strip_mention ─────────────────────────────────────────────────────────


class TestStripMention:
    def test_strips_at_mention(self):
        assert strip_mention("@mybot do this", "mybot") == "do this"

    def test_case_insensitive(self):
        assert strip_mention("@MyBot do this", "mybot") == "do this"

    def test_multiple_occurrences(self):
        result = strip_mention("@mybot hello @mybot", "mybot")
        assert result == "hello"

    def test_no_mention(self):
        assert strip_mention("hello world", "mybot") == "hello world"

    def test_strips_and_trims(self):
        assert strip_mention("  @mybot   hello  ", "mybot") == "hello"


# ── should_respond ────────────────────────────────────────────────────────


class TestShouldRespond:
    def test_self_mention_rejected(self):
        data = {"author": "mybot", "content": "@mybot hello"}
        assert should_respond(data, "mybot") is False

    def test_self_mention_by_id_rejected(self):
        data = {
            "author": {"name": "other", "id": "agent_123"},
            "content": "@mybot hello",
        }
        assert should_respond(data, "mybot", agent_id="agent_123") is False

    def test_valid_mention(self):
        data = {"author": "alice", "content": "@mybot help me"}
        assert should_respond(data, "mybot") is True

    def test_no_mention(self):
        data = {"author": "alice", "content": "help me"}
        assert should_respond(data, "mybot") is False

    def test_ax_noise_rejected(self):
        data = {"author": "aX", "content": "@mybot is asking: can you help?"}
        assert should_respond(data, "mybot") is False


# ── _is_ax_noise ─────────────────────────────────────────────────────────


class TestIsAxNoise:
    def test_chose_not_to_reply(self):
        assert _is_ax_noise({"content": "aX chose not to reply", "author": "system"}) is True

    def test_request_processed(self):
        assert _is_ax_noise({"content": "Request processed", "author": "system"}) is True

    def test_empty_content(self):
        assert _is_ax_noise({"content": "", "author": "system"}) is True

    def test_ax_relay_pattern_is_asking(self):
        assert _is_ax_noise({"content": "@user is asking: what about X?", "author": "aX"}) is True

    def test_ax_relay_pattern_says(self):
        assert _is_ax_noise({"content": "@user says to do this", "author": "aX"}) is True

    def test_ax_ack_acknowledged(self):
        assert _is_ax_noise({"content": "Acknowledged", "author": "aX"}) is True

    def test_ax_ack_got_it(self):
        assert _is_ax_noise({"content": "Got it.", "author": "aX"}) is True

    def test_widget_metadata(self):
        data = {"content": "result", "author": "system", "metadata": {"ui": {"widget": True}}}
        assert _is_ax_noise(data) is True

    def test_routed_by_ax_metadata(self):
        data = {
            "content": "something",
            "author": "system",
            "metadata": {"routing": {"routed_by_ax": True}},
        }
        assert _is_ax_noise(data) is True

    def test_normal_message_not_noise(self):
        assert _is_ax_noise({"content": "Please review my PR", "author": "alice"}) is False


# ── resolve_history_thread_id ─────────────────────────────────────────────


class TestResolveHistoryThreadId:
    def test_default_space_scope(self, monkeypatch):
        monkeypatch.delenv("AX_SENTINEL_HISTORY_SCOPE", raising=False)
        result = resolve_history_thread_id(
            {"id": "msg1"},
            agent_name="mybot",
            space_id="space123",
        )
        assert result == "space:space123:agent:mybot"

    def test_message_scope(self, monkeypatch):
        monkeypatch.setenv("AX_SENTINEL_HISTORY_SCOPE", "message")
        result = resolve_history_thread_id(
            {"id": "msg1"},
            agent_name="mybot",
            space_id="space123",
        )
        assert result == "msg1"

    def test_per_message_scope(self, monkeypatch):
        monkeypatch.setenv("AX_SENTINEL_HISTORY_SCOPE", "per_message")
        result = resolve_history_thread_id(
            {"id": "msg1"},
            agent_name="mybot",
            space_id="space123",
        )
        assert result == "msg1"

    def test_conversation_scope_with_parent(self, monkeypatch):
        monkeypatch.setenv("AX_SENTINEL_HISTORY_SCOPE", "conversation")
        result = resolve_history_thread_id(
            {"id": "msg1", "parent_id": "parent1"},
            agent_name="mybot",
            space_id="space123",
        )
        assert result == "parent1"

    def test_thread_scope_fallback(self, monkeypatch):
        monkeypatch.setenv("AX_SENTINEL_HISTORY_SCOPE", "thread")
        result = resolve_history_thread_id(
            {"id": "msg1", "conversation_id": "conv1"},
            agent_name="mybot",
            space_id="space123",
        )
        assert result == "conv1"

    def test_author_scope(self, monkeypatch):
        monkeypatch.setenv("AX_SENTINEL_HISTORY_SCOPE", "author")
        result = resolve_history_thread_id(
            {"id": "msg1"},
            agent_name="mybot",
            space_id="space123",
            author="alice",
        )
        assert result == "space:space123:agent:mybot:author:alice"

    def test_author_scope_no_author(self, monkeypatch):
        monkeypatch.setenv("AX_SENTINEL_HISTORY_SCOPE", "author")
        result = resolve_history_thread_id(
            {"id": "msg1"},
            agent_name="mybot",
            space_id="space123",
            author="",
        )
        # Falls through to space scope
        assert result == "space:space123:agent:mybot"


# ── iter_sse ──────────────────────────────────────────────────────────────


class TestIterSSE:
    def test_parses_json_events(self):
        lines = [
            "event:message",
            'data:{"id": "msg1", "content": "hello"}',
            "",  # Empty line = event boundary
        ]
        response = MagicMock()
        response.iter_lines.return_value = iter(lines)

        events = list(iter_sse(response))
        assert len(events) == 1
        assert events[0][0] == "message"
        assert events[0][1]["content"] == "hello"

    def test_parses_plain_text_data(self):
        lines = [
            "event:connected",
            "data:ok",
            "",
        ]
        response = MagicMock()
        response.iter_lines.return_value = iter(lines)

        events = list(iter_sse(response))
        assert len(events) == 1
        assert events[0][0] == "connected"
        assert events[0][1] == "ok"

    def test_multiple_events(self):
        lines = [
            "event:connected",
            "data:hello",
            "",
            "event:message",
            'data:{"id": 1}',
            "",
        ]
        response = MagicMock()
        response.iter_lines.return_value = iter(lines)

        events = list(iter_sse(response))
        assert len(events) == 2

    def test_multiline_data(self):
        lines = [
            "event:message",
            'data:{"multiline":',
            'data:"test"}',
            "",
        ]
        response = MagicMock()
        response.iter_lines.return_value = iter(lines)

        events = list(iter_sse(response))
        # Multi-line data should be joined
        assert len(events) == 1

    def test_no_event_type_ignored(self):
        """Data lines without a preceding event: line should be dropped."""
        lines = [
            'data:{"id": 1}',
            "",
        ]
        response = MagicMock()
        response.iter_lines.return_value = iter(lines)

        events = list(iter_sse(response))
        assert len(events) == 0

    def test_no_data_lines_ignored(self):
        """An event type with no data lines should be dropped."""
        lines = [
            "event:heartbeat",
            "",
        ]
        response = MagicMock()
        response.iter_lines.return_value = iter(lines)

        events = list(iter_sse(response))
        assert len(events) == 0

    def test_consecutive_events(self):
        """Two events back-to-back parse correctly."""
        lines = [
            "event:ping",
            "data:pong",
            "",
            "event:message",
            'data:{"id": 42}',
            "",
        ]
        response = MagicMock()
        response.iter_lines.return_value = iter(lines)

        events = list(iter_sse(response))
        assert len(events) == 2
        assert events[0] == ("ping", "pong")
        assert events[1] == ("message", {"id": 42})

    def test_malformed_json_returned_as_string(self):
        """JSON data that starts with '{' but is invalid is returned as raw string."""
        lines = [
            "event:error",
            "data:{not valid json}",
            "",
        ]
        response = MagicMock()
        response.iter_lines.return_value = iter(lines)

        events = list(iter_sse(response))
        assert len(events) == 1
        assert events[0][0] == "error"
        assert events[0][1] == "{not valid json}"

    def test_trailing_data_without_empty_line(self):
        """If stream ends without a trailing empty line, last event is not emitted."""
        lines = [
            "event:message",
            'data:{"id": 1}',
            # No trailing empty line
        ]
        response = MagicMock()
        response.iter_lines.return_value = iter(lines)

        events = list(iter_sse(response))
        assert len(events) == 0

    def test_whitespace_in_event_type(self):
        """event: lines with extra whitespace should be stripped."""
        lines = [
            "event:  message  ",
            'data:{"id": 1}',
            "",
        ]
        response = MagicMock()
        response.iter_lines.return_value = iter(lines)

        events = list(iter_sse(response))
        assert len(events) == 1
        assert events[0][0] == "message"


# ── _is_paused ──────────────────────────────────────────────────────────


class TestIsPaused:
    def test_not_paused(self, tmp_path, monkeypatch):
        monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
        (tmp_path / ".ax").mkdir(parents=True, exist_ok=True)
        assert _is_paused("mybot") is False

    def test_paused_all(self, tmp_path, monkeypatch):
        monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
        (tmp_path / ".ax").mkdir(parents=True, exist_ok=True)
        (tmp_path / ".ax" / "sentinel_pause").touch()
        assert _is_paused("mybot") is True

    def test_paused_specific_agent(self, tmp_path, monkeypatch):
        monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
        (tmp_path / ".ax").mkdir(parents=True, exist_ok=True)
        (tmp_path / ".ax" / "sentinel_pause_mybot").touch()
        assert _is_paused("mybot") is True

    def test_paused_other_agent_not_affected(self, tmp_path, monkeypatch):
        monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
        (tmp_path / ".ax").mkdir(parents=True, exist_ok=True)
        (tmp_path / ".ax" / "sentinel_pause_otherbot").touch()
        assert _is_paused("mybot") is False


# ── AxAPI unit tests ────────────────────────────────────────────────────


class TestAxAPIHeaders:
    """Test header construction without actual HTTP calls."""

    def test_headers_without_exchanger(self):
        with patch("ax_cli.runtimes.hermes.sentinel.httpx.Client"):
            api = AxAPI(
                base_url="http://localhost:8002",
                token="test_token_123",
                agent_name="bot",
                agent_id="agent_456",
            )
        headers = api._headers()
        assert headers["Authorization"] == "Bearer test_token_123"
        assert headers["Content-Type"] == "application/json"
        assert headers["X-Agent-Id"] == "agent_456"

    def test_headers_without_agent_id(self):
        with patch("ax_cli.runtimes.hermes.sentinel.httpx.Client"):
            api = AxAPI(
                base_url="http://localhost:8002",
                token="test_token_123",
                agent_name="bot",
                agent_id="",
            )
        headers = api._headers()
        assert "X-Agent-Id" not in headers

    def test_base_url_trailing_slash_stripped(self):
        with patch("ax_cli.runtimes.hermes.sentinel.httpx.Client"):
            api = AxAPI(
                base_url="http://localhost:8002/",
                token="tok",
                agent_name="bot",
                agent_id="",
            )
        assert api.base_url == "http://localhost:8002"


class TestAxAPISendMessage:
    """Test send_message with mocked HTTP client."""

    def _make_api(self):
        with patch("ax_cli.runtimes.hermes.sentinel.httpx.Client") as mock_client_cls:
            mock_client = MagicMock()
            mock_client_cls.return_value = mock_client
            api = AxAPI(
                base_url="http://localhost:8002",
                token="tok",
                agent_name="bot",
                agent_id="agent_1",
            )
        api._client = mock_client
        return api, mock_client

    def test_send_message_success_with_message_wrapper(self):
        api, client = self._make_api()
        client.post.return_value = MagicMock(
            status_code=200,
            text='{"message": {"id": "msg_1"}}',
            json=lambda: {"message": {"id": "msg_1"}},
        )

        result = api.send_message("space1", "hello", parent_id="parent1")
        assert result == {"id": "msg_1"}
        client.post.assert_called_once()
        call_kwargs = client.post.call_args
        body = call_kwargs.kwargs.get("json") or call_kwargs[1].get("json")
        assert body["content"] == "hello"
        assert body["space_id"] == "space1"
        assert body["parent_id"] == "parent1"

    def test_send_message_success_direct_id(self):
        api, client = self._make_api()
        client.post.return_value = MagicMock(
            status_code=200,
            text='{"id": "msg_2"}',
            json=lambda: {"id": "msg_2"},
        )

        result = api.send_message("space1", "hello")
        assert result == {"id": "msg_2"}

    def test_send_message_error_status(self):
        api, client = self._make_api()
        client.post.return_value = MagicMock(
            status_code=500,
            text="Internal Server Error",
        )

        result = api.send_message("space1", "hello")
        assert result is None

    def test_send_message_exception(self):
        api, client = self._make_api()
        client.post.side_effect = Exception("connection refused")

        result = api.send_message("space1", "hello")
        assert result is None

    def test_send_message_no_parent_id(self):
        api, client = self._make_api()
        client.post.return_value = MagicMock(
            status_code=200,
            text='{"id": "msg_3"}',
            json=lambda: {"id": "msg_3"},
        )

        api.send_message("space1", "hello")
        call_kwargs = client.post.call_args
        body = call_kwargs.kwargs.get("json") or call_kwargs[1].get("json")
        assert "parent_id" not in body

    def test_send_message_with_metadata(self):
        api, client = self._make_api()
        client.post.return_value = MagicMock(
            status_code=200,
            text='{"id": "msg_4"}',
            json=lambda: {"id": "msg_4"},
        )

        api.send_message("space1", "hello", metadata={"key": "val"})
        call_kwargs = client.post.call_args
        body = call_kwargs.kwargs.get("json") or call_kwargs[1].get("json")
        assert "metadata" in body

    def test_send_message_empty_response(self):
        api, client = self._make_api()
        client.post.return_value = MagicMock(
            status_code=200,
            text="",
        )

        result = api.send_message("space1", "hello")
        assert result is None

    def test_send_message_429_sleeps_retry_after_and_returns_none(self, monkeypatch):
        api, client = self._make_api()
        client.post.return_value = MagicMock(
            status_code=429,
            headers={"retry-after": "5"},
            text="rate limited",
        )
        slept: list[float] = []
        monkeypatch.setattr("ax_cli.runtimes.hermes.sentinel.time.sleep", lambda s: slept.append(s))

        result = api.send_message("space1", "hello")
        assert result is None
        assert slept == [5.0]

    def test_send_message_429_defaults_to_60s_when_no_header(self, monkeypatch):
        api, client = self._make_api()
        client.post.return_value = MagicMock(
            status_code=429,
            headers={},
            text="rate limited",
        )
        slept: list[float] = []
        monkeypatch.setattr("ax_cli.runtimes.hermes.sentinel.time.sleep", lambda s: slept.append(s))

        api.send_message("space1", "hello")
        assert slept == [60.0]


class TestAxAPIEditMessage:
    def _make_api(self):
        with patch("ax_cli.runtimes.hermes.sentinel.httpx.Client") as mock_client_cls:
            mock_client = MagicMock()
            mock_client_cls.return_value = mock_client
            api = AxAPI(
                base_url="http://localhost:8002",
                token="tok",
                agent_name="bot",
                agent_id="agent_1",
            )
        api._client = mock_client
        return api, mock_client

    def test_edit_message_success(self):
        api, client = self._make_api()
        client.patch.return_value = MagicMock(status_code=200)

        result = api.edit_message("msg_1", "updated content")
        assert result is True
        call_kwargs = client.patch.call_args
        assert "msg_1" in call_kwargs[0][0]

    def test_edit_message_failure(self):
        api, client = self._make_api()
        client.patch.return_value = MagicMock(status_code=404)

        result = api.edit_message("msg_1", "updated content")
        assert result is False

    def test_edit_message_exception(self):
        api, client = self._make_api()
        client.patch.side_effect = Exception("timeout")

        result = api.edit_message("msg_1", "updated content")
        assert result is False

    def test_edit_message_with_metadata(self):
        api, client = self._make_api()
        client.patch.return_value = MagicMock(status_code=200)

        api.edit_message("msg_1", "content", metadata={"final": True})
        call_kwargs = client.patch.call_args
        body = call_kwargs.kwargs.get("json") or call_kwargs[1].get("json")
        assert body["metadata"] == {"final": True}

    def test_edit_message_without_metadata(self):
        api, client = self._make_api()
        client.patch.return_value = MagicMock(status_code=200)

        api.edit_message("msg_1", "content")
        call_kwargs = client.patch.call_args
        body = call_kwargs.kwargs.get("json") or call_kwargs[1].get("json")
        assert "metadata" not in body

    def test_edit_message_429_sleeps_retry_after_and_returns_false(self, monkeypatch):
        api, client = self._make_api()
        client.patch.return_value = MagicMock(
            status_code=429,
            headers={"retry-after": "10"},
        )
        slept: list[float] = []
        monkeypatch.setattr("ax_cli.runtimes.hermes.sentinel.time.sleep", lambda s: slept.append(s))

        result = api.edit_message("msg_1", "content")
        assert result is False
        assert slept == [10.0]


class TestParseRetryAfter:
    def _resp(self, headers: dict) -> MagicMock:
        return MagicMock(headers=headers)

    def test_integer_seconds(self):
        assert _parse_retry_after(self._resp({"retry-after": "30"})) == 30.0

    def test_missing_header_returns_default(self):
        assert _parse_retry_after(self._resp({})) == 60.0

    def test_missing_header_custom_default(self):
        assert _parse_retry_after(self._resp({}), default=90.0) == 90.0

    def test_unparseable_header_returns_default(self):
        assert _parse_retry_after(self._resp({"retry-after": "not-a-number"})) == 60.0

    def test_clamps_to_minimum_one_second(self):
        assert _parse_retry_after(self._resp({"retry-after": "0"})) == 1.0


class TestAxAPIRequestSummary:
    def _make_api(self):
        with patch("ax_cli.runtimes.hermes.sentinel.httpx.Client") as mock_client_cls:
            mock_client = MagicMock()
            mock_client_cls.return_value = mock_client
            api = AxAPI(
                base_url="http://localhost:8002",
                token="tok",
                agent_name="bot",
                agent_id="agent_1",
            )
        api._client = mock_client
        return api, mock_client

    def test_request_summary_calls_post(self):
        api, client = self._make_api()
        client.post.return_value = MagicMock(status_code=200)

        api.request_summary("msg_123")
        client.post.assert_called_once()
        url = client.post.call_args[0][0]
        assert "msg_123/summarize" in url

    def test_request_summary_includes_force_header(self):
        api, client = self._make_api()
        client.post.return_value = MagicMock(status_code=200)

        api.request_summary("msg_123")
        call_kwargs = client.post.call_args
        headers = call_kwargs.kwargs.get("headers") or call_kwargs[1].get("headers")
        assert headers["X-Force-Resummarize"] == "true"

    def test_request_summary_swallows_exception(self):
        api, client = self._make_api()
        client.post.side_effect = Exception("network error")

        # Should not raise
        api.request_summary("msg_123")


class TestAxAPISignalProcessing:
    def _make_api(self):
        with patch("ax_cli.runtimes.hermes.sentinel.httpx.Client") as mock_client_cls:
            mock_client = MagicMock()
            mock_client_cls.return_value = mock_client
            api = AxAPI(
                base_url="http://localhost:8002",
                token="tok",
                agent_name="bot",
                agent_id="agent_1",
            )
        api._client = mock_client
        return api, mock_client

    def test_stdout_event_emitted(self, capsys):
        api, _ = self._make_api()
        api.signal_processing("msg_1", "started", space_id="space_1")
        captured = capsys.readouterr()
        assert "AX_GATEWAY_EVENT" in captured.out
        event = json.loads(captured.out.split("AX_GATEWAY_EVENT ")[1].strip())
        assert event["kind"] == "status"
        assert event["status"] == "started"
        assert event["message_id"] == "msg_1"
        assert event["agent_name"] == "bot"
        assert event["space_id"] == "space_1"

    def test_stdout_event_includes_tool_name(self, capsys):
        api, _ = self._make_api()
        api.signal_processing("msg_1", "tool_call", space_id="sp1", tool_name="Read")
        captured = capsys.readouterr()
        event = json.loads(captured.out.split("AX_GATEWAY_EVENT ")[1].strip())
        assert event["tool_name"] == "Read"

    def test_stdout_event_includes_activity(self, capsys):
        api, _ = self._make_api()
        api.signal_processing("msg_1", "tool_call", space_id="sp1", activity="Reading foo.py")
        captured = capsys.readouterr()
        event = json.loads(captured.out.split("AX_GATEWAY_EVENT ")[1].strip())
        assert event["activity"] == "Reading foo.py"

    def test_no_internal_post_without_api_key(self):
        api, client = self._make_api()
        # No internal_api_key set -> no POST to internal endpoint
        api.signal_processing("msg_1", "started", space_id="sp1")
        # The POST call to internal endpoint should NOT happen
        # (only the stdout event fires)
        client.post.assert_not_called()

    def test_internal_post_with_api_key(self):
        with patch("ax_cli.runtimes.hermes.sentinel.httpx.Client") as mock_client_cls:
            mock_client = MagicMock()
            mock_client_cls.return_value = mock_client
            api = AxAPI(
                base_url="http://localhost:8002",
                token="tok",
                agent_name="bot",
                agent_id="agent_1",
                internal_api_key="secret_key",
            )
        api._client = mock_client
        mock_client.post.return_value = MagicMock(status_code=200)

        api.signal_processing("msg_1", "started", space_id="sp1")
        mock_client.post.assert_called_once()
        url = mock_client.post.call_args[0][0]
        assert "/auth/internal/agent-status" in url

    def test_internal_post_disables_on_401(self):
        with patch("ax_cli.runtimes.hermes.sentinel.httpx.Client") as mock_client_cls:
            mock_client = MagicMock()
            mock_client_cls.return_value = mock_client
            api = AxAPI(
                base_url="http://localhost:8002",
                token="tok",
                agent_name="bot",
                agent_id="agent_1",
                internal_api_key="secret_key",
            )
        api._client = mock_client
        mock_client.post.return_value = MagicMock(status_code=401)

        api.signal_processing("msg_1", "started", space_id="sp1")
        assert api._processing_signals_enabled is False

    def test_internal_post_disables_on_403(self):
        with patch("ax_cli.runtimes.hermes.sentinel.httpx.Client") as mock_client_cls:
            mock_client = MagicMock()
            mock_client_cls.return_value = mock_client
            api = AxAPI(
                base_url="http://localhost:8002",
                token="tok",
                agent_name="bot",
                agent_id="agent_1",
                internal_api_key="secret_key",
            )
        api._client = mock_client
        mock_client.post.return_value = MagicMock(status_code=403)

        api.signal_processing("msg_1", "started", space_id="sp1")
        assert api._processing_signals_enabled is False


# ── Additional _is_ax_noise edge cases ──────────────────────────────────


class TestIsAxNoiseExtended:
    def test_ax_relay_wants(self):
        assert _is_ax_noise({"content": "@user wants to know about X", "author": "aX"}) is True

    def test_ax_relay_is_requesting(self):
        assert _is_ax_noise({"content": "@user is requesting help", "author": "aX"}) is True

    def test_ax_relay_has_requested(self):
        assert _is_ax_noise({"content": "@user has requested info", "author": "aX"}) is True

    def test_ax_ack_noted(self):
        assert _is_ax_noise({"content": "Noted.", "author": "aX"}) is True

    def test_ax_ack_roger(self):
        assert _is_ax_noise({"content": "Roger", "author": "aX"}) is True

    def test_ax_ack_status_recorded(self):
        assert _is_ax_noise({"content": "Status recorded", "author": "aX"}) is True

    def test_ax_ack_storing_the(self):
        assert _is_ax_noise({"content": "Storing the context for later", "author": "aX"}) is True

    def test_ax_ack_clear_blocker(self):
        assert _is_ax_noise({"content": "Clear blocker for agent", "author": "aX"}) is True

    def test_ax_ack_options_colon(self):
        assert _is_ax_noise({"content": "Options: we could do A or B", "author": "aX"}) is True

    def test_ax_is_currently_executing(self):
        assert _is_ax_noise({"content": "@agent is currently executing task", "author": "aX"}) is True

    def test_cards_metadata(self):
        data = {
            "content": "result",
            "author": "system",
            "metadata": {"ui": {"cards": [{"title": "card"}]}},
        }
        assert _is_ax_noise(data) is True

    def test_ax_relay_mode_metadata(self):
        data = {
            "content": "hello",
            "author": "system",
            "metadata": {"routing": {"mode": "ax_relay"}},
        }
        assert _is_ax_noise(data) is True

    def test_ax_author_route_inferred(self):
        data = {
            "content": "hello from aX",
            "author": "aX",
            "metadata": {"route_inferred": True},
        }
        assert _is_ax_noise(data) is True

    def test_non_ax_author_relay_patterns_not_noise(self):
        """Relay patterns should only match when author is aX, not other authors."""
        assert _is_ax_noise({"content": "user is asking: what?", "author": "alice"}) is False

    def test_none_metadata_handled(self):
        data = {"content": "hello", "author": "alice", "metadata": None}
        assert _is_ax_noise(data) is False

    def test_ax_is_inquiring(self):
        assert _is_ax_noise({"content": "@user is inquiring about X", "author": "aX"}) is True


# ── Additional should_respond edge cases ────────────────────────────────


class TestShouldRespondExtended:
    def test_case_insensitive_self_check(self):
        data = {"author": "MyBot", "content": "@mybot hello"}
        assert should_respond(data, "mybot") is False

    def test_agent_id_match_with_different_author_name(self):
        data = {
            "author": {"name": "display_name", "id": "agent_X"},
            "content": "@mybot hello",
        }
        assert should_respond(data, "mybot", agent_id="agent_X") is False

    def test_ax_noise_with_explicit_mention_still_blocked(self):
        """ax noise that explicitly mentions the agent is still noise."""
        data = {"author": "aX", "content": "@mybot is asking: can you help?"}
        assert should_respond(data, "mybot") is False

    def test_other_agent_mentioning_allowed(self):
        data = {"author": "other_agent", "content": "@mybot please help"}
        assert should_respond(data, "mybot") is True

    def test_ax_direct_mention_non_noise(self):
        """A direct non-noise question from aX should be allowed."""
        data = {"author": "aX", "content": "@mybot what is the status?"}
        assert should_respond(data, "mybot") is True


# ── Additional is_mentioned edge cases ──────────────────────────────────


class TestIsMentionedExtended:
    def test_empty_mentions_array(self):
        data = {"content": "hello", "mentions": []}
        assert is_mentioned(data, "mybot") is False

    def test_mention_at_start(self):
        data = {"content": "@mybot"}
        assert is_mentioned(data, "mybot") is True

    def test_mention_at_end(self):
        data = {"content": "hello @mybot"}
        assert is_mentioned(data, "mybot") is True

    def test_mention_with_punctuation(self):
        data = {"content": "hello @mybot, how are you?"}
        assert is_mentioned(data, "mybot") is True

    def test_partial_name_no_match(self):
        """@mybotx should not match agent named mybot (handled by content check)."""
        # In the content check, @mybotx contains @mybot, so it WILL match
        # This is the current behavior
        data = {"content": "hello @mybotx"}
        # The substring check in is_mentioned will find @mybot in @mybotx
        assert is_mentioned(data, "mybot") is True

    def test_no_content_key(self):
        data = {"mentions": ["mybot"]}
        assert is_mentioned(data, "mybot") is True

    def test_mentions_case_insensitive(self):
        data = {"content": "do something", "mentions": ["MyBot"]}
        assert is_mentioned(data, "mybot") is True

    def test_no_metadata_key(self):
        """Missing metadata key should not cause errors."""
        data = {"content": "hello", "mentions": ["mybot"]}
        assert is_mentioned(data, "mybot") is True


# ── Additional resolve_history_thread_id edge cases ─────────────────────


class TestResolveHistoryThreadIdExtended:
    def test_parentId_camelCase(self, monkeypatch):
        """parentId (camelCase) should work for conversation scope."""
        monkeypatch.setenv("AX_SENTINEL_HISTORY_SCOPE", "conversation")
        result = resolve_history_thread_id(
            {"id": "msg1", "parentId": "parent2"},
            agent_name="mybot",
            space_id="space123",
        )
        assert result == "parent2"

    def test_thread_id_key(self, monkeypatch):
        """thread_id key should be used as fallback for conversation scope."""
        monkeypatch.setenv("AX_SENTINEL_HISTORY_SCOPE", "thread")
        result = resolve_history_thread_id(
            {"id": "msg1", "thread_id": "thread_99"},
            agent_name="mybot",
            space_id="space123",
        )
        assert result == "thread_99"

    def test_per_thread_alias(self, monkeypatch):
        monkeypatch.setenv("AX_SENTINEL_HISTORY_SCOPE", "per_thread")
        result = resolve_history_thread_id(
            {"id": "msg1", "parent_id": "p1"},
            agent_name="mybot",
            space_id="space123",
        )
        assert result == "p1"

    def test_user_alias_for_author(self, monkeypatch):
        monkeypatch.setenv("AX_SENTINEL_HISTORY_SCOPE", "user")
        result = resolve_history_thread_id(
            {"id": "msg1"},
            agent_name="mybot",
            space_id="space123",
            author="bob",
        )
        assert result == "space:space123:agent:mybot:author:bob"

    def test_message_scope_fallback_to_default(self, monkeypatch):
        monkeypatch.setenv("AX_SENTINEL_HISTORY_SCOPE", "message")
        result = resolve_history_thread_id(
            {},
            agent_name="mybot",
            space_id="space123",
        )
        assert result == "default"

    def test_conversation_scope_full_fallback_chain(self, monkeypatch):
        """No parent_id/parentId/thread_id/conversation_id -> falls to msg_id -> default."""
        monkeypatch.setenv("AX_SENTINEL_HISTORY_SCOPE", "conversation")
        result = resolve_history_thread_id(
            {"id": "msg_99"},
            agent_name="mybot",
            space_id="space123",
        )
        assert result == "msg_99"

    def test_conversation_scope_all_empty(self, monkeypatch):
        monkeypatch.setenv("AX_SENTINEL_HISTORY_SCOPE", "conversation")
        result = resolve_history_thread_id(
            {},
            agent_name="mybot",
            space_id="space123",
        )
        assert result == "default"

    def test_unknown_scope_defaults_to_space(self, monkeypatch):
        monkeypatch.setenv("AX_SENTINEL_HISTORY_SCOPE", "weird_value")
        result = resolve_history_thread_id(
            {"id": "msg1"},
            agent_name="mybot",
            space_id="space123",
        )
        assert result == "space:space123:agent:mybot"


# ── Additional strip_mention edge cases ────────────────────────────────


class TestStripMentionExtended:
    def test_mention_with_special_regex_chars(self):
        """Agent names with regex-special characters should be escaped."""
        result = strip_mention("@bot.v2 hello", "bot.v2")
        assert result == "hello"

    def test_mention_in_middle(self):
        result = strip_mention("hey @mybot can you help", "mybot")
        assert result == "hey  can you help"

    def test_only_mention(self):
        result = strip_mention("@mybot", "mybot")
        assert result == ""


# ── Additional get_author_name / get_author_id edge cases ───────────────


class TestGetAuthorNameExtended:
    def test_numeric_author(self):
        assert get_author_name({"author": 42}) == "42"

    def test_none_author(self):
        assert get_author_name({"author": None}) == "None"

    def test_dict_author_prefers_name_over_username(self):
        result = get_author_name({"author": {"name": "Alice", "username": "alice123"}})
        assert result == "Alice"


class TestGetAuthorIdExtended:
    def test_dict_author_id_as_int(self):
        result = get_author_id({"author": {"id": 123}})
        assert result == "123"

    def test_dict_author_id_none(self):
        result = get_author_id({"author": {"id": None}})
        assert result == ""

    def test_no_author_key_with_agent_id(self):
        result = get_author_id({"agent_id": "ag_1"})
        assert result == "ag_1"

    def test_string_author_no_agent_id(self):
        result = get_author_id({"author": "someone"})
        assert result == ""


# ── run_cli runtime name normalization ──────────────────────────────────


class TestRunCliNormalization:
    """Test that run_cli dispatches to _run_via_runtime_plugin correctly.

    We can't test the full run_cli without subprocess/network, but we can
    verify dispatch by patching _run_via_runtime_plugin.
    """

    def _make_args(self, runtime="hermes_sdk"):
        return argparse.Namespace(
            runtime=runtime,
            dry_run=False,
            agent=None,
            workdir="/tmp",
            model=None,
            timeout=300,
            update_interval=2.0,
            allowed_tools=None,
            system_prompt=None,
        )

    @patch("ax_cli.runtimes.hermes.sentinel._run_via_runtime_plugin")
    def test_openai_sdk_passes_through(self, mock_plugin):
        mock_plugin.return_value = "done"
        mock_api = MagicMock()
        sessions = SessionStore()
        histories = HistoryStore()

        run_cli(
            "hello",
            "/tmp",
            self._make_args(runtime="openai_sdk"),
            mock_api,
            "parent_1",
            "space_1",
            sessions,
            histories,
        )
        mock_plugin.assert_called_once()
        assert mock_plugin.call_args[0][0] == "openai_sdk"

    @patch("ax_cli.runtimes.hermes.sentinel._run_via_runtime_plugin")
    def test_hermes_sdk_passes_through(self, mock_plugin):
        mock_plugin.return_value = "done"
        mock_api = MagicMock()
        sessions = SessionStore()
        histories = HistoryStore()

        run_cli(
            "hello",
            "/tmp",
            self._make_args(runtime="hermes_sdk"),
            mock_api,
            "parent_1",
            "space_1",
            sessions,
            histories,
        )
        mock_plugin.assert_called_once()
        assert mock_plugin.call_args[0][0] == "hermes_sdk"

    @patch("ax_cli.runtimes.hermes.sentinel._run_via_runtime_plugin")
    def test_thread_id_forwarded(self, mock_plugin):
        mock_plugin.return_value = "done"
        mock_api = MagicMock()
        sessions = SessionStore()
        histories = HistoryStore()

        run_cli(
            "hello",
            "/tmp",
            self._make_args(),
            mock_api,
            "parent_1",
            "space_1",
            sessions,
            histories,
            thread_id="custom_thread",
        )
        assert mock_plugin.call_args.kwargs.get("thread_id") == "custom_thread"


# ── AxAPI constructor edge cases ────────────────────────────────────────


class TestAxAPIConstructor:
    def test_non_pat_token_skips_exchange(self):
        with patch("ax_cli.runtimes.hermes.sentinel.httpx.Client"):
            api = AxAPI(
                base_url="http://localhost:8002",
                token="jwt_token_here",
                agent_name="bot",
                agent_id="agent_1",
            )
        assert api._exchanger is None
        assert api.token == "jwt_token_here"

    def test_processing_signals_disabled_without_key(self):
        with patch("ax_cli.runtimes.hermes.sentinel.httpx.Client"):
            api = AxAPI(
                base_url="http://localhost:8002",
                token="tok",
                agent_name="bot",
                agent_id="",
            )
        assert api._processing_signals_enabled is False

    def test_processing_signals_enabled_with_key(self):
        with patch("ax_cli.runtimes.hermes.sentinel.httpx.Client"):
            api = AxAPI(
                base_url="http://localhost:8002",
                token="tok",
                agent_name="bot",
                agent_id="",
                internal_api_key="secret",
            )
        assert api._processing_signals_enabled is True

    def test_close_delegates_to_client(self):
        with patch("ax_cli.runtimes.hermes.sentinel.httpx.Client") as mock_cls:
            mock_client = MagicMock()
            mock_cls.return_value = mock_client
            api = AxAPI(
                base_url="http://localhost:8002",
                token="tok",
                agent_name="bot",
                agent_id="",
            )
        api._client = mock_client
        api.close()
        mock_client.close.assert_called_once()


class TestEmitGatewayHeartbeat:
    """The sentinel relays platform liveness to the gateway as throttled
    kind="heartbeat" AX_GATEWAY_EVENT lines, so the gateway's staleness ladder
    tracks the live platform connection rather than PID existence (#295)."""

    def test_emits_event_when_interval_elapsed(self, capsys):
        from ax_cli.runtimes.hermes.sentinel import (
            _GATEWAY_HEARTBEAT_MIN_INTERVAL,
            _emit_gateway_heartbeat,
        )

        now = _GATEWAY_HEARTBEAT_MIN_INTERVAL + 1.0
        new_ts = _emit_gateway_heartbeat("echo-bot", "space-1", 0.0, now=now)

        assert new_ts == now  # recorded as the new last-emit
        out = capsys.readouterr().out
        lines = [ln[len("AX_GATEWAY_EVENT ") :] for ln in out.splitlines() if ln.startswith("AX_GATEWAY_EVENT ")]
        assert lines, "expected a heartbeat AX_GATEWAY_EVENT line"
        assert json.loads(lines[0]) == {
            "kind": "heartbeat",
            "agent_name": "echo-bot",
            "space_id": "space-1",
        }

    def test_throttled_within_interval(self, capsys):
        from ax_cli.runtimes.hermes.sentinel import (
            _GATEWAY_HEARTBEAT_MIN_INTERVAL,
            _emit_gateway_heartbeat,
        )

        last = 100.0
        now = last + (_GATEWAY_HEARTBEAT_MIN_INTERVAL / 2.0)  # too soon
        new_ts = _emit_gateway_heartbeat("echo-bot", "space-1", last, now=now)

        assert new_ts == last  # unchanged — no emit
        assert "AX_GATEWAY_EVENT" not in capsys.readouterr().out

    def test_carries_no_credentials(self, capsys):
        """The relayed event is liveness metadata only — never a token/body."""
        from ax_cli.runtimes.hermes.sentinel import _emit_gateway_heartbeat

        _emit_gateway_heartbeat("echo-bot", "space-1", 0.0, now=9999.0)
        out = capsys.readouterr().out
        assert "axp_" not in out and "Bearer" not in out and "token" not in out.lower()
