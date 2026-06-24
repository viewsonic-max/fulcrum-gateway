"""Tests for ax_cli/agent_manifests.py — declarative agent manifests (GH #91).

Covers: TOML parsing + validation rules, diff computation against an empty
or existing registry entry, register/update kwarg construction, and TOML
serialization round-trip from registry entry back to manifest.
"""

from __future__ import annotations

import pytest

from ax_cli.agent_manifests import (
    ENTRY_TO_MANIFEST,
    FIELD_TO_KWARG,
    ManifestError,
    build_register_kwargs,
    build_update_kwargs,
    compute_diff,
    entry_to_manifest,
    parse_manifest,
    render_diff,
    serialize_toml,
)

# ── Parse + validate ─────────────────────────────────────────────────────


def _write(tmp_path, body: str) -> str:
    p = tmp_path / "nova.agent.toml"
    p.write_text(body, encoding="utf-8")
    return str(p)


def test_parse_minimal_manifest(tmp_path):
    """A manifest with just ``name`` parses cleanly. Everything else is optional."""
    path = _write(tmp_path, 'name = "nova"\n')
    m = parse_manifest(path)
    assert m == {"name": "nova"}


def test_parse_full_manifest(tmp_path):
    """Round-trip every field the schema accepts."""
    path = _write(
        tmp_path,
        """
name = "nova"
template = "hermes"
space = "andrewprograde-workspace"
workdir = "/workspace"
allow_all_users = true
description = "Hermes agent for the nova workspace"
model = "codex:gpt-5.5"
timeout_seconds = 300
system_prompt = "Be terse."
""".strip(),
    )
    m = parse_manifest(path)
    assert m["name"] == "nova"
    assert m["template"] == "hermes"
    assert m["space"] == "andrewprograde-workspace"
    assert m["workdir"] == "/workspace"
    assert m["allow_all_users"] is True
    assert m["timeout_seconds"] == 300
    assert m["system_prompt"] == "Be terse."


def test_parse_missing_name_rejects(tmp_path):
    """``name`` is the agent identity — without it, apply has no entry to find or create."""
    path = _write(tmp_path, 'template = "hermes"\n')
    with pytest.raises(ManifestError, match="name"):
        parse_manifest(path)


def test_parse_blank_name_rejects(tmp_path):
    """Whitespace-only ``name`` is functionally missing — same error class."""
    path = _write(tmp_path, 'name = "   "\n')
    with pytest.raises(ManifestError, match="name"):
        parse_manifest(path)


def test_parse_unknown_field_rejects(tmp_path):
    """Typos like ``descripion`` should fail loudly. Operators author manifests
    by hand; a silently-ignored typo is the worst failure mode."""
    path = _write(tmp_path, 'name = "nova"\ndescripion = "typo here"\n')
    with pytest.raises(ManifestError, match="Unknown manifest field"):
        parse_manifest(path)


def test_parse_mutually_exclusive_prompt_rejects(tmp_path):
    """``system_prompt`` and ``system_prompt_file`` are mutually exclusive on
    the CLI; same rule on the manifest."""
    path = _write(
        tmp_path,
        'name = "nova"\nsystem_prompt = "inline"\nsystem_prompt_file = "/path/to/prompt.txt"\n',
    )
    with pytest.raises(ManifestError, match="mutually exclusive"):
        parse_manifest(path)


def test_parse_bad_timeout_type_rejects(tmp_path):
    """Operator types ``timeout_seconds = "300"`` (quoted) — should fail with a
    clear error rather than be silently coerced."""
    path = _write(tmp_path, 'name = "nova"\ntimeout_seconds = "300"\n')
    with pytest.raises(ManifestError, match="timeout_seconds.*integer"):
        parse_manifest(path)


def test_parse_negative_timeout_rejects(tmp_path):
    path = _write(tmp_path, 'name = "nova"\ntimeout_seconds = -1\n')
    with pytest.raises(ManifestError, match="positive"):
        parse_manifest(path)


def test_parse_bad_allow_all_users_rejects(tmp_path):
    """A string ``"true"`` instead of a TOML bool — TOML's strict typing makes
    this an easy mistake; surface it loudly."""
    path = _write(tmp_path, 'name = "nova"\nallow_all_users = "true"\n')
    with pytest.raises(ManifestError, match="allow_all_users.*boolean"):
        parse_manifest(path)


def test_parse_missing_file_raises():
    """File-not-found should produce a manifest error, not an OSError."""
    with pytest.raises(ManifestError, match="not found"):
        parse_manifest("/does/not/exist/nova.agent.toml")


def test_parse_malformed_toml_raises(tmp_path):
    """Broken TOML syntax wraps to ManifestError (not tomllib.TOMLDecodeError)."""
    path = _write(tmp_path, "name = nova\n")  # unquoted string is invalid TOML
    with pytest.raises(ManifestError, match="Failed to parse"):
        parse_manifest(path)


def test_parse_strips_string_whitespace_except_prompt(tmp_path):
    """Most string fields get ``.strip()`` so trailing whitespace doesn't
    surprise the operator. ``system_prompt`` is preserved verbatim because
    intentional trailing whitespace and indentation matter for LLM prompts."""
    path = _write(
        tmp_path,
        'name = "  nova  "\nsystem_prompt = "  trailing space  "\n',
    )
    m = parse_manifest(path)
    assert m["name"] == "nova"
    assert m["system_prompt"] == "  trailing space  "


# ── Diff ──────────────────────────────────────────────────────────────────


def test_diff_creating_emits_create_rows():
    """No existing entry → every manifest field shows as a ``create`` row."""
    manifest = {"name": "nova", "template": "hermes", "timeout_seconds": 300}
    rows = compute_diff(manifest, current=None)
    ops = {r["field"]: r["op"] for r in rows}
    assert ops == {"name": "create", "template": "create", "timeout_seconds": "create"}


def test_diff_unchanged_emits_noop():
    """Manifest declares a field that matches current → ``noop``."""
    manifest = {"name": "nova", "template": "hermes"}
    current = {"name": "nova", "template_id": "hermes", "runtime_type": "hermes_plugin"}
    rows = compute_diff(manifest, current)
    ops = {r["field"]: r["op"] for r in rows}
    assert ops["template"] == "noop"


def test_diff_changed_emits_change():
    manifest = {"name": "nova", "template": "claude_code_channel"}
    current = {"name": "nova", "template_id": "hermes"}
    rows = compute_diff(manifest, current)
    row = next(r for r in rows if r["field"] == "template")
    assert row["op"] == "change"
    assert row["before"] == "hermes"
    assert row["after"] == "claude_code_channel"


def test_diff_added_emits_add():
    """Field declared in manifest but absent from current entry → ``add``."""
    manifest = {"name": "nova", "description": "First description"}
    current = {"name": "nova"}  # no description
    rows = compute_diff(manifest, current)
    row = next(r for r in rows if r["field"] == "description")
    assert row["op"] == "add"
    assert row["before"] is None


def test_diff_ignores_fields_not_in_manifest():
    """The whole point of declarative apply: fields absent from the manifest
    are left untouched (``_UNSET`` semantics)."""
    manifest = {"name": "nova"}
    current = {"name": "nova", "description": "DO NOT CLEAR", "model": "gpt-4"}
    rows = compute_diff(manifest, current)
    # Only ``name`` was declared; only ``name`` shows up in the diff
    assert [r["field"] for r in rows] == ["name"]


def test_diff_empty_vs_none_treated_as_equal():
    """Phantom diff guard: a manifest declaring an empty string for a field
    that's None in the registry shouldn't show as a change."""
    manifest = {"name": "nova", "description": ""}
    current = {"name": "nova", "description": None}
    rows = compute_diff(manifest, current)
    description_row = next(r for r in rows if r["field"] == "description")
    assert description_row["op"] == "noop"


def test_diff_system_prompt_file_treated_as_informational():
    """The file form can't be value-compared without reading it; the diff line
    is informational so the operator knows where it's coming from."""
    manifest = {"name": "nova", "system_prompt_file": "/etc/nova-prompt.txt"}
    rows = compute_diff(manifest, current={"name": "nova"})
    row = next(r for r in rows if r["field"] == "system_prompt_file")
    assert row["op"] == "add"
    assert "from file" in row["after"]


# ── Render diff ───────────────────────────────────────────────────────────


def test_render_diff_empty_rows():
    assert "no manifest-declared fields" in render_diff([])


def test_render_diff_only_noops():
    rows = [{"field": "name", "op": "noop", "before": "nova", "after": "nova"}]
    out = render_diff(rows)
    assert "no changes" in out


def test_render_diff_change_uses_arrow():
    rows = [{"field": "template", "op": "change", "before": "hermes", "after": "claude_code_channel"}]
    out = render_diff(rows)
    assert "~ template" in out
    assert "→" in out
    assert "hermes" in out
    assert "claude_code_channel" in out


def test_render_diff_long_value_truncated():
    """Verify long single-line values are truncated so the diff stays readable
    in a terminal."""
    long_value = "x" * 500
    rows = [{"field": "description", "op": "add", "before": None, "after": long_value}]
    out = render_diff(rows)
    assert "…" in out  # truncation marker
    assert len(out) < 200  # not the full 500 chars


def test_render_diff_multiline_summarized():
    """Multi-line values (system_prompt) get a single-line summary instead of
    dumping the whole block into the diff."""
    prompt = "Line one\nLine two\nLine three"
    rows = [{"field": "system_prompt", "op": "add", "before": None, "after": prompt}]
    out = render_diff(rows)
    assert "Line two" not in out  # only first line is shown
    assert "3 lines" in out or "chars" in out


# ── Build kwargs ──────────────────────────────────────────────────────────


def test_build_register_kwargs_maps_manifest_to_register_args():
    """register-kwargs only includes fields the manifest declared; the special
    ``system_prompt`` fields are skipped (caller resolves them)."""
    manifest = {
        "name": "nova",
        "template": "hermes",
        "space": "ws-1",
        "workdir": "/workspace",
        "system_prompt": "Ignored here — caller resolves",
    }
    kwargs = build_register_kwargs(manifest)
    assert kwargs == {
        "name": "nova",
        "template_id": "hermes",
        "space_id": "ws-1",
        "workdir": "/workspace",
    }
    assert "system_prompt" not in kwargs


def test_build_update_kwargs_fills_missing_with_sentinel():
    """The whole point of the manifest semantics: a missing field maps to
    the ``_UNSET`` sentinel so the update helper leaves the existing value
    untouched."""
    sentinel = object()
    manifest = {"name": "nova", "template": "claude_code_channel"}
    kwargs = build_update_kwargs(manifest, unset_sentinel=sentinel)
    # The fields the update helper accepts but the manifest didn't declare
    # all come back as the sentinel
    assert kwargs["workdir"] is sentinel
    assert kwargs["description"] is sentinel
    assert kwargs["timeout_seconds"] is sentinel
    # Declared field passes through
    assert kwargs["template_id"] == "claude_code_channel"
    # ``system_prompt`` is skipped here too — caller resolves
    assert "system_prompt" not in kwargs


# ── Export round-trip ─────────────────────────────────────────────────────


def test_entry_to_manifest_drops_none_and_empty():
    """The export shape is "intent" — empty / None fields aren't intentions,
    so they shouldn't appear in the exported manifest."""
    entry = {
        "name": "nova",
        "template_id": "hermes",
        "description": "",
        "model": None,
        "workdir": "/workspace",
    }
    m = entry_to_manifest(entry)
    assert m == {"name": "nova", "template": "hermes", "workdir": "/workspace"}


def test_serialize_toml_basic():
    """Standard scalars: string, int, bool. The output is parseable by
    tomllib (regression for any escape-quote bugs)."""
    import tomllib

    manifest = {"name": "nova", "timeout_seconds": 300, "allow_all_users": True}
    toml_text = serialize_toml(manifest)
    parsed = tomllib.loads(toml_text)
    assert parsed == {"name": "nova", "timeout_seconds": 300, "allow_all_users": True}


def test_serialize_toml_multiline_prompt_uses_triple_quotes():
    """Multi-line ``system_prompt`` should use ``'''`` form so embedded
    quotes don't need escaping. Re-parses to the same value."""
    import tomllib

    prompt = "You are nova.\nBe terse.\n"
    manifest = {"name": "nova", "system_prompt": prompt}
    toml_text = serialize_toml(manifest)
    assert "'''" in toml_text
    parsed = tomllib.loads(toml_text)
    # tomllib strips the leading newline after ''' but otherwise preserves content
    assert "You are nova." in parsed["system_prompt"]
    assert "Be terse." in parsed["system_prompt"]


def test_serialize_toml_escapes_quotes():
    """A description containing double-quotes must survive serialization."""
    import tomllib

    manifest = {"name": "nova", "description": 'Title with "embedded" quotes'}
    toml_text = serialize_toml(manifest)
    parsed = tomllib.loads(toml_text)
    assert parsed["description"] == 'Title with "embedded" quotes'


def test_export_then_apply_round_trip(tmp_path):
    """End-to-end: serialize a registry entry, parse it back, get the same
    manifest. This pins the ``export``→``apply`` round trip."""
    import tomllib

    entry = {
        "name": "nova",
        "template_id": "hermes",
        "workdir": "/workspace",
        "model": "codex:gpt-5.5",
        "timeout_seconds": 300,
        "allow_all_users": True,
    }
    expected_manifest = entry_to_manifest(entry)
    toml_text = serialize_toml(expected_manifest)
    p = tmp_path / "round-trip.toml"
    p.write_text(toml_text, encoding="utf-8")
    parsed = parse_manifest(str(p))
    # Compare with raw tomllib to make sure the parser didn't mangle anything
    raw = tomllib.loads(toml_text)
    for k in expected_manifest:
        assert parsed[k] == expected_manifest[k]
        assert raw[k] == expected_manifest[k]


# ── Schema integrity ──────────────────────────────────────────────────────


def test_every_field_in_field_map_is_known():
    """Belt-and-suspenders: every entry in FIELD_TO_KWARG should have an
    inverse in ENTRY_TO_MANIFEST or be explicitly register-only (audience).

    Catches the bug where someone adds a manifest field but forgets to wire
    the export side."""
    register_only = {"audience", "system_prompt_file"}
    inverse_keys = set(ENTRY_TO_MANIFEST.values())
    for field in FIELD_TO_KWARG:
        if field in register_only:
            continue
        assert field in inverse_keys, f"FIELD_TO_KWARG has {field} but ENTRY_TO_MANIFEST does not"


# ── provider field ────────────────────────────────────────────────────────


def test_provider_field_round_trips_through_manifest(tmp_path):
    """provider parses, maps to kwargs, exports, and serializes correctly."""
    p = tmp_path / "codex-coder.toml"
    p.write_text(
        'name = "codex-coder"\ntemplate = "hermes"\nprovider = "openai-codex"\n',
        encoding="utf-8",
    )
    m = parse_manifest(str(p))
    assert m["provider"] == "openai-codex"

    # build_register_kwargs passes provider through
    kwargs = build_register_kwargs(m)
    assert kwargs["provider"] == "openai-codex"

    # build_update_kwargs includes provider
    sentinel = object()
    update_kwargs = build_update_kwargs(m, unset_sentinel=sentinel)
    assert update_kwargs["provider"] == "openai-codex"

    # entry_to_manifest exports provider from a registry entry
    entry = {"name": "codex-coder", "template_id": "hermes", "provider": "openai-codex"}
    exported = entry_to_manifest(entry)
    assert exported["provider"] == "openai-codex"

    # serialize_toml emits the provider line
    toml_text = serialize_toml(exported)
    assert 'provider = "openai-codex"' in toml_text


def test_parse_disabled_toolsets_accepts_list_of_strings(tmp_path):
    """#366: disabled_toolsets manifest field accepts a list of strings."""
    p = tmp_path / "agent.toml"
    p.write_text('name = "pr-reviewer"\ndisabled_toolsets = ["terminal", "code_execution"]\n')
    m = parse_manifest(p)
    assert m["disabled_toolsets"] == ["terminal", "code_execution"]


def test_parse_bad_disabled_toolsets_type_rejects(tmp_path):
    p = tmp_path / "agent.toml"
    p.write_text('name = "x"\ndisabled_toolsets = "terminal"\n')
    with pytest.raises(ManifestError, match="disabled_toolsets"):
        parse_manifest(p)


def test_parse_disabled_toolsets_with_non_string_entries_rejects(tmp_path):
    p = tmp_path / "agent.toml"
    p.write_text('name = "x"\ndisabled_toolsets = ["terminal", 42]\n')
    with pytest.raises(ManifestError, match="disabled_toolsets"):
        parse_manifest(p)


def test_build_register_kwargs_threads_disabled_toolsets():
    m = {"name": "x", "type": "hermes_plugin", "disabled_toolsets": ["terminal"]}
    kw = build_register_kwargs(m)
    assert kw["disabled_toolsets"] == ["terminal"]


def test_build_update_kwargs_threads_disabled_toolsets(tmp_path):
    m = {"name": "x", "disabled_toolsets": ["code_execution"]}
    sentinel = object()
    kw = build_update_kwargs(m, unset_sentinel=sentinel)
    assert kw["disabled_toolsets"] == ["code_execution"]


def test_entry_to_manifest_round_trips_disabled_toolsets():
    entry = {"name": "x", "disabled_toolsets": ["terminal", "tts"]}
    m = entry_to_manifest(entry)
    assert m["disabled_toolsets"] == ["terminal", "tts"]
