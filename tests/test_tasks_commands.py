import json

from typer.testing import CliRunner

from ax_cli.commands.tasks import (
    _agent_items as task_agent_items,
)
from ax_cli.commands.tasks import (
    _agent_names,
    _annotate_task_space,
    _resolve_assignee_id,
    _space_summary,
)
from ax_cli.commands.tasks import (
    _space_items as task_space_items,
)
from ax_cli.main import app

runner = CliRunner()


def test_tasks_create_assign_accepts_agent_handle(monkeypatch):
    calls = {}

    class FakeClient:
        def list_agents(self, *, space_id=None, limit=None):
            calls["list_agents"] = {"space_id": space_id, "limit": limit}
            return {
                "agents": [
                    {"id": "agent-123", "name": "demo-agent"},
                    {"id": "agent-456", "name": "cipher"},
                ]
            }

        def create_task(self, space_id, title, *, description=None, priority="medium", assignee_id=None):
            calls["create_task"] = {
                "space_id": space_id,
                "title": title,
                "description": description,
                "priority": priority,
                "assignee_id": assignee_id,
            }
            return {"task": {"id": "task-1", "title": title, "assignee_id": assignee_id, "priority": priority}}

    monkeypatch.setattr("ax_cli.commands.tasks.get_client", lambda: FakeClient())
    monkeypatch.setattr("ax_cli.commands.tasks.resolve_space_id", lambda client, explicit=None: "space-1")

    result = runner.invoke(
        app,
        ["tasks", "create", "Review the spec", "--assign", "@demo-agent", "--no-notify", "--json"],
    )

    assert result.exit_code == 0, result.output
    assert calls["list_agents"] == {"space_id": "space-1", "limit": 500}
    assert calls["create_task"]["assignee_id"] == "agent-123"


def test_tasks_create_accepts_space_slug(monkeypatch):
    calls = {}

    class FakeClient:
        def list_spaces(self):
            return {
                "spaces": [
                    {"id": "private-space", "slug": "madtank-workspace", "name": "madtank's Workspace"},
                    {"id": "team-space", "slug": "ax-cli-dev", "name": "ax-cli-dev"},
                ]
            }

        def create_task(self, space_id, title, *, description=None, priority="medium", assignee_id=None):
            calls["create_task"] = {"space_id": space_id, "title": title}
            return {"task": {"id": "task-1", "title": title, "priority": priority}}

    monkeypatch.setattr("ax_cli.commands.tasks.get_client", lambda: FakeClient())

    result = runner.invoke(
        app,
        ["tasks", "create", "Fix routing", "--space", "ax-cli-dev", "--no-notify", "--json"],
    )

    assert result.exit_code == 0, result.output
    assert calls["create_task"]["space_id"] == "team-space"
    payload = json.loads(result.output)
    assert payload["space_id"] == "team-space"
    assert payload["space_slug"] == "ax-cli-dev"


def test_tasks_create_uses_gateway_local_identity(monkeypatch):
    calls = {}

    monkeypatch.setattr(
        "ax_cli.commands.tasks.resolve_gateway_config",
        lambda: {
            "url": "http://127.0.0.1:8765",
            "agent_name": "codex-pass-through",
            "registry_ref": None,
            "workdir": "/repo",
            "space_id": "space-from-config",
        },
    )
    monkeypatch.setattr(
        "ax_cli.commands.tasks._gateway_local_connect",
        lambda **kwargs: {
            "status": "approved",
            "session_token": "session-123",
            "registry_ref": "#5",
            "agent": {"name": "codex-pass-through"},
        },
    )

    class FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {"task": {"id": "task-1", "title": "Lock specs", "priority": "high"}}

    def fake_post(url, *, json=None, headers=None, timeout=None):
        calls["post"] = {"url": url, "json": json, "headers": headers, "timeout": timeout}
        return FakeResponse()

    monkeypatch.setattr("ax_cli.commands.tasks.httpx.post", fake_post)

    result = runner.invoke(
        app,
        [
            "tasks",
            "create",
            "Lock specs",
            "--description",
            "Make Gateway boring.",
            "--priority",
            "high",
            "--json",
        ],
    )

    assert result.exit_code == 0, result.output
    assert calls["post"]["url"] == "http://127.0.0.1:8765/local/tasks"
    assert calls["post"]["headers"]["X-Gateway-Session"] == "session-123"
    assert calls["post"]["json"] == {
        "title": "Lock specs",
        "description": "Make Gateway boring.",
        "priority": "high",
        "space_id": "space-from-config",
    }
    payload = json.loads(result.output)
    # Gateway create --json emits the flat task, not the {"task": {...}} envelope (#81).
    assert payload["id"] == "task-1"
    assert "task" not in payload


def test_tasks_create_human_output_includes_resolved_space(monkeypatch):
    class FakeClient:
        def list_spaces(self):
            return {"spaces": [{"id": "team-space", "slug": "ax-cli-dev", "name": "ax-cli-dev"}]}

        def create_task(self, space_id, title, *, description=None, priority="medium", assignee_id=None):
            return {"task": {"id": "task-1", "title": title, "priority": priority}}

    monkeypatch.setattr("ax_cli.commands.tasks.get_client", lambda: FakeClient())

    result = runner.invoke(
        app,
        ["tasks", "create", "Fix routing", "--space", "ax-cli-dev", "--no-notify"],
    )

    assert result.exit_code == 0, result.output
    assert "in ax-cli-dev (team-space)" in result.output


def test_tasks_create_assign_to_accepts_uuid_without_agent_lookup(monkeypatch):
    calls = {}
    agent_id = "bbbbbbbb-bbbb-4bbb-bbbb-bbbbbbbbbbbb"

    class FakeClient:
        def list_agents(self, *, space_id=None, limit=None):
            calls["list_agents"] = {"space_id": space_id, "limit": limit}
            return {"agents": []}

        def create_task(self, space_id, title, *, description=None, priority="medium", assignee_id=None):
            calls["create_task"] = {"assignee_id": assignee_id}
            return {"task": {"id": "task-1", "title": title, "assignee_id": assignee_id}}

    monkeypatch.setattr("ax_cli.commands.tasks.get_client", lambda: FakeClient())
    monkeypatch.setattr("ax_cli.commands.tasks.resolve_space_id", lambda client, explicit=None: "space-1")

    result = runner.invoke(
        app,
        ["tasks", "create", "Review the spec", "--assign-to", agent_id, "--no-notify", "--json"],
    )

    assert result.exit_code == 0, result.output
    assert "list_agents" not in calls
    assert calls["create_task"]["assignee_id"] == agent_id


def test_tasks_create_assign_unknown_handle_fails(monkeypatch):
    class FakeClient:
        def list_agents(self, *, space_id=None, limit=None):
            return {"agents": [{"id": "agent-456", "name": "cipher"}]}

    monkeypatch.setattr("ax_cli.commands.tasks.get_client", lambda: FakeClient())
    monkeypatch.setattr("ax_cli.commands.tasks.resolve_space_id", lambda client, explicit=None: "space-1")

    result = runner.invoke(
        app,
        ["tasks", "create", "Review the spec", "--assign", "demo-agent", "--no-notify"],
    )

    assert result.exit_code == 1
    assert "No visible agent found" in result.output


def test_tasks_create_mention_prefixes_notification(monkeypatch):
    calls = {}

    class FakeClient:
        def create_task(self, space_id, title, *, description=None, priority="medium", assignee_id=None):
            return {"task": {"id": "task-1", "title": title, "priority": priority}}

        def send_message(self, space_id, content, *, metadata=None, message_type="text"):
            calls["message"] = {
                "space_id": space_id,
                "content": content,
                "metadata": metadata,
                "message_type": message_type,
            }
            return {"id": "msg-1"}

    monkeypatch.setattr("ax_cli.commands.tasks.get_client", lambda: FakeClient())
    monkeypatch.setattr("ax_cli.commands.tasks.resolve_space_id", lambda client, explicit=None: "space-1")

    result = runner.invoke(
        app,
        ["tasks", "create", "Run smoke tests", "--mention", "cipher", "--json"],
    )

    assert result.exit_code == 0, result.output
    assert calls["message"]["space_id"] == "space-1"
    assert calls["message"]["content"].startswith("@cipher New task created:")
    assert calls["message"]["message_type"] == "system"
    metadata = calls["message"]["metadata"]
    assert metadata["ui"]["cards"][0]["type"] == "task"
    assert metadata["ui"]["cards"][0]["payload"]["source"] == "axctl_tasks_create"
    assert metadata["ui"]["widget"]["resource_uri"] == "ui://tasks/detail"
    assert metadata["ui"]["widget"]["initial_data"]["items"][0]["title"] == "Run smoke tests"


def test_tasks_create_assign_handle_mentions_assignee_by_default(monkeypatch):
    calls = {}

    class FakeClient:
        def list_agents(self, *, space_id=None, limit=None):
            return {"agents": [{"id": "agent-123", "name": "demo-agent"}]}

        def create_task(self, space_id, title, *, description=None, priority="medium", assignee_id=None):
            calls["create_task"] = {"assignee_id": assignee_id}
            return {"task": {"id": "task-1", "title": title, "priority": priority}}

        def send_message(self, space_id, content, *, metadata=None, message_type="text"):
            calls["message"] = {
                "space_id": space_id,
                "content": content,
                "metadata": metadata,
                "message_type": message_type,
            }
            return {"id": "msg-1"}

    monkeypatch.setattr("ax_cli.commands.tasks.get_client", lambda: FakeClient())
    monkeypatch.setattr("ax_cli.commands.tasks.resolve_space_id", lambda client, explicit=None: "space-1")

    result = runner.invoke(
        app,
        ["tasks", "create", "Run smoke tests", "--assign", "demo-agent", "--json"],
    )

    assert result.exit_code == 0, result.output
    assert calls["create_task"]["assignee_id"] == "agent-123"
    assert calls["message"]["content"].startswith("@demo-agent New task created:")
    assert calls["message"]["metadata"]["ui"]["cards"][0]["payload"]["assignee"] == {
        "id": "agent-123",
        "name": "demo-agent",
    }


def test_tasks_update_assign_to_accepts_uuid_without_lookup(monkeypatch):
    calls = {}
    agent_id = "bbbbbbbb-bbbb-4bbb-bbbb-bbbbbbbbbbbb"

    class FakeClient:
        def get_task(self, task_id):
            calls["get_task"] = task_id
            return {"id": task_id, "space_id": "space-1"}

        def list_agents(self, *, space_id=None, limit=None):
            calls["list_agents"] = True
            return {"agents": []}

        def update_task(self, task_id, **fields):
            calls["update_task"] = {"task_id": task_id, "fields": fields}
            return {"id": task_id, **fields}

    monkeypatch.setattr("ax_cli.commands.tasks.get_client", lambda: FakeClient())
    monkeypatch.setattr("ax_cli.commands.tasks.resolve_gateway_config", lambda: {})

    result = runner.invoke(
        app,
        ["tasks", "update", "task-42", "--assign-to", agent_id, "--json"],
    )

    assert result.exit_code == 0, result.output
    # UUID short-circuits — no get_task / list_agents needed.
    assert "get_task" not in calls
    assert "list_agents" not in calls
    assert calls["update_task"] == {"task_id": "task-42", "fields": {"assignee_id": agent_id}}


def test_tasks_update_assign_to_resolves_handle_via_task_space(monkeypatch):
    calls = {}

    class FakeClient:
        def get_task(self, task_id):
            calls["get_task"] = task_id
            return {"task": {"id": task_id, "space_id": "space-9"}}

        def list_agents(self, *, space_id=None, limit=None):
            calls["list_agents"] = {"space_id": space_id, "limit": limit}
            return {"agents": [{"id": "agent-789", "name": "demo-agent"}]}

        def update_task(self, task_id, **fields):
            calls["update_task"] = {"task_id": task_id, "fields": fields}
            return {"id": task_id, **fields}

    monkeypatch.setattr("ax_cli.commands.tasks.get_client", lambda: FakeClient())
    monkeypatch.setattr("ax_cli.commands.tasks.resolve_gateway_config", lambda: {})

    result = runner.invoke(
        app,
        ["tasks", "update", "task-42", "--assign", "@demo-agent", "--status", "in_progress", "--json"],
    )

    assert result.exit_code == 0, result.output
    assert calls["get_task"] == "task-42"
    assert calls["list_agents"] == {"space_id": "space-9", "limit": 500}
    assert calls["update_task"] == {
        "task_id": "task-42",
        "fields": {"status": "in_progress", "assignee_id": "agent-789"},
    }


def test_tasks_update_assign_to_uuid_through_gateway(monkeypatch):
    """UUID assignee_id forwards through the Gateway proxy without a handle lookup."""
    calls = []
    agent_id = "bbbbbbbb-bbbb-4bbb-bbbb-bbbbbbbbbbbb"

    monkeypatch.setattr(
        "ax_cli.commands.tasks.resolve_gateway_config",
        lambda: {"url": "http://127.0.0.1:8765", "agent_name": "wishy", "workdir": "/repo"},
    )

    def fake_gateway_local_call(*, gateway_cfg, method, args=None, **_):
        calls.append({"method": method, "args": dict(args or {})})
        if method == "update_task":
            return {"id": args["task_id"], **{k: v for k, v in args.items() if k != "task_id"}}
        raise AssertionError(f"unexpected proxy method: {method}")

    monkeypatch.setattr("ax_cli.commands.tasks._gateway_local_call", fake_gateway_local_call)

    result = runner.invoke(
        app,
        ["tasks", "update", "task-42", "--assign-to", agent_id, "--json"],
    )

    assert result.exit_code == 0, result.output
    # UUID short-circuits — no get_task / list_agents needed.
    assert [c["method"] for c in calls] == ["update_task"]
    assert calls[0]["args"] == {"task_id": "task-42", "assignee_id": agent_id}


def test_tasks_update_assign_to_handle_through_gateway(monkeypatch):
    """Handle assign-to resolves via Gateway-proxied get_task + list_agents, then forwards UUID."""
    calls = []

    monkeypatch.setattr(
        "ax_cli.commands.tasks.resolve_gateway_config",
        lambda: {"url": "http://127.0.0.1:8765", "agent_name": "wishy", "workdir": "/repo"},
    )

    def fake_gateway_local_call(*, gateway_cfg, method, args=None, **_):
        calls.append({"method": method, "args": dict(args or {})})
        if method == "get_task":
            return {"task": {"id": args["task_id"], "space_id": "space-9"}}
        if method == "list_agents":
            return {"agents": [{"id": "agent-789", "name": "demo-agent"}]}
        if method == "update_task":
            return {"id": args["task_id"], **{k: v for k, v in args.items() if k != "task_id"}}
        raise AssertionError(f"unexpected proxy method: {method}")

    monkeypatch.setattr("ax_cli.commands.tasks._gateway_local_call", fake_gateway_local_call)

    result = runner.invoke(
        app,
        ["tasks", "update", "task-42", "--assign", "@demo-agent", "--status", "in_progress", "--json"],
    )

    assert result.exit_code == 0, result.output
    assert [c["method"] for c in calls] == ["get_task", "list_agents", "update_task"]
    assert calls[0]["args"] == {"task_id": "task-42"}
    assert calls[1]["args"] == {"space_id": "space-9", "limit": 500}
    assert calls[2]["args"] == {
        "task_id": "task-42",
        "status": "in_progress",
        "assignee_id": "agent-789",
    }


def test_tasks_update_assign_to_handle_through_gateway_no_match(monkeypatch):
    """A handle that doesn't match any agent in the task's space exits cleanly with a clear error."""
    monkeypatch.setattr(
        "ax_cli.commands.tasks.resolve_gateway_config",
        lambda: {"url": "http://127.0.0.1:8765", "agent_name": "wishy", "workdir": "/repo"},
    )

    def fake_gateway_local_call(*, gateway_cfg, method, args=None, **_):
        if method == "get_task":
            return {"task": {"id": args["task_id"], "space_id": "space-9"}}
        if method == "list_agents":
            return {"agents": [{"id": "other", "name": "someone-else"}]}
        if method == "update_task":
            raise AssertionError("update_task must not run when handle resolution fails")
        raise AssertionError(f"unexpected proxy method: {method}")

    monkeypatch.setattr("ax_cli.commands.tasks._gateway_local_call", fake_gateway_local_call)

    result = runner.invoke(
        app,
        ["tasks", "update", "task-42", "--assign-to", "ghost-agent"],
    )

    assert result.exit_code == 1
    assert "No visible agent found for assignment target 'ghost-agent'" in result.output


def test_tasks_update_requires_at_least_one_field(monkeypatch):
    monkeypatch.setattr("ax_cli.commands.tasks.resolve_gateway_config", lambda: {})
    monkeypatch.setattr("ax_cli.commands.tasks.get_client", lambda: object())

    result = runner.invoke(app, ["tasks", "update", "task-42"])

    assert result.exit_code == 1
    assert "--priority" in result.output
    assert "--status" in result.output
    assert "--assign-to" in result.output


# ---- Task helper functions ----


def test_task_agent_items_list():
    assert task_agent_items([{"name": "a"}, {"name": "b"}]) == [{"name": "a"}, {"name": "b"}]


def test_task_agent_items_dict():
    assert task_agent_items({"agents": [{"name": "a"}]}) == [{"name": "a"}]


def test_task_agent_items_non_dict():
    assert task_agent_items("string") == []


def test_task_agent_items_filters_non_dicts():
    assert task_agent_items([{"name": "a"}, "not-dict", 42]) == [{"name": "a"}]


def test_agent_names_all_fields():
    agent = {"name": "Alice", "username": "alice_bot", "handle": "@Alice", "display_name": "Alice B"}
    names = _agent_names(agent)
    assert "alice" in names
    assert "alice_bot" in names
    assert "alice b" in names


def test_agent_names_empty():
    assert _agent_names({}) == set()


def test_agent_names_strips_at():
    assert "bob" in _agent_names({"handle": "@bob"})


def test_task_space_items_list():
    assert task_space_items([{"id": "s1"}]) == [{"id": "s1"}]


def test_task_space_items_dict():
    assert task_space_items({"spaces": [{"id": "s1"}]}) == [{"id": "s1"}]


def test_annotate_task_space_basic():
    task = {"title": "test"}
    space = {"id": "s1", "label": "my-space", "slug": "my-space", "name": "My Space"}
    result = _annotate_task_space(task, space)
    assert result["space_id"] == "s1"
    assert result["space_slug"] == "my-space"
    assert result["space_name"] == "My Space"


def test_annotate_task_space_no_slug():
    task = {"title": "test"}
    space = {"id": "s1", "label": "s1"}
    result = _annotate_task_space(task, space)
    assert result["space_id"] == "s1"
    assert "space_slug" not in result


def test_space_summary_found():
    from unittest.mock import MagicMock

    client = MagicMock()
    client.list_spaces.return_value = [{"id": "s1", "slug": "my-space", "name": "My Space"}]
    result = _space_summary(client, "s1")
    assert result["id"] == "s1"
    assert result["slug"] == "my-space"
    assert result["label"] == "my-space"


def test_space_summary_not_found():
    from unittest.mock import MagicMock

    client = MagicMock()
    client.list_spaces.return_value = [{"id": "s2"}]
    result = _space_summary(client, "s1")
    assert result["id"] == "s1"
    assert result["label"] == "s1"


def test_space_summary_exception():
    from unittest.mock import MagicMock

    client = MagicMock()
    client.list_spaces.side_effect = Exception("network error")
    result = _space_summary(client, "s1")
    assert result["id"] == "s1"


def test_resolve_assignee_id_none():
    assert _resolve_assignee_id(None, None, space_id="s1") is None


def test_resolve_assignee_id_empty():
    assert _resolve_assignee_id(None, "  ", space_id="s1") is None


def test_resolve_assignee_id_uuid():
    result = _resolve_assignee_id(None, "12345678-1234-1234-1234-123456789abc", space_id="s1")
    assert result == "12345678-1234-1234-1234-123456789abc"


def test_resolve_assignee_id_by_name():
    from unittest.mock import MagicMock

    client = MagicMock()
    client.list_agents.return_value = [
        {"id": "agent-1", "name": "alice"},
        {"id": "agent-2", "name": "bob"},
    ]
    result = _resolve_assignee_id(client, "@alice", space_id="s1")
    assert result == "agent-1"


def test_resolve_assignee_id_not_found():
    from unittest.mock import MagicMock

    import pytest
    from typer import Exit

    client = MagicMock()
    client.list_agents.return_value = [{"id": "agent-1", "name": "alice"}]
    with pytest.raises(Exit):
        _resolve_assignee_id(client, "ghost", space_id="s1")


# Issue #64 — `tasks get` was rendering Python `dict.__str__` for the inner
# task and `--json` was wrapping in `{"task": {...}}` while `tasks create
# --json` returns a flat object. Both code paths should now unwrap.


def test_tasks_get_json_unwraps_task_wrapper(monkeypatch):
    """`tasks get --json` returns the flat task dict (not {"task": {...}})."""

    class FakeClient:
        def get_task(self, task_id):
            return {"task": {"id": task_id, "title": "Demo", "status": "open"}}

    monkeypatch.setattr("ax_cli.commands.tasks.get_client", lambda: FakeClient())
    monkeypatch.setattr("ax_cli.commands.tasks.resolve_gateway_config", lambda: None)

    result = runner.invoke(app, ["tasks", "get", "task-42", "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert set(payload.keys()) == {"id", "title", "status"}
    assert payload["id"] == "task-42"
    assert payload["title"] == "Demo"
    assert payload["status"] == "open"


def test_tasks_get_default_renders_fields_not_dict_repr(monkeypatch):
    """`tasks get` default output prints individual fields, not a Python
    dict.__str__ of the wrapped payload.

    Prior bug: print_kv received `{"task": {...}}` and rendered as
    `task: {'id': '...', 'title': '...'}` — Python repr leaked into the
    operator-facing terminal output. After the fix, print_kv sees the
    unwrapped task dict and renders each field on its own line.
    """

    class FakeClient:
        def get_task(self, task_id):
            return {"task": {"id": task_id, "title": "Demo title", "status": "open"}}

    monkeypatch.setattr("ax_cli.commands.tasks.get_client", lambda: FakeClient())
    monkeypatch.setattr("ax_cli.commands.tasks.resolve_gateway_config", lambda: None)

    result = runner.invoke(app, ["tasks", "get", "task-42"])
    assert result.exit_code == 0, result.output
    # No Python dict repr in the output.
    assert "{'id'" not in result.output
    assert '{"id"' not in result.output
    # Each field on its own line.
    assert "id" in result.output
    assert "title" in result.output
    assert "Demo title" in result.output
    assert "status" in result.output


def test_tasks_get_handles_flat_response(monkeypatch):
    """When the upstream API returns the task at the top level (no
    `{"task": {...}}` wrapper), the unwrap is a no-op and rendering
    still works."""

    class FakeClient:
        def get_task(self, task_id):
            return {"id": task_id, "title": "Flat", "status": "open"}

    monkeypatch.setattr("ax_cli.commands.tasks.get_client", lambda: FakeClient())
    monkeypatch.setattr("ax_cli.commands.tasks.resolve_gateway_config", lambda: None)

    result = runner.invoke(app, ["tasks", "get", "task-42", "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["id"] == "task-42"
    assert payload["title"] == "Flat"


# ── #81: unwrap {"task": ...} envelope in update + gateway create ────────────


def test_tasks_update_json_unwraps_task_wrapper(monkeypatch):
    """`tasks update --json` returns the flat task dict (not {"task": {...}})."""

    class FakeClient:
        def update_task(self, task_id, **fields):
            return {"task": {"id": task_id, "title": "Demo", "status": fields.get("status", "open")}}

    monkeypatch.setattr("ax_cli.commands.tasks.get_client", lambda: FakeClient())
    monkeypatch.setattr("ax_cli.commands.tasks.resolve_gateway_config", lambda: None)

    result = runner.invoke(app, ["tasks", "update", "task-42", "--status", "done", "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    # Assert the wrapper is gone (no "task" key) and the unwrapped fields are
    # present, without pinning the exact keyset — the API may add fields like
    # updated_at without breaking the unwrap behavior under test.
    assert "task" not in payload
    assert {"id", "title", "status"}.issubset(payload.keys())
    assert payload["id"] == "task-42"
    assert payload["status"] == "done"


def test_tasks_update_default_renders_fields_not_dict_repr(monkeypatch):
    """`tasks update` default output prints individual fields, not a Python
    dict.__str__ of the wrapped payload."""

    class FakeClient:
        def update_task(self, task_id, **fields):
            return {"task": {"id": task_id, "title": "Demo title", "status": "done"}}

    monkeypatch.setattr("ax_cli.commands.tasks.get_client", lambda: FakeClient())
    monkeypatch.setattr("ax_cli.commands.tasks.resolve_gateway_config", lambda: None)

    result = runner.invoke(app, ["tasks", "update", "task-42", "--status", "done"])
    assert result.exit_code == 0, result.output
    assert "{'id'" not in result.output
    assert '{"id"' not in result.output
    assert "Demo title" in result.output
    assert "status" in result.output


def test_tasks_update_handles_flat_response(monkeypatch):
    """When update returns a flat task (no wrapper), the unwrap is a no-op."""

    class FakeClient:
        def update_task(self, task_id, **fields):
            return {"id": task_id, "title": "Flat", "status": "done"}

    monkeypatch.setattr("ax_cli.commands.tasks.get_client", lambda: FakeClient())
    monkeypatch.setattr("ax_cli.commands.tasks.resolve_gateway_config", lambda: None)

    result = runner.invoke(app, ["tasks", "update", "task-42", "--status", "done", "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["id"] == "task-42"
    assert payload["title"] == "Flat"


def test_tasks_update_through_gateway_unwraps_task_wrapper(monkeypatch):
    """The gateway update path unwraps the {"task": {...}} envelope too."""
    monkeypatch.setattr(
        "ax_cli.commands.tasks.resolve_gateway_config",
        lambda: {"url": "http://127.0.0.1:8765", "agent_name": "codex-pass-through", "space_id": "space-1"},
    )
    monkeypatch.setattr(
        "ax_cli.commands.tasks._gateway_local_call",
        lambda **kwargs: {"task": {"id": kwargs["args"]["task_id"], "title": "GW", "status": "done"}},
    )

    result = runner.invoke(app, ["tasks", "update", "task-7", "--status", "done", "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["id"] == "task-7"
    assert "task" not in payload


def test_tasks_create_through_gateway_unwraps_task_wrapper(monkeypatch):
    """The gateway create path emits the flat task, not the envelope (#81)."""
    monkeypatch.setattr(
        "ax_cli.commands.tasks.resolve_gateway_config",
        lambda: {"url": "http://127.0.0.1:8765", "agent_name": "codex-pass-through", "space_id": "space-1"},
    )
    monkeypatch.setattr(
        "ax_cli.commands.tasks._gateway_local_task_create",
        lambda **kwargs: {"task": {"id": "task-9", "title": kwargs["title"], "priority": "high"}},
    )

    result = runner.invoke(app, ["tasks", "create", "Ship it", "--priority", "high", "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["id"] == "task-9"
    assert payload["title"] == "Ship it"
    assert "task" not in payload
