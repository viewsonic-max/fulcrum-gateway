"""Gateway ManagedAgentRuntime — listener/worker threads and process supervision.

Extracted from ``ax_cli/gateway.py`` (issue #28 Phase 2).
"""

from __future__ import annotations

import hashlib
import json
import os
import queue
import shlex
import signal
import subprocess
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable

import httpx

from .client import AxClient, _RateLimitState
from .commands.listen import (
    _is_self_authored,
    _iter_sse,
    _remember_reply_anchor,
    _should_respond,
    _strip_mention,
)
from .gateway_constants import (
    _NO_REPLY_STATUSES,
    DEFAULT_QUEUE_SIZE,
    MIN_HANDLER_TIMEOUT_SECONDS,
    RUNTIME_HEARTBEAT_INTERVAL_SECONDS,
    SEEN_IDS_MAX,
    SETUP_ERROR_BACKOFF_SCHEDULE,
    SETUP_ERROR_MAX_CONSECUTIVE,
    SSE_IDLE_TIMEOUT_SECONDS,
    _is_passive_runtime,
    _is_sentinel_hermes_sdk_runtime,
    _is_sentinel_inference_sdk_runtime,
)

RuntimeLogger = Callable[[str], None]


def _setup_error_signature(error: str) -> str:
    """SHA-256 hex digest of a setup-error message for dedup (#34).

    The previous shape (``error[:120]``) collided when two distinct
    failures shared a long common prefix — a deep stack trace through
    the same import path, for instance — and the consecutive-error
    counter incorrectly kept incrementing across what were really
    different errors. A hash sidesteps the issue without growing the
    on-disk field size.
    """
    return hashlib.sha256(error.encode("utf-8", errors="replace")).hexdigest()


def _run_exec_handler(
    command: str,
    prompt: str,
    entry: dict[str, Any],
    *,
    message_id: str | None = None,
    space_id: str | None = None,
    timeout_seconds: int | None = None,
    on_event: Callable[[dict[str, Any]], None] | None = None,
) -> str:
    argv = [*shlex.split(command), prompt]
    env = sanitize_exec_env(prompt, entry)
    if message_id:
        env["AX_GATEWAY_MESSAGE_ID"] = message_id
    if space_id:
        env["AX_GATEWAY_SPACE_ID"] = space_id
    # Expose the composed system prompt (operator role + gateway environment
    # context) so exec-runtime bridges (Ollama, custom python bridges, etc.)
    # can read it via env. Hermes / Claude / Sentinel pass the prompt as a
    # CLI flag instead — this env var is for runtimes that aren't built by
    # _build_sentinel_inference_sdk_cmd / _build_sentinel_claude_cmd.
    composed_prompt = _compose_agent_system_prompt(entry)
    if composed_prompt:
        env["AX_AGENT_SYSTEM_PROMPT"] = composed_prompt
    try:
        process = subprocess.Popen(
            argv,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
            cwd=entry.get("workdir") or None,
            env=env,
        )
    except FileNotFoundError:
        return f"(handler not found: {argv[0]})"

    stdout_lines: list[str] = []
    stderr_lines: list[str] = []

    def _consume_stdout() -> None:
        if process.stdout is None:
            return
        try:
            for raw in process.stdout:
                event = _parse_gateway_exec_event(raw)
                if event is not None:
                    if on_event is not None:
                        try:
                            on_event(event)
                        except Exception:
                            pass
                    continue
                stdout_lines.append(raw)
        except ValueError:
            # process.stdout was closed by the finally block in the main
            # thread before this consumer drained the pipe. Bridges that
            # exit quickly (sub-3s Groq calls) can hit this race, dropping
            # the final reply line. Caller's join(timeout=1.0) already
            # gave us a fair window, treat as end-of-stream and return
            # what we have.
            pass

    def _consume_stderr() -> None:
        if process.stderr is None:
            return
        try:
            for raw in process.stderr:
                stderr_lines.append(raw)
        except ValueError:
            pass

    stdout_thread = threading.Thread(target=_consume_stdout, daemon=True, name=f"gw-exec-stdout-{entry.get('name')}")
    stderr_thread = threading.Thread(target=_consume_stderr, daemon=True, name=f"gw-exec-stderr-{entry.get('name')}")
    stdout_thread.start()
    stderr_thread.start()

    timeout_seconds = max(MIN_HANDLER_TIMEOUT_SECONDS, int(timeout_seconds or runtime_timeout_seconds(entry)))
    timed_out = False
    try:
        process.wait(timeout=timeout_seconds)
    except subprocess.TimeoutExpired:
        timed_out = True
        process.kill()
    finally:
        # Give consumer threads enough time to drain the pipe before
        # closing. With 1.0s, fast-exiting bridges (sub-3s Groq calls
        # via langgraph_bridge.py) can lose their final reply line if
        # the OS hasn't propagated EOF before the close fires. 5.0s is
        # well below any caller-side timeout we'd hit. See issue #104.
        stdout_thread.join(timeout=5.0)
        stderr_thread.join(timeout=5.0)
        if process.stdout is not None:
            process.stdout.close()
        if process.stderr is not None:
            process.stderr.close()

    if timed_out:
        raise GatewayRuntimeTimeoutError(timeout_seconds, runtime_type="exec")

    output = "".join(stdout_lines).strip()
    stderr = "".join(stderr_lines).strip()
    if process.returncode != 0 and stderr:
        output = f"{output}\n(stderr: {stderr[:400]})".strip()
    return output or "(no output)"


def _echo_handler(prompt: str, _entry: dict[str, Any]) -> str:
    return f"Echo: {prompt}"


def _gateway_pickup_activity(runtime_type: object, backlog_depth: int) -> str:
    if _is_passive_runtime(runtime_type):
        if backlog_depth > 1:
            return f"Queued in Gateway ({backlog_depth} pending)"
        return "Queued in Gateway"
    if backlog_depth > 1:
        return f"Picked up by Gateway ({backlog_depth} pending)"
    return "Picked up by Gateway"


def _is_sentinel_cli_runtime(runtime_type: object) -> bool:
    return str(runtime_type or "").strip().lower() in {"sentinel_cli", "claude_cli"}


def _is_hermes_plugin_runtime(runtime_type: object) -> bool:
    return str(runtime_type or "").strip().lower() == "hermes_plugin"


def _is_supervised_subprocess_runtime(runtime_type: object) -> bool:
    """Runtimes Gateway supervises as a single long-running child process.

    Both the legacy in-tree sentinel and the new Hermes plugin path fall
    into this bucket: Gateway spawns the process, monitors liveness, and
    tees stdout to a log file. The lifecycle helpers
    (_start/_stop/_monitor) are runtime-specific; this predicate just lets
    the shared start/stop scaffolding treat both the same.
    """
    return (
        _is_sentinel_inference_sdk_runtime(runtime_type)
        or _is_sentinel_hermes_sdk_runtime(runtime_type)
        or _is_hermes_plugin_runtime(runtime_type)
    )


class ManagedAgentRuntime:
    """Listener + worker pair for one managed agent."""

    def __init__(
        self,
        entry: dict[str, Any],
        *,
        client_factory: Callable[..., Any] = AxClient,
        logger: RuntimeLogger | None = None,
        rate_limit_state: _RateLimitState | None = None,
    ) -> None:
        self.entry = dict(entry)
        self.client_factory = client_factory
        self.logger = logger or (lambda _msg: None)
        self._rate_limit_state = rate_limit_state
        self.stop_event = threading.Event()
        self._listener_thread: threading.Thread | None = None
        self._worker_thread: threading.Thread | None = None
        self._stale_signaled: bool = False
        self._queue: queue.Queue = queue.Queue(maxsize=int(entry.get("queue_size") or DEFAULT_QUEUE_SIZE))
        self._reply_anchor_ids: set[str] = set()
        self._seen_ids: set[str] = set()
        self._completed_seen_ids: set[str] = set()
        self._no_reply_seen_ids: set[str] = set()
        self._sentinel_sessions: dict[str, str] = {}
        self._state_lock = threading.Lock()
        self._stream_client = None
        self._send_client = None
        self._stream_response = None
        self._supervised_process: subprocess.Popen | None = None
        self._supervised_thread: threading.Thread | None = None
        self._state: dict[str, Any] = {
            "effective_state": "stopped",
            "runtime_instance_id": None,
            "backlog_depth": 0,
            "dropped_count": 0,
            "processed_count": 0,
            "current_status": None,
            "current_activity": None,
            "current_tool": None,
            "current_tool_call_id": None,
            "last_error": None,
            "last_connected_at": None,
            "last_listener_error_at": None,
            "last_started_at": None,
            "last_seen_at": None,
            "last_work_received_at": None,
            "last_work_completed_at": None,
            "last_received_message_id": None,
            "last_reply_message_id": None,
            "last_reply_preview": None,
            "reconnect_backoff_seconds": 0,
            "consecutive_setup_errors": int(entry.get("consecutive_setup_errors") or 0),
            "last_setup_error_signature": entry.get("last_setup_error_signature"),
            "setup_disabled": bool(entry.get("setup_disabled")),
            "setup_disabled_at": entry.get("setup_disabled_at"),
            "setup_disabled_reason": entry.get("setup_disabled_reason"),
        }

    @property
    def name(self) -> str:
        return str(self.entry.get("name") or "")

    @property
    def agent_id(self) -> str | None:
        value = self.entry.get("agent_id")
        return str(value) if value else None

    @property
    def base_url(self) -> str:
        return str(self.entry.get("base_url") or "")

    @property
    def space_id(self) -> str:
        return str(self.entry.get("space_id") or "")

    @property
    def token_file(self) -> Path:
        return resolve_agent_token_file(self.entry)

    def _log(self, message: str) -> None:
        self.logger(f"{self.name}: {message}")

    def _token(self) -> str:
        return load_gateway_managed_agent_token(self.entry)

    def _new_client(self):
        return self.client_factory(
            base_url=self.base_url,
            token=self._token(),
            agent_name=self.name,
            agent_id=self.agent_id,
            rate_limit_state=self._rate_limit_state,
            on_request_complete=_daemon_request_logger.make_callback(
                agent_name=self.name,
                agent_id=str(self.agent_id or ""),
            ),
        )

    def _send_heartbeat_best_effort(self, status: str) -> None:
        """Create a short-lived client, send one heartbeat, always close it."""
        client = None
        try:
            client = self._new_client()
            client.send_heartbeat(status=status)
        except Exception:  # noqa: BLE001
            pass
        finally:
            if client is not None:
                try:
                    client.close()
                except Exception:  # noqa: BLE001
                    pass

    def _update_state(self, **fields: Any) -> None:
        with self._state_lock:
            prev = self._state.get("effective_state")
            self._state.update(fields)
            new = self._state.get("effective_state")
        if new == "error" and prev != "error":
            self._send_heartbeat_best_effort("setup_error")

    def _bump(self, field: str, amount: int = 1) -> None:
        with self._state_lock:
            self._state[field] = int(self._state.get(field) or 0) + amount

    def _mark_completed_seen(self, message_id: str) -> None:
        if not message_id:
            return
        with self._state_lock:
            self._completed_seen_ids.add(message_id)

    def _consume_completed_seen(self, message_id: str) -> bool:
        if not message_id:
            return False
        with self._state_lock:
            seen = message_id in self._completed_seen_ids
            if seen:
                self._completed_seen_ids.discard(message_id)
            return seen

    def _mark_no_reply_seen(self, message_id: str) -> None:
        if not message_id:
            return
        with self._state_lock:
            self._no_reply_seen_ids.add(message_id)

    def _consume_no_reply_seen(self, message_id: str) -> bool:
        if not message_id:
            return False
        with self._state_lock:
            seen = message_id in self._no_reply_seen_ids
            if seen:
                self._no_reply_seen_ids.discard(message_id)
            return seen

    def _handle_placement_event(self, data: dict[str, Any]) -> None:
        """Handle SSE ``agent.placement.changed`` for this managed agent.

        Per ``specs/GATEWAY-PLACEMENT-POLICY-001/spec.md`` lines 81-93. The
        event carries the new placement record; we update the local Gateway
        registry to keep operator-visible state in sync, log activity, and
        best-effort POST an ack.

        Stub-resilient: if the backend hasn't shipped the ack endpoint yet
        (task ``31adc3a4``), the POST returns 404 and we log a warning. The
        inbound side still works — operators see placement changes in the
        registry without restarting agents.
        """
        try:
            outcome = _apply_placement_event(self.entry, data, agent_name=self.name)
        except Exception as exc:  # noqa: BLE001
            record_gateway_activity(
                "placement_apply_failed",
                entry=self.entry,
                error=str(exc)[:300],
                event=data.get("event_id") or data.get("id"),
            )
            self._log(f"placement event apply failed: {exc}")
            return
        record_gateway_activity(
            "placement_changed",
            entry=self.entry,
            placement_state=outcome.get("placement_state"),
            previous_space=outcome.get("previous_space"),
            new_space=outcome.get("new_space"),
            policy_revision=outcome.get("policy_revision"),
            applied=outcome.get("applied", False),
        )
        if outcome.get("applied"):
            try:
                client = self._new_client()
                _post_placement_ack(
                    client,
                    self.entry,
                    placement_state=str(outcome.get("placement_state") or "applied"),
                    policy_revision=outcome.get("policy_revision"),
                )
            except Exception as exc:  # noqa: BLE001
                # Ack is best-effort while 31adc3a4 ships. Don't kill the listener.
                self._log(f"placement ack failed (non-fatal): {exc}")

    def snapshot(self) -> dict[str, Any]:
        with self._state_lock:
            snapshot = dict(self._state)
        if not _is_passive_runtime(self.entry.get("runtime_type")):
            return snapshot
        registry = load_gateway_registry()
        stored = find_agent_entry(registry, self.name) or {}
        pending_items = load_agent_pending_messages(self.name)
        backlog_depth = len(pending_items)
        last_pending = pending_items[-1] if pending_items else {}
        merged = dict(snapshot)
        for key in (
            "processed_count",
            "last_work_completed_at",
            "last_reply_message_id",
            "last_reply_preview",
            "last_received_message_id",
            "last_work_received_at",
        ):
            if key in stored:
                merged[key] = stored.get(key)
        if backlog_depth > 0:
            merged["last_work_received_at"] = (
                last_pending.get("queued_at") or last_pending.get("created_at") or snapshot.get("last_work_received_at")
            )
        merged["backlog_depth"] = backlog_depth
        merged["current_status"] = "queued" if backlog_depth > 0 else None
        merged["current_activity"] = (
            _gateway_pickup_activity(self.entry.get("runtime_type"), backlog_depth)[:240] if backlog_depth > 0 else None
        )
        with self._state_lock:
            self._state.update(merged)
            return dict(self._state)

    def _record_setup_error(self, error: str) -> None:
        # SHA-256 over the full error string, not a fixed-length prefix:
        # a 120-char prefix made two distinct errors that happened to share
        # a long leading path or exception class look identical, so the
        # auto-disable counter mis-attributed them to the same root cause
        # and incremented past the threshold when it should have reset
        # (#34). Hashing the full error gives O(1) signature size with no
        # false dedup. Full error text still lives on ``last_error`` /
        # activity events for operator-facing debugging.
        signature = _setup_error_signature(error)
        with self._state_lock:
            prev_sig = self._state.get("last_setup_error_signature")
            prev_count = int(self._state.get("consecutive_setup_errors") or 0)
        if prev_sig == signature:
            count = prev_count + 1
        else:
            count = 1
        self._update_state(
            effective_state="error",
            current_status="error",
            current_activity=error,
            last_error=error,
            last_runtime_error_at=_now_iso(),
            consecutive_setup_errors=count,
            last_setup_error_signature=signature,
        )
        self.entry["consecutive_setup_errors"] = count
        self.entry["last_setup_error_signature"] = signature
        self.entry["last_runtime_error_at"] = self._state.get("last_runtime_error_at")
        record_gateway_activity(
            "runtime_error",
            entry=self.entry,
            error=error,
            consecutive_setup_errors=count,
        )
        self._log(f"setup error ({count}/{SETUP_ERROR_MAX_CONSECUTIVE}): {error}")
        if count >= SETUP_ERROR_MAX_CONSECUTIVE:
            disabled_at = _now_iso()
            reason = f"Auto-disabled after {count} consecutive setup errors: {error[:200]}"
            self._update_state(
                setup_disabled=True,
                setup_disabled_at=disabled_at,
                setup_disabled_reason=reason,
            )
            self.entry["setup_disabled"] = True
            self.entry["setup_disabled_at"] = disabled_at
            self.entry["setup_disabled_reason"] = reason
            record_gateway_activity(
                "runtime_auto_disabled",
                entry=self.entry,
                consecutive_errors=count,
                error=error[:200],
            )
            self._log(f"auto-disabled after {count} consecutive setup errors")

    def _clear_setup_error_state(self) -> None:
        fields = {
            "consecutive_setup_errors": 0,
            "last_setup_error_signature": None,
            "setup_disabled": False,
            "setup_disabled_at": None,
            "setup_disabled_reason": None,
        }
        self._update_state(**fields)
        self.entry.update(fields)

    def start(self) -> None:
        if self.entry.get("setup_disabled"):
            return
        runtime_type = str(self.entry.get("runtime_type") or "").lower()
        if (
            _is_supervised_subprocess_runtime(runtime_type)
            and self._supervised_process is not None
            and self._supervised_process.poll() is None
        ):
            return
        if self._listener_thread and self._listener_thread.is_alive():
            return
        # Escalating backoff: index into the schedule using consecutive error
        # count.  First failure waits 30s, then 60/120/300/600s.  Prevents
        # retry-storms when the precondition (binary, token, script) stays
        # broken — the operator must fix it and `agents start <name>`.
        last_runtime_error_at = self.entry.get("last_runtime_error_at")
        if last_runtime_error_at:
            consecutive = int(self.entry.get("consecutive_setup_errors") or 0)
            idx = min(max(consecutive - 1, 0), len(SETUP_ERROR_BACKOFF_SCHEDULE) - 1)
            backoff = SETUP_ERROR_BACKOFF_SCHEDULE[idx]
            age = _age_seconds(last_runtime_error_at)
            if age is not None and age < backoff:
                return
        self.stop_event.clear()
        self._queue = queue.Queue(maxsize=int(self.entry.get("queue_size") or DEFAULT_QUEUE_SIZE))
        self._reply_anchor_ids = set()
        self._seen_ids = set()
        self._completed_seen_ids = set()
        self._sentinel_sessions = {}
        pending_items = load_agent_pending_messages(self.name) if _is_passive_runtime(runtime_type) else []
        backlog_depth = len(pending_items)
        runtime_instance_id = str(uuid.uuid4())
        self.entry["runtime_instance_id"] = runtime_instance_id
        self._update_state(
            effective_state="starting",
            runtime_instance_id=runtime_instance_id,
            backlog_depth=backlog_depth,
            current_status="queued" if backlog_depth > 0 and _is_passive_runtime(runtime_type) else None,
            current_activity=_gateway_pickup_activity(runtime_type, backlog_depth)
            if backlog_depth > 0 and _is_passive_runtime(runtime_type)
            else None,
            current_tool=None,
            current_tool_call_id=None,
            last_error=None,
            last_listener_error_at=None,
            last_started_at=_now_iso(),
            reconnect_backoff_seconds=0,
        )
        if _is_sentinel_inference_sdk_runtime(runtime_type) or _is_sentinel_hermes_sdk_runtime(runtime_type):
            self._start_sentinel_inference_sdk_process(runtime_instance_id=runtime_instance_id)
            return
        if _is_hermes_plugin_runtime(runtime_type):
            self._start_hermes_plugin_process(runtime_instance_id=runtime_instance_id)
            return
        self._worker_thread = None
        if not _is_passive_runtime(self.entry.get("runtime_type")):
            self._worker_thread = threading.Thread(
                target=self._worker_loop,
                daemon=True,
                name=f"gw-worker-{self.name}",
            )
        self._listener_thread = threading.Thread(
            target=self._listener_loop,
            daemon=True,
            name=f"gw-listener-{self.name}",
        )
        if self._worker_thread is not None:
            self._worker_thread.start()
        self._listener_thread.start()
        record_gateway_activity("runtime_started", entry=self.entry, runtime_instance_id=runtime_instance_id)
        self._log("started")

    def stop(self, timeout: float = 5.0) -> None:
        self.stop_event.set()
        try:
            self._queue.put_nowait(None)
        except queue.Full:
            pass
        if self._stream_response is not None:
            try:
                self._stream_response.close()
            except Exception:
                pass
        self._stop_sentinel_inference_sdk_process(timeout=timeout)
        for thread in (self._listener_thread, self._worker_thread, self._supervised_thread):
            if thread and thread.is_alive():
                thread.join(timeout=timeout)
        for client in (self._stream_client, self._send_client):
            if client is not None:
                try:
                    client.close()
                except Exception:
                    pass
        self._stream_client = None
        self._send_client = None
        self._stream_response = None
        self.entry["runtime_instance_id"] = None
        self._update_state(
            effective_state="stopped",
            runtime_instance_id=None,
            backlog_depth=0,
            current_status=None,
            current_activity=None,
            current_tool=None,
            current_tool_call_id=None,
        )
        self._send_heartbeat_best_effort("offline")
        record_gateway_activity("runtime_stopped", entry=self.entry)
        self._log("stopped")

    def _sentinel_inference_sdk_log_path(self) -> Path:
        configured = str(self.entry.get("log_path") or "").strip()
        if configured:
            return Path(configured).expanduser()
        return _sentinel_inference_sdk_workdir(self.entry) / "gateway-hermes-sentinel.log"

    def _start_sentinel_inference_sdk_process(self, *, runtime_instance_id: str) -> None:
        workdir = _sentinel_inference_sdk_workdir(self.entry)
        script = _sentinel_inference_sdk_script(self.entry)
        if not script.exists():
            self._record_setup_error(f"Sentinel script not found: {script}")
            return
        try:
            load_gateway_managed_agent_token(self.entry)
        except ValueError as exc:
            self._record_setup_error(str(exc))
            return
        python_binary = _sentinel_inference_sdk_python(self.entry)
        python_path = Path(python_binary)
        if python_path.is_absolute() and not python_path.exists():
            self._record_setup_error(
                f"Python binary not found: {python_binary} (listener may need reinstall or venv rebuild)"
            )
            return

        if _is_sentinel_hermes_sdk_runtime(self.entry.get("runtime_type")):
            resolved_sdk_runtime = "hermes_sdk"
        else:
            resolved_sdk_runtime = _resolve_inference_client(self.entry)
            if resolved_sdk_runtime is None:
                self._record_setup_error(
                    "sentinel_inference_sdk requires a client to be configured — "
                    "no default. Set it: ax gateway agents update <name> "
                    "--client openai_sdk  (or gemini_sdk|groq_sdk|mistral_sdk|...)"
                )
                return

        workdir.mkdir(parents=True, exist_ok=True)
        log_path = self._sentinel_inference_sdk_log_path()
        log_path.parent.mkdir(parents=True, exist_ok=True)
        cmd = _build_sentinel_inference_sdk_cmd(self.entry, sdk_runtime=resolved_sdk_runtime)
        env = _build_sentinel_inference_sdk_env(self.entry)
        try:
            log_handle = log_path.open("a", encoding="utf-8")
            log_handle.write(
                f"\n[{_now_iso()}] Gateway starting sentinel process: {' '.join(shlex.quote(part) for part in cmd)}\n"
            )
            log_handle.flush()
            process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
                cwd=str(workdir),
                env=env,
                start_new_session=True,
            )
            self._sentinel_log_handle = log_handle
            self._sentinel_stdout_thread = threading.Thread(
                target=self._consume_sentinel_stdout,
                args=(process, log_handle),
                daemon=True,
                name=f"gw-hermes-stdout-{self.name}",
            )
            self._sentinel_stdout_thread.start()
        except Exception as exc:
            self._record_setup_error(f"Failed to start sentinel process: {str(exc)[:360]}")
            return

        self._supervised_process = process
        self._clear_setup_error_state()
        self._update_state(
            effective_state="running",
            current_status=None,
            current_activity="Sentinel listener running",
            current_tool=None,
            current_tool_call_id=None,
            last_error=None,
            last_runtime_error_at=None,
            last_connected_at=_now_iso(),
            last_seen_at=_now_iso(),
            reconnect_backoff_seconds=0,
        )
        self.entry["last_runtime_error_at"] = None
        record_gateway_activity(
            "runtime_started",
            entry=self.entry,
            runtime_instance_id=runtime_instance_id,
            pid=process.pid,
            log_path=str(log_path),
            supervised_runtime="sentinel_inference_sdk",
        )
        self._supervised_thread = threading.Thread(
            target=self._monitor_sentinel_inference_sdk_process,
            daemon=True,
            name=f"gw-hermes-sentinel-{self.name}",
        )
        self._supervised_thread.start()
        self._log(f"started sentinel_inference_sdk pid={process.pid}")

    def _consume_sentinel_stdout(self, process: subprocess.Popen, log_handle) -> None:
        """Read sentinel stdout line-by-line, parse AX_GATEWAY_EVENT lines and
        forward them to the activity stream. All other lines tee to the
        existing log file unchanged so operator visibility stays the same.

        Also writes gateway-side activity events (record_gateway_activity) so
        the simple-gateway drawer surfaces the same lifecycle the listener-loop
        path produces:
          - first sight of a message_id  → message_received
          - status=accepted              → message_claimed
          - status=completed             → reply_sent (clears the "Working" pill)
          - status=error                 → runtime_error
        Without these, supervised-subprocess runtimes (Hermes) would have an
        activity feed that never clears past "Working" and never shows messages
        delivered via the agent's own SSE listener (e.g. user-authored DMs).
        """
        seen_message_ids: set[str] = set()
        try:
            stdout = process.stdout
            if stdout is None:
                return
            for raw in stdout:
                # Always tee to log file first so operator can `tail -f`.
                try:
                    log_handle.write(raw)
                    log_handle.flush()
                except Exception:
                    pass
                event = _parse_gateway_exec_event(raw)
                if event is None:
                    continue
                kind = str(event.get("kind") or "").strip().lower()
                if kind == "heartbeat":
                    # Relayed platform-liveness beat from the sentinel's embedded
                    # SSE adapter. Refresh freshness only — this is the activity
                    # signal the staleness ladder reads (ADR-008). It replaces the
                    # monitor thread's PID-existence stamping (#295) so a
                    # wedged-but-alive sentinel ages into stale/offline instead of
                    # rendering green forever.
                    self._update_state(last_seen_at=_now_iso())
                    continue
                if kind != "status":
                    continue
                message_id = str(event.get("message_id") or "").strip()
                if not message_id:
                    continue
                status = str(event.get("status") or "processing").strip()
                normalized_status = status.lower()
                activity = str(event.get("activity") or event.get("message") or "").strip() or None
                tool_name = str(event.get("tool_name") or event.get("tool") or "").strip() or None
                # Mirror the runtime worker's update + publish path so the row
                # status pill and the aX UI bubble both reflect what the
                # sentinel is currently doing.
                if normalized_status in _NO_REPLY_STATUSES:
                    self._record_no_reply_decision(
                        message_id,
                        reason=str(event.get("reason") or normalized_status),
                        activity=activity,
                    )
                    continue
                updates: dict[str, Any] = {"current_status": status, "last_seen_at": _now_iso()}
                if activity is not None:
                    updates["current_activity"] = activity[:240]
                if tool_name is not None:
                    updates["current_tool"] = tool_name[:120]
                if status == "completed":
                    updates["current_status"] = None
                    updates["current_activity"] = None
                    updates["current_tool"] = None
                    updates["last_work_completed_at"] = _now_iso()
                self._update_state(**updates)
                self._publish_processing_status(
                    message_id,
                    status,
                    activity=activity,
                    tool_name=tool_name,
                )

                # Drawer-visible lifecycle events. We synthesize them from the
                # sentinel's status stream so the drawer feed matches the
                # backend-side activity bubble.
                if message_id not in seen_message_ids:
                    seen_message_ids.add(message_id)
                    record_gateway_activity(
                        "message_received",
                        entry=self.entry,
                        message_id=message_id,
                        preview=activity,
                    )
                    self._update_state(
                        last_work_received_at=_now_iso(),
                        last_received_message_id=message_id,
                    )
                if status == "accepted":
                    record_gateway_activity(
                        "message_claimed",
                        entry=self.entry,
                        message_id=message_id,
                    )
                elif status == "completed":
                    record_gateway_activity(
                        "reply_sent",
                        entry=self.entry,
                        message_id=message_id,
                        reply_preview=activity,
                    )
                    self._bump("processed_count")
                elif status == "error":
                    record_gateway_activity(
                        "runtime_error",
                        entry=self.entry,
                        message_id=message_id,
                        error=str(event.get("error_message") or activity or "")[:400],
                    )
                elif tool_name and status == "processing":
                    # Surface tool calls so operators can see what Hermes is
                    # actually doing turn-by-turn (not just "thinking").
                    record_gateway_activity(
                        "runtime_activity",
                        entry=self.entry,
                        message_id=message_id,
                        activity_message=f"{tool_name}: {activity}" if activity else tool_name,
                        tool_name=tool_name,
                    )
        except Exception as exc:
            self._log(f"sentinel stdout consumer error: {exc}")
        finally:
            try:
                log_handle.close()
            except Exception:
                pass

    def _monitor_sentinel_inference_sdk_process(self) -> None:
        process = self._supervised_process
        if process is None:
            return
        while not self.stop_event.wait(timeout=5.0):
            returncode = process.poll()
            if returncode is None:
                # PID alive is not activity. Do NOT refresh last_seen_at or clear
                # last_error here: a sentinel that is alive but dead inside
                # (wedged loop, deadlock, SIGSTOP'd, or platform connection
                # silently gone) would otherwise keep a permanently fresh
                # last_seen_at and render green forever, defeating the 75s/300s
                # staleness ladder (ADR-008, #295). Freshness comes from the
                # sentinel's relayed platform heartbeats (kind="heartbeat"
                # exec-events); this poll stays only for fast exit detection.
                # Re-affirm running so a transient state can't strand the row.
                self._update_state(effective_state="running")
                continue
            status = "stopped" if returncode == 0 else "error"
            error = None if returncode == 0 else f"Sentinel process exited with code {returncode}"
            self._update_state(
                effective_state=status,
                current_status=None if returncode == 0 else "error",
                current_activity=None if returncode == 0 else error,
                current_tool=None,
                current_tool_call_id=None,
                last_error=error,
                last_seen_at=_now_iso(),
            )
            record_gateway_activity(
                "runtime_exited",
                entry=self.entry,
                pid=process.pid,
                exit_code=returncode,
                error=error,
            )
            return

    def _stop_sentinel_inference_sdk_process(self, *, timeout: float = 5.0) -> None:
        # Despite the name, this stop path is runtime-agnostic: it just SIGTERMs
        # self._supervised_process. Both sentinel_inference_sdk and hermes_plugin land
        # here from stop(). The function early-returns when there is no
        # supervised child, so it is safe to call for any runtime type.
        process = self._supervised_process
        self._supervised_process = None
        if process is None or process.poll() is not None:
            return
        try:
            os.killpg(process.pid, signal.SIGTERM)
            process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait(timeout=timeout)
        except ProcessLookupError:
            return
        except Exception:
            try:
                process.terminate()
                process.wait(timeout=timeout)
            except Exception:
                pass

    # ----- hermes_plugin runtime (Gateway-supervised `hermes gateway run`) -----

    def _hermes_plugin_log_path(self) -> Path:
        configured = str(self.entry.get("log_path") or "").strip()
        if configured:
            return Path(configured).expanduser()
        return _hermes_plugin_workdir(self.entry) / "gateway-hermes-plugin.log"

    def _start_hermes_plugin_process(self, *, runtime_instance_id: str) -> None:
        try:
            hermes_bin_path = _hermes_bin(self.entry)
        except RuntimeError as exc:
            self._record_setup_error(str(exc))
            return
        try:
            load_gateway_managed_agent_token(self.entry)
        except ValueError as exc:
            self._record_setup_error(str(exc))
            return
        try:
            home = _scaffold_hermes_plugin_home(self.entry)
        except OSError as exc:
            self._record_setup_error(f"Failed to scaffold HERMES_HOME ({_hermes_plugin_home(self.entry)}): {exc}")
            return

        workdir = _hermes_plugin_workdir(self.entry)
        log_path = self._hermes_plugin_log_path()
        log_path.parent.mkdir(parents=True, exist_ok=True)
        cmd = _build_hermes_plugin_cmd(self.entry)
        env = _build_hermes_plugin_env(self.entry)
        try:
            log_handle = log_path.open("a", encoding="utf-8")
            log_handle.write(
                f"\n[{_now_iso()}] Gateway starting Hermes plugin: "
                f"{shlex.quote(hermes_bin_path)} gateway run "
                f"(HERMES_HOME={home}, AX_AGENT_NAME={env.get('AX_AGENT_NAME')})\n"
            )
            log_handle.flush()
            process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
                cwd=str(workdir),
                env=env,
                start_new_session=True,
            )
            self._sentinel_log_handle = log_handle
            # Reuse the sentinel stdout consumer's tee-to-log behavior. The
            # plugin doesn't emit AX_GATEWAY_EVENT lines (it posts activity
            # directly to aX via the platform adapter), so the parser stays
            # silent and only the log-tee side fires. If the plugin ever
            # starts emitting those events, no change needed here.
            self._sentinel_stdout_thread = threading.Thread(
                target=self._consume_sentinel_stdout,
                args=(process, log_handle),
                daemon=True,
                name=f"gw-hermes-plugin-stdout-{self.name}",
            )
            self._sentinel_stdout_thread.start()
        except Exception as exc:
            self._record_setup_error(f"Failed to start Hermes plugin: {str(exc)[:360]}")
            return

        self._supervised_process = process
        self._clear_setup_error_state()
        self._update_state(
            effective_state="running",
            current_status=None,
            current_activity="Hermes plugin runtime running",
            current_tool=None,
            current_tool_call_id=None,
            last_error=None,
            last_runtime_error_at=None,
            last_connected_at=_now_iso(),
            last_seen_at=_now_iso(),
            reconnect_backoff_seconds=0,
        )
        self.entry["last_runtime_error_at"] = None
        record_gateway_activity(
            "runtime_started",
            entry=self.entry,
            runtime_instance_id=runtime_instance_id,
            pid=process.pid,
            log_path=str(log_path),
            supervised_runtime="hermes_plugin",
        )
        self._supervised_thread = threading.Thread(
            target=self._monitor_hermes_plugin_process,
            daemon=True,
            name=f"gw-hermes-plugin-{self.name}",
        )
        self._supervised_thread.start()
        self._log(f"started hermes_plugin pid={process.pid}")

    def _monitor_hermes_plugin_process(self) -> None:
        process = self._supervised_process
        if process is None:
            return
        while not self.stop_event.wait(timeout=5.0):
            returncode = process.poll()
            if returncode is None:
                self._update_state(effective_state="running", last_seen_at=_now_iso(), last_error=None)
                continue
            status = "stopped" if returncode == 0 else "error"
            error = None if returncode == 0 else f"Hermes plugin exited with code {returncode}"
            self._update_state(
                effective_state=status,
                current_status=None if returncode == 0 else "error",
                current_activity=None if returncode == 0 else error,
                current_tool=None,
                current_tool_call_id=None,
                last_error=error,
                last_seen_at=_now_iso(),
            )
            record_gateway_activity(
                "runtime_exited",
                entry=self.entry,
                pid=process.pid,
                exit_code=returncode,
                error=error,
            )
            return

    def _publish_processing_status(
        self,
        message_id: str,
        status: str,
        *,
        activity: str | None = None,
        tool_name: str | None = None,
        progress: dict[str, Any] | None = None,
        detail: dict[str, Any] | None = None,
        reason: str | None = None,
        error_message: str | None = None,
        retry_after_seconds: int | None = None,
        parent_message_id: str | None = None,
    ) -> None:
        # Lazy-init send_client for runtimes that don't enter _listener_loop
        # (e.g. sentinel_inference_sdk and other supervised-subprocess runtimes).
        # Without this, AX_GATEWAY_EVENT lines parsed from the sentinel's
        # stdout would never reach the backend and the activity bubble
        # stalls at "Working".
        if not self._send_client:
            try:
                self._send_client = self._new_client()
            except Exception as exc:  # noqa: BLE001
                self._log(
                    f"processing-status drop (send_client init failed): msg={message_id} status={status} err={exc}"
                )
                return
        try:
            self._send_client.set_agent_processing_status(
                message_id,
                status,
                agent_name=self.name,
                space_id=self.space_id,
                activity=activity,
                tool_name=tool_name,
                progress=progress,
                detail=detail,
                reason=reason,
                error_message=error_message,
                retry_after_seconds=retry_after_seconds,
                parent_message_id=parent_message_id,
            )
        except Exception as exc:  # noqa: BLE001
            self._log(f"processing-status post failed: msg={message_id} status={status} err={exc}")

    def _record_no_reply_decision(
        self,
        message_id: str,
        *,
        reason: str | None = None,
        activity: str | None = None,
    ) -> None:
        """Record an explicit terminal no-reply decision without posting a chat reply."""
        self._mark_no_reply_seen(message_id)
        raw_reason_code = (reason or "no_reply").strip() or "no_reply"
        canonical_reason = "no_reply"
        message = (activity or "Chose not to respond").strip() or "Chose not to respond"
        self._update_state(
            current_status=None,
            current_activity=None,
            current_tool=None,
            current_tool_call_id=None,
            last_error=None,
            last_work_completed_at=_now_iso(),
        )
        self._publish_processing_status(
            message_id,
            "no_reply",
            activity=message,
            reason=canonical_reason,
            detail={"terminal": True, "reply_created": False, "reason_code": raw_reason_code},
        )
        record_gateway_activity(
            "agent_skipped",
            entry=self.entry,
            message_id=message_id,
            status="no_reply",
            activity_message=message,
            reason=canonical_reason,
            reason_code=raw_reason_code,
        )
        if not self._send_client:
            return
        metadata = self._gateway_message_metadata(message_id)
        gateway_meta = metadata.setdefault("gateway", {})
        gateway_meta.update(
            {
                "signal_kind": "agent_skipped",
                "reason": canonical_reason,
                "reason_code": raw_reason_code,
                "reply_created": False,
            }
        )
        metadata.update(
            {
                "signal_only": True,
                "reason": canonical_reason,
                "reason_code": raw_reason_code,
                "signal_kind": "agent_skipped",
            }
        )
        try:
            self._send_client.send_message(
                self.space_id,
                message,
                agent_id=self.agent_id,
                parent_id=message_id,
                metadata=metadata,
                message_type="agent_pause",
            )
        except Exception as exc:  # noqa: BLE001
            self._log(f"agent-pause audit row failed: msg={message_id} reason={raw_reason_code} err={exc}")

    @staticmethod
    def _processing_status_metadata(event: dict[str, Any]) -> dict[str, Any]:
        progress = event.get("progress") if isinstance(event.get("progress"), dict) else None
        detail = event.get("detail") if isinstance(event.get("detail"), dict) else None
        if detail is None and isinstance(event.get("initial_data"), dict):
            detail = event.get("initial_data")
        reason = str(event.get("reason") or "").strip() or None
        error_message = str(event.get("error_message") or "").strip() or None
        parent_message_id = str(event.get("parent_message_id") or "").strip() or None

        retry_after_seconds = None
        retry_after_raw = event.get("retry_after_seconds")
        if retry_after_raw is not None:
            try:
                retry_after_seconds = int(retry_after_raw)
            except (TypeError, ValueError):
                retry_after_seconds = None

        return {
            "progress": progress,
            "detail": detail,
            "reason": reason,
            "error_message": error_message,
            "retry_after_seconds": retry_after_seconds,
            "parent_message_id": parent_message_id,
        }

    def _record_tool_call(self, *, message_id: str, event: dict[str, Any]) -> None:
        # Lazy-init for supervised-subprocess runtimes (see _publish_processing_status).
        if not self._send_client:
            try:
                self._send_client = self._new_client()
            except Exception as exc:  # noqa: BLE001
                self._log(f"tool-call drop (send_client init failed): err={exc}")
                return
        tool_name = str(event.get("tool_name") or event.get("tool") or "").strip()
        if not tool_name:
            return
        tool_call_id = str(event.get("tool_call_id") or uuid.uuid4())
        arguments = event.get("arguments") if isinstance(event.get("arguments"), dict) else None
        initial_data = event.get("initial_data") if isinstance(event.get("initial_data"), dict) else None
        duration_raw = event.get("duration_ms")
        try:
            duration_ms = int(duration_raw) if duration_raw is not None else None
        except (TypeError, ValueError):
            duration_ms = None
        try:
            self._send_client.record_tool_call(
                tool_name=tool_name,
                tool_call_id=tool_call_id,
                space_id=self.space_id,
                tool_action=str(event.get("tool_action") or event.get("tool_action_name") or event.get("command") or "")
                or None,
                resource_uri=str(event.get("resource_uri") or "ui://gateway/tool-call"),
                arguments_hash=_hash_tool_arguments(arguments),
                kind=str(event.get("kind_name") or event.get("result_kind") or "gateway_runtime"),
                arguments=arguments,
                initial_data=initial_data,
                status=str(event.get("status") or "success"),
                duration_ms=duration_ms,
                agent_name=self.name,
                agent_id=self.agent_id,
                message_id=message_id,
                correlation_id=str(event.get("correlation_id") or message_id),
            )
            record_gateway_activity(
                "tool_call_recorded",
                entry=self.entry,
                message_id=message_id,
                tool_name=tool_name,
                tool_call_id=tool_call_id,
            )
        except Exception as exc:
            record_gateway_activity(
                "tool_call_record_failed",
                entry=self.entry,
                message_id=message_id,
                tool_name=tool_name,
                tool_call_id=tool_call_id,
                error=str(exc)[:400],
            )

    def _handle_exec_event(self, event: dict[str, Any], *, message_id: str) -> None:
        kind = str(event.get("kind") or event.get("type") or "").strip().lower()
        if not kind:
            return
        if kind == "status":
            status = str(event.get("status") or "processing").strip()
            normalized_status = status.lower()
            if status == "completed":
                self._mark_completed_seen(message_id)
            activity = str(event.get("message") or event.get("activity") or "").strip() or None
            tool_name = str(event.get("tool") or event.get("tool_name") or "").strip() or None
            metadata = self._processing_status_metadata(event)
            if normalized_status in _NO_REPLY_STATUSES:
                self._record_no_reply_decision(
                    message_id,
                    reason=metadata["reason"] or normalized_status,
                    activity=activity,
                )
                return
            updates: dict[str, Any] = {}
            updates["current_status"] = status
            if activity is not None:
                updates["current_activity"] = activity[:240]
            if tool_name is not None:
                updates["current_tool"] = tool_name[:120]
            if status == "completed":
                updates["current_status"] = None
                updates.setdefault("current_activity", None)
                updates.setdefault("current_tool", None)
                updates["current_tool_call_id"] = None
            if updates:
                self._update_state(**updates)
            if message_id:
                self._publish_processing_status(
                    message_id,
                    status,
                    activity=activity,
                    tool_name=tool_name,
                    **metadata,
                )
            record_gateway_activity(
                "runtime_status",
                entry=self.entry,
                message_id=message_id,
                status=status,
                activity_message=activity,
                tool_name=tool_name,
            )
            return

        if kind == "tool_start":
            tool_name = str(event.get("tool_name") or event.get("tool") or "tool").strip()
            tool_call_id = str(event.get("tool_call_id") or uuid.uuid4())
            activity = str(event.get("message") or f"Using {tool_name}").strip()
            status = str(event.get("status") or "tool_call").strip()
            metadata = self._processing_status_metadata(event)
            self._update_state(
                current_status=status,
                current_activity=activity[:240],
                current_tool=tool_name[:120] or None,
                current_tool_call_id=tool_call_id,
            )
            if message_id:
                self._publish_processing_status(
                    message_id,
                    status,
                    activity=activity,
                    tool_name=tool_name or None,
                    **metadata,
                )
            record_gateway_activity(
                "tool_started",
                entry=self.entry,
                message_id=message_id,
                tool_name=tool_name,
                tool_call_id=tool_call_id,
                tool_action=str(event.get("tool_action") or event.get("command") or "") or None,
            )
            return

        if kind == "tool_result":
            tool_name = str(event.get("tool_name") or event.get("tool") or "tool").strip()
            tool_call_id = str(event.get("tool_call_id") or uuid.uuid4())
            status = str(event.get("status") or "success").strip()
            metadata = self._processing_status_metadata(event)
            self._record_tool_call(message_id=message_id, event=event)
            step_status = (
                "tool_complete" if status.lower() in {"success", "completed", "ok", "tool_complete"} else "error"
            )
            self._update_state(
                current_status=None if step_status == "tool_complete" else step_status,
                current_activity=None,
                current_tool=None,
                current_tool_call_id=None,
            )
            if message_id:
                self._publish_processing_status(
                    message_id,
                    step_status,
                    tool_name=tool_name or None,
                    detail=metadata["detail"],
                    reason=metadata["reason"] or (None if step_status == "tool_complete" else status),
                    error_message=metadata["error_message"],
                    retry_after_seconds=metadata["retry_after_seconds"],
                    parent_message_id=metadata["parent_message_id"],
                )
            record_gateway_activity(
                "tool_finished",
                entry=self.entry,
                message_id=message_id,
                tool_name=tool_name,
                tool_call_id=tool_call_id,
                status=status,
            )
            return

        if kind == "activity":
            activity = str(event.get("message") or event.get("activity") or "").strip()
            if activity:
                self._update_state(current_activity=activity[:240])
            record_gateway_activity(
                "runtime_activity",
                entry=self.entry,
                message_id=message_id,
                activity_message=activity or None,
            )

    def _sentinel_session_id(self, session_key: str) -> str | None:
        with self._state_lock:
            return self._sentinel_sessions.get(session_key)

    def _remember_sentinel_session(self, session_key: str, session_id: str | None) -> None:
        if not session_id:
            return
        with self._state_lock:
            self._sentinel_sessions[session_key] = session_id

    def _build_sentinel_cmd(self, session_id: str | None) -> list[str]:
        command_override = str(self.entry.get("sentinel_command") or "").strip()
        if command_override:
            command = shlex.split(command_override)
            if session_id:
                command.extend(["--resume", session_id])
            return command
        return _build_sentinel_claude_cmd(self.entry, session_id)

    def _handle_sentinel_cli_prompt(self, prompt: str, *, message_id: str, data: dict[str, Any] | None = None) -> str:
        runtime_name = _sentinel_runtime_name(self.entry)
        session_key = _sentinel_session_key(self.entry, data, message_id)
        existing_session = self._sentinel_session_id(session_key)
        cmd = self._build_sentinel_cmd(existing_session)
        env = sanitize_exec_env(prompt, self.entry)
        if message_id:
            env["AX_GATEWAY_MESSAGE_ID"] = message_id
        if self.space_id:
            env["AX_GATEWAY_SPACE_ID"] = self.space_id
        env["AX_GATEWAY_SENTINEL_SESSION_KEY"] = session_key

        start_activity = (
            f"Resuming {runtime_name} sentinel session"
            if existing_session
            else f"Starting {runtime_name} sentinel session"
        )
        self._publish_processing_status(message_id, "thinking", activity=start_activity)
        self._update_state(current_status="thinking", current_activity=start_activity[:240])
        record_gateway_activity(
            "runtime_status",
            entry=self.entry,
            message_id=message_id,
            status="thinking",
            activity_message=start_activity,
        )

        try:
            process = subprocess.Popen(
                cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
                cwd=self.entry.get("workdir") or None,
                env=env,
            )
        except FileNotFoundError:
            return f"(handler not found: {cmd[0]})"

        if process.stdin is not None:
            try:
                process.stdin.write(prompt)
                process.stdin.close()
            except Exception:
                pass

        accumulated_text = ""
        stderr_lines: list[str] = []
        new_session_id: str | None = None
        last_activity_time = time.time()
        exit_reason = "done"
        timeout_seconds = runtime_timeout_seconds(self.entry)
        finished = threading.Event()

        def _consume_stderr() -> None:
            if process.stderr is None:
                return
            for raw in process.stderr:
                stderr_lines.append(raw)

        def _timeout_watchdog() -> None:
            nonlocal exit_reason
            while not finished.wait(timeout=5.0):
                if time.time() - last_activity_time <= timeout_seconds:
                    continue
                exit_reason = "timeout"
                try:
                    process.kill()
                except Exception:
                    pass
                return

        stderr_thread = threading.Thread(target=_consume_stderr, daemon=True, name=f"gw-sentinel-stderr-{self.name}")
        watchdog_thread = threading.Thread(
            target=_timeout_watchdog, daemon=True, name=f"gw-sentinel-watchdog-{self.name}"
        )
        stderr_thread.start()
        watchdog_thread.start()

        try:
            if process.stdout is not None:
                for raw in process.stdout:
                    line = raw.strip()
                    if not line:
                        continue
                    last_activity_time = time.time()
                    try:
                        event = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if not isinstance(event, dict):
                        continue

                    event_type = str(event.get("type") or "")
                    if event_type == "assistant":
                        for block in event.get("message", {}).get("content", []):
                            if not isinstance(block, dict):
                                continue
                            block_type = str(block.get("type") or "")
                            if block_type == "text":
                                accumulated_text = str(block.get("text") or accumulated_text)
                            elif block_type == "tool_use":
                                self._handle_claude_tool_use(block, message_id=message_id)
                    elif event_type == "content_block_delta":
                        delta = event.get("delta") if isinstance(event.get("delta"), dict) else {}
                        if delta.get("type") == "text_delta":
                            accumulated_text += str(delta.get("text") or "")
                    elif event_type == "result":
                        result_text = str(event.get("result") or "").strip()
                        if result_text:
                            accumulated_text = result_text
                        new_session_id = str(event.get("session_id") or "") or new_session_id
        except Exception as exc:
            exit_reason = "crashed"
            record_gateway_activity(
                "runtime_error",
                entry=self.entry,
                message_id=message_id or None,
                error=f"sentinel stream error: {str(exc)[:360]}",
            )
        finally:
            finished.set()

        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
        stderr_thread.join(timeout=1.0)

        if process.returncode != 0 and exit_reason == "done":
            exit_reason = "crashed"
        self._remember_sentinel_session(session_key, new_session_id)
        if new_session_id:
            record_gateway_activity(
                "runtime_session_saved",
                entry=self.entry,
                message_id=message_id,
                session_key=session_key,
                session_id=new_session_id[:24],
            )

        final = accumulated_text.strip()
        stderr = "".join(stderr_lines).strip()
        if exit_reason == "timeout":
            raise GatewayRuntimeTimeoutError(timeout_seconds, runtime_type=runtime_name)
        if exit_reason == "crashed":
            if final:
                return final
            if stderr:
                return f"Hit an error processing that.\n\n(stderr: {stderr[:400]})"
            return "Hit an error processing that."
        return final or "Completed with no text output."

    def _handle_sentinel_tool_item(self, item: dict[str, Any], *, message_id: str, phase: str) -> None:
        item_type = str(item.get("type") or "tool").strip() or "tool"
        tool_call_id = str(item.get("id") or item.get("call_id") or uuid.uuid4())
        if item_type == "command_execution":
            command = str(item.get("command") or "").strip()
            arguments = {"command": command} if command else None
            initial_data: dict[str, Any] = {}
            if item.get("aggregated_output"):
                initial_data["output"] = str(item.get("aggregated_output"))[:4000]
            if item.get("exit_code") is not None:
                initial_data["exit_code"] = item.get("exit_code")
            event = {
                "kind": "tool_start" if phase == "start" else "tool_result",
                "tool_name": "shell",
                "tool_action": command or "command_execution",
                "tool_call_id": tool_call_id,
                "arguments": arguments,
                "initial_data": initial_data or None,
                "message": _summarize_sentinel_command(command) if command else "Running command...",
                "status": "tool_call"
                if phase == "start"
                else ("tool_complete" if int(item.get("exit_code") or 0) == 0 else "error"),
            }
        else:
            event = {
                "kind": "tool_start" if phase == "start" else "tool_result",
                "tool_name": item_type,
                "tool_action": str(item.get("title") or item_type),
                "tool_call_id": tool_call_id,
                "initial_data": {"item": item},
                "message": f"Using {item_type}",
                "status": "tool_call" if phase == "start" else "tool_complete",
            }
        self._handle_exec_event(event, message_id=message_id)

    def _handle_claude_tool_use(self, block: dict[str, Any], *, message_id: str) -> None:
        tool_name = str(block.get("name") or "tool").strip()
        tool_input = block.get("input") if isinstance(block.get("input"), dict) else {}
        tool_call_id = str(block.get("id") or uuid.uuid4())
        event = {
            "kind": "tool_start",
            "tool_name": tool_name,
            "tool_action": str(tool_input.get("command") or tool_name),
            "tool_call_id": tool_call_id,
            "arguments": tool_input,
            "message": _sentinel_tool_summary(tool_name, tool_input),
            "status": "tool_call",
        }
        self._handle_exec_event(event, message_id=message_id)

    def _handle_prompt(self, prompt: str, *, message_id: str, data: dict[str, Any] | None = None) -> str:
        runtime_type = str(self.entry.get("runtime_type") or "echo").lower()
        if runtime_type == "echo":
            return _echo_handler(prompt, self.entry)
        if runtime_type in {"inbox", "passive", "monitor"}:
            return ""
        if _is_sentinel_cli_runtime(runtime_type):
            return self._handle_sentinel_cli_prompt(prompt, message_id=message_id, data=data)
        if runtime_type in {"exec", "command"}:
            command = str(self.entry.get("exec_command") or "").strip()
            if not command:
                raise ValueError("exec runtime requires exec_command")
            return _run_exec_handler(
                command,
                prompt,
                self.entry,
                message_id=message_id or None,
                space_id=self.space_id,
                timeout_seconds=runtime_timeout_seconds(self.entry),
                on_event=lambda event: self._handle_exec_event(event, message_id=message_id),
            )
        raise ValueError(f"Unsupported runtime_type: {runtime_type}")

    def _gateway_message_metadata(self, parent_message_id: str | None = None) -> dict[str, Any]:
        registry = load_gateway_registry()
        gateway = registry.get("gateway", {})
        metadata: dict[str, Any] = {
            "control_plane": "gateway",
            "gateway": {
                "managed": True,
                "gateway_id": gateway.get("gateway_id"),
                "agent_name": self.name,
                "agent_id": self.agent_id,
                "runtime_type": self.entry.get("runtime_type"),
                "transport": self.entry.get("transport", "gateway"),
                "credential_source": self.entry.get("credential_source", "gateway"),
            },
        }
        if parent_message_id:
            metadata["gateway"]["parent_message_id"] = parent_message_id
        return metadata

    def _worker_loop(self) -> None:
        while not self.stop_event.is_set():
            try:
                data = self._queue.get(timeout=0.5)
            except queue.Empty:
                continue
            if data is None:
                break

            message_id = str(data.get("id") or "")
            prompt = _strip_mention(str(data.get("content") or ""), self.name)
            self._update_state(backlog_depth=self._queue.qsize())
            if not prompt:
                self._queue.task_done()
                continue

            if message_id:
                runtime_type = str(self.entry.get("runtime_type") or "echo").lower()
                start_status = "processing"
                start_activity = "Preparing response"
                if runtime_type == "echo":
                    start_activity = "Composing echo reply"
                elif runtime_type in {"exec", "command"}:
                    start_activity = "Preparing runtime"
                elif _is_sentinel_cli_runtime(runtime_type):
                    start_activity = "Preparing sentinel runtime"
                if runtime_type in {"echo", "exec", "command"} or _is_sentinel_cli_runtime(runtime_type):
                    self._update_state(current_status=start_status, current_activity=start_activity[:240])
                    self._publish_processing_status(message_id, start_status, activity=start_activity)
                    record_gateway_activity(
                        "runtime_status",
                        entry=self.entry,
                        message_id=message_id,
                        status=start_status,
                        activity_message=start_activity,
                    )
            try:
                response_text = self._handle_prompt(prompt, message_id=message_id, data=data)
                runtime_declined = self._consume_no_reply_seen(message_id)
                if response_text and self._send_client and not runtime_declined:
                    result = self._send_client.send_message(
                        self.space_id,
                        response_text,
                        agent_id=self.agent_id,
                        parent_id=message_id or None,
                        metadata=self._gateway_message_metadata(message_id or None),
                    )
                    message = result.get("message", result) if isinstance(result, dict) else {}
                    _remember_reply_anchor(self._reply_anchor_ids, message.get("id"))
                    reply_id = message.get("id")
                    preview = response_text.strip().replace("\n", " ")
                    if len(preview) > 120:
                        preview = preview[:117] + "..."
                    self._update_state(last_reply_message_id=reply_id, last_reply_preview=preview or None)
                    record_gateway_activity(
                        "reply_sent",
                        entry=self.entry,
                        message_id=message_id or None,
                        reply_message_id=reply_id,
                        reply_preview=preview or None,
                    )
                runtime_type = str(self.entry.get("runtime_type") or "echo").lower()
                bridge_already_closed = (
                    runtime_type in {"exec", "command"} or _is_sentinel_cli_runtime(runtime_type)
                ) and self._consume_completed_seen(message_id)
                if message_id and not bridge_already_closed and not runtime_declined:
                    self._publish_processing_status(message_id, "completed")
                self._bump("processed_count")
                self._update_state(
                    current_status=None,
                    current_activity=None,
                    current_tool=None,
                    current_tool_call_id=None,
                    last_error=None,
                    last_work_completed_at=_now_iso(),
                    backlog_depth=self._queue.qsize(),
                )
            except GatewayRuntimeTimeoutError as exc:
                activity = f"Timed out after {exc.timeout_seconds}s"
                self._update_state(
                    current_status="error",
                    current_activity=activity,
                    current_tool=None,
                    current_tool_call_id=None,
                    last_error=str(exc)[:400],
                    backlog_depth=self._queue.qsize(),
                )
                if message_id:
                    self._publish_processing_status(
                        message_id,
                        "error",
                        activity=activity,
                        reason="runtime_timeout",
                        error_message=str(exc)[:400],
                        detail={"timeout_seconds": exc.timeout_seconds, "runtime_type": exc.runtime_type},
                    )
                record_gateway_activity(
                    "runtime_timeout",
                    entry=self.entry,
                    message_id=message_id or None,
                    timeout_seconds=exc.timeout_seconds,
                    runtime_type=exc.runtime_type,
                )
                self._log(f"worker timeout: {exc}")
            except Exception as exc:
                self._update_state(
                    current_status="error",
                    current_activity=None,
                    current_tool=None,
                    current_tool_call_id=None,
                    last_error=str(exc)[:400],
                    backlog_depth=self._queue.qsize(),
                )
                if message_id:
                    self._publish_processing_status(
                        message_id,
                        "error",
                        error_message=str(exc)[:400],
                    )
                record_gateway_activity(
                    "runtime_error",
                    entry=self.entry,
                    message_id=message_id or None,
                    error=str(exc)[:400],
                )
                self._log(f"worker error: {exc}")
            finally:
                self._queue.task_done()

    def _listener_loop(self) -> None:
        backoff = 1.0
        while not self.stop_event.is_set():
            try:
                self._stream_client = self._new_client()
                self._send_client = self._new_client()
                timeout = httpx.Timeout(
                    connect=10.0,
                    read=SSE_IDLE_TIMEOUT_SECONDS,
                    write=10.0,
                    pool=10.0,
                )
                reconnected = backoff > 1.0
                with self._stream_client.connect_sse(space_id=self.space_id, timeout=timeout) as response:
                    self._stream_response = response
                    if response.status_code != 200:
                        raise ConnectionError(f"SSE failed: {response.status_code}")
                    self._stale_signaled = False
                    self._update_state(
                        effective_state="running",
                        current_status=None,
                        last_error=None,
                        last_connected_at=_now_iso(),
                        last_listener_error_at=None,
                        last_seen_at=_now_iso(),
                        reconnect_backoff_seconds=0,
                    )
                    record_gateway_activity("listener_connected", entry=self.entry, reconnected=reconnected)
                    backoff = 1.0
                    import time as _time

                    _last_heartbeat = _time.monotonic() - RUNTIME_HEARTBEAT_INTERVAL_SECONDS
                    for event_type, data in _iter_sse(response):
                        if self.stop_event.is_set():
                            break
                        _now = _time.monotonic()
                        if _now - _last_heartbeat >= RUNTIME_HEARTBEAT_INTERVAL_SECONDS:
                            try:
                                self._send_client.send_heartbeat(status="connected")
                            except Exception:  # noqa: BLE001
                                pass
                            _last_heartbeat = _now
                        if event_type in {"bootstrap", "heartbeat", "ping", "identity_bootstrap", "connected"}:
                            self._update_state(last_seen_at=_now_iso())
                            continue
                        if event_type == "agent.placement.changed" and isinstance(data, dict):
                            self._update_state(last_seen_at=_now_iso())
                            self._handle_placement_event(data)
                            continue
                        if event_type not in {"message", "mention"} or not isinstance(data, dict):
                            continue
                        message_id = str(data.get("id") or "")
                        if not message_id or message_id in self._seen_ids:
                            continue
                        if _is_self_authored(data, self.name, self.agent_id):
                            _remember_reply_anchor(self._reply_anchor_ids, message_id)
                            self._seen_ids.add(message_id)
                            continue
                        if not _should_respond(
                            data,
                            self.name,
                            self.agent_id,
                            reply_anchor_ids=self._reply_anchor_ids,
                        ):
                            continue

                        self._seen_ids.add(message_id)
                        if len(self._seen_ids) > SEEN_IDS_MAX:
                            self._seen_ids = set(list(self._seen_ids)[-SEEN_IDS_MAX // 2 :])
                        _remember_reply_anchor(self._reply_anchor_ids, message_id)
                        self._update_state(
                            last_seen_at=_now_iso(),
                            last_work_received_at=_now_iso(),
                            last_received_message_id=message_id,
                        )
                        record_gateway_activity("message_received", entry=self.entry, message_id=message_id)
                        runtime_type = str(self.entry.get("runtime_type") or "").lower()
                        try:
                            if _is_passive_runtime(runtime_type):
                                pending_items = append_agent_pending_message(self.name, data)
                                backlog_depth = len(pending_items)
                            else:
                                self._queue.put_nowait(data)
                                backlog_depth = self._queue.qsize()
                            pickup_status = "queued" if _is_passive_runtime(runtime_type) else "started"
                            accepted_activity = _gateway_pickup_activity(runtime_type, backlog_depth)
                            self._update_state(
                                backlog_depth=backlog_depth,
                                current_status=pickup_status,
                                current_activity=accepted_activity[:240],
                            )
                            self._publish_processing_status(
                                message_id,
                                pickup_status,
                                activity=accepted_activity,
                                detail={
                                    "backlog_depth": backlog_depth,
                                    "pickup_state": "queued" if _is_passive_runtime(runtime_type) else "claimed",
                                },
                            )
                            if _is_passive_runtime(self.entry.get("runtime_type")):
                                record_gateway_activity(
                                    "message_queued",
                                    entry=self.entry,
                                    message_id=message_id,
                                    backlog_depth=backlog_depth,
                                )
                            else:
                                record_gateway_activity(
                                    "message_claimed",
                                    entry=self.entry,
                                    message_id=message_id,
                                    backlog_depth=backlog_depth,
                                )
                        except queue.Full:
                            self._bump("dropped_count")
                            self._update_state(last_error="queue full", backlog_depth=self._queue.qsize())
                            self._publish_processing_status(
                                message_id,
                                "error",
                                reason="queue_full",
                                error_message="Gateway queue full",
                            )
                            record_gateway_activity(
                                "message_dropped",
                                entry=self.entry,
                                message_id=message_id,
                                error="queue full",
                            )
                            self._log("queue full; dropped message")
                        except Exception as exc:
                            self._update_state(last_error=str(exc)[:400])
                            self._publish_processing_status(
                                message_id,
                                "error",
                                error_message=str(exc)[:400],
                            )
                            record_gateway_activity(
                                "message_queue_error",
                                entry=self.entry,
                                message_id=message_id,
                                error=str(exc)[:400],
                            )
                            self._log(f"queue error: {exc}")
            except Exception as exc:
                if self.stop_event.is_set():
                    break
                error_text = str(exc)[:400]
                event_name = "listener_error"
                if isinstance(exc, httpx.ReadTimeout):
                    error_text = f"idle timeout after {int(SSE_IDLE_TIMEOUT_SECONDS)}s without SSE heartbeat"
                    event_name = "listener_timeout"
                self._update_state(
                    effective_state="reconnecting",
                    last_error=error_text,
                    last_listener_error_at=_now_iso(),
                    reconnect_backoff_seconds=int(backoff),
                )
                if not self._stale_signaled:
                    if self._send_client is not None:
                        try:
                            self._send_client.send_heartbeat(status="stale")
                        except Exception:  # noqa: BLE001
                            pass
                    else:
                        self._send_heartbeat_best_effort("stale")
                    self._stale_signaled = True
                record_gateway_activity(
                    event_name, entry=self.entry, error=error_text, reconnect_in_seconds=int(backoff)
                )
                self._log(f"listener error: {error_text}")
                time.sleep(backoff)
                backoff = min(backoff * 2, 30.0)
            finally:
                self._stream_response = None
                if self._stream_client is not None:
                    try:
                        self._stream_client.close()
                    except Exception:
                        pass
                    self._stream_client = None
        self._update_state(
            effective_state="stopped",
            backlog_depth=self._queue.qsize(),
            current_status=None,
            current_activity=None,
            current_tool=None,
            current_tool_call_id=None,
        )


# Deferred cross-module imports (bottom-of-file to avoid import cycles;
# bound into module globals after defs, resolved at call time).
from .gateway_entries import (  # noqa: E402
    GatewayRuntimeTimeoutError,
    _apply_placement_event,
    _hash_tool_arguments,
    _parse_gateway_exec_event,
    _post_placement_ack,
    runtime_timeout_seconds,
    sanitize_exec_env,
)
from .gateway_health import _age_seconds, _now_iso  # noqa: E402
from .gateway_hermes import (  # noqa: E402
    _build_hermes_plugin_cmd,
    _build_hermes_plugin_env,
    _build_sentinel_claude_cmd,
    _build_sentinel_inference_sdk_cmd,
    _build_sentinel_inference_sdk_env,
    _compose_agent_system_prompt,
    _hermes_bin,
    _hermes_plugin_home,
    _hermes_plugin_workdir,
    _resolve_inference_client,
    _scaffold_hermes_plugin_home,
    _sentinel_inference_sdk_python,
    _sentinel_inference_sdk_script,
    _sentinel_inference_sdk_workdir,
    _sentinel_runtime_name,
    _sentinel_session_key,
    _sentinel_tool_summary,
    _summarize_sentinel_command,
)
from .gateway_storage import (  # noqa: E402
    _daemon_request_logger,
    append_agent_pending_message,
    find_agent_entry,
    load_agent_pending_messages,
    load_gateway_managed_agent_token,
    load_gateway_registry,
    record_gateway_activity,
    resolve_agent_token_file,
)
