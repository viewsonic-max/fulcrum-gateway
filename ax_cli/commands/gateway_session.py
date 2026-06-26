"""ax gateway — local pass-through session protocol (fingerprint, challenge, routing).

Extracted from ``ax_cli/commands/gateway.py`` (issue #28 Phase 1).
"""

from __future__ import annotations

import getpass
import os
import secrets
import sys
from datetime import datetime, timezone
from pathlib import Path

import httpx

from .. import gateway as gateway_core
from ..gateway import (
    annotate_runtime_health,
    apply_entry_current_space,
    evaluate_runtime_attestation,
    find_agent_entry,
    find_agent_entry_by_ref,
    get_gateway_approval,
    issue_local_session,
    load_agent_pending_messages,
    load_gateway_registry,
    load_gateway_session,
    record_gateway_activity,
    save_agent_pending_messages,
    save_gateway_registry,
    verify_local_session_token,
)
from ..mentions import merge_explicit_mentions_metadata


def _local_process_fingerprint(
    *,
    agent_name: str,
    cwd: str | None = None,
    pid: int | None = None,
    exe_path: str | None = None,
) -> dict:
    resolved_pid = int(pid or os.getpid())
    resolved_cwd = str(Path(cwd or os.getcwd()).expanduser().resolve())
    resolved_exe = str(Path(exe_path or sys.executable).expanduser().resolve())
    fingerprint = {
        "agent_name": agent_name,
        "pid": resolved_pid,
        "parent_pid": os.getppid() if resolved_pid == os.getpid() else None,
        "cwd": resolved_cwd,
        "exe_path": resolved_exe,
        "user": getpass.getuser(),
        "platform": sys.platform,
    }
    try:
        fingerprint["exe_sha256"] = gateway_core._file_sha256(Path(resolved_exe))  # type: ignore[attr-defined]
    except Exception:
        fingerprint["exe_sha256"] = None
    return fingerprint


def _local_trust_signature(agent_name: str, fingerprint: dict) -> str:
    payload = {
        "agent_name": agent_name,
        "exe_path": str(fingerprint.get("exe_path") or ""),
        "cwd": str(fingerprint.get("cwd") or ""),
        "user": str(fingerprint.get("user") or ""),
    }
    return gateway_core._payload_hash(payload)  # type: ignore[attr-defined]


def _local_origin_signature(fingerprint: dict) -> str:
    """Stable origin key that intentionally excludes the mutable agent handle."""
    payload = {
        "exe_path": str(fingerprint.get("exe_path") or ""),
        "cwd": str(fingerprint.get("cwd") or ""),
        "user": str(fingerprint.get("user") or ""),
    }
    return gateway_core._payload_hash(payload)  # type: ignore[attr-defined]


def _find_local_origin_collision(registry: dict, *, fingerprint: dict, requested_name: str) -> dict | None:
    origin_signature = _local_origin_signature(fingerprint)
    requested_normalized = requested_name.strip().lower()
    for candidate in registry.get("agents") or []:
        candidate_name = str(candidate.get("name") or "").strip()
        if not candidate_name or candidate_name.lower() == requested_normalized:
            continue
        candidate_fingerprint = candidate.get("local_fingerprint")
        if not isinstance(candidate_fingerprint, dict):
            continue
        if _local_origin_signature(candidate_fingerprint) == origin_signature:
            return candidate
    return None


def _local_fingerprint_verification(fingerprint: dict) -> dict:
    """Best-effort OS cross-check for the self-reported local fingerprint."""
    pid = str(fingerprint.get("pid") or "").strip()
    if not pid or not pid.isdigit():
        return {"status": "unverified", "reason": "missing_pid"}
    proc_root = Path("/proc") / pid
    if not proc_root.exists():
        return {"status": "unavailable", "reason": "procfs_unavailable"}
    observed: dict[str, str] = {}
    try:
        observed["exe_path"] = str((proc_root / "exe").resolve())
    except OSError:
        pass
    try:
        observed["cwd"] = str((proc_root / "cwd").resolve())
    except OSError:
        pass
    mismatches = []
    for key in ("exe_path", "cwd"):
        reported = str(fingerprint.get(key) or "")
        if observed.get(key) and reported and observed[key] != reported:
            mismatches.append({"field": key, "reported": reported, "observed": observed[key]})
    if mismatches:
        return {"status": "mismatch", "reason": "fingerprint_mismatch", "observed": observed, "mismatches": mismatches}
    return {"status": "verified", "observed": observed}


def _connect_local_pass_through_agent(
    *,
    agent_name: str | None = None,
    registry_ref: str | None = None,
    fingerprint: dict,
    space_id: str | None = None,
    auto_create: bool = True,
) -> dict:
    requested_name = str(agent_name or "").strip()
    requested_ref = str(registry_ref or "").strip()
    if not requested_name and not requested_ref:
        raise ValueError("Local agent name or registry ref is required.")
    registry = load_gateway_registry()
    entry = find_agent_entry_by_ref(registry, requested_ref) if requested_ref else None
    if requested_ref and entry is None:
        raise LookupError(f"Gateway registry agent not found: {requested_ref}")
    if entry is not None and str(entry.get("template_id") or "").strip() not in {"", "pass_through"}:
        raise ValueError("registry_ref_not_attachable")
    normalized_name = str(entry.get("name") if entry else requested_name).strip()
    if not normalized_name:
        raise ValueError("Local agent name is required.")
    verification = _local_fingerprint_verification(fingerprint)
    if verification.get("status") == "mismatch":
        record_gateway_activity(
            "local_connect_fingerprint_mismatch",
            agent_name=normalized_name,
            fingerprint=fingerprint,
            verification=verification,
        )
        raise ValueError("fingerprint_mismatch")

    # Look up the requested agent by name FIRST. If it already exists in the
    # registry, the operator has previously approved this identity at this
    # workdir (or anywhere) and is just (re-)connecting. Multi-tenant case:
    # cli_god and pulse-cc can both legitimately operate from the same
    # physical workdir, each with its own registry row. Running the
    # collision check before the by-name lookup would have rejected
    # cli_god's reconnect just because pulse-cc's row also fingerprints
    # this directory.
    if entry is None:
        entry = find_agent_entry(registry, normalized_name)

    # Collision check only runs when the requested name is genuinely new
    # (no existing registry row). This still protects against accidental
    # duplicate registrations — registering a fresh agent at a workdir
    # already owned by a different agent surfaces the explicit error so
    # the operator can decide how to proceed.
    if entry is None:
        collision = _find_local_origin_collision(
            registry,
            fingerprint=fingerprint,
            requested_name=normalized_name,
        )
        if collision is not None:
            existing_name = str(collision.get("name") or "").strip()
            raise ValueError(
                "Gateway identity mismatch: "
                f"this local origin is already registered as @{existing_name}. "
                "Use that repo-local .ax/config.toml identity, reconnect by registry ref, "
                "or remove/rename the existing registry row before requesting a new agent name. "
                "If multiple agents legitimately share this workdir, register the new agent "
                "from a different working directory first, then it can re-connect from here."
            )
    if entry is None:
        if not auto_create:
            raise LookupError(f"Managed agent not found: {normalized_name}")
        entry = _register_managed_agent(
            name=normalized_name,
            template_id="pass_through",
            workdir=str(fingerprint.get("cwd") or "").strip() or None,
            space_id=str(space_id or "").strip() or None,
            start=True,
            source="session:autocreate",
        )
        registry = load_gateway_registry()
        entry = find_agent_entry(registry, normalized_name) or entry
    elif not space_id:
        _hydrate_entry_space_from_database(registry, entry)

    entry["template_id"] = entry.get("template_id") or "pass_through"
    entry["template_label"] = entry.get("template_label") or "Pass-through"
    entry["runtime_type"] = entry.get("runtime_type") or "inbox"
    if space_id:
        entry["space_id"] = str(space_id).strip()
    entry["workdir"] = str(fingerprint.get("cwd") or entry.get("workdir") or "").strip() or None
    entry["local_connection_mode"] = "pass_through"
    entry["local_auth_mode"] = "gateway-session"
    entry["credential_source"] = "gateway"
    entry["local_fingerprint"] = dict(fingerprint)
    entry["local_trust_signature"] = _local_trust_signature(normalized_name, fingerprint)
    entry["local_fingerprint_verification"] = verification
    entry["last_local_connect_at"] = datetime.now(timezone.utc).isoformat()
    if requested_ref:
        entry["last_local_registry_ref"] = requested_ref
    entry.update(evaluate_runtime_attestation(registry, entry))
    save_gateway_registry(registry)

    approved = (
        str(entry.get("approval_state") or "").lower() in {"not_required", "approved"}
        and str(entry.get("attestation_state") or "").lower() == "verified"
    )
    payload = {
        "status": "approved" if approved else "pending",
        "agent": _with_registry_refs(registry, annotate_runtime_health(entry, registry=registry)),
        "approval_id": entry.get("approval_id"),
        "registry_ref": _registry_ref_for_agent(registry, entry),
        "fingerprint": fingerprint,
        "fingerprint_verification": verification,
    }
    if entry.get("approval_id"):
        try:
            approval = get_gateway_approval(str(entry["approval_id"]))
            payload["approval"] = {
                "approval_id": approval.get("approval_id"),
                "approval_kind": approval.get("approval_kind"),
                "action": approval.get("action"),
                "risk": approval.get("risk"),
                "resource": approval.get("resource"),
                "reason": approval.get("reason"),
                "requested_at": approval.get("requested_at"),
                "status": approval.get("status"),
            }
        except LookupError:
            pass
    if approved:
        session_payload = issue_local_session(registry, entry, fingerprint=fingerprint)
        save_gateway_registry(registry)
        payload["session_token"] = session_payload["session_token"]
        payload["expires_at"] = session_payload["session"]["expires_at"]
    record_gateway_activity(
        "local_connect_requested",
        entry=entry,
        status=payload["status"],
        approval_id=entry.get("approval_id"),
        fingerprint_signature=entry.get("local_trust_signature"),
    )
    return payload


def _gateway_session_challenge_enabled() -> bool:
    """Phase-1 opt-in flag for the pass-through session challenge.

    Closes aX task ``68cb4d29`` (Phase-1: ``/local/send`` only). Truthy values
    on ``AX_GATEWAY_SESSION_CHALLENGE`` enable the challenge cycle. Anything
    else — including an unset env var — preserves the current easy path so
    operators who haven't opted in see no behavior change.

    The challenge is intentionally testing-flavored, not production hardening:
    use it as a memory/session-retention probe, and as a guard against
    accidental identity sharing when several ephemeral sessions run from the
    same workdir.
    """
    raw = os.environ.get("AX_GATEWAY_SESSION_CHALLENGE", "").strip().lower()
    return raw in {"1", "true", "yes", "on"}


def _generate_session_challenge_code() -> str:
    """Short URL-safe code suitable for an operator to read and echo back.

    Uppercased so it's distinct from any base64-shaped token in the same
    output and easy to type. ~6–8 chars depending on the random bytes' shape.
    """
    return secrets.token_urlsafe(4).upper()


def _find_local_session_record(registry: dict, session_id: str) -> dict | None:
    """Look up the registry record for a verified local session id."""
    target = (session_id or "").strip()
    if not target:
        return None
    for item in registry.get("local_sessions") or []:
        if str(item.get("session_id") or "") == target:
            return item
    return None


def _ensure_session_challenge(
    registry: dict,
    session_id: str,
    *,
    provided_proof: str | None,
) -> str:
    """Verify or issue a session-continuity challenge.

    Returns the next challenge code (the proof the caller should echo back
    on the *next* send) when the provided proof matches the stored one.

    Raises ``ValueError`` with a structured error message in two cases:
    no stored challenge yet (a new one is issued for the next send), or
    proof mismatch / missing proof against an existing stored challenge.
    Both error messages start with a recognizable prefix so callers can
    surface them without further parsing.
    """
    record = _find_local_session_record(registry, session_id)
    if record is None:
        # The token verified upstream, but the registry record is gone —
        # treat as a hard rejection rather than auto-issuing a challenge for
        # an unknown session.
        raise ValueError("session_challenge_unknown_session: no record for this session.")

    stored = str(record.get("challenge_code") or "").strip()
    proof = str(provided_proof or "").strip()

    if not stored:
        # First send under the flag for this session: issue a challenge and
        # require the caller to echo it on the next send.
        new_code = _generate_session_challenge_code()
        record["challenge_code"] = new_code
        record["challenge_issued_at"] = datetime.now(timezone.utc).isoformat()
        save_gateway_registry(registry)
        raise ValueError(
            f"session_challenge_required: {new_code}. Re-run with --session-proof <code> to confirm session continuity."
        )

    if not proof:
        raise ValueError(
            f"session_challenge_required: {stored}. Re-run with --session-proof <code> to confirm session continuity."
        )

    if proof != stored:
        raise ValueError(
            f"invalid_session_proof: expected {stored}. "
            "Run once without --session-proof to re-issue the challenge if you've lost it."
        )

    # Valid proof: rotate the code so the caller has a fresh proof for the
    # next send. Each successful send consumes one code.
    new_code = _generate_session_challenge_code()
    record["challenge_code"] = new_code
    record["challenge_issued_at"] = datetime.now(timezone.utc).isoformat()
    save_gateway_registry(registry)
    return new_code


def _platform_error_detail(exc: httpx.HTTPStatusError) -> str:
    """Pull the platform's JSON ``detail`` out of a failed response.

    FastAPI errors are ``{"detail": "..."}``; the bare httpx message
    (``Client error '404 Not Found' for url ...``) hides the server's
    reason. Surface ``detail`` so the operator sees e.g. ``Space not
    found: '<id>'`` instead of an opaque URL.
    """
    try:
        detail = exc.response.json().get("detail")
        if isinstance(detail, str) and detail.strip():
            return detail.strip()
    except Exception:
        pass
    return (exc.response.text or "").strip() or str(exc)


def _is_stale_space_error(exc: httpx.HTTPStatusError) -> bool:
    """True when the send was rejected because the target space is gone.

    The platform returns ``404 Space not found: '<id>'`` (see platform
    ``require_space``) when a pinned/hydrated ``space_id`` is no longer in
    the live space set — e.g. after an in-memory backend restart regenerates
    space ids while an agent row still references the old one.
    """
    return exc.response.status_code == 404 and "space not found" in _platform_error_detail(exc).lower()


def _live_space_ids(client) -> set[str]:
    """Best-effort set of space ids the platform currently knows about.

    Uses the agent's own send client (no user PAT), and returns an empty
    set on any failure so the caller can degrade to a clear error rather
    than crash.
    """
    try:
        raw = client.list_spaces()
    except Exception:
        return set()
    items = raw.get("spaces", raw) if isinstance(raw, dict) else raw
    ids: set[str] = set()
    for item in items or []:
        if isinstance(item, dict):
            sid = str(item.get("id") or item.get("space_id") or "").strip()
            if sid:
                ids.add(sid)
    return ids


def _resolve_live_fallback_space(client, attempted: str) -> str | None:
    """Pick a live space to retry into when ``attempted`` is stale.

    Prefers the Gateway session's current space (the operator's explicit
    selection via ``ax gateway spaces use``) when it is live, otherwise the
    first live space deterministically. Returns ``None`` when no usable live
    space exists or the only live space is the one that already failed.
    """
    live = _live_space_ids(client)
    if not live or attempted in live:
        return None
    session = load_gateway_session() or {}
    current = str(session.get("space_id") or session.get("active_space_id") or "").strip()
    if current and current in live and current != attempted:
        return current
    candidates = sorted(s for s in live if s != attempted)
    return candidates[0] if candidates else None


def _send_local_session_message(*, session_token: str, body: dict) -> dict:
    registry = load_gateway_registry()
    session = verify_local_session_token(registry, session_token)
    entry = find_agent_entry(registry, str(session.get("agent_name") or ""))
    if not entry:
        raise LookupError("Local session agent is no longer registered.")
    annotated = annotate_runtime_health(entry, registry=registry)
    if str(annotated.get("approval_state") or "").lower() not in {"not_required", "approved"}:
        raise ValueError("Local session agent is not approved.")
    if not str(body.get("space_id") or "").strip():
        _hydrate_entry_space_from_database(registry, entry)
    space_id = str(
        body.get("space_id")
        or entry.get("active_space_id")
        or entry.get("space_id")
        or entry.get("default_space_id")
        or ""
    ).strip()
    content = str(body.get("content") or "").strip()
    if not space_id:
        raise ValueError("space_id is required.")
    if not content:
        raise ValueError("content is required.")

    # aX task 68cb4d29: Phase-1 opt-in session-continuity challenge. Only
    # gates the /local/send path for now; everything else (inbox poll,
    # generic proxy methods) keeps the easy path. When the env flag is
    # not set, this is a no-op — preserving the current behavior.
    next_session_proof: str | None = None
    if _gateway_session_challenge_enabled():
        next_session_proof = _ensure_session_challenge(
            registry,
            str(session.get("session_id") or ""),
            provided_proof=str(body.get("session_proof") or "").strip() or None,
        )

    client = _load_managed_agent_client(entry)
    metadata = {
        **(body.get("metadata") if isinstance(body.get("metadata"), dict) else {}),
        "gateway_local_session_id": session.get("session_id"),
        "gateway_pass_through_agent": entry.get("name"),
        "gateway_pass_through_agent_id": entry.get("agent_id"),
        "gateway_pass_through_fingerprint_signature": session.get("fingerprint_signature"),
    }
    parent_id = str(body.get("parent_id") or "").strip()
    if parent_id:
        metadata.setdefault("routing_intent", "reply_with_mentions")
    # Defense for clients that skip mention extraction (third-party scripts,
    # older CLI versions, etc.). The helper is idempotent — re-running on
    # already-extracted metadata is a no-op.
    sender_name = str(entry.get("name") or "").strip()
    metadata = (
        merge_explicit_mentions_metadata(metadata, content, exclude=[sender_name] if sender_name else ()) or metadata
    )
    raw_attachments = body.get("attachments")
    attachments_payload: list[dict] | None = None
    if isinstance(raw_attachments, list) and raw_attachments:
        attachments_payload = [a for a in raw_attachments if isinstance(a, dict)]

    def _do_send(target_space: str) -> dict:
        return client.send_message(
            target_space,
            content,
            agent_id=str(entry.get("agent_id") or "") or None,
            channel=str(body.get("channel") or "main"),
            parent_id=parent_id or None,
            metadata=metadata,
            message_type=str(body.get("message_type") or "text"),
            attachments=attachments_payload,
        )

    try:
        payload = _do_send(space_id)
    except httpx.HTTPStatusError as exc:
        # A pinned/hydrated space_id can go stale (platform restart, moved
        # agent). The platform fails closed with 404 "Space not found"; rather
        # than surface a bare URL 404, fall back to a live space once. This is
        # the gateway companion to platform PR #7's fail-closed behavior.
        if not _is_stale_space_error(exc):
            raise
        fallback = _resolve_live_fallback_space(client, space_id)
        if not fallback:
            raise ValueError(
                f"Space {space_id!r} no longer exists on the server "
                f"({_platform_error_detail(exc)}). No live space was available to "
                f"fall back to — re-pin this agent with "
                f"`ax gateway agents move {entry.get('name')} --space <live-space-id>` "
                f"or send with `--space <live-space-id>`."
            ) from exc
        record_gateway_activity(
            "local_message_space_fallback",
            entry=entry,
            stale_space_id=space_id,
            fallback_space_id=fallback,
        )
        apply_entry_current_space(entry, fallback, make_default=False)
        save_gateway_registry(registry)
        payload = _do_send(fallback)
        space_id = fallback
    record_gateway_activity(
        "local_message_sent",
        entry=entry,
        message_id=(payload.get("message") or {}).get("id"),
        attachment_count=len(attachments_payload or []),
    )
    response = {"agent": entry.get("name"), "message": payload, "session": session}
    if next_session_proof is not None:
        response["next_session_proof"] = next_session_proof
    return response


def _create_local_session_task(*, session_token: str, body: dict) -> dict:
    registry = load_gateway_registry()
    session = verify_local_session_token(registry, session_token)
    entry = find_agent_entry(registry, str(session.get("agent_name") or ""))
    if not entry:
        raise LookupError("Local session agent is no longer registered.")
    annotated = annotate_runtime_health(entry, registry=registry)
    if str(annotated.get("approval_state") or "").lower() not in {"not_required", "approved"}:
        raise ValueError("Local session agent is not approved.")
    if not str(body.get("space_id") or "").strip():
        _hydrate_entry_space_from_database(registry, entry)
    space_id = str(
        body.get("space_id")
        or entry.get("active_space_id")
        or entry.get("space_id")
        or entry.get("default_space_id")
        or ""
    ).strip()
    title = str(body.get("title") or "").strip()
    if not space_id:
        raise ValueError("space_id is required.")
    if not title:
        raise ValueError("title is required.")

    client = _load_managed_agent_client(entry)
    payload = client.create_task(
        space_id,
        title,
        description=str(body.get("description") or "").strip() or None,
        priority=str(body.get("priority") or "medium").strip() or "medium",
        assignee_id=str(body.get("assignee_id") or "").strip() or None,
        agent_id=str(entry.get("agent_id") or "") or None,
    )
    task = payload.get("task", payload) if isinstance(payload, dict) else {}
    record_gateway_activity(
        "local_task_created",
        entry=entry,
        task_id=task.get("id") if isinstance(task, dict) else None,
        activity_message=title[:240],
        session_id=session.get("session_id"),
    )
    return {"agent": entry.get("name"), "task": task, "session": session}


_LOCAL_PROXY_METHODS: dict[str, dict] = {
    "whoami": {"tier": "use"},
    "list_spaces": {"tier": "use"},
    "list_agents": {"tier": "use", "kwargs": ["space_id", "limit"]},
    "list_agents_availability": {"tier": "use", "kwargs": ["space_id", "filter_"]},
    "list_context": {"tier": "use", "kwargs": ["prefix", "space_id"]},
    "get_context": {"tier": "use", "args": ["key"], "kwargs": ["space_id"]},
    "list_messages": {
        "tier": "use",
        "kwargs": ["limit", "space_id", "channel", "agent_id", "unread_only", "mark_read"],
    },
    "get_message": {"tier": "use", "args": ["message_id"]},
    "search_messages": {"tier": "use", "args": ["query"], "kwargs": ["limit", "agent_id"]},
    "list_tasks": {"tier": "use", "kwargs": ["limit", "space_id"]},
    "get_task": {"tier": "use", "args": ["task_id"]},
    "update_task": {"tier": "admin", "args": ["task_id"], "kwargs": ["status", "priority", "assignee_id"]},
    # File upload proxy: agents on the Gateway-native path can attach files
    # to messages without holding the user PAT. Daemon reads the path on
    # behalf of the agent and uploads via the agent's managed AxClient, so
    # the upload is correctly attributed to the agent identity. Local-only
    # by construction (paths are relative to the operator's filesystem).
    "upload_file": {"tier": "admin", "args": ["file_path"], "kwargs": ["space_id"]},
}


def _proxy_local_session_call(*, session_token: str, body: dict) -> dict:
    method = str(body.get("method") or "").strip()
    if method not in _LOCAL_PROXY_METHODS:
        raise ValueError(f"method not on Gateway proxy allowlist: {method!r}")
    spec = _LOCAL_PROXY_METHODS[method]
    args_in = body.get("args") if isinstance(body.get("args"), dict) else {}

    registry = load_gateway_registry()
    session = verify_local_session_token(registry, session_token)
    entry = find_agent_entry(registry, str(session.get("agent_name") or ""))
    if not entry:
        raise LookupError("Local session agent is no longer registered.")
    annotated = annotate_runtime_health(entry, registry=registry)
    if str(annotated.get("approval_state") or "").lower() not in {"not_required", "approved"}:
        raise ValueError("Local session agent is not approved.")

    positional: list = []
    for arg_name in spec.get("args", []):
        if arg_name not in args_in or args_in[arg_name] in (None, ""):
            raise ValueError(f"missing required arg for {method}: {arg_name}")
        positional.append(args_in[arg_name])
    keyword: dict = {}
    for key in spec.get("kwargs", []):
        if key in args_in and args_in[key] is not None:
            keyword[key] = args_in[key]

    if method == "upload_file":
        workdir = str(entry.get("workdir") or "").strip()
        if not workdir:
            raise ValueError("upload_file requires the agent to have a workdir configured")
        file_path = Path(positional[0]).resolve()
        workdir_path = Path(workdir).resolve()
        if not str(file_path).startswith(str(workdir_path) + os.sep) and file_path != workdir_path:
            raise ValueError(f"upload_file path {file_path} is outside the agent workdir {workdir_path}")

    client = _load_managed_agent_client(entry)
    method_fn = getattr(client, method, None)
    if not callable(method_fn):
        raise ValueError(f"method not implemented on AxClient: {method!r}")
    result = method_fn(*positional, **keyword)
    record_gateway_activity(
        f"local_proxy_{method}",
        entry=entry,
        session_id=session.get("session_id"),
    )
    return {
        "agent": entry.get("name"),
        "method": method,
        "result": result,
        "session": session,
    }


def _local_session_inbox(
    *,
    session_token: str,
    limit: int = 20,
    channel: str = "main",
    space_id: str | None = None,
    unread_only: bool = True,
    mark_read: bool = True,
) -> dict:
    registry = load_gateway_registry()
    session = verify_local_session_token(registry, session_token)
    entry = find_agent_entry(registry, str(session.get("agent_name") or ""))
    if not entry:
        raise LookupError("Local session agent is no longer registered.")
    annotated = annotate_runtime_health(entry, registry=registry)
    if str(annotated.get("approval_state") or "").lower() not in {"not_required", "approved"}:
        raise ValueError("Local session agent is not approved.")

    selected_space = str(space_id or entry.get("space_id") or "").strip() or None
    client = _load_managed_agent_client(entry)
    data = client.list_messages(
        limit=limit,
        channel=channel,
        space_id=selected_space,
        agent_id=str(entry.get("agent_id") or "") or None,
        unread_only=unread_only,
        mark_read=mark_read,
    )
    messages = data if isinstance(data, list) else data.get("messages", [])
    local_marked_read_count = 0
    if mark_read:
        agent_name = str(entry.get("name") or "")
        pending_items = load_agent_pending_messages(agent_name)
        # The local queue powers the unread badge. Once the inbox has been
        # checked with mark_read enabled, stale local notifications should not
        # keep the Gateway UI saying there is new mail.
        remaining: list[dict] = []
        local_marked_read_count = len(pending_items) - len(remaining)
        save_agent_pending_messages(agent_name, remaining)
        registry = load_gateway_registry()
        stored = find_agent_entry(registry, agent_name) or entry
        stored["backlog_depth"] = len(remaining)
        stored["queue_depth"] = len(remaining)
        stored["current_status"] = "queued" if remaining else None
        stored["current_activity"] = (
            gateway_core._gateway_pickup_activity(str(stored.get("runtime_type") or ""), len(remaining))[:240]
            if remaining
            else None
        )
        if remaining:
            last_pending = remaining[-1]
            stored["last_received_message_id"] = last_pending.get("message_id")
            stored["last_work_received_at"] = (
                last_pending.get("queued_at") or last_pending.get("created_at") or stored.get("last_work_received_at")
            )
        else:
            stored["last_received_message_id"] = None
        save_gateway_registry(registry)
    record_gateway_activity(
        "local_inbox_polled",
        entry=entry,
        message_count=len(messages),
        mark_read=mark_read,
        local_marked_read_count=local_marked_read_count,
        session_id=session.get("session_id"),
    )
    return {
        "agent": entry.get("name"),
        "messages": messages,
        "unread_count": data.get("unread_count") if isinstance(data, dict) else None,
        "marked_read_count": (
            data.get("marked_read_count", local_marked_read_count)
            if isinstance(data, dict)
            else local_marked_read_count
        ),
        "session": session,
    }


# Deferred cross-module imports (bottom-of-file to avoid import cycles; bound
# into module globals after defs, resolved at call time).
from .gateway_agents import (  # noqa: E402
    _load_managed_agent_client,
    _register_managed_agent,
    _registry_ref_for_agent,
    _with_registry_refs,
)
from .gateway_spaces import _hydrate_entry_space_from_database  # noqa: E402
