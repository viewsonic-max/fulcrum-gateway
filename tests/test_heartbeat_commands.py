"""Tests for HEARTBEAT-001: local-first heartbeat CLI primitive."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import typer
from typer.testing import CliRunner

from ax_cli.main import app

runner = CliRunner()


class _FakeClient:
    def __init__(self, *, raise_on_send: Exception | None = None) -> None:
        self.heartbeats: list[dict[str, Any]] = []
        self._raise_on_send = raise_on_send

    def send_heartbeat(self, *, agent_id=None, status=None, note=None, cadence_seconds=None) -> dict:
        if self._raise_on_send is not None:
            raise self._raise_on_send
        record = {
            "agent_id": "agent-orion",
            "status": status,
            "note": note,
            "cadence_seconds": cadence_seconds,
            "ttl_seconds": 30,
            "last_heartbeat": "2026-04-25T00:00:00Z",
            "presence": "online",
        }
        self.heartbeats.append(record)
        return record


def _install_runtime(monkeypatch, client: _FakeClient) -> None:
    monkeypatch.setattr("ax_cli.commands.heartbeat.get_client", lambda: client)
    monkeypatch.setattr("ax_cli.commands.heartbeat.resolve_agent_name", lambda client=None: "orion")


def _load(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text())


def test_send_records_and_pushes_when_online(monkeypatch, tmp_path):
    fake = _FakeClient()
    _install_runtime(monkeypatch, fake)
    store_file = tmp_path / "heartbeats.json"

    result = runner.invoke(
        app,
        [
            "heartbeat",
            "send",
            "--status",
            "active",
            "--note",
            "starting up",
            "--cadence",
            "30",
            "--file",
            str(store_file),
            "--json",
        ],
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)

    assert payload["record"]["status"] == "active"
    assert payload["record"]["pushed"] is True
    assert payload["record"]["backend_ttl_seconds"] == 30

    assert len(fake.heartbeats) == 1
    assert fake.heartbeats[0]["status"] == "active"
    assert fake.heartbeats[0]["cadence_seconds"] == 30

    store = _load(store_file)
    assert store["current_status"] == "active"
    assert store["cadence_seconds"] == 30
    assert store["last_pushed_at"] is not None
    assert len(store["history"]) == 1


def test_send_queues_locally_on_network_error(monkeypatch, tmp_path):
    fake = _FakeClient(raise_on_send=httpx.ConnectError("offline (test)"))
    _install_runtime(monkeypatch, fake)
    store_file = tmp_path / "heartbeats.json"

    result = runner.invoke(
        app,
        ["heartbeat", "send", "--status", "active", "--cadence", "60", "--file", str(store_file), "--json"],
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["record"]["pushed"] is False
    assert "network" in payload["record"]["push_error"]

    store = _load(store_file)
    assert store["last_pushed_at"] is None
    assert len(store["history"]) == 1
    assert store["history"][0]["pushed"] is False


def test_send_skip_push_records_local_only(monkeypatch, tmp_path):
    """--skip-push records locally without attempting a network call."""

    class _ExplodingClient:
        def send_heartbeat(self, *_a, **_kw):
            raise AssertionError("should not be called when --skip-push")

    monkeypatch.setattr("ax_cli.commands.heartbeat.get_client", lambda: _ExplodingClient())
    monkeypatch.setattr("ax_cli.commands.heartbeat.resolve_agent_name", lambda client=None: "orion")

    store_file = tmp_path / "heartbeats.json"
    result = runner.invoke(
        app,
        ["heartbeat", "send", "--status", "busy", "--skip-push", "--file", str(store_file), "--json"],
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["record"]["pushed"] is False
    assert payload["record"]["push_error"] is None  # not queued, just local-only


def test_send_rejects_invalid_cadence(monkeypatch, tmp_path):
    fake = _FakeClient()
    _install_runtime(monkeypatch, fake)
    store_file = tmp_path / "heartbeats.json"

    result = runner.invoke(
        app,
        ["heartbeat", "send", "--cadence", "0", "--file", str(store_file)],
    )
    assert result.exit_code != 0
    assert "cadence" in result.output.lower()


def test_send_passes_through_unknown_status(monkeypatch, tmp_path):
    """Unknown status values pass through so the protocol can evolve."""
    fake = _FakeClient()
    _install_runtime(monkeypatch, fake)
    store_file = tmp_path / "heartbeats.json"

    result = runner.invoke(
        app,
        [
            "heartbeat",
            "send",
            "--status",
            "future_value_xyz",
            "--cadence",
            "30",
            "--skip-push",  # don't depend on backend accepting it
            "--file",
            str(store_file),
            "--json",
        ],
    )
    assert result.exit_code == 0, result.output
    store = _load(store_file)
    assert store["current_status"] == "future_value_xyz"


def test_status_reports_queued_unpushed(monkeypatch, tmp_path):
    fake = _FakeClient()
    _install_runtime(monkeypatch, fake)
    store_file = tmp_path / "heartbeats.json"
    store_file.write_text(
        json.dumps(
            {
                "version": 1,
                "agent_name": "orion",
                "agent_id": "abc",
                "cadence_seconds": 60,
                "current_status": "active",
                "current_note": None,
                "last_sent_at": "2026-04-25T15:00:00Z",
                "last_pushed_at": "2026-04-25T15:00:00Z",
                "next_due_at": "2026-04-25T15:01:00Z",
                "history": [
                    {"id": "hb-1", "status": "active", "sent_at": "2026-04-25T15:00:00Z", "pushed": True},
                    {
                        "id": "hb-2",
                        "status": "active",
                        "sent_at": "2026-04-25T15:01:00Z",
                        "pushed": False,
                        "push_error": "network",
                    },
                    {
                        "id": "hb-3",
                        "status": "busy",
                        "sent_at": "2026-04-25T15:02:00Z",
                        "pushed": False,
                        "push_error": "network",
                    },
                ],
            }
        )
    )

    result = runner.invoke(app, ["heartbeat", "status", "--file", str(store_file), "--skip-probe", "--json"])
    assert result.exit_code == 0, result.output
    snapshot = json.loads(result.output)
    assert snapshot["online"] is False
    assert snapshot["offline_reason"] == "probe skipped"
    assert snapshot["queued_unpushed"] == 2
    assert snapshot["agent_name"] == "orion"
    assert snapshot["current_status"] == "active"
    assert snapshot["cadence_seconds"] == 60
    assert snapshot["next_due_at"] == "2026-04-25T15:01:00Z"


def test_status_reports_no_history_gracefully(tmp_path):
    """Empty store: status returns sensible defaults, never crashes."""
    store_file = tmp_path / "heartbeats.json"
    result = runner.invoke(app, ["heartbeat", "status", "--file", str(store_file), "--skip-probe", "--json"])
    assert result.exit_code == 0, result.output
    snapshot = json.loads(result.output)
    assert snapshot["queued_unpushed"] == 0
    assert snapshot["last_sent_at"] is None
    assert snapshot["next_due_at"] is None


def test_list_filters_unpushed_only(monkeypatch, tmp_path):
    fake = _FakeClient()
    _install_runtime(monkeypatch, fake)
    store_file = tmp_path / "heartbeats.json"
    store_file.write_text(
        json.dumps(
            {
                "version": 1,
                "history": [
                    {"id": "hb-1", "status": "active", "sent_at": "T1", "pushed": True},
                    {"id": "hb-2", "status": "active", "sent_at": "T2", "pushed": False},
                    {"id": "hb-3", "status": "busy", "sent_at": "T3", "pushed": False},
                ],
            }
        )
    )

    result = runner.invoke(app, ["heartbeat", "list", "--unpushed", "--file", str(store_file), "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    ids = [h["id"] for h in payload["history"]]
    assert ids == ["hb-3", "hb-2"], "most recent first, only unpushed"


def test_push_drains_queue_when_online(monkeypatch, tmp_path):
    fake = _FakeClient()
    _install_runtime(monkeypatch, fake)
    store_file = tmp_path / "heartbeats.json"
    store_file.write_text(
        json.dumps(
            {
                "version": 1,
                "agent_name": "orion",
                "cadence_seconds": 30,
                "current_status": "busy",
                "history": [
                    {"id": "hb-1", "status": "active", "sent_at": "T1", "pushed": False, "push_error": "old"},
                    {"id": "hb-2", "status": "busy", "sent_at": "T2", "pushed": False, "push_error": "old"},
                ],
            }
        )
    )

    result = runner.invoke(app, ["heartbeat", "push", "--file", str(store_file), "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["pushed"] == ["hb-2"]
    assert payload["drained_count"] == 2

    store = _load(store_file)
    assert all(h["pushed"] for h in store["history"]), "all queued records marked pushed"
    assert store["history"][-1]["backend_ttl_seconds"] == 30
    assert store["last_pushed_at"] is not None
    # The push uses the LATEST (busy) status, not the oldest
    assert fake.heartbeats[0]["status"] == "busy"


def test_push_no_queued_returns_clean(monkeypatch, tmp_path):
    fake = _FakeClient()
    _install_runtime(monkeypatch, fake)
    store_file = tmp_path / "heartbeats.json"
    store_file.write_text(json.dumps({"version": 1, "history": [{"id": "hb-1", "pushed": True}]}))

    result = runner.invoke(app, ["heartbeat", "push", "--file", str(store_file), "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["pushed"] == []
    assert payload["reason"] == "no_queued_heartbeats"
    assert fake.heartbeats == []


def test_push_returns_error_when_offline(monkeypatch, tmp_path):
    fake = _FakeClient(raise_on_send=httpx.ConnectError("offline"))
    _install_runtime(monkeypatch, fake)
    store_file = tmp_path / "heartbeats.json"
    store_file.write_text(json.dumps({"version": 1, "history": [{"id": "hb-1", "status": "active", "pushed": False}]}))

    result = runner.invoke(app, ["heartbeat", "push", "--file", str(store_file), "--json"])
    assert result.exit_code != 0
    payload = json.loads(result.output)
    assert payload["pushed"] == []
    assert "network" in payload["error"]

    # Record updated with push_error but still queued
    store = _load(store_file)
    assert store["history"][0]["pushed"] is False
    assert "network" in store["history"][0]["push_error"]


# ---------------------------------------------------------------------------
# NEW COVERAGE TESTS
# ---------------------------------------------------------------------------


def test_default_store_file_env_override(monkeypatch, tmp_path):
    """AX_HEARTBEATS_FILE env var overrides the default store location."""
    from ax_cli.commands.heartbeat import _default_store_file

    custom = tmp_path / "custom-heartbeats.json"
    monkeypatch.setenv("AX_HEARTBEATS_FILE", str(custom))
    assert _default_store_file() == custom


def test_default_store_file_walks_to_ax_dir(monkeypatch, tmp_path):
    """_default_store_file walks up to find nearest .ax/ directory."""
    from ax_cli.commands.heartbeat import _default_store_file

    monkeypatch.delenv("AX_HEARTBEATS_FILE", raising=False)
    ax_dir = tmp_path / ".ax"
    ax_dir.mkdir()
    nested = tmp_path / "a" / "b" / "c"
    nested.mkdir(parents=True)
    monkeypatch.chdir(nested)
    result = _default_store_file()
    assert result == ax_dir / "heartbeats.json"


def test_default_store_file_falls_back_to_home(monkeypatch, tmp_path):
    """Without .ax/ or env var, falls back to ~/.ax/heartbeats.json."""
    from ax_cli.commands.heartbeat import _default_store_file

    monkeypatch.delenv("AX_HEARTBEATS_FILE", raising=False)
    # Use a temp dir with no .ax directory
    bare = tmp_path / "bare"
    bare.mkdir()
    monkeypatch.chdir(bare)
    result = _default_store_file()
    assert result == Path.home() / ".ax" / "heartbeats.json"


def test_load_store_invalid_json(tmp_path):
    """Invalid JSON in heartbeats file exits with error."""
    from ax_cli.commands.heartbeat import _load_store

    path = tmp_path / "heartbeats.json"
    path.write_text("{not valid json")
    import pytest

    with pytest.raises((SystemExit, typer.Exit)):
        _load_store(path)


def test_load_store_non_dict(tmp_path):
    """Non-dict JSON in heartbeats file exits with error."""
    from ax_cli.commands.heartbeat import _load_store

    path = tmp_path / "heartbeats.json"
    path.write_text('"just a string"')
    import pytest

    with pytest.raises((SystemExit, typer.Exit)):
        _load_store(path)


def test_load_store_non_list_history(tmp_path):
    """Non-list history field exits with error."""
    from ax_cli.commands.heartbeat import _load_store

    path = tmp_path / "heartbeats.json"
    path.write_text(json.dumps({"version": 1, "history": "not-a-list"}))
    import pytest

    with pytest.raises((SystemExit, typer.Exit)):
        _load_store(path)


def test_normalize_status_invalid_strict():
    """Invalid status with allow_passthrough=False raises BadParameter."""
    import pytest
    import typer

    from ax_cli.commands.heartbeat import _normalize_status

    with pytest.raises(typer.BadParameter, match="got 'bogus'"):
        _normalize_status("bogus", allow_passthrough=False)


def test_try_push_http_status_error():
    """HTTPStatusError returns a formatted error string."""
    from ax_cli.commands.heartbeat import _try_push

    class FailClient:
        def send_heartbeat(self, **kwargs):
            raise httpx.HTTPStatusError(
                "test",
                request=httpx.Request("POST", "http://test/heartbeat"),
                response=httpx.Response(500, text="Internal Server Error"),
            )

    pushed, err, resp = _try_push(FailClient(), status="active", note=None, cadence_seconds=60)
    assert pushed is False
    assert "500" in err
    assert resp is None


def test_try_push_generic_exception():
    """Generic exceptions are caught defensively."""
    from ax_cli.commands.heartbeat import _try_push

    class FailClient:
        def send_heartbeat(self, **kwargs):
            raise ValueError("something broke")

    pushed, err, resp = _try_push(FailClient(), status="active", note=None, cadence_seconds=60)
    assert pushed is False
    assert "something broke" in err
    assert resp is None


def test_send_exception_on_get_client(monkeypatch, tmp_path):
    """When get_client raises, heartbeat is queued with push_error."""
    monkeypatch.setattr(
        "ax_cli.commands.heartbeat.get_client", lambda: (_ for _ in ()).throw(RuntimeError("no config"))
    )

    store_file = tmp_path / "heartbeats.json"
    result = runner.invoke(
        app,
        ["heartbeat", "send", "--status", "active", "--cadence", "30", "--file", str(store_file), "--json"],
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["record"]["pushed"] is False
    assert "client unavailable" in payload["record"]["push_error"]


def test_send_exception_on_resolve_agent_name(monkeypatch, tmp_path):
    """resolve_agent_name exception is swallowed; heartbeat still sends."""
    fake = _FakeClient()
    monkeypatch.setattr("ax_cli.commands.heartbeat.get_client", lambda: fake)
    monkeypatch.setattr(
        "ax_cli.commands.heartbeat.resolve_agent_name",
        lambda client=None: (_ for _ in ()).throw(RuntimeError("no agent")),
    )

    store_file = tmp_path / "heartbeats.json"
    result = runner.invoke(
        app,
        ["heartbeat", "send", "--status", "active", "--cadence", "30", "--file", str(store_file), "--json"],
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["record"]["pushed"] is True


def test_send_human_output_pushed(monkeypatch, tmp_path):
    """Human-readable output shows pushed=yes with TTL."""
    fake = _FakeClient()
    _install_runtime(monkeypatch, fake)
    store_file = tmp_path / "heartbeats.json"

    result = runner.invoke(
        app,
        ["heartbeat", "send", "--status", "active", "--cadence", "30", "--file", str(store_file)],
    )
    assert result.exit_code == 0, result.output
    assert "pushed=yes" in result.output


def test_send_human_output_queued(monkeypatch, tmp_path):
    """Human-readable output shows queued marker on network error."""
    fake = _FakeClient(raise_on_send=httpx.ConnectError("offline"))
    _install_runtime(monkeypatch, fake)
    store_file = tmp_path / "heartbeats.json"

    result = runner.invoke(
        app,
        ["heartbeat", "send", "--status", "active", "--cadence", "30", "--file", str(store_file)],
    )
    assert result.exit_code == 0, result.output
    assert "queued" in result.output.lower() or "local-only" in result.output.lower()


def test_send_human_output_local_only(monkeypatch, tmp_path):
    """Human-readable output with --skip-push shows local-only marker."""
    fake = _FakeClient()
    _install_runtime(monkeypatch, fake)
    store_file = tmp_path / "heartbeats.json"

    result = runner.invoke(
        app,
        ["heartbeat", "send", "--status", "active", "--cadence", "30", "--skip-push", "--file", str(store_file)],
    )
    assert result.exit_code == 0, result.output
    assert "local-only" in result.output.lower()


def test_list_history_table_output(monkeypatch, tmp_path):
    """List command in human mode outputs a table."""
    fake = _FakeClient()
    _install_runtime(monkeypatch, fake)
    store_file = tmp_path / "heartbeats.json"
    store_file.write_text(
        json.dumps(
            {
                "version": 1,
                "history": [
                    {
                        "id": "hb-1",
                        "status": "active",
                        "sent_at": "T1",
                        "pushed": True,
                        "backend_ttl_seconds": 30,
                        "note": "test note",
                        "push_error": None,
                    },
                ],
            }
        )
    )

    result = runner.invoke(app, ["heartbeat", "list", "--file", str(store_file)])
    assert result.exit_code == 0, result.output
    assert "hb-1" in result.output
    assert "active" in result.output


def test_list_history_empty(monkeypatch, tmp_path):
    """List command with no heartbeats shows helpful message."""
    store_file = tmp_path / "heartbeats.json"

    result = runner.invoke(app, ["heartbeat", "list", "--file", str(store_file)])
    assert result.exit_code == 0, result.output
    assert "No heartbeats" in result.output


def test_probe_online_no_base_url(monkeypatch):
    """_probe_online returns offline when no base_url configured."""
    from ax_cli.commands.heartbeat import _probe_online

    class NoUrlClient:
        pass

    monkeypatch.setattr("ax_cli.commands.heartbeat.get_client", lambda: NoUrlClient())
    ok, reason = _probe_online()
    assert ok is False
    assert "no base_url" in reason


def test_probe_online_client_unavailable(monkeypatch):
    """_probe_online returns offline when get_client raises."""
    from ax_cli.commands.heartbeat import _probe_online

    monkeypatch.setattr(
        "ax_cli.commands.heartbeat.get_client",
        lambda: (_ for _ in ()).throw(RuntimeError("broken")),
    )
    ok, reason = _probe_online()
    assert ok is False
    assert "client unavailable" in reason


def test_probe_online_success(monkeypatch):
    """_probe_online returns True when health endpoint returns 200."""
    from ax_cli.commands.heartbeat import _probe_online

    class FakeClient:
        base_url = "http://localhost:8000"

    class FakeResp:
        status_code = 200

    monkeypatch.setattr("ax_cli.commands.heartbeat.get_client", lambda: FakeClient())
    monkeypatch.setattr("ax_cli.commands.heartbeat.httpx.get", lambda url, timeout=None: FakeResp())
    ok, reason = _probe_online()
    assert ok is True
    assert reason is None


def test_probe_online_server_error(monkeypatch):
    """_probe_online returns False for 5xx status."""
    from ax_cli.commands.heartbeat import _probe_online

    class FakeClient:
        base_url = "http://localhost:8000"

    class FakeResp:
        status_code = 502

    monkeypatch.setattr("ax_cli.commands.heartbeat.get_client", lambda: FakeClient())
    monkeypatch.setattr("ax_cli.commands.heartbeat.httpx.get", lambda url, timeout=None: FakeResp())
    ok, reason = _probe_online()
    assert ok is False
    assert "502" in reason


def test_probe_online_network_error(monkeypatch):
    """_probe_online returns False on ConnectError."""
    from ax_cli.commands.heartbeat import _probe_online

    class FakeClient:
        base_url = "http://localhost:8000"

    monkeypatch.setattr("ax_cli.commands.heartbeat.get_client", lambda: FakeClient())
    monkeypatch.setattr(
        "ax_cli.commands.heartbeat.httpx.get",
        lambda url, timeout=None: (_ for _ in ()).throw(httpx.ConnectError("down")),
    )
    ok, reason = _probe_online()
    assert ok is False
    assert "network" in reason


def test_probe_online_generic_exception(monkeypatch):
    """_probe_online returns False on unexpected exceptions."""
    from ax_cli.commands.heartbeat import _probe_online

    class FakeClient:
        base_url = "http://localhost:8000"

    monkeypatch.setattr("ax_cli.commands.heartbeat.get_client", lambda: FakeClient())
    monkeypatch.setattr(
        "ax_cli.commands.heartbeat.httpx.get",
        lambda url, timeout=None: (_ for _ in ()).throw(ValueError("weird")),
    )
    ok, reason = _probe_online()
    assert ok is False
    assert "weird" in reason


def test_status_human_output_online(monkeypatch, tmp_path):
    """Human-mode status output includes ONLINE and details."""
    fake = _FakeClient()
    _install_runtime(monkeypatch, fake)

    class FakeClient:
        base_url = "http://localhost:8000"

    class FakeResp:
        status_code = 200

    monkeypatch.setattr("ax_cli.commands.heartbeat.get_client", lambda: FakeClient())
    monkeypatch.setattr("ax_cli.commands.heartbeat.httpx.get", lambda url, timeout=None: FakeResp())

    store_file = tmp_path / "heartbeats.json"
    store_file.write_text(
        json.dumps(
            {
                "version": 1,
                "agent_name": "orion",
                "current_status": "active",
                "cadence_seconds": 60,
                "last_sent_at": "2026-04-25T15:00:00Z",
                "last_pushed_at": "2026-04-25T15:00:00Z",
                "next_due_at": "2020-01-01T00:00:00Z",
                "history": [
                    {"id": "hb-1", "status": "active", "pushed": False, "push_error": "old"},
                ],
            }
        )
    )

    result = runner.invoke(app, ["heartbeat", "status", "--file", str(store_file)])
    assert result.exit_code == 0, result.output
    assert "ONLINE" in result.output
    assert "orion" in result.output
    assert "active" in result.output
    assert "DUE NOW" in result.output
    assert "Queued" in result.output


def test_status_human_output_offline(monkeypatch, tmp_path):
    """Human-mode status output includes OFFLINE with reason."""
    store_file = tmp_path / "heartbeats.json"
    store_file.write_text(
        json.dumps(
            {
                "version": 1,
                "current_status": "unknown",
                "last_sent_at": None,
                "next_due_at": None,
                "history": [],
            }
        )
    )

    result = runner.invoke(app, ["heartbeat", "status", "--file", str(store_file), "--skip-probe"])
    assert result.exit_code == 0, result.output
    assert "OFFLINE" in result.output
    assert "probe skipped" in result.output
    assert "(never)" in result.output
    assert "no heartbeats sent yet" in result.output


def test_status_online_probe_path(monkeypatch, tmp_path):
    """Status without --skip-probe calls _probe_online."""

    class FakeClient:
        base_url = "http://localhost:8000"

    class FakeResp:
        status_code = 200

    monkeypatch.setattr("ax_cli.commands.heartbeat.get_client", lambda: FakeClient())
    monkeypatch.setattr("ax_cli.commands.heartbeat.httpx.get", lambda url, timeout=None: FakeResp())

    store_file = tmp_path / "heartbeats.json"

    result = runner.invoke(app, ["heartbeat", "status", "--file", str(store_file), "--json"])
    assert result.exit_code == 0, result.output
    snapshot = json.loads(result.output)
    assert snapshot["online"] is True


def test_push_empty_queue_human_output(monkeypatch, tmp_path):
    """Push with no queued heartbeats in human mode shows message."""
    fake = _FakeClient()
    _install_runtime(monkeypatch, fake)
    store_file = tmp_path / "heartbeats.json"
    store_file.write_text(json.dumps({"version": 1, "history": [{"id": "hb-1", "pushed": True}]}))

    result = runner.invoke(app, ["heartbeat", "push", "--file", str(store_file)])
    assert result.exit_code == 0, result.output
    assert "No queued" in result.output


def test_push_client_unavailable_human_output(monkeypatch, tmp_path):
    """Push when client is unavailable in human mode shows error."""
    monkeypatch.setattr(
        "ax_cli.commands.heartbeat.get_client",
        lambda: (_ for _ in ()).throw(RuntimeError("no config")),
    )
    store_file = tmp_path / "heartbeats.json"
    store_file.write_text(json.dumps({"version": 1, "history": [{"id": "hb-1", "status": "active", "pushed": False}]}))

    result = runner.invoke(app, ["heartbeat", "push", "--file", str(store_file)])
    assert result.exit_code != 0
    assert "client unavailable" in result.output.lower()


def test_push_client_unavailable_json_output(monkeypatch, tmp_path):
    """Push when client is unavailable in JSON mode returns error JSON."""
    monkeypatch.setattr(
        "ax_cli.commands.heartbeat.get_client",
        lambda: (_ for _ in ()).throw(RuntimeError("no config")),
    )
    store_file = tmp_path / "heartbeats.json"
    store_file.write_text(json.dumps({"version": 1, "history": [{"id": "hb-1", "status": "active", "pushed": False}]}))

    result = runner.invoke(app, ["heartbeat", "push", "--file", str(store_file), "--json"])
    # JSON mode returns error without raising Exit(1)
    payload = json.loads(result.output)
    assert payload["pushed"] == []
    assert "client unavailable" in payload["error"]


def test_push_success_human_output(monkeypatch, tmp_path):
    """Push success in human mode shows pushed message with TTL."""
    fake = _FakeClient()
    _install_runtime(monkeypatch, fake)
    store_file = tmp_path / "heartbeats.json"
    store_file.write_text(
        json.dumps(
            {
                "version": 1,
                "cadence_seconds": 30,
                "history": [
                    {"id": "hb-1", "status": "active", "pushed": False, "push_error": "old"},
                ],
            }
        )
    )

    result = runner.invoke(app, ["heartbeat", "push", "--file", str(store_file)])
    assert result.exit_code == 0, result.output
    assert "pushed" in result.output.lower()
    assert "hb-1" in result.output
    assert "drained" in result.output


def test_push_failure_human_output(monkeypatch, tmp_path):
    """Push failure in human mode shows red error message."""
    fake = _FakeClient(raise_on_send=httpx.ConnectError("offline"))
    _install_runtime(monkeypatch, fake)
    store_file = tmp_path / "heartbeats.json"
    store_file.write_text(json.dumps({"version": 1, "history": [{"id": "hb-1", "status": "active", "pushed": False}]}))

    result = runner.invoke(app, ["heartbeat", "push", "--file", str(store_file)])
    assert result.exit_code != 0
    assert "push failed" in result.output.lower()


def test_watch_max_ticks(monkeypatch, tmp_path):
    """Watch with --max-ticks stops after N ticks."""
    import ax_cli.commands.heartbeat as hb_mod

    fake = _FakeClient()
    _install_runtime(monkeypatch, fake)
    monkeypatch.setattr(hb_mod.time, "sleep", lambda _: None)
    store_file = tmp_path / "heartbeats.json"

    result = runner.invoke(
        app,
        ["heartbeat", "watch", "--interval", "1", "--max-ticks", "2", "--file", str(store_file)],
    )
    assert result.exit_code == 0, result.output
    assert "tick 1" in result.output
    assert "tick 2" in result.output
    assert "Reached --max-ticks" in result.output

    store = _load(store_file)
    assert len(store["history"]) == 2


def test_watch_queues_on_client_error(monkeypatch, tmp_path):
    """Watch queues heartbeats when get_client raises."""
    import ax_cli.commands.heartbeat as hb_mod

    call_count = [0]

    def failing_get_client():
        call_count[0] += 1
        raise RuntimeError("no config")

    monkeypatch.setattr("ax_cli.commands.heartbeat.get_client", failing_get_client)
    monkeypatch.setattr(hb_mod.time, "sleep", lambda _: None)
    store_file = tmp_path / "heartbeats.json"

    result = runner.invoke(
        app,
        ["heartbeat", "watch", "--interval", "1", "--max-ticks", "1", "--file", str(store_file)],
    )
    assert result.exit_code == 0, result.output
    assert "queued" in result.output.lower()

    store = _load(store_file)
    assert len(store["history"]) == 1
    assert store["history"][0]["pushed"] is False


def test_watch_invalid_interval():
    """Watch rejects --interval < 1."""
    result = runner.invoke(
        app,
        ["heartbeat", "watch", "--interval", "0"],
    )
    assert result.exit_code != 0
    assert "interval" in result.output.lower()
