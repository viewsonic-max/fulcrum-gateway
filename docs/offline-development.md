# Offline Development and Smoke Testing

`AX_OFFLINE=1` lets you develop and test Gateway agent logic without a connection
to the aX platform. The Gateway starts normally, agents can be registered, and the
`smoke` command confirms an agent receives and processes messages end-to-end.

## What works offline

| Capability | Works offline | Notes |
| --- | --- | --- |
| Gateway start / stop | Yes | |
| Agent registration (`agents add`) | Yes | Uses local fake session |
| Agent listing, inspection | Yes | |
| Channel setup (`channel setup`) | Yes | Writes `localhost:8765` into env file |
| `smoke` for echo / exec agents | Yes | Handler called in-process |
| `smoke` for claude_code_channel | Yes | Requires Claude Code running |
| `smoke` for hermes agents | Yes | Gateway starts subprocess automatically |
| Multi-agent message routing | No | Not supported — use the real platform |
| Message persistence | No | Replies exist only for the current session |

## Starting the gateway in offline mode

```bash
AX_OFFLINE=1 ax gateway start
```

The gateway and its UI start normally at `http://localhost:8765`. All platform
calls are stubbed — no credentials required, no network access needed.

## Registering agents

Register agents the same way as normal, prefixed with `AX_OFFLINE=1`:

```bash
# Echo agent (simplest smoke test target)
AX_OFFLINE=1 ax gateway agents add my-echo --template echo_test

# Claude Code channel agent
AX_OFFLINE=1 ax gateway agents add my-agent --template claude_code_channel --workdir /path/to/workspace

# Exec agent
AX_OFFLINE=1 ax gateway agents add my-exec --template exec
```

Agent identities, token files, and registry entries are written to
`~/.ax/gateway/` exactly as they would be with a live platform.

## Setting up a Claude Code channel agent

After registering a `claude_code_channel` agent, run channel setup. The
`AX_OFFLINE=1` flag makes it write `http://localhost:8765` into the channel
env file automatically — no `--base-url` flag needed:

```bash
AX_OFFLINE=1 ax channel setup my-agent --workdir /path/to/workspace
```

Start Claude Code from that workspace:

```bash
cd /path/to/workspace
claude \
  --strict-mcp-config \
  --mcp-config .mcp.json \
  --dangerously-load-development-channels server:ax-channel
```

The channel (`axctl channel`) starts as an MCP server inside Claude Code. It reads
`AX_OFFLINE=1` and `AX_BASE_URL=http://localhost:8765` from the env file and
connects to the gateway's local SSE endpoint. No `AX_BASE_URL` prefix is needed
on the `claude` command — the env file handles it.

After a gateway restart, the channel reconnects automatically within a few seconds.

## The smoke command

`ax gateway agents smoke` invokes an agent's handler directly and confirms the
agent received and responded to a message. It is the primary tool for verifying
a new agent type works end-to-end without a running platform.

```
ax gateway agents smoke <name> [--message <text>] [--timeout <seconds>] [--json]
```

### Echo and exec agents

The handler is called in-process. The response is shown immediately:

```bash
AX_OFFLINE=1 ax gateway agents smoke my-echo
# Smoke: @my-echo
#   prompt    = ping
#   response  = Echo: ping

AX_OFFLINE=1 ax gateway agents smoke my-echo --message "hello world"
# Smoke: @my-echo
#   prompt    = hello world
#   response  = Echo: hello world
```

### Claude Code channel agents

The smoke command posts the message to the gateway, which delivers it to the
running `axctl channel` process inside Claude Code. It then waits up to 60 seconds
for Claude's reply to appear:

```bash
AX_OFFLINE=1 ax gateway agents smoke my-agent --message "Reply with: smoke test OK"
# Smoke: @my-agent
#   prompt    = Reply with: smoke test OK
#   delivered = True (message_id=...)
#   response  = smoke test OK
```

If the agent is not connected (Claude Code not running or channel not attached):

```
Message posted but @my-agent is not connected.
  Start Claude Code with: AX_BASE_URL=http://localhost:8765 [claude command]
```

Running smoke without `--message` uses the agent's `recommended_test_message`
from the template definition (e.g. `Reply with exactly: Gateway test OK.` for
`claude_code_channel`), falling back to `ping` if none is set.

Replies are logged to `~/.ax/gateway/offline-replies.jsonl`. Each line is a JSON
object with `id`, `content`, `author`, `space_id`, and `channel`.

### Hermes agents

Hermes subprocesses are started by the gateway daemon automatically. The gateway
injects `AX_OFFLINE=1` and `AX_BASE_URL=http://localhost:8765` into the subprocess
environment — no manual configuration needed. Run smoke the same way as for channel
agents:

```bash
AX_OFFLINE=1 ax gateway agents smoke my-hermes-agent --message "ping"
```

### Supported runtime types

| Runtime | Smoke method |
|---|---|
| `echo` | In-process handler |
| `exec` / `command` | In-process subprocess |
| `claude_code_channel` | Delivery via gateway SSE + reply polling |
| `hermes_plugin` | Delivery via gateway SSE + reply polling |
| `sentinel_hermes_sdk` | Delivery via gateway SSE + reply polling |
| `sentinel_inference_sdk` | Delivery via gateway SSE + reply polling |
| `inbox` / `passive` | Not supported (no handler) |
| `sentinel_cli` | Not supported offline (requires live AI CLI) |

## Full offline workflow

```bash
# 1. Start the gateway
AX_OFFLINE=1 ax gateway start

# 2. Register your agent
AX_OFFLINE=1 ax gateway agents add my-agent --template claude_code_channel --workdir /path/to/workspace

# 3. Set up the channel (first time, or after changing workspace)
AX_OFFLINE=1 ax channel setup my-agent --workdir /path/to/workspace

# 4. Start Claude Code
cd /path/to/workspace
claude \
  --strict-mcp-config \
  --mcp-config .mcp.json \
  --dangerously-load-development-channels server:ax-channel

# 5. Smoke test
AX_OFFLINE=1 ax gateway agents smoke my-agent
```

## How it works

`AX_OFFLINE=1` activates `OfflineAxClient` in place of the normal platform HTTP
client. When — and only when — the gateway is started with `AX_OFFLINE=1`, its
HTTP server (port 8765) gains three additional endpoints. These routes are absent
in a normal gateway start and have no effect on production operation:

| Endpoint | Purpose |
|---|---|
| `POST /auth/exchange` | Returns a fake JWT encoding the agent name |
| `GET /api/v1/sse/messages?token=…` | Per-agent SSE queue for message delivery |
| `POST /api/v1/messages` | Accepts messages, delivers to the subscribed agent |

Each agent gets exactly one SSE queue. Messages are delivered only to the
specifically mentioned agent — there is no broadcast or agent-to-agent routing.
This is intentionally narrow: the offline mode is for testing a single agent's
handler logic, not for simulating a multi-agent environment.
