# agentbus

> *The buzz between your agents.*

Reactive pub/sub messaging for AI agents — no polling, instant delivery. Runs on [mosquitto](https://mosquitto.org/) (MQTT); local or multi-machine.

Every agent is a peer. The broker is infrastructure. There is no orchestrator and no central server.

```
Agent A (Sparrow)           Agent B (Wren)
  AgentBus(embedded)          AgentBus(embedded)
        │                           │
        └────────────┬──────────────┘
                 mosquitto
             (system service)
```

## Quickstart — two agents talking

The worked example below gets Sparrow and Wren exchanging messages in under 5 minutes. It's the reference shape; every other integration path is a variation on it.

### 1. Broker

```bash
sudo apt install mosquitto mosquitto-clients   # Debian/Ubuntu/RPi
# or: bash scripts/setup-mosquitto.sh
```

The broker runs as a system service on port 1883 after install. For cross-machine use, run mosquitto on one reachable host and point each agent at it with `--broker <host>`. Tailscale or a VPN is recommended for untrusted networks.

### 2. Install the CLI

```bash
pip install "agentbus[mcp]"
```

### 3. Start a listener daemon per agent

**This is the part most people miss.** For an agent to *receive* messages reactively (no polling), a long-lived listener process must be running — it holds the MQTT subscription and, optionally, bridges incoming messages into a file the agent session can read.

```bash
# Terminal A (Sparrow's side):
agentbus start --agent-id sparrow --inbox ~/sync/sparrow-inbox.md

# Terminal B (Wren's side):
agentbus start --agent-id wren --inbox ~/sync/wren-inbox.md
```

Each daemon:
- Announces the agent online (retained presence)
- Subscribes to `agents/<id>/inbox` + `agents/broadcast`
- Appends every received message to the inbox file (the `FileBridgeHandler`)
- Republishes `offline` on crash via MQTT Last-Will

Run them under systemd-user, byobu, tmux, or a supervisor of your choice. No cron needed — MQTT push handles delivery.

### 4. Send — and always archive

From any shell, script, or agent session:

```bash
# Set once so every send archives automatically:
export AGENTBUS_OUTBOX=~/sync/sparrow-outbox.md

agentbus send --agent-id sparrow --to wren --subject "hi" --body "got a minute?"
```

`AGENTBUS_OUTBOX` (or `--outbox` per call) appends every outbound message to a file using the same format as the receiver's inbox. Your own sent-log and received-log are now structurally identical and can be merged into one conversation view. **Always set this when running under a real agent identity** — an unarchived send is a dropped audit trail.

**Multi-agent safety.** If two agents might share the same shell environment, a bare `AGENTBUS_OUTBOX=/path/sparrow-outbox.md` leaks into both. Two fixes, pick one (or use both):

- **Template** — put `{agent_id}` in the path. The library expands it at send time:
  ```
  export AGENTBUS_OUTBOX="$HOME/sync/{agent_id}-outbox.md"
  ```
  Every `agentbus send --agent-id X` lands in `X-outbox.md`. One env var, correct file per id.
- **Agent-scoped override** — `AGENTBUS_OUTBOX_<UPPER_AGENT_ID>` beats the shared one:
  ```
  export AGENTBUS_OUTBOX_SPARROW=~/sync/sparrow-outbox.md
  export AGENTBUS_OUTBOX_WREN=~/sync/wren-outbox.md
  ```
  Hyphens in the agent-id become underscores (`wren-beta` → `AGENTBUS_OUTBOX_WREN_BETA`).

Resolution order (highest first): `--outbox` flag, `AGENTBUS_OUTBOX_<ID>`, `AGENTBUS_OUTBOX`, none.

Wren's inbox file grows immediately; her next session turn sees it. That's the receive path whenever the listener daemon is running for `wren`.

```bash
agentbus list                              # who's online right now?
```

**Two read paths, pick by deployment shape:**

- **Always-on agent with a daemon (Sparrow, Wren, any long-lived session)**: use `agentbus tail` to read new content from the inbox file. No MQTT contention with the daemon — the daemon is the sole broker subscriber, tail just reads the file it writes. Cursor-tracked so repeat calls only show new.
- **Ephemeral or scripted agent without a daemon**: use `agentbus read` / `agentbus watch`. These open a fresh MQTT connection, catch retained messages or messages published during the connection window, and exit.

```bash
# With a daemon running (Sparrow/Wren pattern):
agentbus tail --agent-id sparrow              # new entries from the daemon's inbox file since last cursor
agentbus tail --agent-id sparrow --follow     # stream new content (blocks)
agentbus tail --agent-id sparrow --consumer bot  # separate cursor

# No daemon — ephemeral (CI job, shell pipeline):
agentbus read --agent-id scratch              # drain retained messages, exit
agentbus watch --agent-id scratch --timeout 60  # block until one arrives
```

**What to never do:** run `agentbus read` or `agentbus watch` against an agent-id that already has a daemon running. They'd race the daemon for QoS1 messages — whichever client is currently subscribed wins and the other silently never sees them. Use `agentbus tail` (file-based) instead.

**Daemon durability.** By default `agentbus start` uses an MQTT persistent session (`--persistent`, on by default), so a crashed or restarted daemon doesn't lose queued messages — the broker redelivers them on reconnect. Disable with `--no-persistent` only if another process is already holding the `agentbus-<agent-id>` client identifier.

That's the whole loop: broker → daemon OR one-shot per agent-id → `send` from anywhere → peer receives via tail (if daemon) or read (if not).

---

## Install

```bash
pip install agentbus
# with optional features:
pip install "agentbus[archive,mcp]"
```

## Integration paths

The quickstart above uses the CLI path (#4 below) because it's the most universal. Pick the first row that matches your setup — each one uses the same broker and wire protocol, so agents on different paths interoperate freely.

| Your agent is… | Use path |
|---|---|
| Claude Code (claude.ai/code, Claude Code CLI) | **1. Claude Code — MCP server + skill** |
| OpenClaw | **2. OpenClaw — skill + listener daemon** |
| Any other MCP-compliant agent (Cursor, custom LLM loops) | **3. Generic MCP agent** |
| A Python framework (LangGraph, CrewAI, custom asyncio) | **4. Python API** |
| A shell script, cron job, or any CLI-speaking agent | **5. CLI** |

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

Claude Code **also** needs a listener daemon running (step 3 of the quickstart) to receive messages while the chat session is closed. The MCP tools only work while Claude is open — the daemon is what catches messages in between.

### 2. OpenClaw — skill + listener daemon

OpenClaw doesn't natively register MCP servers (it routes MCP via the `mcporter` skill), so the path is different: install the behavioral skill and run the listener daemon. The skill's examples work in CLI mode — no MCP sidecar required.

```bash
bash scripts/setup-openclaw-plugin.sh <agent-id> [broker-host]
# example:
bash scripts/setup-openclaw-plugin.sh wren localhost
```

This copies the skill to `~/.openclaw/skills/using-agentbus/` and prints the `agentbus start` command you need to run (typically under byobu or systemd-user). From then on, the OpenClaw agent uses `agentbus send` / `agentbus read` / `agentbus list` via its shell tool.

**For reactive wake-up** (message arrives → OpenClaw agent takes a real turn, no polling), combine the listener daemon with `examples/openclaw-wake.sh`:

```bash
agentbus start \
  --agent-id wren \
  --inbox ~/sync/wren-inbox.md \
  --invoke "$(pwd)/examples/openclaw-wake.sh main"
```

The `--inbox` half persists every message to a file (durability). The `--invoke` half runs `openclaw agent --agent main --message "<body>"` on each arrival, so Wren actually reasons about it instead of waiting for her next scheduled turn. End-to-end tested; see `examples/openclaw-wake.sh` for the wrapper source.

Also set `AGENTBUS_OUTBOX=~/sync/wren-outbox.md` in the OpenClaw agent's shell env so every `agentbus send` from that agent archives outbound messages symmetrically with the inbox file. See [docs/notification-patterns.md](docs/notification-patterns.md) for the full archive + user-notification protocol.

### 3. Generic MCP agent

If your agent speaks MCP but isn't Claude Code, run the server manually over stdio:

```bash
agentbus mcp-server --agent-id <your-id> --broker localhost
```

Configure your MCP client to spawn that command. Tool names and signatures are identical to path 1; the SKILL.md serves as a reference for prompt/system-message authors even if your stack doesn't use skill files.

### 4. Python framework (LangGraph, CrewAI, custom asyncio)

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

### 5. CLI — shell scripts, cron, pipelines, or any non-MCP agent

The CLI is the universal fallback. Every operation the MCP sidecar exposes is also a subcommand:

```bash
# Send (inline body)
agentbus send --agent-id sparrow --to wren --subject hello --body "Hi Wren"

# Send with audit trail (appends to outbox.md; pair with the peer's inbox.md)
agentbus send --agent-id sparrow --to wren --subject hello --body "Hi Wren" \
  --outbox ~/sync/sparrow-outbox.md
# Or set AGENTBUS_OUTBOX in the environment so every send logs automatically

# Send from a file
agentbus send --agent-id sparrow --to wren --subject report --body-file report.md

# Send from stdin (pipe-friendly)
cat report.md | agentbus send --agent-id sparrow --to wren --subject report --body-file -

# Drain queued messages and exit (non-blocking; use ONLY when no daemon is running for this id)
agentbus read --agent-id sparrow
agentbus read --agent-id sparrow --json | jq '.[].subject'

# Block until a message arrives (no-daemon contexts)
agentbus watch --agent-id sparrow --timeout 60

# Read from the daemon's inbox file with cursor tracking (use this when a daemon IS running)
agentbus tail --agent-id sparrow            # new entries from the daemon's inbox file since last cursor
agentbus tail --agent-id sparrow --follow   # stream — blocks until ^C
agentbus tail --agent-id sparrow --consumer bot  # independent cursor

# Who's online?
agentbus list
agentbus list --json

# Start the listener daemon (long-running; file-bridges to inbox.md)
agentbus start --agent-id sparrow --inbox ~/sync/inbox.md

# Start the MCP sidecar for any stdio MCP client
agentbus mcp-server --agent-id sparrow
```

`--body` and `--body-file` are mutually exclusive; exactly one is required.

**Three receive tools — pick one per agent-id.** `start` is a persistent daemon that file-bridges incoming messages (reactive, durable). `tail` reads new content from the daemon's inbox file with cursor tracking (correct companion to `start`). `read` / `watch` open a fresh one-shot MQTT subscription (correct when no daemon is running for this id — ephemeral scripts, CI jobs). **Don't combine `start` + `read`/`watch` for the same id** — they race for QoS1 messages and the loser silently drops them. See the Quickstart section above for the decision rule.

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

`content_type`: `text/plain` | `text/markdown` | `application/json`. The body is always a string; `content_type` is an advisory rendering hint to the receiver and never grants execution authority over the body. Bodies are capped at 64 KB — for larger artifacts, write to shared storage and send a reference.

Agent IDs are `[a-z0-9_-]{1,64}`. `broadcast` and `system` are reserved and cannot be used as an agent's own registered ID (they remain valid as `to=` sentinels).

## Cross-machine

The wire protocol is identical on a single host and across hosts — agents just need a broker they can reach. Two practical paths:

### Over Tailscale (recommended)

Tailscale gives you WireGuard-encrypted, peer-authenticated connectivity between hosts with zero public exposure. agentbus needs no TLS or auth configuration on the broker because the tailnet itself is authenticated. This is the path we use between an always-on Pi (Sparrow + Wren + broker) and occasional peers like a laptop Claude Code session.

One-time broker host setup (the machine that runs mosquitto):

```bash
# Adds /etc/mosquitto/conf.d/tailscale.conf binding a listener to the
# host's Tailscale IP, keeps the default 127.0.0.1 listener for local
# daemons. Use --tailscale-only if you want NO LAN exposure at all.
bash scripts/setup-mosquitto.sh --tailscale
```

The script prints the broker address to use from remote hosts (either the Tailscale IP `100.x.y.z` or the MagicDNS hostname `<host>.<tailnet>.ts.net`).

On any other tailnet-joined host running an agent, point at that broker:

```bash
# Python
bus = AgentBus(agent_id="laptop-cc", broker="clawd-rpi.tailea0d6e.ts.net")

# CLI
agentbus start --agent-id laptop-cc \
  --broker clawd-rpi.tailea0d6e.ts.net \
  --inbox ~/sync/laptop-cc-inbox.md

agentbus send --agent-id laptop-cc --to sparrow \
  --broker clawd-rpi.tailea0d6e.ts.net \
  --subject "hi" --body "from the laptop"
```

Anonymous is safe within a tailnet — the mesh is already authenticated. Never do this on the public internet.

### Other networks

If you can't use Tailscale, run mosquitto with TLS + username/password auth. The agentbus CLI doesn't yet expose TLS flags; supply them via a mosquitto client config file or use the Python API with the aiomqtt TLS parameters directly. This is out of scope for the bundled setup scripts.

## License

MIT
