"""Gateway filesystem paths and registry/session/space-cache/PID/activity I/O.

Extracted from ``ax_cli/gateway.py`` (issue #28 Phase 2).
"""

from __future__ import annotations

import contextlib
import copy
import hashlib
import json
import os
import re
import subprocess
import tempfile
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import _global_config_dir
from .gateway_constants import _GATEWAY_PROCESS_RE, _GATEWAY_UI_PROCESS_RE, DEFAULT_ACTIVITY_LIMIT, phase_for_event

_ACTIVITY_LOCK = threading.Lock()


@contextlib.contextmanager
def _exclusive_activity_lock(path: Path):
    """Cross-process exclusive lock for the activity log write path.

    Uses fcntl.flock(LOCK_EX) on a companion .lock file on POSIX systems so
    two separate ax/gateway processes can't interleave their read-modify-append
    and produce a forked chain (duplicate seq + prev_hash_mismatch).

    Falls back to the in-process threading.Lock on platforms where fcntl is
    unavailable (Windows), which preserves the pre-existing within-process
    guarantee without crashing.

    Note: on network filesystems where flock is a silent no-op (e.g. NFS
    without lockd), the cross-process guarantee degrades to in-process only.
    """
    try:
        import fcntl as _fcntl
    except ImportError:
        with _ACTIVITY_LOCK:
            yield
        return
    lock_path = path.with_suffix(".lock")
    with lock_path.open("w") as _lf:
        _fcntl.flock(_lf.fileno(), _fcntl.LOCK_EX)
        try:
            yield
        finally:
            _fcntl.flock(_lf.fileno(), _fcntl.LOCK_UN)


def _chmod_quiet(path: Path, mode: int) -> None:
    """Best-effort chmod that tolerates EPERM when the mode is already correct.

    macOS sandboxes (e.g. Codex sandbox-exec) raise PermissionError on chmod
    against an already-existing directory even when the mode would not change.
    Swallow that case so Gateway-touching commands don't crash; re-raise if the
    mode is actually wrong so we don't leak a too-permissive dir silently.
    """
    try:
        path.chmod(mode)
    except PermissionError:
        try:
            current = path.stat().st_mode & 0o777
        except OSError:
            raise
        if current != mode:
            raise


def gateway_dir() -> Path:
    explicit = str(os.environ.get("AX_GATEWAY_DIR") or "").strip()
    if explicit:
        path = Path(explicit).expanduser()
    else:
        root = _global_config_dir() / "gateway"
        env_name = gateway_environment()
        path = root if env_name is None else root / "envs" / env_name
    path.mkdir(parents=True, exist_ok=True)
    _chmod_quiet(path, 0o700)
    return path


def gateway_environment() -> str | None:
    raw = (
        str(os.environ.get("AX_GATEWAY_ENV") or "").strip()
        or str(os.environ.get("AX_USER_ENV") or "").strip()
        or str(os.environ.get("AX_ENV") or "").strip()
    )
    if not raw:
        return None
    normalized = re.sub(r"[^a-z0-9_.-]+", "-", raw.lower()).strip(".-")
    if not normalized or normalized in {"default", "user"}:
        return None
    return normalized


def gateway_agents_dir() -> Path:
    path = gateway_dir() / "agents"
    path.mkdir(parents=True, exist_ok=True)
    _chmod_quiet(path, 0o700)
    return path


def session_path() -> Path:
    return gateway_dir() / "session.json"


def registry_path() -> Path:
    return gateway_dir() / "registry.json"


def pid_path() -> Path:
    return gateway_dir() / "gateway.pid"


def ui_state_path() -> Path:
    return gateway_dir() / "gateway-ui.json"


def daemon_log_path() -> Path:
    return gateway_dir() / "gateway.log"


def ui_log_path() -> Path:
    return gateway_dir() / "gateway-ui.log"


def activity_log_path() -> Path:
    return gateway_dir() / "activity.jsonl"


def api_requests_log_path() -> Path:
    return gateway_dir() / "api-requests.log"


_API_REQUESTS_LOG_MAX_BYTES = 10 * 1024 * 1024  # rotate at 10MB
_API_REQUESTS_LOG_BACKUPS = 1  # keep one rotated file: api-requests.log.1


class _RequestLogger:
    """Logs every API request to api-requests.log unless AX_LOG_API_REQUESTS is disabled.

    Each instance carries a role label and optional agent identity so log
    records identify which process/client/agent made the request. All three
    gateway destinations (daemon, UI server, CLI) write to the same file via
    O_APPEND; a per-instance lock serialises writes (and rotation) within each
    process. The log rotates at ``_API_REQUESTS_LOG_MAX_BYTES``, keeping one
    backup file.
    """

    def __init__(self, role: str) -> None:
        self.role = role
        self._lock = threading.Lock()
        self._enabled = os.environ.get("AX_LOG_API_REQUESTS", "").lower() not in {"0", "false", "no"}

    def _rotate_if_needed(self, log_path: Path) -> None:
        try:
            if log_path.stat().st_size < _API_REQUESTS_LOG_MAX_BYTES:
                return
        except OSError:
            return
        backup_path = log_path.with_name(log_path.name + ".1")
        try:
            backup_path.unlink(missing_ok=True)
            log_path.rename(backup_path)
        except OSError:
            pass

    def make_callback(self, *, agent_name: str | None = None, agent_id: str | None = None):
        """Return an on_request_complete callback capturing this logger's identity."""

        def _cb(
            method: str, path: str, status: int, remaining: int | None, reset_at: float | None, content_type: str = ""
        ) -> None:
            self._write(
                method,
                path,
                status,
                remaining,
                reset_at,
                agent_name=agent_name,
                agent_id=agent_id,
                content_type=content_type,
            )

        return _cb

    def _write(
        self,
        method: str,
        path: str,
        status: int,
        remaining: int | None,
        reset_at: float | None,
        *,
        agent_name: str | None,
        agent_id: str | None,
        content_type: str = "",
    ) -> None:
        if not self._enabled:
            return
        record: dict = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "pid": os.getpid(),
            "role": self.role,
            "method": method,
            "path": path,
            "status": status,
        }
        record["content_type"] = content_type or None
        record["agent_name"] = agent_name or None
        record["agent_id"] = agent_id or None
        record["remaining"] = remaining
        record["reset_at"] = reset_at or None
        line = json.dumps(record) + "\n"
        log_path = api_requests_log_path()
        try:
            with self._lock:
                self._rotate_if_needed(log_path)
                with open(log_path, "a") as f:
                    f.write(line)
        except OSError as exc:
            import sys

            print(f"[ax-gateway] WARNING: api-requests.log write failed: {exc}", file=sys.stderr)


_daemon_request_logger = _RequestLogger(role="daemon")
_ui_request_logger = _RequestLogger(role="ui_server")


def space_cache_path() -> Path:
    """Disk cache of {id, name, slug} triples for the user's visible spaces.

    Single source for slug→UUID resolution and friendly-name hydration.
    Populated by any successful upstream `list_spaces()` call. Consulted by
    space-ref resolvers and the UI before falling back to upstream — that
    keeps slug/name lookups out of the 429 path and lets the UI render
    friendly names even when paxai.app rate-limits us.
    """
    return gateway_dir() / "spaces.cache.json"


def load_space_cache() -> list[dict[str, Any]]:
    path = space_cache_path()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    items = raw.get("spaces") if isinstance(raw, dict) else raw
    return [item for item in (items or []) if isinstance(item, dict)]


def save_space_cache(spaces: list[dict[str, Any]]) -> None:
    """Atomically replace the spaces cache.

    Caller passes already-normalized rows ({id, name, slug}); we mirror them
    verbatim. Empty input is a no-op so callers don't have to null-guard.
    """
    if not spaces:
        return
    path = space_cache_path()
    payload = {"spaces": spaces, "saved_at": datetime.now(timezone.utc).isoformat()}
    tmp = path.with_suffix(".json.tmp")
    try:
        tmp.write_text(json.dumps(payload), encoding="utf-8")
        _chmod_quiet(tmp, 0o600)
        tmp.replace(path)
    except OSError:
        pass


def upsert_space_cache_entry(space_id: str, *, name: str | None = None, slug: str | None = None) -> None:
    """Update a single entry in the spaces cache without touching the rest.

    Used after a slug-resolve so a non-cached space gets persisted for future
    slug switches without forcing a full list_spaces refresh.
    """
    sid = str(space_id or "").strip()
    if not sid or not looks_like_space_uuid(sid):
        return
    rows = load_space_cache()
    found = False
    for row in rows:
        if str(row.get("id") or row.get("space_id") or "").strip() == sid:
            if name:
                row["name"] = str(name)
            if slug:
                row["slug"] = str(slug)
            found = True
            break
    if not found:
        rows.append(
            {
                "id": sid,
                "name": str(name or sid),
                "slug": str(slug) if slug else None,
            }
        )
    save_space_cache(rows)


def lookup_space_in_cache(ref: str) -> dict[str, Any] | None:
    """Resolve a space ref (UUID, slug, name) against the local cache.

    Returns the cached row when the ref unambiguously matches exactly one
    cached space, else None. UUID matches always short-circuit (a UUID
    cannot collide). Slug and name matches are collected across the whole
    cache: if more than one row matches, we return None so the caller
    falls through to the live-fetch path, where the resolver's ambiguity
    branch in `_resolve_space_ref` produces the correct fail-closed error
    instead of silently selecting the first match (issue #47).
    """
    needle = str(ref or "").strip()
    if not needle:
        return None
    norm = needle.lower()
    matches: list[dict[str, Any]] = []
    for row in load_space_cache():
        sid = str(row.get("id") or row.get("space_id") or "").strip()
        if not sid:
            continue
        if sid == needle:
            return row
        slug = str(row.get("slug") or "").strip().lower()
        name = str(row.get("name") or "").strip().lower()
        if (slug and slug == norm) or (name and name == norm):
            matches.append(row)
    if len(matches) == 1:
        return matches[0]
    return None


def space_name_from_cache(space_id: str) -> str | None:
    sid = str(space_id or "").strip()
    if not sid:
        return None
    for row in load_space_cache():
        if str(row.get("id") or row.get("space_id") or "").strip() == sid:
            n = str(row.get("name") or "").strip()
            if n:
                return n
    return None


def agent_dir(name: str) -> Path:
    path = gateway_agents_dir() / name
    path.mkdir(parents=True, exist_ok=True)
    _chmod_quiet(path, 0o700)
    return path


def agent_token_path(name: str) -> Path:
    return agent_dir(name) / "token"


def agent_token_relpath(name: str) -> str:
    """Canonical registry-stored ``token_file``: relative to ``gateway_dir()``.

    The managed token always lives at ``<gateway_dir>/agents/<name>/token`` (see
    ``_save_agent_token``), so the portable registry value is just the relative
    tail — it resolves correctly no matter which host or container opens the
    registry (#89).
    """
    return f"agents/{name}/token"


def resolve_agent_token_file(entry: dict[str, Any]) -> Path:
    """Resolve a registry entry's ``token_file`` to an absolute path.

    ``token_file`` is stored relative to ``gateway_dir()`` so registries stay
    portable across hosts/containers (#89). Absolute values (legacy entries
    written before the migration, or any future out-of-tree path) are honored
    as-is for backward compatibility. Empty values pass through unchanged so
    callers keep their existing emptiness guards.
    """
    raw = str(entry.get("token_file") or "")
    p = Path(raw).expanduser()
    if raw and not p.is_absolute():
        return gateway_dir() / p
    return p


def migrate_registry_token_files(registry: dict[str, Any]) -> int:
    """Rewrite managed-agent ``token_file`` paths to the portable relative form.

    ``ax gateway agents add`` historically froze ``token_file`` as an absolute
    path (``<gateway_dir>/agents/<name>/token``) into ``registry.json``, so a
    registry minted on one host couldn't open in another even when the token
    file was reachable (#89). Detect entries whose ``token_file`` is the
    canonical managed token — basename ``token`` directly under
    ``agents/<name>/`` — and rewrite to ``agents/<name>/token``, resolved against
    ``gateway_dir()`` at read time.

    Matches on the path *shape* rather than "is under the current gateway_dir"
    on purpose: the whole point is to heal a path captured under a *different*
    host's gateway_dir (e.g. ``/Users/.../agents/nova/token`` opened inside a
    Linux container). Idempotent. Returns the count rewritten.
    """
    migrated = 0
    for entry in registry.get("agents", []) or []:
        if not isinstance(entry, dict):
            continue
        name = str(entry.get("name") or "").strip()
        if not name:
            continue
        raw = str(entry.get("token_file") or "").strip()
        if not raw:
            continue
        canonical = agent_token_relpath(name)
        if raw == canonical:
            continue  # already portable
        p = Path(raw)
        if p.name == "token" and p.parent.name == name and p.parent.parent.name == "agents":
            entry["token_file"] = canonical
            migrated += 1
    return migrated


def load_gateway_managed_agent_token(entry: dict[str, Any]) -> str:
    """Read a Gateway-managed runtime token and reject bootstrap credentials."""
    token_file = resolve_agent_token_file(entry)
    if not token_file.exists():
        raise ValueError(f"Gateway-managed token file is missing: {token_file}")
    token = token_file.read_text().strip()
    if not token:
        raise ValueError(f"Gateway-managed token file is empty: {token_file}")
    if token.startswith("axp_u_"):
        raise ValueError(
            "Gateway-managed agents require an agent-bound token. "
            f"Refusing to use a user bootstrap PAT from {token_file}."
        )
    if token.startswith("axp_a_offline_") and not os.environ.get("AX_OFFLINE"):
        name = str(entry.get("name") or "").strip()
        raise ValueError(
            f"Agent {('@' + name) if name else '<unknown>'} has a stale offline-mode token "
            "(axp_a_offline_*), which is only valid under AX_OFFLINE=1. Re-register it to "
            f"connect to the live platform: ax gateway agents remove {name or '<name>'} "
            "(then re-add it)."
        )
    if not str(entry.get("agent_id") or "").strip():
        raise ValueError("Gateway-managed agents require a bound agent_id before runtime use.")
    return token


def agent_pending_queue_path(name: str) -> Path:
    return agent_dir(name) / "pending.json"


def _default_pending_queue() -> dict[str, Any]:
    return {"version": 1, "items": []}


def load_agent_pending_messages(name: str) -> list[dict[str, Any]]:
    payload = _read_json(agent_pending_queue_path(name), default=_default_pending_queue())
    items = payload.get("items")
    if not isinstance(items, list):
        return []
    return [dict(item) for item in items if isinstance(item, dict)]


def save_agent_pending_messages(name: str, items: list[dict[str, Any]]) -> Path:
    payload = {
        "version": 1,
        "items": [dict(item) for item in items if isinstance(item, dict)],
    }
    _write_json(agent_pending_queue_path(name), payload)
    return agent_pending_queue_path(name)


def append_agent_pending_message(name: str, message: dict[str, Any]) -> list[dict[str, Any]]:
    message_id = str(message.get("message_id") or message.get("id") or "").strip()
    items = load_agent_pending_messages(name)
    if any(str(item.get("message_id") or "").strip() == message_id for item in items):
        return items
    items.append(
        {
            "message_id": message_id,
            "parent_id": str(message.get("parent_id") or "").strip() or None,
            "conversation_id": str(message.get("conversation_id") or "").strip() or None,
            "content": str(message.get("content") or ""),
            "display_name": str(
                message.get("display_name") or message.get("agent_name") or message.get("sender_name") or ""
            )
            or None,
            "created_at": str(message.get("created_at") or _now_iso()),
            "queued_at": _now_iso(),
        }
    )
    save_agent_pending_messages(name, items)
    return items


def remove_agent_pending_message(name: str, message_id: str | None) -> list[dict[str, Any]]:
    target = str(message_id or "").strip()
    if not target:
        return load_agent_pending_messages(name)
    items = [item for item in load_agent_pending_messages(name) if str(item.get("message_id") or "").strip() != target]
    save_agent_pending_messages(name, items)
    return items


def _default_registry() -> dict[str, Any]:
    return {
        "version": 1,
        "gateway": {
            "gateway_id": str(uuid.uuid4()),
            "desired_state": "stopped",
            "effective_state": "stopped",
            "session_connected": False,
            "pid": None,
            "last_started_at": None,
            "last_reconcile_at": None,
        },
        "agents": [],
        "bindings": [],
        "identity_bindings": [],
        "approvals": [],
    }


def _write_json(path: Path, payload: dict[str, Any], *, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as tmp:
            tmp_path = Path(tmp.name)
            json.dump(payload, tmp, indent=2, sort_keys=True)
            tmp.write("\n")
            tmp.flush()
            os.fsync(tmp.fileno())
        tmp_path.chmod(mode)
        tmp_path.replace(path)
    finally:
        if tmp_path and tmp_path.exists():
            tmp_path.unlink()
    path.chmod(mode)


def _read_json(path: Path, *, default: dict[str, Any]) -> dict[str, Any]:
    if not path.exists():
        return copy.deepcopy(default)
    return json.loads(path.read_text())


def load_gateway_session() -> dict[str, Any]:
    return _read_json(session_path(), default={})


def save_gateway_session(data: dict[str, Any]) -> Path:
    payload = dict(data)
    payload.setdefault("saved_at", _now_iso())
    _write_json(session_path(), payload)
    return session_path()


def apply_space_to_gateway_session(
    space_id: str,
    *,
    space_name: str | None = None,
) -> dict[str, Any] | None:
    """Point the Gateway bootstrap session at ``space_id`` so it can't diverge
    from the CLI's current space.

    Returns ``None`` when no Gateway session exists (Gateway was never logged
    in) — the caller has nothing to sync and should stay silent. A session is
    never fabricated here: if one is created later by ``ax gateway login`` the
    operator picks the space then.

    The write is atomic and daemon-independent (see :func:`_write_json` /
    :func:`save_gateway_session`), so it is safe while the daemon is stopped.
    The daemon reads ``session.json`` only at startup, so when a daemon is
    already running the change applies on the next ``ax gateway start`` — the
    returned ``daemon_running`` flag lets the caller warn about that.

    Returns a status dict::

        {
          "updated": bool,            # False when the session already matched
          "session_path": str,
          "previous_space_id": str | None,
          "space_id": str,
          "space_name": str | None,
          "daemon_running": bool,
        }
    """
    session = load_gateway_session()
    if not session:
        return None

    previous_space_id = str(session.get("space_id") or "").strip() or None
    daemon_running = active_gateway_pid() is not None

    if previous_space_id == space_id:
        # Already aligned — don't rewrite the file or emit a redundant audit
        # event. Still report daemon state in case the caller wants to message.
        return {
            "updated": False,
            "session_path": str(session_path()),
            "previous_space_id": previous_space_id,
            "space_id": space_id,
            "space_name": space_name or session.get("space_name"),
            "daemon_running": daemon_running,
        }

    session["space_id"] = space_id
    if space_name:
        session["space_name"] = space_name
    path = save_gateway_session(session)
    # Keep the spaces cache warm so subsequent slug switches stay cache-served.
    upsert_space_cache_entry(space_id, name=space_name, slug=None)
    record_gateway_activity("gateway_space_use", space_id=space_id, space_name=space_name)
    return {
        "updated": True,
        "session_path": str(path),
        "previous_space_id": previous_space_id,
        "space_id": space_id,
        "space_name": space_name,
        "daemon_running": daemon_running,
    }


_LOAD_SNAPSHOT_KEY = "_load_snapshot"

# Fields the operator (CLI / UI server) writes authoritatively. The daemon's
# reconcile loop should NEVER clobber these mid-flight: if a field's value
# on disk differs from what was present at this caller's load, another
# writer changed it and we must take disk's value, not memory's stale view.
_OPERATOR_AUTHORITATIVE_FIELDS = (
    "desired_state",
    "manual_attach_state",
    "manual_attached_at",
    "manual_attach_note",
    "manual_attach_source",
    "lifecycle_phase",
    "archived_at",
    "archived_reason",
    "desired_state_before_archive",
    "hidden_at",
    "hidden_reason",
    "desired_state_before_hide",
)


_SPACE_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.IGNORECASE,
)


def looks_like_space_uuid(value: Any) -> bool:
    return isinstance(value, str) and bool(_SPACE_UUID_RE.match(value.strip()))


def reconcile_corrupt_space_ids(registry: dict[str, Any]) -> int:
    """Heal agent rows where ``space_id`` holds a name/slug instead of a UUID.

    Recovers the correct UUID from sibling fields (``active_space_id``,
    ``default_space_id``, ``allowed_spaces[].space_id``). Idempotent — rows
    whose ``space_id`` is already UUID-shaped or empty are left alone.
    Returns the count of repaired rows.
    """
    repaired = 0
    for entry in registry.get("agents", []) or []:
        if not isinstance(entry, dict):
            continue
        sid = entry.get("space_id")
        if not isinstance(sid, str) or not sid.strip() or looks_like_space_uuid(sid):
            continue
        candidate = ""
        for key in ("active_space_id", "default_space_id"):
            v = entry.get(key)
            if looks_like_space_uuid(v):
                candidate = str(v).strip()
                break
        if not candidate:
            allowed = entry.get("allowed_spaces") or []
            if isinstance(allowed, list):
                for row in allowed:
                    if isinstance(row, dict) and looks_like_space_uuid(row.get("space_id")):
                        candidate = str(row["space_id"]).strip()
                        break
        if candidate:
            entry["space_id"] = candidate
            repaired += 1
    return repaired


def load_gateway_registry() -> dict[str, Any]:
    registry = _read_json(registry_path(), default=_default_registry())
    registry.setdefault("version", 1)
    registry.setdefault("gateway", {})
    registry.setdefault("agents", [])
    registry.setdefault("bindings", [])
    registry.setdefault("identity_bindings", [])
    registry.setdefault("approvals", [])
    gateway = registry["gateway"]
    gateway.setdefault("gateway_id", str(uuid.uuid4()))
    gateway.setdefault("desired_state", "stopped")
    gateway.setdefault("effective_state", "stopped")
    gateway.setdefault("session_connected", False)
    gateway.setdefault("pid", None)
    gateway.setdefault("last_started_at", None)
    gateway.setdefault("last_reconcile_at", None)
    # Active-space lives in session.json — strip any stale duplicate from the
    # gateway record so callers can't accidentally read a stale value. Older
    # registries (pre-simplification) carry these keys; this is the
    # auto-migration path so we don't need a separate migration step.
    gateway.pop("space_id", None)
    gateway.pop("space_name", None)
    reconcile_corrupt_space_ids(registry)
    # Heal absolute token_file paths frozen in by an older `agents add` into the
    # portable `agents/<name>/token` relative form (#89). In-memory only, like
    # the space-id reconcile above — the next save_gateway_registry persists it.
    migrate_registry_token_files(registry)
    # Stamp a load-time snapshot so save_gateway_registry can distinguish:
    #   - "caller removed this row" vs "another writer added this row"
    #     (row existence diff)
    #   - "caller updated this field" vs "another writer updated this
    #     field" (field-level diff for operator-authoritative fields like
    #     desired_state — if our load-time value matches memory but disk
    #     differs, another writer changed it; respect disk's view)
    snapshot: dict[str, dict[str, Any]] = {}
    for entry in registry["agents"]:
        if not isinstance(entry, dict):
            continue
        name = str(entry.get("name") or "")
        if not name:
            continue
        snapshot[name] = {field: entry.get(field) for field in _OPERATOR_AUTHORITATIVE_FIELDS}
    registry[_LOAD_SNAPSHOT_KEY] = snapshot
    return registry


def save_gateway_registry(registry: dict[str, Any], *, merge_archive: bool = True) -> Path:
    """Persist the registry to disk.

    Performs four race-safety merges before writing:

    1. **Row addition preservation** (always on): re-reads disk and
       appends any agent rows that exist on disk but not in memory *and*
       were not in the caller's load-time snapshot. Recovers writes from
       a second writer (e.g. the UI server's POST /api/agents add) that
       landed between this caller's load and save.

    1b. **Row deletion preservation** (always on, #42): drops in-memory
       agent rows that *were* in the caller's load-time snapshot but are
       no longer on disk. Without this, a daemon poll loop saving its
       long-lived view would resurrect agents that the CLI explicitly
       removed — `axctl gateway agents remove <name>` exits 0, deletes
       the token file, but the daemon's next save brings the entry back
       within one poll cycle.

    2. **Operator-authoritative field preservation** (always on): for
       each field in _OPERATOR_AUTHORITATIVE_FIELDS (desired_state,
       lifecycle_phase, archive/hide flags), if the value on disk
       differs from this caller's load-time snapshot, another writer
       changed it; take disk's value. This is what makes
       `ax gateway agents stop` actually stick: the daemon's stale
       `desired_state=running` view does not clobber the CLI's freshly
       written `desired_state=stopped`.

    3. **Archive-field merge** (gated on merge_archive=True, the
       default): legacy bidirectional archive merge from PR #147.
       Subsumed by (2) for normal flows; preserved for the explicit
       archived↔active transition path so atomic CLI ops can opt out
       via merge_archive=False to avoid seesawing with their own writes.
    """
    # Pop the load snapshot so it never leaks to disk.
    snapshot_raw = registry.pop(_LOAD_SNAPSHOT_KEY, None)
    snapshot: dict[str, dict[str, Any]]
    if isinstance(snapshot_raw, dict):
        snapshot = snapshot_raw
    elif isinstance(snapshot_raw, list):
        # Backwards-compat with names-only snapshot from earlier load.
        snapshot = {name: {} for name in snapshot_raw}
    else:
        snapshot = {}
    loaded_names = set(snapshot.keys())

    try:
        on_disk = _read_json(registry_path(), default=None)
    except Exception:  # noqa: BLE001
        on_disk = None

    if isinstance(on_disk, dict):
        disk_agents = on_disk.get("agents") or []
        in_memory_names = {
            str(a.get("name") or "") for a in registry.get("agents") or [] if isinstance(a, dict) and a.get("name")
        }

        # (1) Preserve rows added by another writer after this caller loaded.
        for disk_entry in disk_agents:
            if not isinstance(disk_entry, dict):
                continue
            name = str(disk_entry.get("name") or "")
            if not name:
                continue
            if name in in_memory_names:
                continue  # already in memory; either updating or untouched
            if name in loaded_names:
                continue  # caller removed it (was in our snapshot, not in memory)
            registry.setdefault("agents", []).append(disk_entry)

        # (1b) Preserve row deletions made by another writer (#42).
        # If a row was in our load-time snapshot AND is still in memory
        # but is no longer on disk, another writer removed it after we
        # loaded — respect their delete. The snapshot gate is essential:
        # rows the caller added since load (not in snapshot) must not be
        # dropped, only ones we shared with the prior on-disk state.
        disk_names = {str(a.get("name") or "") for a in disk_agents if isinstance(a, dict) and a.get("name")}
        registry["agents"] = [
            entry
            for entry in registry.get("agents") or []
            if not (
                isinstance(entry, dict)
                and str(entry.get("name") or "") in loaded_names
                and str(entry.get("name") or "") not in disk_names
            )
        ]

        # (2) Operator-authoritative field preservation.
        # If a field's disk value differs from our load-time snapshot,
        # another writer changed it; take disk's value over ours.
        disk_by_name = {str(a.get("name") or ""): a for a in disk_agents if isinstance(a, dict) and a.get("name")}
        for entry in registry.get("agents") or []:
            if not isinstance(entry, dict):
                continue
            name = str(entry.get("name") or "")
            disk_entry = disk_by_name.get(name)
            if not isinstance(disk_entry, dict):
                continue
            loaded_fields = snapshot.get(name, {})
            for field in _OPERATOR_AUTHORITATIVE_FIELDS:
                disk_value = disk_entry.get(field)
                loaded_value = loaded_fields.get(field)
                if disk_value != loaded_value:
                    # Another writer changed this field after our load.
                    # Preserve their write — overwrite our memory's view.
                    if field in disk_entry:
                        entry[field] = disk_value
                    else:
                        entry.pop(field, None)

        # (3) Existing archive-field merge (kept for the merge_archive=False
        # opt-out semantics; (2) covers the common case).
        if merge_archive:
            disk_by_name = {str(a.get("name") or ""): a for a in disk_agents if isinstance(a, dict) and a.get("name")}
            for entry in registry.get("agents") or []:
                if not isinstance(entry, dict):
                    continue
                disk_entry = disk_by_name.get(str(entry.get("name") or ""))
                if not isinstance(disk_entry, dict):
                    continue
                # CLI is authoritative for the archived ↔ active transition.
                # Take disk's archive fields whenever the disk OR the in-memory
                # copy has archive state — covers both directions of the race
                # (CLI archive into the daemon's active view, *and* CLI restore
                # into the daemon's still-archived view).
                disk_phase = str(disk_entry.get("lifecycle_phase") or "")
                mem_phase = str(entry.get("lifecycle_phase") or "")
                if disk_phase == "archived" or (mem_phase == "archived" and disk_phase != "archived"):
                    for field in (
                        "lifecycle_phase",
                        "archived_at",
                        "archived_reason",
                        "desired_state_before_archive",
                        "desired_state",
                    ):
                        if field in disk_entry:
                            entry[field] = disk_entry[field]
                        else:
                            entry.pop(field, None)
    _write_json(registry_path(), registry)
    return registry_path()


def _pid_alive(pid: int | None) -> bool:
    if not pid:
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def daemon_status() -> dict[str, Any]:
    pid = None
    if pid_path().exists():
        try:
            pid = int(pid_path().read_text().strip())
        except ValueError:
            pid = None
    running = _pid_alive(pid)
    if not running:
        scanned = _scan_gateway_process_pids()
        if scanned:
            pid = scanned[0]
            running = True
    registry = load_gateway_registry()
    return {
        "pid": pid,
        "running": running,
        "gateway_dir": str(gateway_dir()),
        "gateway_environment": gateway_environment(),
        "registry_path": str(registry_path()),
        "session_path": str(session_path()),
        "registry": registry,
    }


def _scan_process_pids(pattern: re.Pattern[str]) -> list[int]:
    current_pid = os.getpid()
    parent_pid = os.getppid()
    try:
        output = subprocess.check_output(
            ["ps", "-axo", "pid=,command="],
            text=True,
            stderr=subprocess.DEVNULL,
        )
    except Exception:
        return []

    pids: list[int] = []
    for raw_line in output.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        pid_text, _, command = line.partition(" ")
        try:
            pid = int(pid_text)
        except ValueError:
            continue
        if pid in {current_pid, parent_pid} or not _pid_alive(pid):
            continue
        command = command.strip()
        if command and pattern.search(command):
            pids.append(pid)
    return sorted(set(pids))


def _scan_gateway_process_pids() -> list[int]:
    """Best-effort fallback for live daemons that predate the pid file."""
    return _scan_process_pids(_GATEWAY_PROCESS_RE)


def _default_ui_state() -> dict[str, Any]:
    return {
        "pid": None,
        "host": "127.0.0.1",
        "port": 8765,
        "last_started_at": None,
    }


def load_gateway_ui_state() -> dict[str, Any]:
    state = _read_json(ui_state_path(), default=_default_ui_state())
    state.setdefault("pid", None)
    state.setdefault("host", "127.0.0.1")
    state.setdefault("port", 8765)
    state.setdefault("last_started_at", None)
    return state


def save_gateway_ui_state(data: dict[str, Any]) -> Path:
    payload = _default_ui_state()
    payload.update(data)
    _write_json(ui_state_path(), payload)
    return ui_state_path()


def ui_status() -> dict[str, Any]:
    state = load_gateway_ui_state()
    pid = state.get("pid")
    try:
        pid_value = int(pid) if pid is not None else None
    except (TypeError, ValueError):
        pid_value = None
    host = str(state.get("host") or "127.0.0.1")
    try:
        port = int(state.get("port") or 8765)
    except (TypeError, ValueError):
        port = 8765
    running = _pid_alive(pid_value)
    if not running:
        scanned = _scan_gateway_ui_process_pids()
        if scanned:
            pid_value = scanned[0]
            running = True
    return {
        "pid": pid_value,
        "running": running,
        "host": host,
        "port": port,
        "url": f"http://{host}:{port}",
        "state_path": str(ui_state_path()),
        "log_path": str(ui_log_path()),
        "last_started_at": state.get("last_started_at"),
    }


def _scan_gateway_ui_process_pids() -> list[int]:
    """Best-effort fallback for live UIs that predate the ui state file."""
    return _scan_process_pids(_GATEWAY_UI_PROCESS_RE)


def active_gateway_ui_pids() -> list[int]:
    """Return all known live Gateway UI PIDs except the current process."""
    status = ui_status()
    pids: list[int] = []
    pid = status.get("pid")
    if isinstance(pid, int) and status.get("running") and pid != os.getpid():
        pids.append(pid)
    pids.extend(_scan_gateway_ui_process_pids())
    return sorted(set(pids))


def active_gateway_ui_pid() -> int | None:
    """Return the PID of a live Gateway UI, if one is already running."""
    pids = active_gateway_ui_pids()
    return pids[0] if pids else None


def write_gateway_ui_state(*, pid: int, host: str, port: int) -> None:
    save_gateway_ui_state(
        {
            "pid": pid,
            "host": host,
            "port": port,
            "last_started_at": _now_iso(),
        }
    )


def clear_gateway_ui_state(pid: int | None = None) -> None:
    if not ui_state_path().exists():
        return
    if pid is not None:
        try:
            state = load_gateway_ui_state()
            existing_pid = int(state.get("pid")) if state.get("pid") is not None else None
        except (TypeError, ValueError):
            existing_pid = None
        if existing_pid not in {None, pid}:
            return
    ui_state_path().unlink()


def active_gateway_pids() -> list[int]:
    """Return all known live Gateway daemon PIDs except the current process."""
    status = daemon_status()
    pids: list[int] = []
    pid = status.get("pid")
    if isinstance(pid, int) and status.get("running") and pid != os.getpid():
        pids.append(pid)
    pids.extend(_scan_gateway_process_pids())
    return sorted(set(pids))


def active_gateway_pid() -> int | None:
    """Return the PID of a live Gateway daemon, if one is already running."""
    pids = active_gateway_pids()
    return pids[0] if pids else None


def write_gateway_pid(pid: int) -> None:
    pid_path().write_text(f"{pid}\n")
    pid_path().chmod(0o600)


def clear_gateway_pid(pid: int | None = None) -> None:
    if not pid_path().exists():
        return
    if pid is not None:
        try:
            existing_pid = int(pid_path().read_text().strip())
        except ValueError:
            existing_pid = None
        if existing_pid not in {None, pid}:
            return
    pid_path().unlink()


def _read_last_chain_state(path: Path) -> tuple[int, str | None]:
    """Tail the activity log for (last_seq, last_record_hash).

    Returns (0, None) when the file is missing, empty, or the trailing line has
    no `seq` field (pre-feature record — the chain starts fresh on next write).
    """
    if not path.exists():
        return (0, None)
    last_line: str | None = None
    try:
        with path.open("r", encoding="utf-8") as handle:
            for raw in handle:
                stripped = raw.rstrip("\n")
                if stripped:
                    last_line = stripped
    except OSError:
        return (0, None)
    if not last_line:
        return (0, None)
    try:
        rec = json.loads(last_line)
    except json.JSONDecodeError:
        return (0, None)
    seq_val = rec.get("seq") if isinstance(rec, dict) else None
    if not isinstance(seq_val, int) or seq_val <= 0:
        return (0, None)
    return (seq_val, hashlib.sha256(last_line.encode("utf-8")).hexdigest())


def record_gateway_activity(
    event: str,
    *,
    entry: dict[str, Any] | None = None,
    **fields: Any,
) -> dict[str, Any]:
    record: dict[str, Any] = {
        "ts": _now_iso(),
        "event": event,
    }
    phase = phase_for_event(event)
    if phase is not None:
        record["phase"] = phase
    registry = load_gateway_registry()
    gateway = registry.get("gateway", {})
    if gateway.get("gateway_id"):
        record["gateway_id"] = gateway["gateway_id"]
    if entry:
        record.update(
            {
                "agent_name": entry.get("name"),
                "agent_id": entry.get("agent_id"),
                "asset_id": _asset_id_for_entry(entry) or None,
                "install_id": entry.get("install_id"),
                "runtime_instance_id": entry.get("runtime_instance_id"),
                "runtime_type": entry.get("runtime_type"),
                "transport": entry.get("transport", "gateway"),
                "credential_source": entry.get("credential_source", "gateway"),
            }
        )
    for key, value in fields.items():
        if value is not None:
            record[key] = value

    path = activity_log_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with _exclusive_activity_lock(path):
        last_seq, last_hash = _read_last_chain_state(path)
        record["seq"] = last_seq + 1
        record["prev_hash"] = last_hash
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, sort_keys=True) + "\n")
        path.chmod(0o600)
    return record


def load_recent_gateway_activity(
    limit: int = DEFAULT_ACTIVITY_LIMIT,
    *,
    agent_name: str | None = None,
) -> list[dict[str, Any]]:
    path = activity_log_path()
    if not path.exists():
        return []
    try:
        lines = path.read_text().splitlines()
    except OSError:
        return []
    if limit <= 0:
        return []
    agent_filter = agent_name.strip().lower() if agent_name else None
    items: list[dict[str, Any]] = []
    for line in reversed(lines):
        if not line.strip():
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(payload, dict):
            continue
        if agent_filter and str(payload.get("agent_name") or "").lower() != agent_filter:
            continue
        items.append(payload)
        if len(items) >= limit:
            break
    items.reverse()
    return items


def find_agent_entry(registry: dict[str, Any], name: str) -> dict[str, Any] | None:
    for entry in registry.get("agents", []):
        if str(entry.get("name", "")).lower() == name.lower():
            return entry
    return None


def find_agent_entry_by_ref(registry: dict[str, Any], ref: str) -> dict[str, Any] | None:
    """Find an agent by registry row number, name, or stable id prefix."""
    raw = str(ref or "").strip()
    if not raw:
        return None
    normalized = raw.lower().lstrip("#").strip()
    agents = [entry for entry in registry.get("agents", []) if isinstance(entry, dict)]
    if normalized.isdigit():
        idx = int(normalized) - 1
        if 0 <= idx < len(agents):
            return agents[idx]
    for entry in agents:
        if str(entry.get("name") or "").lower() == normalized:
            return entry
    id_fields = ("install_id", "agent_id", "asset_id", "runtime_instance_id", "approval_id")
    exact_matches = [
        entry for entry in agents for field in id_fields if str(entry.get(field) or "").lower() == normalized
    ]
    if len(exact_matches) == 1:
        return exact_matches[0]
    if len(normalized) >= 6:
        prefix_matches = []
        for entry in agents:
            for field in id_fields:
                value = str(entry.get(field) or "").lower()
                if value and value.startswith(normalized):
                    prefix_matches.append(entry)
                    break
        if len(prefix_matches) == 1:
            return prefix_matches[0]
    return None


def upsert_agent_entry(registry: dict[str, Any], agent: dict[str, Any]) -> dict[str, Any]:
    agents = registry.setdefault("agents", [])
    for idx, existing in enumerate(agents):
        if str(existing.get("name", "")).lower() == str(agent.get("name", "")).lower():
            merged = dict(existing)
            merged.update(agent)
            agents[idx] = merged
            return merged
    agents.append(agent)
    return agent


def remove_agent_entry(registry: dict[str, Any], name: str) -> dict[str, Any] | None:
    agents = registry.setdefault("agents", [])
    for idx, entry in enumerate(agents):
        if str(entry.get("name", "")).lower() == name.lower():
            return agents.pop(idx)
    return None


# Deferred cross-module imports (bottom-of-file to avoid import cycles;
# bound into module globals after defs, resolved at call time).
from .gateway_health import _now_iso  # noqa: E402
from .gateway_identity import _asset_id_for_entry  # noqa: E402
