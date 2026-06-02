# NEW: not yet vendored from ax-agents. Pending upstream PR before the next
# vendor sync. See ax_cli/runtimes/hermes/README.md for vendoring guidance.
"""Palantir AIP runtime — wraps Foundry's OpenAI-compatible LLM proxy.

Phase 3: multi-turn agent loop with tool calls. The runtime streams a
chat.completions call against a Palantir Foundry stack via the official
`openai` Python SDK pointed at the AIP OpenAI-compatible proxy endpoint,
accumulates assistant text and tool_calls across chunks, executes
requested tools through the shared `tools` module, and loops until the
model emits a final text-only reply (or MAX_TURNS is hit).

Foundry exposes proxy endpoints that accept the OpenAI Chat Completions
wire format so open-source SDKs and tooling work unchanged while requests
still flow through Foundry's rate limiting, data governance (ZDR /
georestriction), and usage tracking. The proxy path is:

    /api/v2/llm/proxy/openai/v1/chat/completions

so the operator-supplied base_url ends at `/api/v2/llm/proxy/openai/v1`.
Because the wire format is identical to openai_sdk's chat.completions
path, no schema adapter or history-shape conversion is needed (unlike
gemini_sdk). Confirmed against Palantir's "OpenAI Chat Completions
(Proxy)" API reference (2026-06).

Auth:
  PALANTIR_AIP_TOKEN    — Foundry bearer token (OAuth/third-party app or
                          user token) authorized for the AIP LLM proxy
  PALANTIR_AIP_BASE_URL — the Foundry stack's proxy base URL, ending in
                          `/api/v2/llm/proxy/openai/v1` (no public default;
                          each Foundry stack is a private hostname)

Both are required. Like leapfrog_sdk (and unlike openai_sdk, which has a
hardcoded ChatGPT backend URL), Foundry stacks are per-customer, so the
operator MUST supply the endpoint URL alongside the token.

Model:
  There is no universal default. The `model` field must be a Foundry
  language-model resource identifier from the stack's Model Catalog, e.g.
  `ri.language-model-service..language-model.gpt-5-2`. Operators set it via
  the `model` field on the managed-agent registry entry. The runtime fails
  closed with an actionable message when no model is supplied rather than
  guessing a model name that won't exist on the operator's stack.
"""

from __future__ import annotations

import json
import logging
import os
import time

from . import BaseRuntime, RuntimeResult, StreamCallback, register

log = logging.getLogger("runtime.palantir_sdk")

# No DEFAULT_MODEL: Foundry models are resource identifiers scoped to a
# stack's Model Catalog, so there is no safe cross-stack default. The
# operator must supply one (see module docstring).
MAX_TURNS = 25
TOOL_OUTPUT_CAP = 10_000  # bytes of tool output fed back to the model per call

PALANTIR_TOKEN_ENV = "PALANTIR_AIP_TOKEN"
PALANTIR_BASE_URL_ENV = "PALANTIR_AIP_BASE_URL"


def _resolve_auth() -> tuple[tuple[str, str] | None, str]:
    """Read PALANTIR_AIP_TOKEN + PALANTIR_AIP_BASE_URL from the environment.

    Returns ((token, base_url), "") on success or (None, error_message)
    when either var is missing. The error message names the missing var
    so the operator can act on it.
    """
    token = os.environ.get(PALANTIR_TOKEN_ENV, "").strip()
    base_url = os.environ.get(PALANTIR_BASE_URL_ENV, "").strip()
    if not token:
        return None, f"{PALANTIR_TOKEN_ENV} not set"
    if not base_url:
        return (
            None,
            f"{PALANTIR_BASE_URL_ENV} not set "
            "(Foundry stacks are private; operator must provide the proxy base URL "
            "ending in /api/v2/llm/proxy/openai/v1)",
        )
    return (token, base_url), ""


def _to_chat_completion_tool(rd_tool: dict) -> dict:
    """Convert a Responses-API tool definition (flat `name`) into the
    chat-completions `{"type": "function", "function": {...}}` shape that
    Foundry's OpenAI-compat proxy accepts.

    Like leapfrog_sdk and unlike gemini_sdk, no schema field stripping is
    needed: the proxy forwards JSON-Schema through to the underlying model,
    so `default`, `examples`, `$ref`, and friends are all fine.
    """
    return {
        "type": "function",
        "function": {
            "name": rd_tool["name"],
            "description": rd_tool.get("description", ""),
            "parameters": rd_tool.get("parameters", {}),
        },
    }


def _tool_display(name: str, args: dict) -> str:
    """Human-readable one-liner for tool activity log."""
    if name in ("read_file", "write_file", "edit_file"):
        p = args.get("path", "")
        verb = {"read_file": "Read", "write_file": "Write", "edit_file": "Edit"}[name]
        tail = p.rsplit("/", 1)[-1] if "/" in p else p
        return f"{verb} {tail}"
    if name == "bash":
        cmd = str(args.get("command", ""))[:60]
        return f"Run: {cmd}"
    if name == "grep":
        return f"Search: {args.get('pattern', '')}"
    if name == "glob_files":
        return f"Find: {args.get('pattern', '')}"
    return name


@register("palantir_sdk")
class PalantirSDKRuntime(BaseRuntime):
    """Runs agent turns via the OpenAI Python SDK pointed at Foundry's AIP proxy.

    Phase 3: multi-turn loop with tool calling. Buffers text deltas
    locally per turn (only emits via StreamCallback.on_text_complete once
    the turn is confirmed text-only — prevents pre-tool chatter from
    leaking as visible chat content and suppressing the sentinel's
    tool-progress UI). Accumulates tool_call fragments across the
    streaming chunks, executes tools through the shared `tools` module,
    and loops until the model produces a final text-only reply or
    MAX_TURNS is reached.

    Mirrors the deadline-checked + clamped-tool-timeout pattern from
    leapfrog_sdk.py so a single tool cannot block the sentinel past its
    --timeout budget.
    """

    def execute(
        self,
        message: str,
        *,
        workdir: str,
        model: str | None = None,
        system_prompt: str | None = None,
        session_id: str | None = None,
        stream_cb: StreamCallback | None = None,
        timeout: int = 300,
        extra_args: dict | None = None,
    ) -> RuntimeResult:
        auth, err = _resolve_auth()
        if auth is None:
            log.error(f"palantir_sdk: auth resolution failed: {err}")
            return RuntimeResult(
                text=f"Agent could not authenticate with Palantir AIP ({err}).",
                exit_reason="crashed",
                elapsed_seconds=0,
            )
        token, base_url = auth

        # Fail closed on a missing model: Foundry models are stack-scoped
        # resource identifiers, so there is no default we can substitute.
        # Surface an actionable message instead of letting the proxy reject
        # an empty/guessed model name with an opaque 4xx.
        model = (model or "").strip()
        if not model:
            log.error("palantir_sdk: no model supplied")
            return RuntimeResult(
                text=(
                    "Palantir AIP requires an explicit model resource identifier "
                    "(e.g. ri.language-model-service..language-model.gpt-5-2). Set it "
                    "via the managed-agent `model` field; find the identifier in the "
                    "Foundry Model Catalog."
                ),
                exit_reason="crashed",
                elapsed_seconds=0,
            )

        try:
            from openai import (
                OpenAI,
                APIStatusError,
                APITimeoutError,
                AuthenticationError,
                InternalServerError,
                PermissionDeniedError,
                RateLimitError,
            )
        except ImportError as e:
            # pyproject.toml does not declare `openai` as a hard dep — the
            # sibling openai_sdk.py is also lazy-imported. Packaged axctl
            # installs may not have it. Surface a clean RuntimeResult so the
            # sentinel can render an actionable message instead of crashing
            # on a bare ModuleNotFoundError.
            log.error(f"palantir_sdk: openai Python SDK is not installed ({e})")
            return RuntimeResult(
                text=(
                    "Agent could not start because the `openai` Python package "
                    "is not installed in this runtime environment. "
                    "Install it with `pip install openai` and retry."
                ),
                exit_reason="crashed",
                elapsed_seconds=0,
            )

        # Absolute import matches the sibling runtimes. The Hermes sentinel
        # prepends ax_cli/runtimes/hermes to sys.path and loads this module
        # as `runtimes.palantir_sdk`, so a relative `from ..tools` would
        # escape past the top-level package and raise ImportError at runtime.
        # tests/test_palantir_sdk_runtime.py inserts the same hermes
        # directory into sys.path so the absolute form resolves there too.
        from tools import TOOL_DEFINITIONS, execute_tool

        cb = stream_cb or StreamCallback()
        instructions = system_prompt or "You are a helpful coding assistant."

        tools = [_to_chat_completion_tool(t) for t in TOOL_DEFINITIONS]

        start_time = time.time()
        deadline = start_time + timeout

        # Build the messages list. The proxy uses the OpenAI chat-completions
        # message shape natively, so history rows pass straight through.
        history: list[dict] = list((extra_args or {}).get("history", []))
        history.append({"role": "user", "content": message})

        final_text = ""
        tool_count = 0
        files_written: list[str] = []

        client = OpenAI(api_key=token, base_url=base_url)

        for turn in range(MAX_TURNS):
            now = time.time()
            remaining = deadline - now
            if remaining <= 0:
                log.warning(
                    f"palantir_sdk: timeout exceeded at turn {turn + 1} "
                    f"(budget={timeout}s, elapsed {int(now - start_time)}s)"
                )
                return RuntimeResult(
                    text=(final_text or "Agent timed out before producing a final answer."),
                    history=history,
                    session_id=None,
                    tool_count=tool_count,
                    files_written=files_written,
                    exit_reason="timeout",
                    elapsed_seconds=int(now - start_time),
                )

            log.info(f"palantir_sdk: turn {turn + 1}, {len(history)} messages")

            # Chat-completions expects a `system` message at the front, not a
            # separate `system_instruction` kwarg (that's a Gemini-ism). Prepend
            # one each turn so the model's behavior is consistent across turns.
            messages = [{"role": "system", "content": instructions}, *history]

            try:
                stream = client.chat.completions.create(
                    model=model,
                    messages=messages,
                    tools=tools,
                    stream=True,
                    timeout=remaining,
                )
            except RateLimitError as e:
                # 429. Throttle, surface the status code so the operator can
                # tell rate-limit from auth-fail at a glance.
                log.warning(f"palantir_sdk: rate limited (HTTP {e.status_code}): {e.message}")
                return RuntimeResult(
                    text=(
                        final_text
                        or f"Foundry AIP proxy rate-limited (HTTP {e.status_code}). Retry after a short delay."
                    ),
                    history=history,
                    tool_count=tool_count,
                    files_written=files_written,
                    exit_reason="rate_limited",
                    elapsed_seconds=int(time.time() - start_time),
                )
            except APITimeoutError as e:
                # Connection or read timeout from the openai SDK (httpx-backed).
                # Distinct from a sentinel-budget timeout, but maps to the same
                # exit_reason since the user-visible cause is identical.
                log.error(f"palantir_sdk: API timeout: {e}")
                return RuntimeResult(
                    text=(final_text or "Agent timed out while waiting for the model."),
                    history=history,
                    tool_count=tool_count,
                    files_written=files_written,
                    exit_reason="timeout",
                    elapsed_seconds=int(time.time() - start_time),
                )
            except (AuthenticationError, PermissionDeniedError) as e:
                # 401 / 403. Operator-actionable — the user must see this in
                # the chat reply so they can rotate PALANTIR_AIP_TOKEN or fix
                # the third-party app's AIP permissions. Never silently swallow
                # auth failures.
                log.error(f"palantir_sdk: auth failed (HTTP {e.status_code}): {e.message}")
                return RuntimeResult(
                    text=(
                        f"Palantir AIP authentication failed (HTTP {e.status_code}). "
                        "Check PALANTIR_AIP_TOKEN and the app's AIP LLM proxy permissions."
                    ),
                    history=history,
                    tool_count=tool_count,
                    files_written=files_written,
                    exit_reason="auth_error",
                    elapsed_seconds=int(time.time() - start_time),
                )
            except InternalServerError as e:
                # 5xx from the Foundry stack. Retry is plausible; signal that
                # to the operator.
                log.error(f"palantir_sdk: server error (HTTP {e.status_code}): {e.message}")
                return RuntimeResult(
                    text=(
                        final_text
                        or f"Foundry AIP proxy returned HTTP {e.status_code}. Retry may succeed."
                    ),
                    history=history,
                    tool_count=tool_count,
                    files_written=files_written,
                    exit_reason="server_error",
                    elapsed_seconds=int(time.time() - start_time),
                )
            except APIStatusError as e:
                # Any other 4xx not matched above (e.g. 400 BadRequest,
                # 422 UnprocessableEntity, 404 NotFound — a 404 here usually
                # means the model resource identifier is wrong for this stack).
                # Surface the status and the message so the operator knows what
                # to fix.
                log.error(f"palantir_sdk: API error (HTTP {e.status_code}): {e.message}")
                return RuntimeResult(
                    text=(
                        final_text
                        or f"Palantir AIP API error (HTTP {e.status_code}): {e.message}"
                    ),
                    history=history,
                    tool_count=tool_count,
                    files_written=files_written,
                    exit_reason="api_error",
                    elapsed_seconds=int(time.time() - start_time),
                )
            except Exception as e:
                # Catch-all for anything outside the openai SDK's typed
                # exception hierarchy (network adapter bugs, connection
                # refused before an APIConnectionError, etc.). Logged with
                # full repr so the underlying type is visible in ops triage.
                log.error(f"palantir_sdk: unexpected error opening stream: {e!r}")
                return RuntimeResult(
                    text=final_text or "Agent encountered an unexpected error and could not complete the task.",
                    history=history,
                    tool_count=tool_count,
                    files_written=files_written,
                    exit_reason="crashed",
                    elapsed_seconds=int(time.time() - start_time),
                )

            # Accumulate text and tool_call fragments across the stream.
            #
            # Why we buffer text instead of streaming via on_text_delta: if the
            # model says "Let me check that..." and then makes function calls,
            # that pre-tool chatter would leak as visible chat content and
            # suppress the sentinel's tool-progress UI. We only emit via
            # cb.on_text_complete once the turn is confirmed text-only.
            # Mirrors the buffering pattern in leapfrog_sdk.py and openai_sdk.py.
            #
            # OpenAI tool_call streaming: each chunk's delta.tool_calls is a
            # sparse list keyed by `index` — fragments arrive across many
            # chunks. We accumulate by index into a dict, then materialize at
            # turn end.
            turn_text = ""
            tool_call_fragments: dict[int, dict] = {}

            try:
                for chunk in stream:
                    choices = getattr(chunk, "choices", None) or []
                    for choice in choices:
                        delta = getattr(choice, "delta", None)
                        if delta is None:
                            continue

                        # Text content fragment
                        content = getattr(delta, "content", None)
                        if content:
                            turn_text += content

                        # Tool call fragments (sparse, indexed)
                        deltas = getattr(delta, "tool_calls", None) or []
                        for tc_delta in deltas:
                            idx = getattr(tc_delta, "index", 0)
                            slot = tool_call_fragments.setdefault(
                                idx,
                                {"id": "", "name": "", "arguments": ""},
                            )
                            tc_id = getattr(tc_delta, "id", None)
                            if tc_id:
                                slot["id"] = tc_id
                            fn = getattr(tc_delta, "function", None)
                            if fn is not None:
                                fn_name = getattr(fn, "name", None)
                                if fn_name:
                                    slot["name"] = fn_name
                                fn_args = getattr(fn, "arguments", None)
                                if fn_args:
                                    slot["arguments"] += fn_args
            except Exception as e:
                log.error(f"palantir_sdk: stream error after {len(turn_text)} chars: {e}")
                partial = turn_text.strip()
                if partial:
                    history.append({"role": "assistant", "content": partial})
                return RuntimeResult(
                    text=partial or "Agent encountered a stream error mid-response.",
                    history=history,
                    tool_count=tool_count,
                    files_written=files_written,
                    exit_reason="crashed",
                    elapsed_seconds=int(time.time() - start_time),
                )

            # Materialize tool calls in arrival order so call_id/name/args
            # alignment matches what the model emitted. Skip any slot that
            # never got a name (defensive — shouldn't happen on a healthy stream).
            tool_calls = [
                {
                    "id": slot["id"] or f"call_{turn}_{idx}",
                    "name": slot["name"],
                    "arguments": slot["arguments"],
                }
                for idx, slot in sorted(tool_call_fragments.items())
                if slot["name"]
            ]

            if tool_calls:
                # Append the assistant turn carrying the tool calls in
                # chat-completions shape (already the native shape — no
                # back-conversion needed, unlike Gemini).
                assistant_tool_calls = [
                    {
                        "id": tc["id"],
                        "type": "function",
                        "function": {
                            "name": tc["name"],
                            "arguments": tc["arguments"] or "{}",
                        },
                    }
                    for tc in tool_calls
                ]
                history.append(
                    {
                        "role": "assistant",
                        "content": turn_text or None,
                        "tool_calls": assistant_tool_calls,
                    }
                )

                for tc in tool_calls:
                    # Re-check the deadline before each tool. A long-running
                    # tool can otherwise block the listener well past the
                    # operator's --timeout.
                    now_tool = time.time()
                    remaining_for_tool = deadline - now_tool
                    if remaining_for_tool <= 0:
                        log.warning(
                            f"palantir_sdk: timeout exceeded before tool "
                            f"{tc['name']} (elapsed {int(now_tool - start_time)}s)"
                        )
                        return RuntimeResult(
                            text=(final_text or "Agent timed out before completing tool calls."),
                            history=history,
                            session_id=None,
                            tool_count=tool_count,
                            files_written=files_written,
                            exit_reason="timeout",
                            elapsed_seconds=int(now_tool - start_time),
                        )

                    tool_count += 1
                    name = tc["name"]
                    try:
                        args = json.loads(tc["arguments"]) if tc["arguments"] else {}
                    except json.JSONDecodeError:
                        args = {}

                    # Clamp any model-supplied "timeout" arg to the remaining
                    # wall-clock budget. Tools like `bash` honor args["timeout"]
                    # directly, so without this a model could request a 600s
                    # bash inside a 30s sentinel budget. Tools without a
                    # "timeout" arg are unaffected.
                    if "timeout" in args:
                        try:
                            args["timeout"] = min(
                                int(args["timeout"]),
                                max(1, int(remaining_for_tool)),
                            )
                        except (TypeError, ValueError):
                            args["timeout"] = max(1, int(remaining_for_tool))

                    summary = _tool_display(name, args)
                    log.info(f"palantir_sdk: tool {name}({json.dumps(args, default=str)[:80]})")
                    cb.on_tool_start(name, summary)
                    result = execute_tool(name, args, workdir)

                    if name == "write_file" and not result.is_error:
                        files_written.append(args.get("path", ""))

                    short = result.output[:200] if result.output else ""
                    cb.on_tool_end(name, short)

                    # Cap tool output at TOOL_OUTPUT_CAP bytes to bound context
                    # growth, and surface a truncation marker when we hit the
                    # cap so the model can tell content was clipped (otherwise
                    # it may reason as if it has the full output, e.g. assume a
                    # large file was fully read). Mirrors leapfrog_sdk.py.
                    full_output = result.output or ""
                    if len(full_output) > TOOL_OUTPUT_CAP:
                        tool_content = full_output[:TOOL_OUTPUT_CAP] + "\n[output truncated]"
                    else:
                        tool_content = full_output
                    history.append(
                        {
                            "role": "tool",
                            "tool_call_id": tc["id"],
                            "content": tool_content,
                        }
                    )

                cb.on_status("thinking")
                continue  # Next turn: model sees tool results.

            # No tool calls — text-only response. Treat as final.
            visible = turn_text.strip()
            if visible:
                final_text = visible
                cb.on_text_complete(final_text)
                history.append({"role": "assistant", "content": visible})
            break
        else:
            # The for-loop completed without break, meaning every turn produced
            # tool calls and the model never finalized. Surface this as
            # iteration_limit so the sentinel renders a bounded-loop notice
            # rather than a misleading "Completed with no text output".
            elapsed = int(time.time() - start_time)
            log.warning(
                f"palantir_sdk: hit MAX_TURNS={MAX_TURNS} without final answer "
                f"(elapsed {elapsed}s, {tool_count} tools)"
            )
            return RuntimeResult(
                text=(final_text or "Agent hit the maximum turn limit without producing a final answer."),
                history=history,
                session_id=None,
                tool_count=tool_count,
                files_written=files_written,
                exit_reason="iteration_limit",
                elapsed_seconds=elapsed,
            )

        elapsed = int(time.time() - start_time)
        log.info(f"palantir_sdk: done in {elapsed}s, {tool_count} tools, {len(final_text)} chars")
        return RuntimeResult(
            text=final_text,
            history=history,
            session_id=None,
            tool_count=tool_count,
            files_written=files_written,
            exit_reason="done",
            elapsed_seconds=elapsed,
        )
