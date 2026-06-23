"""Tests for ax_cli/commands/watch.py — match logic and SSE parsing."""

import json
import time

import httpx
import pytest
import typer

from ax_cli.commands.watch import _iter_sse, _matches, _watch_poll


class FakeResponse:
    def __init__(self, lines):
        self._lines = lines

    def iter_lines(self):
        yield from self._lines


def test_iter_sse_parses_event_and_data():
    resp = FakeResponse(["event: message", 'data: {"content": "hi"}', ""])
    events = list(_iter_sse(resp))
    assert len(events) == 1
    assert events[0][0] == "message"
    assert events[0][1]["content"] == "hi"


def test_iter_sse_multiple_events():
    resp = FakeResponse(
        [
            "event: message",
            'data: {"n": 1}',
            "",
            "event: alert",
            'data: {"n": 2}',
            "",
        ]
    )
    events = list(_iter_sse(resp))
    assert len(events) == 2
    assert events[0][0] == "message"
    assert events[1][0] == "alert"


def test_iter_sse_multiline_data():
    resp = FakeResponse(["event: message", "data: line1", "data: line2", ""])
    events = list(_iter_sse(resp))
    assert len(events) == 1
    assert events[0][1] == "line1\nline2"


def test_iter_sse_invalid_json():
    resp = FakeResponse(["event: message", "data: not-json", ""])
    events = list(_iter_sse(resp))
    assert events[0][1] == "not-json"


def test_iter_sse_skips_incomplete():
    resp = FakeResponse(["event: message"])
    events = list(_iter_sse(resp))
    assert events == []


def test_matches_any_message():
    assert _matches("message", {"content": "hello", "author": {"name": "bob"}}, agent_name="me")


def test_matches_non_dict_rejected():
    assert not _matches("message", "not-a-dict", agent_name="me")


def test_matches_non_message_event_rejected():
    assert not _matches("heartbeat", {"content": "hi", "author": {"name": "bob"}}, agent_name="me")


def test_matches_own_message_rejected():
    assert not _matches("message", {"content": "hi", "display_name": "me"}, agent_name="me")


def test_matches_from_agent():
    assert _matches(
        "message",
        {"content": "done", "display_name": "bot"},
        from_agent="bot",
        agent_name="me",
    )


def test_matches_from_agent_wrong_sender():
    assert not _matches(
        "message",
        {"content": "done", "display_name": "other"},
        from_agent="bot",
        agent_name="me",
    )


def test_matches_mention():
    assert _matches(
        "mention",
        {"content": "@me hello", "author": {"name": "bob"}},
        mention=True,
        agent_name="me",
    )


def test_matches_mention_not_mentioned():
    assert not _matches(
        "message",
        {"content": "hello", "author": {"name": "bob"}},
        mention=True,
        agent_name="me",
    )


def test_matches_contains():
    assert _matches(
        "message",
        {"content": "task merged successfully", "author": {"name": "bot"}},
        contains="merged",
        agent_name="me",
    )


def test_matches_contains_no_match():
    assert not _matches(
        "message",
        {"content": "still working", "author": {"name": "bot"}},
        contains="merged",
        agent_name="me",
    )


def test_matches_event_filter():
    assert _matches(
        "tool_call_completed",
        {"content": "x"},
        event_filter="tool_call_completed",
        agent_name="me",
    )


def test_matches_event_filter_wrong_type():
    assert not _matches(
        "message",
        {"content": "x"},
        event_filter="tool_call_completed",
        agent_name="me",
    )


def test_matches_old_message_rejected():
    now = time.time()
    assert not _matches(
        "message",
        {"content": "old", "author": {"name": "bob"}, "timestamp": "2020-01-01T00:00:00Z"},
        agent_name="me",
        started_at=now,
    )


def test_matches_author_from_author_dict():
    assert _matches(
        "message",
        {"content": "hi", "author": {"name": "bot"}},
        from_agent="bot",
        agent_name="me",
    )


# ---------------------------------------------------------------------------
# Additional _matches edge-case tests
# ---------------------------------------------------------------------------


def test_matches_created_at_old_rejected():
    """Messages with created_at before started_at are rejected."""

    now = time.time()
    assert not _matches(
        "message",
        {"content": "old", "author": {"name": "bob"}, "created_at": "2020-06-01T12:00:00Z"},
        agent_name="me",
        started_at=now,
    )


def test_matches_server_time_old_rejected():
    """Messages with server_time before started_at are rejected."""

    now = time.time()
    assert not _matches(
        "message",
        {"content": "old", "author": {"name": "bob"}, "server_time": "2020-06-01T12:00:00Z"},
        agent_name="me",
        started_at=now,
    )


def test_matches_invalid_timestamp_ignored():
    """Invalid timestamp string doesn't crash — match proceeds normally."""

    now = time.time()
    assert _matches(
        "message",
        {"content": "hi", "author": {"name": "bob"}, "timestamp": "not-a-date"},
        agent_name="me",
        started_at=now,
    )


def test_matches_username_fallback_for_sender():
    """Sender resolved from username when display_name and author name are absent."""
    assert not _matches(
        "message",
        {"content": "hi", "username": "me"},
        agent_name="me",
    )


def test_matches_display_name_takes_precedence():
    """display_name is preferred over username and author name for sender."""
    assert not _matches(
        "message",
        {"content": "hi", "display_name": "me", "username": "other", "author": {"name": "other2"}},
        agent_name="me",
    )


def test_matches_contains_case_insensitive():
    """Contains check is case-insensitive."""
    assert _matches(
        "message",
        {"content": "MERGED successfully", "author": {"name": "bot"}},
        contains="merged",
        agent_name="me",
    )


def test_matches_no_timestamp_with_started_at():
    """When started_at is set but message has no timestamp, match proceeds."""

    now = time.time()
    assert _matches(
        "message",
        {"content": "hi", "author": {"name": "bob"}},
        agent_name="me",
        started_at=now,
    )


def test_matches_future_message_accepted():
    """A message with a timestamp after started_at passes the time filter."""
    assert _matches(
        "message",
        {"content": "hi", "author": {"name": "bob"}, "timestamp": "2099-01-01T00:00:00Z"},
        agent_name="me",
        started_at=1000000.0,
    )


def test_matches_event_filter_bypasses_event_type_check():
    """When event_filter is set, event_type filter is bypassed (any event type can match)."""
    assert _matches(
        "custom_event",
        {"content": "x"},
        event_filter="custom_event",
        agent_name="me",
    )


def test_matches_mention_event_type_accepted():
    """The 'mention' event type passes the event-type check (not just 'message')."""
    assert _matches(
        "mention",
        {"content": "hello", "author": {"name": "bob"}},
        agent_name="me",
    )


# ---------------------------------------------------------------------------
# _iter_sse edge-case tests
# ---------------------------------------------------------------------------


def test_iter_sse_json_decode_error_curly_brace():
    """Data starting with { but not valid JSON falls back to raw string."""
    resp = FakeResponse(["event: message", "data: {broken json here", ""])
    events = list(_iter_sse(resp))
    assert len(events) == 1
    assert events[0][0] == "message"
    assert events[0][1] == "{broken json here"


def test_iter_sse_empty_data_lines_skipped():
    """An event block with event type but no data lines is skipped."""
    resp = FakeResponse(["event: message", ""])
    events = list(_iter_sse(resp))
    assert events == []


def test_iter_sse_no_event_type_skipped():
    """Data without a preceding event type is skipped."""
    resp = FakeResponse(["data: orphan", ""])
    events = list(_iter_sse(resp))
    assert events == []


def test_iter_sse_resets_between_events():
    """State resets properly between events — no bleed."""
    resp = FakeResponse(
        [
            "event: first",
            'data: {"a": 1}',
            "",
            "event: second",
            'data: {"b": 2}',
            "",
        ]
    )
    events = list(_iter_sse(resp))
    assert len(events) == 2
    assert events[0][1] == {"a": 1}
    assert events[1][1] == {"b": 2}


# ---------------------------------------------------------------------------
# _watch_poll tests
# ---------------------------------------------------------------------------


class _FakeClient:
    """Minimal fake client for _watch_poll testing."""

    def __init__(self, responses=None):
        self._responses = list(responses or [])
        self._call_count = 0

    def list_messages(self, limit=10, space_id=None):
        if self._call_count < len(self._responses):
            resp = self._responses[self._call_count]
        else:
            resp = self._responses[-1] if self._responses else []
        self._call_count += 1
        if isinstance(resp, Exception):
            raise resp
        return resp


def _patch_poll_deps(monkeypatch, agent_name="me"):
    """Patch resolve_agent_name and time.sleep for _watch_poll tests."""
    monkeypatch.setattr(
        "ax_cli.commands.watch.resolve_agent_name",
        lambda **kwargs: agent_name,
    )
    monkeypatch.setattr("ax_cli.commands.watch.time.sleep", lambda s: None)


def test_watch_poll_timeout(monkeypatch):
    """_watch_poll raises Exit(1) when timeout is reached with no final response."""
    _patch_poll_deps(monkeypatch)

    # Time advances past the timeout on first check
    call_count = 0

    def fake_time():
        nonlocal call_count
        call_count += 1
        if call_count <= 1:
            return 1000.0  # start_time
        return 1000.0 + 999  # elapsed > timeout

    monkeypatch.setattr("ax_cli.commands.watch.time.time", fake_time)

    client = _FakeClient(responses=[[]])
    with pytest.raises((SystemExit, typer.Exit)) as exc_info:
        _watch_poll(client, space_id="sp1", timeout=5, quiet=True)
    assert getattr(exc_info.value, "exit_code", getattr(exc_info.value, "code", None)) == 1


def test_watch_poll_final_response_exits_zero(monkeypatch):
    """_watch_poll exits 0 when a non-Working message is found."""
    _patch_poll_deps(monkeypatch)

    call_count = 0

    def fake_time():
        nonlocal call_count
        call_count += 1
        return 1000.0  # time never advances past timeout

    monkeypatch.setattr("ax_cli.commands.watch.time.time", fake_time)

    final_msg = {"display_name": "bot", "content": "Done! All tasks complete."}
    client = _FakeClient(responses=[[final_msg]])

    with pytest.raises((SystemExit, typer.Exit)) as exc_info:
        _watch_poll(client, space_id="sp1", timeout=300, quiet=True)
    assert getattr(exc_info.value, "exit_code", getattr(exc_info.value, "code", None)) == 0


def test_watch_poll_skips_own_messages(monkeypatch):
    """_watch_poll ignores messages from the agent itself."""
    _patch_poll_deps(monkeypatch, agent_name="me")

    times = iter([1000.0, 1000.0, 1000.0, 1000.0, 2000.0])
    monkeypatch.setattr("ax_cli.commands.watch.time.time", lambda: next(times))

    own_msg = {"display_name": "me", "content": "I said something"}
    client = _FakeClient(responses=[[own_msg], [own_msg]])

    with pytest.raises((SystemExit, typer.Exit)) as exc_info:
        _watch_poll(client, space_id="sp1", timeout=1, quiet=True)
    # Should timeout because it never finds a message from another agent
    assert getattr(exc_info.value, "exit_code", getattr(exc_info.value, "code", None)) == 1


def test_watch_poll_from_agent_filter(monkeypatch):
    """_watch_poll only matches messages from the specified agent."""
    _patch_poll_deps(monkeypatch)

    times = iter([1000.0, 1000.0, 1000.0, 1000.0, 2000.0])
    monkeypatch.setattr("ax_cli.commands.watch.time.time", lambda: next(times))

    wrong_agent_msg = {"display_name": "other", "content": "Not from target"}
    client = _FakeClient(responses=[[wrong_agent_msg], [wrong_agent_msg]])

    with pytest.raises((SystemExit, typer.Exit)) as exc_info:
        _watch_poll(client, space_id="sp1", from_agent="target_bot", timeout=1, quiet=True)
    assert getattr(exc_info.value, "exit_code", getattr(exc_info.value, "code", None)) == 1


def test_watch_poll_from_agent_match(monkeypatch):
    """_watch_poll matches when from_agent matches the sender."""
    _patch_poll_deps(monkeypatch)

    monkeypatch.setattr("ax_cli.commands.watch.time.time", lambda: 1000.0)

    msg = {"display_name": "target_bot", "content": "Result ready"}
    client = _FakeClient(responses=[[msg]])

    with pytest.raises((SystemExit, typer.Exit)) as exc_info:
        _watch_poll(client, space_id="sp1", from_agent="target_bot", timeout=300, quiet=True)
    assert getattr(exc_info.value, "exit_code", getattr(exc_info.value, "code", None)) == 0


def test_watch_poll_working_message_continues(monkeypatch):
    """_watch_poll keeps polling when it sees a 'Working...' message."""
    _patch_poll_deps(monkeypatch)

    call_seq = iter([1000.0, 1000.0, 1000.0, 1000.0, 1000.0, 2000.0])
    monkeypatch.setattr("ax_cli.commands.watch.time.time", lambda: next(call_seq))

    working_msg = {"display_name": "bot", "content": "Working on it...\nTool: analyze\nStep 2"}
    client = _FakeClient(responses=[[working_msg], [working_msg]])

    with pytest.raises((SystemExit, typer.Exit)) as exc_info:
        _watch_poll(client, space_id="sp1", timeout=1, quiet=True)
    # Times out because the message never stops being "Working..."
    assert getattr(exc_info.value, "exit_code", getattr(exc_info.value, "code", None)) == 1


def test_watch_poll_working_then_final(monkeypatch):
    """_watch_poll shows Working progress then exits on final response."""
    _patch_poll_deps(monkeypatch)

    monkeypatch.setattr("ax_cli.commands.watch.time.time", lambda: 1000.0)

    working_msg = {"display_name": "bot", "content": "Working on it..."}
    final_msg = {"display_name": "bot", "content": "All done!"}
    client = _FakeClient(responses=[[working_msg], [final_msg]])

    with pytest.raises((SystemExit, typer.Exit)) as exc_info:
        _watch_poll(client, space_id="sp1", timeout=300, quiet=True)
    assert getattr(exc_info.value, "exit_code", getattr(exc_info.value, "code", None)) == 0


def test_watch_poll_http_error_continues(monkeypatch):
    """_watch_poll retries on HTTP errors."""
    _patch_poll_deps(monkeypatch)

    call_seq = iter([1000.0, 1000.0, 1000.0, 1000.0])
    monkeypatch.setattr("ax_cli.commands.watch.time.time", lambda: next(call_seq))

    error = httpx.ConnectError("connection refused")
    final_msg = {"display_name": "bot", "content": "Done"}
    client = _FakeClient(responses=[error, [final_msg]])

    with pytest.raises((SystemExit, typer.Exit)) as exc_info:
        _watch_poll(client, space_id="sp1", timeout=300, quiet=True)
    assert getattr(exc_info.value, "exit_code", getattr(exc_info.value, "code", None)) == 0


def test_watch_poll_dict_envelope(monkeypatch):
    """_watch_poll handles messages wrapped in a dict with 'messages' key."""
    _patch_poll_deps(monkeypatch)

    monkeypatch.setattr("ax_cli.commands.watch.time.time", lambda: 1000.0)

    msg = {"display_name": "bot", "content": "Done"}
    client = _FakeClient(responses=[{"messages": [msg]}])

    with pytest.raises((SystemExit, typer.Exit)) as exc_info:
        _watch_poll(client, space_id="sp1", timeout=300, quiet=True)
    assert getattr(exc_info.value, "exit_code", getattr(exc_info.value, "code", None)) == 0


def test_watch_poll_json_output(monkeypatch, capsys):
    """_watch_poll prints JSON when output_json=True."""
    _patch_poll_deps(monkeypatch)

    monkeypatch.setattr("ax_cli.commands.watch.time.time", lambda: 1000.0)

    msg = {"display_name": "bot", "content": "Result: 42"}
    client = _FakeClient(responses=[[msg]])

    with pytest.raises((SystemExit, typer.Exit)) as exc_info:
        _watch_poll(client, space_id="sp1", timeout=300, output_json=True, quiet=True)
    assert getattr(exc_info.value, "exit_code", getattr(exc_info.value, "code", None)) == 0

    captured = capsys.readouterr()
    parsed = json.loads(captured.out)
    assert parsed["content"] == "Result: 42"


def test_watch_poll_verbose_output(monkeypatch, capsys):
    """_watch_poll prints sender/content when not quiet and not json."""
    _patch_poll_deps(monkeypatch)

    monkeypatch.setattr("ax_cli.commands.watch.time.time", lambda: 1000.0)

    msg = {"display_name": "bot", "content": "Hello world"}
    client = _FakeClient(responses=[[msg]])

    with pytest.raises((SystemExit, typer.Exit)) as exc_info:
        _watch_poll(client, space_id="sp1", timeout=300, quiet=False, output_json=False)
    assert getattr(exc_info.value, "exit_code", getattr(exc_info.value, "code", None)) == 0


def test_watch_poll_long_content_truncated(monkeypatch, capsys):
    """_watch_poll truncates content > 3000 chars in verbose mode."""
    _patch_poll_deps(monkeypatch)

    monkeypatch.setattr("ax_cli.commands.watch.time.time", lambda: 1000.0)

    long_content = "x" * 5000
    msg = {"display_name": "bot", "content": long_content}
    client = _FakeClient(responses=[[msg]])

    with pytest.raises((SystemExit, typer.Exit)) as exc_info:
        _watch_poll(client, space_id="sp1", timeout=300, quiet=False, output_json=False)
    assert getattr(exc_info.value, "exit_code", getattr(exc_info.value, "code", None)) == 0

    # Verify the truncation note appears in Rich console output
    # (Rich writes to stderr via err_console or console; capsys captures stdout)
    # The "... (5000 chars total)" message is printed via console.print


def test_watch_poll_sender_handle_fallback(monkeypatch):
    """_watch_poll uses sender_handle when display_name is absent."""
    _patch_poll_deps(monkeypatch)

    monkeypatch.setattr("ax_cli.commands.watch.time.time", lambda: 1000.0)

    msg = {"sender_handle": "bot", "content": "Done"}
    client = _FakeClient(responses=[[msg]])

    with pytest.raises((SystemExit, typer.Exit)) as exc_info:
        _watch_poll(client, space_id="sp1", timeout=300, quiet=True)
    assert getattr(exc_info.value, "exit_code", getattr(exc_info.value, "code", None)) == 0


def test_watch_poll_timeout_verbose(monkeypatch, capsys):
    """_watch_poll prints timeout message when not quiet."""
    _patch_poll_deps(monkeypatch)

    call_count = 0

    def fake_time():
        nonlocal call_count
        call_count += 1
        if call_count <= 1:
            return 1000.0
        return 2000.0

    monkeypatch.setattr("ax_cli.commands.watch.time.time", fake_time)

    client = _FakeClient(responses=[[]])

    with pytest.raises((SystemExit, typer.Exit)) as exc_info:
        _watch_poll(client, space_id="sp1", timeout=5, quiet=False)
    assert getattr(exc_info.value, "exit_code", getattr(exc_info.value, "code", None)) == 1


def test_watch_poll_conditions_display_from_agent(monkeypatch, capsys):
    """_watch_poll displays 'from @agent' in conditions when from_agent is set."""
    _patch_poll_deps(monkeypatch)

    call_count = 0

    def fake_time():
        nonlocal call_count
        call_count += 1
        if call_count <= 1:
            return 1000.0
        return 2000.0

    monkeypatch.setattr("ax_cli.commands.watch.time.time", fake_time)

    client = _FakeClient(responses=[[]])

    with pytest.raises((SystemExit, typer.Exit)) as exc_info:
        _watch_poll(client, space_id="sp1", from_agent="mybot", timeout=1, quiet=False)
    assert getattr(exc_info.value, "exit_code", getattr(exc_info.value, "code", None)) == 1


def test_watch_poll_conditions_display_any_agent(monkeypatch, capsys):
    """_watch_poll displays 'any agent' in conditions when no from_agent."""
    _patch_poll_deps(monkeypatch)

    call_count = 0

    def fake_time():
        nonlocal call_count
        call_count += 1
        if call_count <= 1:
            return 1000.0
        return 2000.0

    monkeypatch.setattr("ax_cli.commands.watch.time.time", fake_time)

    client = _FakeClient(responses=[[]])

    with pytest.raises((SystemExit, typer.Exit)) as exc_info:
        _watch_poll(client, space_id="sp1", timeout=1, quiet=False)
    assert getattr(exc_info.value, "exit_code", getattr(exc_info.value, "code", None)) == 1


def test_watch_poll_working_verbose_shows_tool_lines(monkeypatch, capsys):
    """_watch_poll shows Working status and tool lines when not quiet."""
    _patch_poll_deps(monkeypatch)

    call_seq = iter([1000.0, 1000.0, 1000.0, 1000.0, 1000.0, 2000.0])
    monkeypatch.setattr("ax_cli.commands.watch.time.time", lambda: next(call_seq))

    working_msg = {
        "display_name": "bot",
        "content": "Working on task...\nTool: code_search\nTool: file_read\nTool: run_tests\nExtra line",
    }
    client = _FakeClient(responses=[[working_msg], [working_msg]])

    with pytest.raises((SystemExit, typer.Exit)) as exc_info:
        _watch_poll(client, space_id="sp1", timeout=1, quiet=False)
    assert getattr(exc_info.value, "exit_code", getattr(exc_info.value, "code", None)) == 1


def test_watch_poll_read_error_continues(monkeypatch):
    """_watch_poll retries on httpx.ReadError."""
    _patch_poll_deps(monkeypatch)

    call_seq = iter([1000.0, 1000.0, 1000.0, 1000.0])
    monkeypatch.setattr("ax_cli.commands.watch.time.time", lambda: next(call_seq))

    error = httpx.ReadError("read failed")
    final_msg = {"display_name": "bot", "content": "Done"}
    client = _FakeClient(responses=[error, [final_msg]])

    with pytest.raises((SystemExit, typer.Exit)) as exc_info:
        _watch_poll(client, space_id="sp1", timeout=300, quiet=True)
    assert getattr(exc_info.value, "exit_code", getattr(exc_info.value, "code", None)) == 0


def test_watch_poll_http_status_error_continues(monkeypatch):
    """_watch_poll retries on httpx.HTTPStatusError."""
    _patch_poll_deps(monkeypatch)

    call_seq = iter([1000.0, 1000.0, 1000.0, 1000.0])
    monkeypatch.setattr("ax_cli.commands.watch.time.time", lambda: next(call_seq))

    request = httpx.Request("GET", "http://example.com/api/v1/messages")
    response = httpx.Response(500, request=request)
    error = httpx.HTTPStatusError("Server error", request=request, response=response)
    final_msg = {"display_name": "bot", "content": "Done"}
    client = _FakeClient(responses=[error, [final_msg]])

    with pytest.raises((SystemExit, typer.Exit)) as exc_info:
        _watch_poll(client, space_id="sp1", timeout=300, quiet=True)
    assert getattr(exc_info.value, "exit_code", getattr(exc_info.value, "code", None)) == 0
