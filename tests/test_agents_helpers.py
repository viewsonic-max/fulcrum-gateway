"""Tests for agents.py helper functions — avoids the CLI commands that hang."""

import base64

import pytest

from ax_cli.commands.agents import (
    AVATAR_URL_MAX_LENGTH,
    _agent_control_reason,
    _agent_control_state,
    _agent_control_status,
    _agent_is_blocked,
    _agent_items,
    _agent_mention_name,
    _agent_name_candidates,
    _build_avatar_data_uri_from_file,
    _check_avatar_url_length,
    _find_agent,
    _legacy_badge,
    _normalize_availability_rows,
    _warn_if_fields_dropped,
)

# ---- _agent_items ----


def test_agent_items_from_list():
    assert _agent_items([{"id": "1"}, {"id": "2"}]) == [{"id": "1"}, {"id": "2"}]


def test_agent_items_from_dict_agents():
    assert _agent_items({"agents": [{"id": "1"}]}) == [{"id": "1"}]


def test_agent_items_from_dict_items():
    assert _agent_items({"items": [{"id": "1"}]}) == [{"id": "1"}]


def test_agent_items_from_dict_results():
    assert _agent_items({"results": [{"id": "1"}]}) == [{"id": "1"}]


def test_agent_items_non_dict_non_list():
    assert _agent_items("garbage") == []


def test_agent_items_dict_no_known_key():
    assert _agent_items({"other": [{"id": "1"}]}) == []


def test_agent_items_filters_non_dict_items():
    assert _agent_items([{"id": "1"}, "bad", 42]) == [{"id": "1"}]


# ---- _agent_name_candidates ----


def test_name_candidates_all_fields():
    agent = {"id": "uuid", "name": "bot", "username": "botuser", "handle": "@handle"}
    result = _agent_name_candidates(agent)
    assert "uuid" in result
    assert "bot" in result
    assert "botuser" in result
    assert "handle" in result


def test_name_candidates_empty():
    assert _agent_name_candidates({}) == set()


def test_name_candidates_strips_at():
    agent = {"handle": "@mybot"}
    result = _agent_name_candidates(agent)
    assert "mybot" in result


# ---- _find_agent ----


def test_find_agent_by_name():
    agents = [{"name": "alpha"}, {"name": "beta"}]
    assert _find_agent(agents, "beta") == {"name": "beta"}


def test_find_agent_by_handle():
    agents = [{"handle": "@mybot", "id": "1"}]
    assert _find_agent(agents, "@mybot")["id"] == "1"


def test_find_agent_not_found():
    assert _find_agent([{"name": "alpha"}], "missing") is None


def test_find_agent_case_insensitive():
    agents = [{"name": "MyBot"}]
    assert _find_agent(agents, "mybot") is not None


# ---- _agent_mention_name ----


def test_mention_name_from_handle():
    assert _agent_mention_name({"handle": "@bot"}, "fallback") == "bot"


def test_mention_name_from_name():
    assert _agent_mention_name({"name": "robot"}, "fallback") == "robot"


def test_mention_name_fallback():
    assert _agent_mention_name({}, "@fallback") == "fallback"


# ---- _agent_control_state ----


def test_control_state_dict():
    assert _agent_control_state({"control": {"is_disabled": True}}) == {"is_disabled": True}


def test_control_state_missing():
    assert _agent_control_state({}) == {}


def test_control_state_non_dict():
    assert _agent_control_state({"control": "bad"}) == {}


# ---- _agent_control_status ----


def test_control_status_active():
    assert _agent_control_status({}) == "active"


def test_control_status_disabled():
    assert _agent_control_status({"control": {"is_disabled": True}}) == "disabled"


def test_control_status_no_reply():
    assert _agent_control_status({"control": {"no_reply": True}}) == "no_reply"


# ---- _agent_control_reason ----


def test_control_reason_active():
    assert _agent_control_reason({}) == ""


def test_control_reason_disabled():
    result = _agent_control_reason({"control": {"is_disabled": True, "disabled_reason": "maintenance"}})
    assert result == "maintenance"


def test_control_reason_disabled_default():
    result = _agent_control_reason({"control": {"is_disabled": True}})
    assert "Kill switch" in result


def test_control_reason_no_reply():
    result = _agent_control_reason({"control": {"no_reply": True, "no_reply_reason": "busy"}})
    assert result == "busy"


# ---- _agent_is_blocked ----


def test_agent_is_blocked_false():
    assert not _agent_is_blocked({})


def test_agent_is_blocked_true():
    assert _agent_is_blocked({"control": {"is_disabled": True}})


# ---- _build_avatar_data_uri_from_file ----


def test_build_avatar_svg(tmp_path):
    svg = tmp_path / "avatar.svg"
    svg.write_text("<svg></svg>")
    uri = _build_avatar_data_uri_from_file(str(svg))
    assert uri.startswith("data:image/svg+xml;base64,")
    decoded = base64.b64decode(uri.split(",")[1]).decode()
    assert "<svg>" in decoded


def test_build_avatar_png(tmp_path):
    png = tmp_path / "avatar.png"
    png.write_bytes(b"\x89PNG\r\n\x1a\n")
    uri = _build_avatar_data_uri_from_file(str(png))
    assert uri.startswith("data:image/png;base64,")


def test_build_avatar_unknown_ext(tmp_path):
    f = tmp_path / "avatar.xyz"
    f.write_bytes(b"data")
    uri = _build_avatar_data_uri_from_file(str(f))
    assert "application/octet-stream" in uri


# ---- _check_avatar_url_length ----


def test_check_avatar_url_ok():
    _check_avatar_url_length("https://example.com/avatar.png")


def test_check_avatar_url_too_long():
    from typer import Exit

    with pytest.raises(Exit):
        _check_avatar_url_length("x" * (AVATAR_URL_MAX_LENGTH + 1))


# ---- _warn_if_fields_dropped ----


def test_warn_if_fields_dropped_none(capsys):
    result = _warn_if_fields_dropped({"bio": "test"}, {"bio": "test"})
    assert result == []


def test_warn_if_fields_dropped_some(capsys):
    result = _warn_if_fields_dropped({"bio": "sent"}, {"bio": "different"})
    assert "--bio" in result


# ---- _legacy_badge ----


def test_legacy_badge_legacy_row():
    assert _legacy_badge({"_legacy": True, "status": "active"}) == "active"


def test_legacy_badge_legacy_no_status():
    assert _legacy_badge({"_legacy": True}) == "—"


def test_legacy_badge_online():
    assert _legacy_badge({"presence": "online"}) == "Live"


def test_legacy_badge_offline():
    assert _legacy_badge({"presence": "offline"}) == "Offline"


def test_legacy_badge_unknown():
    assert _legacy_badge({"presence": "idle"}) == "idle"


def test_legacy_badge_empty():
    assert _legacy_badge({}) == "—"


# ---- _normalize_availability_rows ----


def test_normalize_availability_rows_list():
    data = [{"name": "alice"}, {"name": "bob"}]
    result = _normalize_availability_rows(data)
    assert len(result) == 2


def test_normalize_availability_rows_dict():
    data = {"agents": [{"name": "alice"}]}
    result = _normalize_availability_rows(data)
    assert len(result) == 1


def test_normalize_availability_rows_with_state():
    data = [{"name": "alice", "agent_state": {"badge_state": "live", "confidence": 0.9}}]
    result = _normalize_availability_rows(data)
    assert result[0].get("badge_state") == "live"
