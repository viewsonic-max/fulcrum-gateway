# Palantir AIP

**Shape:** Model runtime — `palantir_sdk`
**Code:** `ax_cli/runtimes/hermes/runtimes/palantir_sdk.py`
**Status:** Functional (OpenAI-compatible passthrough)

## Overview

Palantir Foundry's **AIP** (Artificial Intelligence Platform) exposes
OpenAI-compatible **proxy** endpoints so open-source SDKs work unchanged while
requests still flow through Foundry's rate limiting, data governance (zero
data retention, georestriction), and usage tracking. Palantir is one of the
software primes writing core Golden Dome software, so this lets a
Gateway-managed agent reason with a model hosted on the operator's Foundry
stack.

Because the proxy speaks the OpenAI Chat Completions wire format, the runtime
is a direct clone of `leapfrog_sdk`'s private-endpoint pattern — no schema
adapter needed.

## How it works

- Drives the `openai` Python SDK pointed at the Foundry proxy base URL.
- Multi-turn agent loop with tool calls, streaming buffering, wall-clock
  deadline, and clamped per-tool timeouts (same engine as `leapfrog_sdk`).
- The proxy path is `/api/v2/llm/proxy/openai/v1/chat/completions`, so the
  operator-supplied base URL ends at `/api/v2/llm/proxy/openai/v1`.
- The `model` must be a Foundry **resource identifier** from the Model
  Catalog, e.g. `ri.language-model-service..language-model.gpt-5-2`. The
  runtime **fails closed** with an actionable message when no model is set
  rather than guessing.

## Configuration

| Env var | Required | Purpose |
| --- | --- | --- |
| `PALANTIR_AIP_TOKEN` | yes | Foundry bearer token authorized for the AIP LLM proxy |
| `PALANTIR_AIP_BASE_URL` | yes | Proxy base URL ending in `/api/v2/llm/proxy/openai/v1` (private; no default) |

Select on a managed-agent entry with `sentinel_sdk_runtime: palantir_sdk` and
a `model` set to the Foundry resource identifier. See the
[SDK runtime selection note](README.md#sdk-runtime-selection--wiring-note-incl-xai_sdk).

## Security

The Foundry token is Gateway-brokered and injected at launch; it must not be
written to `.ax/config.toml`, profiles, logs, or messages. A wrong model id
surfaces as an actionable HTTP 404 from the proxy.

## Sources

- [OpenAI Chat Completions (Proxy) — Palantir](https://www.palantir.com/docs/foundry/api/llm-apis/models/openai-chat-completions-proxy)
- [LLM-provider compatible APIs — Palantir](https://www.palantir.com/docs/foundry/aip/llm-provider-compatible-apis)
- [Supported LLMs — Palantir](https://www.palantir.com/docs/foundry/aip/supported-llms)
