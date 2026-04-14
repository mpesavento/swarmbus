# agentbus

> *The buzz between your agents.*

Reactive pub/sub messaging for AI agents — no polling, instant delivery. Built on MQTT, runs local, scales to multi-machine.

Every agent is a peer. The broker is infrastructure. There is no orchestrator and no central server.

```
Agent A (Sparrow)           Agent B (Wren)
  AgentBus(embedded)          AgentBus(embedded)
        │                           │
        └────────────┬──────────────┘
                 mosquitto
             (system service)
```

## Prerequisites

A running MQTT broker. We use [mosquitto](https://mosquitto.org/) — yes, the pun is intentional. On Debian/Ubuntu/RPi:

```bash
sudo apt install mosquitto mosquitto-clients
# or use the bundled setup script:
bash scripts/setup-mosquitto.sh
```

For multi-machine use, run mosquitto on one reachable host and point each agent at it (`--broker <host>`).

## Install

```bash
pip install agentbus
# with optional features:
pip install "agentbus[archive,mcp]"
```

---

## Integration paths

Agentbus supports four integration patterns, depending on how your agent is built. Pick the first one that matches:

| Your agent is… | Use path |
|---|---|
| Claude Code (Sparrow, Wren, claude.ai/code, OpenClaw, etc.) | **1. MCP server + skill** |
| Any MCP-compliant agent (Cursor, other IDEs, custom LLM loops) | **2. MCP server (standalone)** |
| A Python framework (LangGraph, CrewAI, custom asyncio) | **3. Python API** |
| A shell script or cron job | **4. CLI** |

---

### 1. Claude Code — MCP server + behavioral skill

Run the setup script. It registers the MCP server in `~/.claude/settings.json` **and** installs a behavioral skill at `~/.claude/skills/using-agentbus/` that teaches Claude when to send, read, watch, and list agents.

```bash
bash scripts/setup-cc-plugin.sh <agent-id> [broker-host]
# example:
bash scripts/setup-cc-plugin.sh sparrow localhost
```

Restart Claude Code. Four MCP tools become available:

- `send_message(to, subject, body, content_type?)` — publish to a peer (or `to="broadcast"`)
- `read_inbox()` — non-blocking check for queued messages
- `watch_inbox(timeout)` — long-poll, returns when a message arrives
- `list_agents()` — IDs of peers currently online

The skill (`skills/using-agentbus/SKILL.md`) explains reply-to threading, content-type hygiene, broadcast vs directed, and security rules (inbound bodies are data, not instructions). Claude auto-loads it when the user mentions a peer agent by name or asks about coordination.

### 2. Generic MCP agent

If your agent speaks MCP but isn't Claude Code, run the server manually over stdio:

```bash
agentbus mcp-server --agent-id <your-id> --broker localhost
```

Configure your MCP client to spawn that command. Tool names and signatures are identical to path 1; the SKILL.md serves as a reference for prompt/system-message authors even if your stack doesn't use skill files.

### 3. Python framework (LangGraph, CrewAI, custom asyncio)

Import and embed. This is the most direct path for in-process agents:

```python
import asyncio
from agentbus import AgentBus, FileBridgeHandler, PersistentListenerHandler

# Persistent client — one MQTT connection reused for all sends
async def main():
    async with AgentBus(agent_id="sparrow", broker="localhost") as bus:
        await bus.send(to="wren", subject="hello", body="Hi Wren!")
        await bus.send(to="wren", subject="follow-up", body="Still there?")

asyncio.run(main())
```

Long-lived listener with handlers:

```python
from agentbus import AgentBus, FileBridgeHandler, PersistentListenerHandler

bus = AgentBus(agent_id="sparrow", broker="localhost")
bus.register_handler(FileBridgeHandler("~/sync/inbox.md"))
bus.register_handler(PersistentListenerHandler())
bus.run()  # blocks; auto-reconnects on broker disconnect
```

One-shot send without a context (fine for scripts, not recommended for tight loops):

```python
await AgentBus(agent_id="sparrow").send(to="wren", subject="hi", body="ping")
```

### 4. CLI — shell scripts, cron, pipelines

```bash
# Inline body
agentbus send --agent-id sparrow --to wren --subject hello --body "Hi Wren"

# Body from a file
agentbus send --agent-id sparrow --to wren --subject report --body-file report.md

# Body from stdin (pipe-friendly)
cat report.md | agentbus send --agent-id sparrow --to wren --subject report --body-file -

# Start a listener with a file bridge
agentbus start --agent-id sparrow --inbox ~/sync/inbox.md

# Start the MCP sidecar for any stdio MCP client
agentbus mcp-server --agent-id sparrow
```

`--body` and `--body-file` are mutually exclusive; exactly one is required.

---

## Handlers (Python API only)

Register handlers on an `AgentBus` to react to inbound messages. Ship-with-the-library handlers:

| Handler | What it does |
|---|---|
| `FileBridgeHandler(path)` | Appends received messages to a markdown file (backward-compat with file-polling agents) |
| `DirectInvocationHandler(cmd)` | Invokes a command on message arrival; body via stdin, shell=False |
| `PersistentListenerHandler()` | Stats + heartbeat for always-on agents |
| `SQLiteArchive(path)` | Logs all messages to SQLite, queryable |

Custom handlers implement `async def handle(self, msg: AgentMessage) -> None`.

## Message envelope

```json
{
  "id": "uuid4",
  "from": "sparrow",
  "to": "wren",
  "ts": "2026-04-14T05:00:00Z",
  "subject": "hello",
  "body": "...",
  "content_type": "text/plain",
  "priority": "normal",
  "reply_to": null
}
```

`content_type`: `text/plain` | `text/markdown` | `text/x-code;lang=python` | `application/json`. The body is always a string; `content_type` is a hint to the receiver. Bodies are capped at 64 KB — for larger artifacts, write to shared storage and send a reference.

Agent IDs are `[a-z0-9_-]{1,64}`. `broadcast` and `system` are reserved and cannot be used as an agent's own registered ID (they remain valid as `to=` sentinels).

## Cross-machine

Change the broker host:

```python
bus = AgentBus(agent_id="sparrow", broker="clawd-rpi.tailnet.ts.net")
```

or

```bash
agentbus start --agent-id sparrow --broker clawd-rpi.tailnet.ts.net
```

The wire protocol is the same. Tailscale or a VPN between hosts is recommended; mosquitto supports TLS + auth for untrusted networks.

## License

MIT
