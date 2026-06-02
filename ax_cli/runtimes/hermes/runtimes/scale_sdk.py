# NEW: not yet vendored from ax-agents. Pending upstream PR before the next
# vendor sync. See ax_cli/runtimes/hermes/README.md for vendoring guidance.
"""Scale GenAI Platform (SGP) runtime — wraps Scale's chat-completions API.

Scale's GenAI Platform (a.k.a. Scale GP / "Spellbook"/EGP) hosts the defense
LLMs relevant to this fleet — notably **Defense Llama**, available inside
**Scale Donovan** in controlled US-government enclaves. This runtime lets a
Gateway-managed agent use those models as its brain.

IMPORTANT — this is NOT an OpenAI-wire-compatible endpoint (unlike
leapfrog_sdk / palantir_sdk):

  * path is `/egp/v1/chat-completions` (hyphenated), not OpenAI's
    `/v1/chat/completions`,
  * auth header is `x-api-key: <key>`, not `Authorization: Bearer <key>`.

So we talk to it with the repo's own `httpx` client rather than the `openai`
SDK passthrough. Verified against Scale's public SGP chat-completions docs
(api.spellbook.scale.com, 2026-06); a Donovan/Defense-Llama enclave will use
the same shape behind a private base URL.

Scope (Phase 1): single-turn chat completion. The runtime sends the system
prompt + the user message and returns the model's text. Tool-calling and
streaming are intentionally deferred — SGP's tool-call + SSE shapes are not
yet confirmed for the enclave deployments, and guessing them in a defense
context would be worse than not shipping them. Tracked as a follow-up.

Auth / config:
  SCALE_API_KEY   — SGP API key, sent as `x-api-key` (required)
  SCALE_BASE_URL  — SGP API base, default `https://api.spellbook.scale.com/egp/v1`.
                    Override for a Donovan/government enclave (private host).

Model:
  Supply the SGP model name/id via the managed-agent `model` field (e.g. a
  Defense Llama deployment id). Fails closed with an actionable message when
  absent rather than guessing a model that won't exist on the enclave.
"""

from __future__ import annotations

import logging
import os
import time

from . import BaseRuntime, RuntimeResult, StreamCallback, register

log = logging.getLogger("runtime.scale_sdk")

DEFAULT_BASE_URL = "https://api.spellbook.scale.com/egp/v1"
CHAT_COMPLETIONS_PATH = "/chat-completions"

SCALE_API_KEY_ENV = "SCALE_API_KEY"
SCALE_BASE_URL_ENV = "SCALE_BASE_URL"


def _resolve_auth() -> tuple[tuple[str, str] | None, str]:
    """Read SCALE_API_KEY (required) + SCALE_BASE_URL (defaulted).

    Returns ((api_key, base_url), "") on success or (None, error_message)
    when the API key is missing.
    """
    api_key = os.environ.get(SCALE_API_KEY_ENV, "").strip()
    base_url = os.environ.get(SCALE_BASE_URL_ENV, "").strip() or DEFAULT_BASE_URL
    if not api_key:
        return None, f"{SCALE_API_KEY_ENV} not set"
    return (api_key, base_url.rstrip("/")), ""


def _extract_text(payload: object) -> str:
    """Pull assistant text out of an SGP chat-completions response.

    SGP's exact envelope varies by version, so probe the shapes seen in the
    wild (OpenAI-style `choices[].message.content`, and SGP's
    `chat_completion.message.content` / top-level `message.content`) before
    giving up. Defensive on purpose — a wrong guess should degrade to a
    visible raw snippet, not a crash.
    """
    if not isinstance(payload, dict):
        return ""
    choices = payload.get("choices")
    if isinstance(choices, list) and choices:
        msg = choices[0].get("message") if isinstance(choices[0], dict) else None
        if isinstance(msg, dict) and isinstance(msg.get("content"), str):
            return msg["content"]
    chat_completion = payload.get("chat_completion")
    if isinstance(chat_completion, dict):
        msg = chat_completion.get("message")
        if isinstance(msg, dict) and isinstance(msg.get("content"), str):
            return msg["content"]
    message = payload.get("message")
    if isinstance(message, dict) and isinstance(message.get("content"), str):
        return message["content"]
    if isinstance(payload.get("content"), str):
        return payload["content"]
    return ""


@register("scale_sdk")
class ScaleSDKRuntime(BaseRuntime):
    """Single-turn chat completion against Scale's GenAI Platform.

    Uses `httpx` (a hard dependency of ax-cli) directly because SGP is not
    OpenAI-wire-compatible. No tool loop in Phase 1 — see module docstring.
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
        start_time = time.time()

        auth, err = _resolve_auth()
        if auth is None:
            log.error(f"scale_sdk: auth resolution failed: {err}")
            return RuntimeResult(
                text=f"Agent could not authenticate with Scale GenAI Platform ({err}).",
                exit_reason="crashed",
                elapsed_seconds=0,
            )
        api_key, base_url = auth

        model = (model or "").strip()
        if not model:
            log.error("scale_sdk: no model supplied")
            return RuntimeResult(
                text=(
                    "Scale GenAI Platform requires an explicit model id (e.g. a "
                    "Defense Llama deployment id from your Donovan enclave). Set it via "
                    "the managed-agent `model` field."
                ),
                exit_reason="crashed",
                elapsed_seconds=0,
            )

        try:
            import httpx
        except ImportError as e:  # pragma: no cover - httpx is a core dep
            log.error(f"scale_sdk: httpx is not installed ({e})")
            return RuntimeResult(
                text="Agent could not start because `httpx` is not installed in this runtime environment.",
                exit_reason="crashed",
                elapsed_seconds=0,
            )

        cb = stream_cb or StreamCallback()
        instructions = system_prompt or "You are a helpful assistant."

        # History passthrough: SGP uses the OpenAI message shape for the
        # `messages` array, so prior rows (if any) carry over unchanged.
        history: list[dict] = list((extra_args or {}).get("history", []))
        messages = [{"role": "system", "content": instructions}, *history, {"role": "user", "content": message}]

        cb.on_status("thinking")
        url = f"{base_url}{CHAT_COMPLETIONS_PATH}"
        headers = {"x-api-key": api_key, "Content-Type": "application/json"}
        body = {"model": model, "messages": messages}

        try:
            resp = httpx.post(url, headers=headers, json=body, timeout=timeout)
        except httpx.TimeoutException as e:
            log.error(f"scale_sdk: request timed out: {e}")
            return RuntimeResult(
                text="Agent timed out while waiting for Scale GenAI Platform.",
                exit_reason="timeout",
                elapsed_seconds=int(time.time() - start_time),
            )
        except httpx.HTTPError as e:
            log.error(f"scale_sdk: transport error: {e!r}")
            return RuntimeResult(
                text=f"Could not reach Scale GenAI Platform at {base_url}: {e}",
                exit_reason="crashed",
                elapsed_seconds=int(time.time() - start_time),
            )

        if resp.status_code in (401, 403):
            # Operator-actionable: surface so SCALE_API_KEY can be rotated or
            # the enclave ACL fixed. Never swallow auth failures.
            log.error(f"scale_sdk: auth failed (HTTP {resp.status_code})")
            return RuntimeResult(
                text=(
                    f"Scale GenAI Platform authentication failed (HTTP {resp.status_code}). "
                    "Check SCALE_API_KEY and the enclave's API permissions."
                ),
                exit_reason="auth_error",
                elapsed_seconds=int(time.time() - start_time),
            )
        if resp.status_code == 429:
            log.warning("scale_sdk: rate limited (HTTP 429)")
            return RuntimeResult(
                text="Scale GenAI Platform rate-limited (HTTP 429). Retry after a short delay.",
                exit_reason="rate_limited",
                elapsed_seconds=int(time.time() - start_time),
            )
        if resp.status_code >= 400:
            detail = resp.text[:300] if resp.text else ""
            log.error(f"scale_sdk: API error (HTTP {resp.status_code}): {detail}")
            return RuntimeResult(
                text=f"Scale GenAI Platform API error (HTTP {resp.status_code}): {detail}",
                exit_reason="api_error",
                elapsed_seconds=int(time.time() - start_time),
            )

        try:
            payload = resp.json()
        except ValueError as e:
            log.error(f"scale_sdk: non-JSON response: {e}")
            return RuntimeResult(
                text=f"Scale GenAI Platform returned a non-JSON response: {resp.text[:200]}",
                exit_reason="crashed",
                elapsed_seconds=int(time.time() - start_time),
            )

        text = _extract_text(payload).strip()
        elapsed = int(time.time() - start_time)
        if not text:
            log.warning("scale_sdk: could not extract assistant text from response envelope")
            return RuntimeResult(
                text="Scale GenAI Platform returned a response, but no assistant text could be extracted.",
                exit_reason="crashed",
                elapsed_seconds=elapsed,
            )

        cb.on_text_complete(text)
        history.append({"role": "user", "content": message})
        history.append({"role": "assistant", "content": text})
        log.info(f"scale_sdk: done in {elapsed}s, {len(text)} chars")
        return RuntimeResult(
            text=text,
            history=history,
            session_id=None,
            tool_count=0,
            files_written=[],
            exit_reason="done",
            elapsed_seconds=elapsed,
        )
