# NEW: not yet vendored from ax-agents. Pending upstream PR before the next
# vendor sync. See ax_cli/runtimes/hermes/README.md for vendoring guidance.
"""Together AI SDK runtime, wraps Together's OpenAI-compatible Chat Completions endpoint.

Phase 3: multi-turn agent loop with tool calls. The runtime streams a
chat.completions call against Together AI's public endpoint via the
official `openai` Python SDK pointed at `https://api.together.xyz/v1`,
accumulates assistant text and tool_calls across chunks, executes
requested tools through the shared `tools` module, and loops until the
model emits a final text-only reply (or MAX_TURNS is hit).

Together AI exposes the OpenAI Chat Completions API specification, so
the wire format is identical to openai_sdk's chat.completions path. No
schema adapter or history shape conversion is needed (unlike Gemini),
matching the leapfrog_sdk + xai_sdk pattern.

Together AI hosts open-source models (Llama, Qwen, DeepSeek, Mistral
variants, etc.) with a public single-endpoint API, so unlike leapfrog_sdk
the base_url is hardcoded rather than operator-supplied. Only the API
key is read from the environment.

Auth:
  TOGETHER_API_KEY - bearer token issued at api.together.xyz

Models: operator-configured per agent registry entry. Default is
`meta-llama/Llama-3.3-70B-Instruct-Turbo` which is Together's canonical
high-quality Llama tier. Operators override via the `model` field in
the managed-agent registry entry. The full Together model catalog at
https://docs.together.ai/docs/serverless-models is operator-selectable.
"""

from __future__ import annotations

import json
import logging
import os
import time

from . import BaseRuntime, RuntimeResult, StreamCallback, register

log = logging.getLogger("runtime.together_sdk")

DEFAULT_MODEL = "meta-llama/Llama-3.3-70B-Instruct-Turbo"
MAX_TURNS = 25
TOOL_OUTPUT_CAP = 10_000  # bytes of tool output fed back to the model per call

TOGETHER_API_KEY_ENV = "TOGETHER_API_KEY"
TOGETHER_BASE_URL = "https://api.together.xyz/v1"


def _to_chat_completion_tool(rd_tool: dict) -> dict:
    """Convert a Responses-API tool definition (flat `name`) into the
    chat-completions `{"type": "function", "function": {...}}` shape that
    Together AI's OpenAI-compatible endpoint accepts.

    Same converter as the openai_sdk, groq_sdk, mistral_sdk, xai_sdk, and
    leapfrog_sdk siblings. Together passes JSON-Schema through to the
    underlying model unchanged, so `default`, `examples`, `$ref`, etc.
    are all fine.
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


@register("together_sdk")
class TogetherSDKRuntime(BaseRuntime):
    """Runs agent turns via the OpenAI Python SDK pointed at Together AI.

    Phase 3: multi-turn loop with tool calling. Buffers text deltas
    locally per turn (only emits via StreamCallback.on_text_complete once
    the turn is confirmed text-only, prevents pre-tool chatter from
    leaking as visible chat content and suppressing the sentinel's
    tool-progress UI). Accumulates tool_call fragments across the
    streaming chunks, executes tools through the shared `tools` module,
    and loops until the model produces a final text-only reply or
    MAX_TURNS is reached.

    Mirrors the deadline-checked + clamped-tool-timeout pattern from the
    sibling openai-SDK-backed runtimes so a single tool cannot block the
    sentinel past its --timeout budget.
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
        api_key = os.environ.get(TOGETHER_API_KEY_ENV, "").strip()
        if not api_key:
            log.error(f"together_sdk: {TOGETHER_API_KEY_ENV} not set in environment")
            return RuntimeResult(
                text=f"Agent could not authenticate with Together AI ({TOGETHER_API_KEY_ENV} not set).",
                exit_reason="crashed",
                elapsed_seconds=0,
            )

        try:
            from openai import (
                APIStatusError,
                APITimeoutError,
                AuthenticationError,
                InternalServerError,
                OpenAI,
                PermissionDeniedError,
                RateLimitError,
            )
        except ImportError as e:
            # pyproject.toml does not declare `openai` as a hard dep, the
            # sibling openai_sdk.py is also lazy-imported. Packaged axctl
            # installs may not have it. Surface a clean RuntimeResult so the
            # sentinel can render an actionable message instead of crashing
            # on a bare ModuleNotFoundError.
            log.error(f"together_sdk: openai Python SDK is not installed ({e})")
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
        # as `runtimes.together_sdk`, so a relative `from ..tools` would
        # escape past the top-level package and raise ImportError at runtime.
        # tests/test_together_sdk_runtime.py inserts the same hermes
        # directory into sys.path so the absolute form resolves there too.
        from tools import TOOL_DEFINITIONS, execute_tool

        cb = stream_cb or StreamCallback()
        model = model or DEFAULT_MODEL
        instructions = system_prompt or "You are a helpful coding assistant."

        tools = [_to_chat_completion_tool(t) for t in TOOL_DEFINITIONS]

        start_time = time.time()
        deadline = start_time + timeout

        history: list[dict] = list((extra_args or {}).get("history", []))
        history.append({"role": "user", "content": message})

        final_text = ""
        tool_count = 0
        files_written: list[str] = []

        client = OpenAI(api_key=api_key, base_url=TOGETHER_BASE_URL)

        for turn in range(MAX_TURNS):
            now = time.time()
            remaining = deadline - now
            if remaining <= 0:
                log.warning(
                    f"together_sdk: timeout exceeded at turn {turn + 1} "
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

            log.info(f"together_sdk: turn {turn + 1}, {len(history)} messages")

            # Chat-completions expects a `system` message at the front of
            # the messages list. Prepend one each turn so the model's
            # behavior is consistent across turns.
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
                log.warning(f"together_sdk: rate limited (HTTP {e.status_code}): {e.message}")
                return RuntimeResult(
                    text=(
                        final_text
                        or f"Together AI rate-limited (HTTP {e.status_code}). Retry after a short delay."
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
                log.error(f"together_sdk: API timeout: {e}")
                return RuntimeResult(
                    text=(
                        final_text
                        or "Agent timed out while waiting for the model."
                    ),
                    history=history,
                    tool_count=tool_count,
                    files_written=files_written,
                    exit_reason="timeout",
                    elapsed_seconds=int(time.time() - start_time),
                )
            except (AuthenticationError, PermissionDeniedError) as e:
                # 401 / 403. Operator-actionable, the user must see this in
                # the chat reply so they can rotate TOGETHER_API_KEY. Never
                # silently swallow auth failures.
                log.error(f"together_sdk: auth failed (HTTP {e.status_code}): {e.message}")
                return RuntimeResult(
                    text=f"Together AI authentication failed (HTTP {e.status_code}). Check TOGETHER_API_KEY.",
                    history=history,
                    tool_count=tool_count,
                    files_written=files_written,
                    exit_reason="auth_error",
                    elapsed_seconds=int(time.time() - start_time),
                )
            except InternalServerError as e:
                # 5xx from Together AI. Retry is plausible; signal to the operator.
                log.error(f"together_sdk: server error (HTTP {e.status_code}): {e.message}")
                return RuntimeResult(
                    text=(
                        final_text
                        or f"Together AI returned HTTP {e.status_code}. Retry may succeed."
                    ),
                    history=history,
                    tool_count=tool_count,
                    files_written=files_written,
                    exit_reason="server_error",
                    elapsed_seconds=int(time.time() - start_time),
                )
            except APIStatusError as e:
                # Any other 4xx not matched above (400 BadRequest, 422
                # UnprocessableEntity, 404 NotFound for unknown model, etc).
                # Surface the status and message so the operator knows what
                # to fix (most common is a model name typo for a model not
                # in Together's serverless catalog).
                log.error(f"together_sdk: API error (HTTP {e.status_code}): {e.message}")
                return RuntimeResult(
                    text=(
                        final_text
                        or f"Together AI error (HTTP {e.status_code}): {e.message}"
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
                # refused, etc). Logged with full repr so the underlying
                # type is visible in ops triage.
                log.error(f"together_sdk: unexpected error opening stream: {e!r}")
                return RuntimeResult(
                    text=final_text or "Agent encountered an unexpected error and could not complete the task.",
                    history=history,
                    tool_count=tool_count,
                    files_written=files_written,
                    exit_reason="crashed",
                    elapsed_seconds=int(time.time() - start_time),
                )

            # Accumulate text and tool_call deltas across the stream.
            # OpenAI chat-completions streams `chunk.choices[0].delta` with
            # `.content` for text and `.tool_calls` (an indexed list) for
            # function calls. Tool call args arrive as a sequence of
            # arguments fragments that concatenate into a JSON string.
            turn_text = ""
            tool_calls_acc: dict[int, dict] = {}

            try:
                for chunk in stream:
                    if not chunk.choices:
                        continue
                    delta = chunk.choices[0].delta
                    content_piece = getattr(delta, "content", None)
                    if content_piece:
                        # Buffer text locally for this turn. Don't publish to
                        # the callback yet, if the model pivots into tool
                        # calls, this is pre-tool chatter that would leak as
                        # visible chat content and suppress the sentinel's
                        # tool-progress UI. We only emit via on_text_complete
                        # when the turn is confirmed text-only (no tool calls).
                        # Mirrors the buffering pattern in openai_sdk.py and
                        # groq_sdk.py.
                        turn_text += content_piece

                    tcalls = getattr(delta, "tool_calls", None) or []
                    for tc_delta in tcalls:
                        idx = getattr(tc_delta, "index", 0)
                        slot = tool_calls_acc.setdefault(
                            idx,
                            {
                                "id": "",
                                "type": "function",
                                "function": {"name": "", "arguments": ""},
                            },
                        )
                        if getattr(tc_delta, "id", None):
                            slot["id"] = tc_delta.id
                        fn = getattr(tc_delta, "function", None)
                        if fn:
                            if getattr(fn, "name", None):
                                slot["function"]["name"] = fn.name
                            if getattr(fn, "arguments", None):
                                slot["function"]["arguments"] += fn.arguments
            except Exception as e:
                log.error(f"together_sdk: stream error after {len(turn_text)} chars: {e}")
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

            tool_calls = [tool_calls_acc[i] for i in sorted(tool_calls_acc)]

            # If the model requested tools, execute them and continue the loop.
            if tool_calls:
                history.append(
                    {
                        "role": "assistant",
                        "content": turn_text or None,
                        "tool_calls": tool_calls,
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
                            f"together_sdk: timeout exceeded before tool "
                            f"{tc['function']['name']} "
                            f"(elapsed {int(now_tool - start_time)}s)"
                        )
                        return RuntimeResult(
                            text=(
                                final_text
                                or "Agent timed out before completing tool calls."
                            ),
                            history=history,
                            session_id=None,
                            tool_count=tool_count,
                            files_written=files_written,
                            exit_reason="timeout",
                            elapsed_seconds=int(now_tool - start_time),
                        )

                    tool_count += 1
                    name = tc["function"]["name"]
                    raw_args = tc["function"]["arguments"]
                    try:
                        args = json.loads(raw_args) if raw_args else {}
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
                    log.info(
                        f"together_sdk: tool {name}({json.dumps(args, default=str)[:80]})"
                    )
                    cb.on_tool_start(name, summary)
                    result = execute_tool(name, args, workdir)

                    if name == "write_file" and not result.is_error:
                        files_written.append(args.get("path", ""))

                    short = result.output[:200] if result.output else ""
                    cb.on_tool_end(name, short)

                    # Cap tool output at TOOL_OUTPUT_CAP bytes to bound context
                    # growth, and surface a truncation marker when we hit the
                    # cap so the model can tell content was clipped.
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

            # No tool calls, text-only response. Treat as final.
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
                f"together_sdk: hit MAX_TURNS={MAX_TURNS} without final answer "
                f"(elapsed {elapsed}s, {tool_count} tools)"
            )
            return RuntimeResult(
                text=(
                    final_text
                    or "Agent hit the maximum turn limit without producing a final answer."
                ),
                history=history,
                session_id=None,
                tool_count=tool_count,
                files_written=files_written,
                exit_reason="iteration_limit",
                elapsed_seconds=elapsed,
            )

        elapsed = int(time.time() - start_time)
        log.info(
            f"together_sdk: done in {elapsed}s, {tool_count} tools, "
            f"{len(final_text)} chars"
        )
        return RuntimeResult(
            text=final_text,
            history=history,
            session_id=None,
            tool_count=tool_count,
            files_written=files_written,
            exit_reason="done",
            elapsed_seconds=elapsed,
        )
