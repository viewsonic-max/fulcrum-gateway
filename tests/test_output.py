"""Tests for ax_cli/output.py — mention_prefix, print_table, handle_error."""

from unittest.mock import MagicMock, PropertyMock, patch

import httpx
import pytest
import typer

from ax_cli.output import handle_error, mention_prefix, print_table, unwrap_envelope


def test_mention_prefix_whitespace_only():
    assert mention_prefix("   ") == ""


def test_mention_prefix_none():
    assert mention_prefix(None) == ""


def test_mention_prefix_empty():
    assert mention_prefix("") == ""


def test_mention_prefix_with_handle():
    assert mention_prefix("alice") == "@alice"


def test_mention_prefix_already_prefixed():
    assert mention_prefix("@alice") == "@alice"


def test_mention_prefix_strips_whitespace():
    assert mention_prefix("  bob  ") == "@bob"


# ---------- unwrap_envelope (GH #167) ----------
#
# Backend convention: single-resource GET / CREATE / UPDATE responses come back
# as ``{<resource>: {...}}``. Each call site used to open-code the unwrap and
# we kept missing it. The helper formalizes the pattern.


def test_unwrap_envelope_wrapped_dict_returns_inner():
    """The canonical case: server wrapped {"task": {...inner...}} and we
    unwrap to ``inner``."""
    inner = {"id": "task-1", "title": "Sample"}
    wrapped = {"task": inner}
    assert unwrap_envelope(wrapped, "task") is inner


def test_unwrap_envelope_flat_dict_passes_through():
    """Backend may already return flat (legacy or alternate endpoint).
    The helper must be a no-op so we don't break those handlers."""
    flat = {"id": "task-1", "title": "Sample"}
    assert unwrap_envelope(flat, "task") is flat


def test_unwrap_envelope_dict_missing_key_passes_through():
    """If the key isn't present, return data unchanged (no narrowing,
    no exception)."""
    data = {"unrelated": "value"}
    assert unwrap_envelope(data, "task") is data


def test_unwrap_envelope_none_passes_through():
    """``None`` is a legitimate response shape from some endpoints
    (DELETE typically). The helper must be safe."""
    assert unwrap_envelope(None, "task") is None


def test_unwrap_envelope_list_passes_through():
    """List responses (the common list-endpoint shape) must be returned
    unchanged. We don't pretend a list is an envelope."""
    items = [{"id": "1"}, {"id": "2"}]
    assert unwrap_envelope(items, "task") is items


def test_unwrap_envelope_scalar_passes_through():
    """Scalars and strings — unusual but possible — should pass through
    rather than crash."""
    assert unwrap_envelope("plain string", "task") == "plain string"
    assert unwrap_envelope(42, "task") == 42


def test_unwrap_envelope_inner_value_not_dict_passes_through():
    """If ``data[key]`` is a non-dict (scalar, list, None), don't unwrap.
    The shape isn't really an envelope; ``data`` likely carries other
    fields the caller needs.

    Example: a token response like ``{"token": "axp_...", "expires_at": ...}``
    isn't a ``{token: <wrapped object>}`` envelope — ``token`` is just one
    field on a flat object."""
    pat_response = {"token": "axp_secret", "expires_at": "2026-12-31"}
    assert unwrap_envelope(pat_response, "token") is pat_response


def test_unwrap_envelope_works_for_every_real_envelope_key():
    """Smoke check against the resource keys we currently unwrap across
    commands/."""
    for key in ("task", "agent", "message", "space", "context"):
        wrapped = {key: {"id": f"{key}-uuid"}}
        unwrapped = unwrap_envelope(wrapped, key)
        assert unwrapped == {"id": f"{key}-uuid"}, key


def test_print_table_auto_keys(capsys):
    with patch("ax_cli.output.console") as mock_console:
        print_table(
            ["Agent Name", "Status"],
            [{"agent_name": "bot", "status": "online"}],
        )
        mock_console.print.assert_called_once()
        table = mock_console.print.call_args[0][0]
        assert table.columns[0].header == "Agent Name"
        assert table.columns[1].header == "Status"


def test_print_table_explicit_keys(capsys):
    with patch("ax_cli.output.console") as mock_console:
        print_table(
            ["Name", "Value"],
            [{"n": "foo", "v": "bar"}],
            keys=["n", "v"],
        )
        mock_console.print.assert_called_once()


def _make_http_status_error(status_code, response_text="", response_json=None, url="http://test.local/api"):
    request = httpx.Request("GET", url)
    response = MagicMock(spec=httpx.Response)
    response.status_code = status_code
    response.text = response_text
    if response_json is not None:
        response.json.return_value = response_json
    else:
        response.json.side_effect = Exception("not json")
    err = httpx.HTTPStatusError("error", request=request, response=response)
    return err


def test_handle_error_html_response():
    err = _make_http_status_error(
        502,
        response_text="<html><body>Bad Gateway</body></html>",
    )
    with pytest.raises(typer.Exit):
        handle_error(err)


# ---------- #57: handle_error must preserve _parse_json's CLI-authored detail ----------
#
# Two paths can raise httpx.HTTPStatusError:
#   1. response.raise_for_status() — httpx builds the message and always
#      includes the substring "For more information check:".
#   2. client._parse_json (or any caller) raises directly with a route-aware
#      message — no such substring.
#
# handle_error must distinguish the two when the body is HTML: for (2), surface
# the author's detail; for (1), use the existing generic body-derived message.


_PARSE_JSON_DETAIL = (
    "Agent create returned HTML instead of JSON. The hosted API must return a "
    "JSON 4xx with an explicit reason such as quota, rate limit, name conflict, "
    "or feature flag; the CLI cannot safely infer the denied create reason from "
    "the SPA shell."
)


def test_handle_error_surfaces_cli_authored_detail_for_html_response(capsys):
    """Regression for #57: when _parse_json raises with a route-aware detail
    and the body is the SPA shell, handle_error must print that detail rather
    than the generic 'Got HTML instead of JSON' fallback."""
    request = httpx.Request("POST", "https://paxai.app/api/v1/agents")
    response = MagicMock(spec=httpx.Response)
    response.status_code = 200
    response.text = "<!DOCTYPE html><html><head><title>aX Platform</title></head></html>"
    response.json.side_effect = Exception("not json")
    err = httpx.HTTPStatusError(_PARSE_JSON_DETAIL, request=request, response=response)

    with pytest.raises(typer.Exit):
        handle_error(err)

    stderr = capsys.readouterr().err
    assert "Agent create returned HTML instead of JSON" in stderr
    assert "cannot safely infer the denied create reason" in stderr
    assert "Got HTML instead of JSON (frontend may be catching this route)" not in stderr


def test_handle_error_uses_generic_message_for_raise_for_status_with_html(capsys):
    """Sibling guard for #57: when httpx.Response.raise_for_status() raised the
    exception (its message always includes 'For more information check:'),
    handle_error must NOT mistake the boilerplate message for a CLI-authored
    detail. The generic 'Got HTML instead of JSON' fallback should still fire."""
    request = httpx.Request("GET", "https://paxai.app/api/v1/something")
    response = MagicMock(spec=httpx.Response)
    response.status_code = 502
    response.text = "<html><body>Bad Gateway</body></html>"
    response.json.side_effect = Exception("not json")
    # Mimics the exact shape httpx generates in raise_for_status — the
    # discriminator handle_error keys off is "For more information check:".
    raise_for_status_message = (
        "Server error '502 Bad Gateway' for url 'https://paxai.app/api/v1/something'\n"
        "For more information check: https://developer.mozilla.org/en-US/docs/Web/HTTP/Status/502"
    )
    err = httpx.HTTPStatusError(raise_for_status_message, request=request, response=response)

    with pytest.raises(typer.Exit):
        handle_error(err)

    stderr = capsys.readouterr().err
    assert "Got HTML instead of JSON (frontend may be catching this route)" in stderr
    assert "For more information check:" not in stderr


def test_handle_error_cli_authored_detail_with_non_html_body_uses_body(capsys):
    """The CLI-authored-detail preference is gated on the body being HTML.
    If the body is JSON or plain text, the existing parsing flow stays in
    charge — we should not start preferring exception strings over real
    server error bodies for the unrelated common case."""
    err = _make_http_status_error(
        409,
        response_json={"detail": "Agent with that name already exists in this space."},
    )
    # Override the exception message to something CLI-authored-looking, to
    # prove that the body-is-JSON path still wins.
    err.args = ("placeholder cli message that should NOT appear",)

    with pytest.raises(typer.Exit):
        handle_error(err)

    stderr = capsys.readouterr().err
    assert "Agent with that name already exists" in stderr
    assert "placeholder cli message that should NOT appear" not in stderr


def test_handle_error_plain_text_response():
    err = _make_http_status_error(
        500,
        response_text="Internal server error occurred",
    )
    with pytest.raises(typer.Exit):
        handle_error(err)


def test_handle_error_plain_text_with_invalid_credential(capsys):
    err = _make_http_status_error(
        401,
        response_text="invalid_credential: token expired",
    )
    with pytest.raises(typer.Exit):
        handle_error(err)


def test_handle_error_json_with_invalid_credential_dict():
    err = _make_http_status_error(
        401,
        response_json={"detail": {"error": "invalid_credential"}},
    )
    with pytest.raises(typer.Exit):
        handle_error(err)


def test_handle_error_request_url_host_exception():
    response = MagicMock(spec=httpx.Response)
    response.status_code = 401
    response.text = "invalid_credential"
    response.json.side_effect = Exception("not json")

    request = MagicMock()
    request.url = "http://test.local/"
    type(request).url = PropertyMock(
        side_effect=[
            MagicMock(__str__=lambda self: "http://test.local/"),
            MagicMock(host=PropertyMock(side_effect=Exception("no host"))),
        ]
    )

    err = httpx.HTTPStatusError("error", request=httpx.Request("GET", "http://test.local/"), response=response)
    err._request = MagicMock()
    err._request.url.__str__ = lambda self: "http://test.local/"

    url_mock = MagicMock()
    url_mock.host = property(lambda self: (_ for _ in ()).throw(Exception("boom")))

    err2 = _make_http_status_error(
        401,
        response_text="invalid_credential present here",
    )
    err2.request = MagicMock()
    err2.request.url = MagicMock()
    err2.request.url.__str__ = MagicMock(return_value="http://test.local/")
    type(err2.request.url).host = PropertyMock(side_effect=Exception("no host attr"))

    with pytest.raises(typer.Exit):
        handle_error(err2)
