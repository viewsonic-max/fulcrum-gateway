# Scale GenAI Platform (Donovan / Defense Llama)

**Shape:** Model runtime — `scale_sdk`
**Code:** `ax_cli/runtimes/hermes/runtimes/scale_sdk.py`
**Status:** Functional — single-turn (tool-calling + streaming deferred)

## Overview

Scale AI's **GenAI Platform** (Scale GP / "Spellbook"/EGP) hosts the defense
LLMs relevant to this fleet — notably **Defense Llama**, available inside
**Scale Donovan** in controlled US-government enclaves (IL4 / SIPR+ / JWICS).
Scale is named among the AI vendors on the Golden Dome program. This runtime
lets a Gateway-managed agent use those models as its brain.

## Why it is not an OpenAI passthrough

Unlike Palantir AIP and LeapfrogAI, Scale GP is **not** OpenAI-wire-compatible:

- the path is `/egp/v1/chat-completions` (hyphenated), not `/v1/chat/completions`;
- the auth header is `x-api-key: <key>`, not `Authorization: Bearer <key>`.

So `scale_sdk` drives the repo's own `httpx` client directly rather than the
`openai` SDK.

## How it works

- Single POST to `{base}/chat-completions` with `x-api-key` auth and an
  OpenAI-shaped `{model, messages}` body (system prompt + history + user turn).
- Response text is extracted defensively across the envelopes seen in the
  wild (`choices[].message.content`, `chat_completion.message.content`,
  top-level `message.content`).
- Fails closed when `SCALE_API_KEY` or `model` is missing; surfaces 401/403,
  429, and other 4xx/5xx with actionable text.

### Scope / limitations

Phase 1 is **single-turn** — no tool loop, no streaming. SGP's tool-call and
SSE shapes are not yet confirmed for the enclave deployments, and guessing
them in a defense context would be worse than not shipping them. Tracked as a
follow-up.

## Configuration

| Env var | Required | Purpose |
| --- | --- | --- |
| `SCALE_API_KEY` | yes | SGP API key, sent as `x-api-key` |
| `SCALE_BASE_URL` | no | SGP base; default `https://api.spellbook.scale.com/egp/v1`. Override for a Donovan/government enclave (private host) |

Supply the SGP model id (e.g. a Defense Llama deployment id) via the
managed-agent `model` field. Select with `sentinel_sdk_runtime: scale_sdk`.

## Security

The API key is Gateway-brokered and must not be written to the workspace,
logs, or messages. For classified enclaves, set `SCALE_BASE_URL` to the
enclave host; the public Spellbook default is for unclassified use only.

## Sources

- [Scale GenAI Platform Python SDK](https://scaleapi.github.io/egp-py/)
- [Scale GP — Chat Completions](https://scale-egp.readme.io/docs/chat-completions-intro)
- [Defense Llama on Scale Donovan](https://scale.com/donovan/defense-llm)
- [Introducing Defense Llama](https://scale.com/blog/defense-llama)
