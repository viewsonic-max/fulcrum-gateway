"""Business logic for ax agents profiles — runtime settings management."""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

_PROFILES_DIR = Path(__file__).parent / "agent_profiles"
_AX_PROFILES_KEY = "_axProfiles"

# Per-client config. Add a new entry here when a client gains profile support.
#
# `client` here is the MCP/coding-agent client whose settings format the
# profile fragments target (per ADR-010 §3 / ADR-011 — the profile directory
# maps to the `--client` parameter), NOT the Gateway `runtime_type` (how
# Gateway supervises the agent process — see `_gateway_runtime_to_client`
# below for the mapping between the two axes).
#
# settings_path: relative path within the agent workdir for the settings file.
_CLIENT_CONFIG: dict[str, dict[str, Any]] = {
    "claude_cli": {
        "settings_path": ".claude/settings.local.json",
    },
}

SUPPORTED_CLIENTS: frozenset[str] = frozenset(_CLIENT_CONFIG)

# Maps Gateway `runtime_type` (how Gateway supervises the agent process) to the
# profile client (which MCP/coding-agent tool the agent runs, and therefore
# which settings file and profile fragments apply). These are different axes —
# `claude_code_channel` and `sentinel_cli` both run the Claude Code CLI and
# share its `.claude/settings.local.json` surface, so both resolve to `claude_cli`
# (per ADR-014 — `claude_cli` names the tool, not the AI model).
# `hermes_plugin` and the sentinel SDK runtimes run Hermes's own AIAgent (no
# `.claude/settings.local.json`; their capability surface is connector policy
# instead — see docs/agent-permission-model.md), and runtimes with no
# agent-owned settings surface (echo, exec, inbox, ollama) aren't covered yet.
# Unmapped runtimes resolve to None so callers can surface a clear message
# rather than guessing.
_GATEWAY_RUNTIME_TO_CLIENT: dict[str, str] = {
    "claude_code_channel": "claude_cli",
    "sentinel_cli": "claude_cli",
}


def _gateway_runtime_to_client(runtime_type: str | None) -> str | None:
    """Derive the profile client implied by a Gateway ``runtime_type``.

    Returns None when the gateway runtime has no profile support yet, so
    callers can surface a clear message instead of guessing or crashing.
    """
    return _GATEWAY_RUNTIME_TO_CLIENT.get(str(runtime_type or "").strip().lower())


def _require_supported_client(client: str) -> None:
    if client not in SUPPORTED_CLIENTS:
        supported = ", ".join(sorted(SUPPORTED_CLIENTS))
        raise ValueError(f"Client '{client}' is not supported for profiles apply/diff (supported: {supported})")


def _settings_path(workdir: str | Path, client: str) -> Path:
    return Path(workdir) / _CLIENT_CONFIG[client]["settings_path"]


def _read_settings(workdir: str | Path, client: str) -> dict[str, Any]:
    """Read the client's settings file, or {} if it doesn't exist.

    Raises ValueError if the file exists but isn't valid JSON — callers
    (notably `apply`) must not silently treat an unreadable file as empty,
    since merging into {} and writing back would discard its content.
    """
    path = _settings_path(workdir, client)
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text())
    except (json.JSONDecodeError, OSError) as exc:
        raise ValueError(f"Could not read existing settings file {path}: {exc}") from exc


# ---------------------------------------------------------------------------
# Profile fragment discovery
# ---------------------------------------------------------------------------


def _profiles_dir(client: str) -> Path:
    return _PROFILES_DIR / client


def list_available(client: str) -> list[str]:
    """Return profile names available for *client* (filename stems, no extension)."""
    d = _profiles_dir(client)
    if not d.is_dir():
        return []
    return sorted(p.stem for p in d.glob("*.json"))


def list_all() -> dict[str, list[str]]:
    """Return all profiles grouped by client: {client: [profile, ...]}.

    Only includes clients in SUPPORTED_CLIENTS — fixtures for unsupported
    clients (e.g. ``echo``, used in tests) ship in the package tree but
    can never be applied, so they're excluded here.
    """
    if not _PROFILES_DIR.is_dir():
        return {}
    return {
        d.name: list_available(d.name)
        for d in sorted(_PROFILES_DIR.iterdir())
        if d.is_dir() and d.name in SUPPORTED_CLIENTS
    }


# ---------------------------------------------------------------------------
# Fragment loading and merging
# ---------------------------------------------------------------------------


def _load_profile_fragment(client: str, profile_name: str) -> dict[str, Any]:
    if "/" in profile_name or "\\" in profile_name or profile_name in (".", ".."):
        raise ValueError(f"Invalid profile name '{profile_name}'")
    path = _profiles_dir(client) / f"{profile_name}.json"
    if not path.exists():
        raise FileNotFoundError(f"Profile '{profile_name}' not found for client '{client}' (looked in {path})")
    return json.loads(path.read_text())


def _deep_merge(base: dict[str, Any], overlay: dict[str, Any], path: tuple[str, ...] = ()) -> None:
    """Merge *overlay* into *base* in-place: lists union, scalars last-wins.

    List union requires hashable items (`set(base[key])`). Today's fragments
    are all string lists, so this holds — but a fragment that introduces a
    list of dicts (e.g. ``hooks``) would otherwise raise an opaque TypeError.
    Raise a clear ValueError instead, naming the offending key, so it's
    caught by the CLI's existing ValueError handling.
    """
    for key, val in overlay.items():
        sub_path = path + (key,)
        if key in base:
            if isinstance(base[key], dict) and isinstance(val, dict):
                _deep_merge(base[key], val, sub_path)
            elif isinstance(base[key], list) and isinstance(val, list):
                try:
                    existing = set(base[key])
                except TypeError as exc:
                    label = ".".join(sub_path)
                    raise ValueError(
                        f"Cannot merge list at '{label}': items must be hashable (got non-hashable items, e.g. dicts)"
                    ) from exc
                base[key] = base[key] + [item for item in val if item not in existing]
            else:
                base[key] = val
        else:
            base[key] = val


def _merge_fragments(fragments: list[dict[str, Any]]) -> dict[str, Any]:
    merged: dict[str, Any] = {}
    for fragment in fragments:
        _deep_merge(merged, fragment)
    return merged


def resolve(profiles: list[str], client: str) -> dict[str, Any]:
    """Merge profile fragments for *profiles* into a single settings dict."""
    fragments = [_load_profile_fragment(client, p) for p in profiles]
    return _merge_fragments(fragments)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def current_profile_list(workdir: str | Path, client: str) -> list[str]:
    """Return the profiles currently applied for *client* in *workdir*."""
    _require_supported_client(client)
    return list(_read_settings(workdir, client).get(_AX_PROFILES_KEY, []))


def _list_diff(
    current: dict[str, Any], target: dict[str, Any], path: tuple[str, ...] = ()
) -> tuple[list[str], list[str]]:
    """Recursively compare list-valued entries between *current* and *target*.

    Profile fragments can touch any list in the settings tree — tool
    permissions (permissions.allow, permissions.deny) and MCP server
    enablement (enabledMcpjsonServers) alike — via the same generic
    `_deep_merge` that `apply()` uses, so `diff()` needs to walk the whole
    tree rather than special-case `permissions.allow`. Entries are labelled
    with their dotted path (e.g. "permissions.allow: mcp__ax-channel__*") so
    additions to different lists stay distinguishable in the +/- summary.

    `_AX_PROFILES_KEY` bookkeeping is skipped — `apply()` always overwrites it
    with the new profile list, so it's not "applied settings" content and
    diffing it would just produce noise when switching between profiles.
    """
    add: list[str] = []
    remove: list[str] = []
    for key in sorted(set(current) | set(target)):
        if key == _AX_PROFILES_KEY:
            continue
        sub_path = path + (key,)
        cur_val = current.get(key)
        tgt_val = target.get(key)
        if isinstance(cur_val, dict) or isinstance(tgt_val, dict):
            sub_add, sub_remove = _list_diff(
                cur_val if isinstance(cur_val, dict) else {},
                tgt_val if isinstance(tgt_val, dict) else {},
                sub_path,
            )
            add += sub_add
            remove += sub_remove
        elif isinstance(cur_val, list) or isinstance(tgt_val, list):
            cur_set = set(cur_val) if isinstance(cur_val, list) else set()
            tgt_set = set(tgt_val) if isinstance(tgt_val, list) else set()
            label = ".".join(sub_path)
            add += [f"{label}: {item}" for item in sorted(tgt_set - cur_set)]
            remove += [f"{label}: {item}" for item in sorted(cur_set - tgt_set)]
    return add, remove


def diff(profiles: list[str], client: str, workdir: str | Path, *, reset: bool = False) -> dict[str, Any]:
    """Return a dict describing the difference between current settings and *profiles*.

    Mirrors `apply()`'s semantics for *reset*: by default `apply` merges into
    existing settings (nothing is removed), so the default diff target is
    `merge(current, resolved)` too — `remove` will be empty unless `--reset`
    is also used. With *reset*, the target is the resolved profile content
    alone, matching `apply(..., reset=True)`'s replace semantics.

    Raises ValueError for unsupported clients.

    Walks every list-valued entry in the resolved settings tree — not just
    permissions.allow — so additions like enabledMcpjsonServers or
    permissions.deny show up alongside tool-permission changes.

    Keys: ``add``, ``remove`` (lists of "<dotted.path>: <item>" entries),
    ``profiles_before``, ``profiles_after``.
    """
    _require_supported_client(client)

    current = _read_settings(workdir, client)
    current_profiles: list[str] = current.get(_AX_PROFILES_KEY, [])
    resolved = resolve(profiles, client)
    if reset:
        target = resolved
    else:
        target = copy.deepcopy({k: v for k, v in current.items() if k != _AX_PROFILES_KEY})
        _deep_merge(target, resolved)
    add, remove = _list_diff(current, target)

    return {
        "profiles_before": current_profiles,
        "profiles_after": profiles,
        "add": add,
        "remove": remove,
    }


def write_model(workdir: str | Path, client: str, model: str | None) -> Path:
    """Write or remove the ``model`` key in the client's settings file in *workdir*.

    Preserves all other keys including ``_axProfiles``. If *model* is None or
    empty the key is removed. Creates the settings file and its parent directory
    if they don't exist.

    Returns the path written.
    """
    _require_supported_client(client)
    path = _settings_path(workdir, client)
    path.parent.mkdir(parents=True, exist_ok=True)
    current = _read_settings(workdir, client)
    normalized = str(model or "").strip() or None
    if normalized:
        current["model"] = normalized
    else:
        current.pop("model", None)
    path.write_text(json.dumps(current, indent=2, sort_keys=True) + "\n")
    return path


def apply(
    profiles: list[str],
    client: str,
    workdir: str | Path,
    *,
    reset: bool = False,
) -> Path:
    """Merge profiles into the client's settings file in *workdir*.

    If *reset* is True, start from a clean slate (only profile content).
    Otherwise, merge into existing settings (list union, scalar last-wins).

    Raises ValueError for unsupported clients.

    Returns the path written.
    """
    _require_supported_client(client)
    path = _settings_path(workdir, client)
    path.parent.mkdir(parents=True, exist_ok=True)

    base: dict[str, Any] = {} if reset else _read_settings(workdir, client)
    base.pop(_AX_PROFILES_KEY, None)

    _deep_merge(base, resolve(profiles, client))
    base[_AX_PROFILES_KEY] = profiles

    path.write_text(json.dumps(base, indent=2, sort_keys=True) + "\n")
    return path


class RegistryLookupError(Exception):
    """The local Gateway registry could not be consulted for agent info.

    Distinct from "agent not found" (a healthy registry with no entry for the
    agent, which `agent_info_from_registry` reports as None): this means the
    registry itself is missing or unreadable, and the message says which.
    """


def agent_info_from_registry(agent_name: str) -> dict[str, str | None] | None:
    """Return registry-derived info for *agent_name*, or None if the agent
    has no entry in the registry.

    Keys:
      ``workdir``     — the agent's workdir, or None if unset.
      ``runtime_type``— the raw Gateway runtime_type (e.g. ``claude_code_channel``),
                        or None if unset.
      ``client``      — the profile client derived from ``runtime_type`` via
                        `_gateway_runtime_to_client` (e.g. ``claude_cli``), or None
                        when that gateway runtime has no profile support yet.

    Raises RegistryLookupError when the registry file is missing (the Gateway
    has likely never run on this machine) or exists but can't be read or
    parsed — collapsing those into "not found" sends the operator to check
    the daemon when the real problem is a broken registry file (#298).
    """
    from . import gateway as gateway_core

    registry_file = gateway_core.registry_path()
    if not registry_file.exists():
        raise RegistryLookupError(f"No local Gateway registry found at {registry_file}. Is the Gateway running?")
    try:
        registry = gateway_core.load_gateway_registry()
    # json.JSONDecodeError and UnicodeDecodeError are ValueErrors; OSError
    # covers permission/IO failures. Anything else is a bug and should crash.
    except (OSError, ValueError) as exc:
        raise RegistryLookupError(f"Could not read the Gateway registry at {registry_file}: {exc}") from exc
    entry = gateway_core.find_agent_entry(registry, agent_name)
    if not entry:
        return None
    workdir = str(entry.get("workdir") or "").strip() or None
    runtime_type = str(entry.get("runtime_type") or "").strip() or None
    return {
        "workdir": workdir,
        "runtime_type": runtime_type,
        "client": _gateway_runtime_to_client(runtime_type),
    }
