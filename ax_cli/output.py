"""Shared output helpers: --json flag, tables, error handling."""

import json
import re
from typing import Any

import httpx
import typer
from rich.console import Console
from rich.table import Table

# axp_<class>_<keyid>.<secret> — PATs and similar secrets must never reach
# user-facing output, even if a server echoes them in an error body or they
# leak into a request URL/query string.
_AXP_SECRET_RE = re.compile(r"axp_[a-zA-Z0-9_]+\.[A-Za-z0-9_\-]+")


def _redact_secrets(text: str) -> str:
    """Redact axp_* PAT shapes from any user-facing string."""
    return _AXP_SECRET_RE.sub("axp_<redacted>", text)


console = Console()
# Dedicated stderr console for status/log lines that mustn't pollute stdout
# (e.g. when the caller parses --json output or pipes the command).
err_console = Console(stderr=True)

JSON_OPTION = typer.Option(False, "--json", help="Output as JSON")
SPACE_OPTION = typer.Option(None, "--space-id", help="Override default space")
EXIT_NOT_OK = 2
EXIT_SKIPPED = 3


def apply_envelope(
    data: dict, *, summary: dict | None = None, details: list | None = None, skipped: bool = False
) -> dict:
    """Add the stable QA/diagnostic envelope without removing legacy fields."""
    data["version"] = 1
    data["skipped"] = skipped
    data["summary"] = summary or {}
    data["details"] = details or []
    return data


def unwrap_envelope(data: Any, key: str) -> Any:
    """Unwrap a single-resource API envelope like ``{<key>: {...}}``.

    Returns ``data[key]`` if ``data`` is a dict whose ``key`` maps to another
    dict; otherwise returns ``data`` unchanged.

    Backend convention: single-resource GET / CREATE / UPDATE responses wrap
    the payload in ``{<resource>: {...}}`` while list responses come flat.
    Each call site used to open-code ``data.get(key, data) if isinstance(...) ...``
    and we kept missing it — tasks alone leaked it three times before we added
    this helper. See GH #167.

    Safe to call on:
      - flat dicts (no ``key``): returned unchanged
      - lists: returned unchanged
      - ``None`` / scalars: returned unchanged
      - dicts where ``data[key]`` is non-dict (e.g. a scalar or list): returned
        unchanged so we don't accidentally narrow a richer response shape

    Always pass the explicit ``key`` (``"task"``, ``"agent"``, ``"message"``,
    ...). The contract is "I expect a ``{<key>: ...}`` envelope; if it's
    already flat, hand it back."
    """
    if isinstance(data, dict):
        inner = data.get(key)
        if isinstance(inner, dict):
            return inner
    return data


def mention_prefix(mention: str | None) -> str:
    """Normalize an optional agent/user mention to the @handle form."""
    if not mention:
        return ""
    value = mention.strip()
    if not value:
        return ""
    return value if value.startswith("@") else f"@{value}"


def print_json(data):
    console.print_json(json.dumps(data, default=str))


def print_table(columns: list[str], rows: list[dict], *, keys: list[str] | None = None):
    if keys is None:
        keys = [c.lower().replace(" ", "_") for c in columns]
    table = Table()
    for col in columns:
        table.add_column(col)
    for row in rows:
        table.add_row(*[str(row.get(k, "")) for k in keys])
    console.print(table)


def print_kv(data: dict):
    for k, v in data.items():
        console.print(f"[bold]{k}[/bold]: {v}")


def handle_error(e: httpx.HTTPStatusError):
    url = str(e.request.url) if e.request else "unknown"
    invalid_credential = False
    body_text = e.response.text or ""
    body_is_html = "<html" in body_text.lower()[:200]

    # When the response is HTML AND the exception itself carries a CLI-authored
    # detail (e.g. from client._parse_json's route-aware HTML-detection paths
    # in client.py:_parse_json), prefer the author's message — it's route-aware
    # and operator-actionable in ways the body isn't. httpx.Response
    # .raise_for_status() messages always include "For more information check:";
    # CLI-authored messages don't. See #57.
    exc_message = str(e).strip()
    cli_authored = bool(exc_message) and "For more information check:" not in exc_message

    if body_is_html and cli_authored:
        detail = exc_message
    else:
        try:
            body = e.response.json()
            detail = body.get("detail", body_text[:200])
            if isinstance(detail, dict) and detail.get("error") == "invalid_credential":
                invalid_credential = True
        except Exception:
            body = body_text[:200]
            if body_is_html:
                detail = "Got HTML instead of JSON (frontend may be catching this route)"
            else:
                detail = body
                if "invalid_credential" in body.lower():
                    invalid_credential = True
    typer.echo(_redact_secrets(f"Error {e.response.status_code}: {detail}"), err=True)
    typer.echo(_redact_secrets(f"  URL: {url}"), err=True)
    if invalid_credential:
        host = ""
        try:
            host = e.request.url.host or ""
        except Exception:
            host = ""
        url_hint = f"https://{host}" if host else "<your-host>"
        typer.echo(
            "  Recovery: token rejected — likely from a different environment. "
            f"Run `axctl auth doctor --probe` to confirm, then `axctl login --url {url_hint}`.",
            err=True,
        )
    raise typer.Exit(1)
