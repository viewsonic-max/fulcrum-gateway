"""Declarative agent manifests (GH #91).

Operators describe an agent's intended configuration in a TOML file and
``ax gateway agents apply <manifest.toml>`` creates or updates the agent
to match it. ``ax gateway agents export <name>`` round-trips the current
registry entry back to manifest form so an operator can capture in-place
state as a source-of-truth file.

This module is the schema + diff + serialization layer. The CLI commands
in ``ax_cli/commands/gateway.py`` thread it together with the existing
``_register_managed_agent`` / ``_update_managed_agent`` / registry helpers.

Why a separate module: the manifest workflow has its own validation +
diff rules that are independent of the typer command surface and benefit
from being unit-tested in isolation. ``commands/gateway.py`` is already
~10k lines (see the refactor PR #43/#46) — keeping the manifest schema
out keeps it reviewable.

Format choice: TOML for v1 (matches the issue's primary spec, in stdlib
since 3.11 via ``tomllib``). YAML and JSON variants are deferred per the
issue's open question — the parser is a thin slice and a second format
would slot in without touching the schema or apply semantics.
"""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Any, TypedDict

# ── Schema ────────────────────────────────────────────────────────────────


class ManifestDict(TypedDict, total=False):
    """The fields a manifest may declare.

    All fields are optional at the TypedDict level; ``parse_manifest`` enforces
    ``name`` is required (the agent identity) and applies the mutually-exclusive
    rule on ``system_prompt`` vs ``system_prompt_file``. Everything else is
    optional and falls through to the existing register/update defaults.
    """

    name: str
    template: str  # → template_id
    type: str  # → runtime_type (advanced; mutually exclusive with template in practice)
    space: str  # → space_id
    workdir: str
    description: str
    model: str
    system_prompt: str
    system_prompt_file: str
    timeout_seconds: int
    allow_all_users: bool
    allowed_users: str  # comma-separated list per existing CLI
    disabled_toolsets: list[str]  # hermes_plugin only — Hermes toolsets to disable for this agent (#366)
    exec_command: str  # → exec_cmd
    client: str
    connector_ref: str
    provider: str  # hermes provider (e.g. openai-codex); hermes template only
    audience: str  # PAT audience for register-time only


#: Manifest field → ``_register_managed_agent`` / ``_update_managed_agent`` kwarg.
#:
#: This is the single source of truth for the mapping the issue lays out as
#: a table. Order is deterministic so ``export`` produces a stable file shape.
FIELD_TO_KWARG: dict[str, str] = {
    "name": "name",
    "template": "template_id",
    "type": "runtime_type",
    "space": "space_id",
    "workdir": "workdir",
    "description": "description",
    "model": "model",
    "system_prompt": "system_prompt",
    "system_prompt_file": "system_prompt_file",
    "timeout_seconds": "timeout_seconds",
    "allow_all_users": "allow_all_users",
    "allowed_users": "allowed_users",
    "disabled_toolsets": "disabled_toolsets",
    "exec_command": "exec_cmd",
    "client": "agent_client",
    "connector_ref": "connector_ref",
    "provider": "provider",
    "audience": "audience",
}

#: Registry entry field → manifest field. Used by ``export`` to round-trip an
#: in-memory registry entry into manifest shape. Mirrors FIELD_TO_KWARG but
#: keyed on the persisted-entry shape rather than the function-kwarg shape.
ENTRY_TO_MANIFEST: dict[str, str] = {
    "name": "name",
    "template_id": "template",
    "runtime_type": "type",
    "space_id": "space",
    "workdir": "workdir",
    "description": "description",
    "model": "model",
    "system_prompt": "system_prompt",
    "timeout_seconds": "timeout_seconds",
    "allow_all_users": "allow_all_users",
    "allowed_users": "allowed_users",
    "disabled_toolsets": "disabled_toolsets",
    "exec_command": "exec_command",
    "client": "client",
    "connector_ref": "connector_ref",
    "provider": "provider",
}

#: Fields whose ``register`` shape needs special handling — they don't pass
#: straight through. Currently just ``system_prompt`` (resolved via the existing
#: ``_resolve_system_prompt_input`` helper at apply time) — listed here for the
#: documentation contract.
SPECIAL_FIELDS: set[str] = {"system_prompt", "system_prompt_file"}


# ── Exceptions ────────────────────────────────────────────────────────────


class ManifestError(ValueError):
    """Raised when a manifest fails parsing or validation.

    The string form is the message printed to the operator — keep it short and
    actionable. Path-of-error context is the responsibility of the CLI command
    that calls ``parse_manifest``.
    """


# ── Parsing + validation ──────────────────────────────────────────────────


def parse_manifest(path: Path | str) -> ManifestDict:
    """Read + validate a manifest TOML file.

    Validation rules:
      - File exists and is readable
      - Top-level structure is a dict (TOML root)
      - ``name`` is present and non-empty
      - ``system_prompt`` and ``system_prompt_file`` are mutually exclusive
      - Unknown keys are reported (typo guard — manifests are operator-authored
        and a silently-ignored typo is a worse failure mode than a parse error)
      - Field types match TypedDict annotations (best-effort: TOML is already
        typed at the syntax level, but we coerce + check booleans / ints)

    Returns a ``ManifestDict`` (technically a regular dict at runtime) ready for
    handing to ``apply``. Raises ``ManifestError`` on every validation miss.
    """
    p = Path(path).expanduser()
    if not p.is_file():
        raise ManifestError(f"Manifest file not found: {p}")
    try:
        raw = tomllib.loads(p.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ManifestError(f"Failed to parse manifest {p}: {exc}") from exc

    if not isinstance(raw, dict):
        raise ManifestError(f"Manifest must be a TOML table at the root; got {type(raw).__name__}")

    known = set(FIELD_TO_KWARG)
    unknown = sorted(k for k in raw if k not in known)
    if unknown:
        raise ManifestError(f"Unknown manifest field(s): {', '.join(unknown)}. Allowed: {', '.join(sorted(known))}.")

    name = str(raw.get("name") or "").strip()
    if not name:
        raise ManifestError("Manifest is missing required field: name")

    if "system_prompt" in raw and "system_prompt_file" in raw:
        raise ManifestError("Manifest declares both system_prompt and system_prompt_file; they are mutually exclusive.")

    # Coerce + lightly validate types where TOML's typing is ambiguous.
    # tomllib already produces native Python types so most fields are fine.
    if "timeout_seconds" in raw:
        ts = raw["timeout_seconds"]
        if not isinstance(ts, int) or isinstance(ts, bool):  # bool is an int subclass
            raise ManifestError(f"timeout_seconds must be an integer; got {type(ts).__name__}")
        if ts <= 0:
            raise ManifestError(f"timeout_seconds must be positive; got {ts}")
    if "allow_all_users" in raw:
        au = raw["allow_all_users"]
        if not isinstance(au, bool):
            raise ManifestError(f"allow_all_users must be a boolean; got {type(au).__name__}")
    if "disabled_toolsets" in raw:
        dt = raw["disabled_toolsets"]
        if not isinstance(dt, list) or not all(isinstance(x, str) for x in dt):
            raise ManifestError(f"disabled_toolsets must be a list of strings; got {type(dt).__name__}")

    manifest: ManifestDict = {}
    for k, v in raw.items():
        if isinstance(v, str):
            # Strip strings, but never coerce empty → missing — an explicit empty
            # string in the manifest means "clear this field" on update.
            manifest[k] = v.strip() if k != "system_prompt" else v  # preserve system_prompt verbatim
        else:
            manifest[k] = v
    return manifest


# ── Diff ──────────────────────────────────────────────────────────────────


def compute_diff(manifest: ManifestDict, current: dict[str, Any] | None) -> list[dict[str, Any]]:
    """Compute a stable, sorted diff between a manifest and the current entry.

    ``current`` is the live registry entry (as returned by ``find_agent_entry``),
    or ``None`` when no agent with the manifest's ``name`` is in the registry yet
    (a future ``create``).

    Returns one row per manifest-declared field that would change, in the order
    those fields appear in ``FIELD_TO_KWARG``:
      ``{field, op, before, after}``
    where ``op`` is one of:
      ``create``    — the agent doesn't exist; field is being set for the first time
      ``add``       — the field is being set; no prior value
      ``change``    — the field has a different value
      ``noop``      — manifest matches current; included so ``--diff`` can show
                       "no changes" without misleading silence

    Diff is computed ONLY against fields the manifest declares. Fields not in
    the manifest are left alone — that's the ``_UNSET`` semantics of the
    underlying update helper and is the whole point of declarative apply.
    """
    rows: list[dict[str, Any]] = []
    creating = current is None
    for field in FIELD_TO_KWARG:
        if field not in manifest:
            continue
        if field == "system_prompt_file":
            # The file content is resolved at apply time; the diff line for the
            # file form is informational ("will read from <path>") rather than
            # a value comparison.
            after = f"<from file: {manifest[field]}>"
            before = None
            op = "create" if creating else "add"
            rows.append({"field": field, "op": op, "before": before, "after": after})
            continue
        after = manifest[field]
        if creating:
            rows.append({"field": field, "op": "create", "before": None, "after": after})
            continue
        # Look up the current value via the manifest→entry mapping. Some
        # manifest fields don't have a direct entry equivalent (e.g. audience
        # is register-only); skip those when computing the "current" side.
        entry_key = _manifest_to_entry_key(field)
        before = current.get(entry_key) if entry_key else None
        if _values_equal(before, after):
            rows.append({"field": field, "op": "noop", "before": before, "after": after})
        elif before is None or before == "":
            rows.append({"field": field, "op": "add", "before": before, "after": after})
        else:
            rows.append({"field": field, "op": "change", "before": before, "after": after})
    return rows


def _manifest_to_entry_key(manifest_field: str) -> str | None:
    """Map a manifest field name to the registry-entry field name, if any.

    Returns ``None`` for register-only fields (currently just ``audience``).
    """
    for entry_key, m_field in ENTRY_TO_MANIFEST.items():
        if m_field == manifest_field:
            return entry_key
    return None


def _values_equal(a: Any, b: Any) -> bool:
    """Treat ``None`` / empty string as equivalent so a missing-in-registry
    field doesn't show as a phantom change when the manifest declares an
    empty value (rare but possible)."""
    if a in (None, "") and b in (None, ""):
        return True
    return a == b


def render_diff(rows: list[dict[str, Any]]) -> str:
    """Render a diff for human display. Stable, deterministic, easy to grep.

    Format:
      ~ field_name: <before> → <after>   (change)
      + field_name: <after>                (add / create)
      = field_name: <value>                (noop, only shown when --verbose)

    The renderer suppresses ``noop`` rows by default; callers that want them
    (``--diff`` with explicit verbosity) pass them through directly.
    """
    if not rows:
        return "(no manifest-declared fields; nothing to do)"
    out: list[str] = []
    for row in rows:
        field = row["field"]
        op = row["op"]
        after = row["after"]
        before = row["before"]
        if op == "noop":
            continue
        if op in ("add", "create"):
            out.append(f"  + {field}: {_fmt(after)}")
        elif op == "change":
            out.append(f"  ~ {field}: {_fmt(before)} → {_fmt(after)}")
    if not out:
        return "(no changes)"
    return "\n".join(out)


def _fmt(value: Any) -> str:
    """Format a value for diff display — single-line, truncated for very long
    fields like ``system_prompt`` so the diff stays readable."""
    if value is None:
        return "<unset>"
    if isinstance(value, bool):
        return "true" if value else "false"
    s = str(value)
    if "\n" in s:
        first_line = s.split("\n", 1)[0]
        return f"{first_line[:80]} … ({len(s)} chars, {s.count(chr(10)) + 1} lines)"
    return s if len(s) <= 120 else s[:117] + "…"


# ── Apply (planning side) ─────────────────────────────────────────────────


def build_register_kwargs(manifest: ManifestDict) -> dict[str, Any]:
    """Translate a manifest into kwargs for ``_register_managed_agent``.

    Used when no entry exists for the manifest's ``name`` — we're creating from
    scratch. The CLI command resolves ``system_prompt`` / ``system_prompt_file``
    using the existing ``_resolve_system_prompt_input`` helper BEFORE calling
    this, so we pass the resolved prompt through and never see the file path.
    """
    kwargs: dict[str, Any] = {}
    for m_field, kwarg in FIELD_TO_KWARG.items():
        if m_field in SPECIAL_FIELDS:
            continue  # resolved by the caller
        if m_field in manifest:
            kwargs[kwarg] = manifest[m_field]
    return kwargs


def build_update_kwargs(manifest: ManifestDict, *, unset_sentinel: Any) -> dict[str, Any]:
    """Translate a manifest into kwargs for ``_update_managed_agent``.

    Fields ABSENT from the manifest map to ``unset_sentinel`` (the caller passes
    ``ax_cli.commands.gateway._UNSET``) so existing registry values are
    preserved untouched. This is the core declarative semantic — a manifest
    declares the fields the operator cares about, leaves the rest alone.

    Like ``build_register_kwargs``, ``system_prompt`` is resolved by the caller
    before this runs.
    """
    # Fields the update helper accepts (some are register-only and skipped)
    update_capable = {
        "template",
        "type",
        "workdir",
        "description",
        "model",
        "system_prompt",
        "timeout_seconds",
        "allow_all_users",
        "allowed_users",
        "disabled_toolsets",
        "exec_command",
        "client",
        "connector_ref",
        "provider",
    }
    kwargs: dict[str, Any] = {}
    for m_field in update_capable:
        kwarg = FIELD_TO_KWARG[m_field]
        if m_field in SPECIAL_FIELDS:
            continue
        if m_field in manifest:
            kwargs[kwarg] = manifest[m_field]
        else:
            kwargs[kwarg] = unset_sentinel
    return kwargs


# ── Export (round-trip) ───────────────────────────────────────────────────


def entry_to_manifest(entry: dict[str, Any]) -> ManifestDict:
    """Project a registry entry into manifest shape for round-trip export.

    Fields that the registry doesn't store (e.g. ``audience`` is consumed at
    register time and not persisted) are skipped. Empty / None values are
    dropped — a manifest's job is to describe intent, and an explicit empty
    value in an exported manifest would re-clear the field on next apply.
    """
    manifest: ManifestDict = {}
    for entry_key, m_field in ENTRY_TO_MANIFEST.items():
        value = entry.get(entry_key)
        if value is None or value == "":
            continue
        manifest[m_field] = value
    return manifest


def serialize_toml(manifest: ManifestDict) -> str:
    """Emit a manifest dict as TOML text.

    Hand-rolled rather than using a TOML writer dep — the schema is small and
    flat, and we want full control over key ordering and the multi-line string
    style for ``system_prompt``. Matches the example shape in GH #91.
    """
    lines: list[str] = []
    # Emit in FIELD_TO_KWARG order so the output is stable across exports
    for field in FIELD_TO_KWARG:
        if field not in manifest:
            continue
        value = manifest[field]
        if field == "system_prompt" and isinstance(value, str) and ("\n" in value or len(value) > 80):
            # Multi-line triple-quoted form — matches the issue's example.
            # Use ''' to avoid escaping double-quotes in operator-authored prompts.
            body = value.rstrip("\n")
            lines.append(f"{field} = '''")
            lines.extend(body.split("\n"))
            lines.append("'''")
            continue
        lines.append(f"{field} = {_toml_value(value)}")
    return "\n".join(lines) + "\n"


def _toml_value(value: Any) -> str:
    """Format a single scalar value for TOML output."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, str):
        # Use double-quotes; escape backslashes and double-quotes per TOML spec
        escaped = value.replace("\\", "\\\\").replace('"', '\\"')
        return f'"{escaped}"'
    # Fallback — shouldn't hit this with our schema, but be safe
    escaped = str(value).replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'
