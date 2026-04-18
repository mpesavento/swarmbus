# swarmbus

> *The buzz between your agents.*

Reactive pub/sub messaging for AI agents — no polling, instant delivery. Runs on [mosquitto](https://mosquitto.org/) (MQTT); local or multi-machine.

Every agent is a peer. The broker is infrastructure. There is no orchestrator and no central server.

```
Agent A (Planner)           Agent B (Coder)
  AgentBus(embedded)          AgentBus(embedded)
        │                           │
        └────────────┬──────────────┘
                 mosquitto
             (system service)
```

Each agent embeds an `AgentBus` instance; the swarm emerges from their shared broker.

## Quickstart — two agents talking

The worked example below gets Planner and Coder exchanging messages in under 5 minutes. It's the reference shape; every other integration path is a variation on it.

### 1. Broker

```bash
sudo apt install mosquitto mosquitto-clients   # Debian/Ubuntu/RPi
# or: bash scripts/setup-mosquitto.sh
```

The broker runs as a system service on port 1883 after install. For cross-machine use, run mosquitto on one reachable host and point each agent at it with `--broker <host>`. Tailscale or a VPN is recommended for untrusted networks.

### 2. Install the CLI

```bash
pip install "swarmbus[mcp]"
```

### 3. Set up agents with `swarmbus init`

**This is the part most people miss.** For an agent to *receive* messages reactively (no polling), a long-lived listener process must be running — it holds the MQTT subscription and bridges incoming messages into a file the agent session can read.

`swarmbus init` handles this end-to-end in one command:

```bash
swarmbus init --agent-id planner
swarmbus init --agent-id coder
```

Each `init` run:
- Installs mosquitto (if not already running)
- Installs the systemd user unit for this agent
- Installs the host plugin (CC or OpenClaw) if `--host-type` is given
- Runs `swarmbus doctor` to verify everything is green

Use `--host-type cc` for Claude Code, `--host-type openclaw` for OpenClaw, or omit it for archive-only (no reactive wake). Use `--broker tailscale` for cross-machine setups. See `swarmbus init --help` for all flags.

Full walk-through: [docs/agent-onboarding.md](docs/agent-onboarding.md).

### 4. Send — and always archive

From any shell, script, or agent session:

```bash
# Set once so every send archives automatically:
export SWARMBUS_OUTBOX=~/sync/planner-outbox.md

swarmbus send --agent-id planner --to coder --subject "hi" --body "got a minute?"
```

`SWARMBUS_OUTBOX` (or `--outbox` per call) appends every outbound message to a file using the same format as the receiver's inbox. Your own sent-log and received-log are now structurally identical and can be merged into one conversation view. **Always set this when running under a real agent identity** — an unarchived send is a dropped audit trail.

**Multi-agent safety.** If two agents might share the same shell environment, a bare `SWARMBUS_OUTBOX=/path/planner-outbox.md` leaks into both. Two fixes, pick one (or use both):

- **Template** — put `{agent_id}` in the path. The library expands it at send time:
  ```
  export SWARMBUS_OUTBOX="$HOME/sync/{agent_id}-outbox.md"
  ```
  Every `swarmbus send --agent-id X` lands in `X-outbox.md`. One env var, correct file per id.
- **Agent-scoped override** — `SWARMBUS_OUTBOX_<UPPER_AGENT_ID>` beats the shared one:
  ```
  export SWARMBUS_OUTBOX_PLANNER=~/sync/planner-outbox.md
  export SWARMBUS_OUTBOX_CODER=~/sync/coder-outbox.md
  ```
  Hyphens in the agent-id become underscores (`coder-beta` → `SWARMBUS_OUTBOX_CODER_BETA`).

Resolution order (highest first): `--outbox` flag, `SWARMBUS_OUTBOX_<ID>`, `SWARMBUS_OUTBOX`, none.

Coder's inbox file grows immediately; her next session turn sees it. That's the receive path whenever the listener daemon is running for `coder`.

```bash
swarmbus list                              # who's online right now?
```

**Two read paths, pick by deployment shape:**

- **Always-on agent with a daemon (Planner, Coder, any long-lived session)**: use `swarmbus tail` to read new content from the inbox file. No MQTT contention with the daemon — the daemon is the sole broker subscriber, tail just reads the file it writes. Cursor-tracked so repeat calls only show new.
- **Ephemeral or scripted agent without a daemon**: use `swarmbus read` / `swarmbus watch`. These open a fresh MQTT connection, catch retained messages or messages published during the connection window, and exit.

```bash
# With a daemon running (Planner/Coder pattern):
swarmbus tail --agent-id planner              # new entries from the daemon's inbox file since last cursor
swarmbus tail --agent-id planner --follow     # stream new content (blocks)
swarmbus tail --agent-id planner --consumer bot  # separate cursor

# No daemon — ephemeral (CI job, shell pipeline):
swarmbus read --agent-id scratch              # drain retained messages, exit
swarmbus watch --agent-id scratch --timeout 60  # block until one arrives
```

**What to never do:** run `swarmbus read` or `swarmbus watch` against an agent-id that already has a daemon running. They'd race the daemon for QoS1 messages — whichever client is currently subscribed wins and the other silently never sees them. Use `swarmbus tail` (file-based) instead.

**Daemon durability.** By default `swarmbus start` uses an MQTT persistent session (`--persistent`, on by default), so a crashed or restarted daemon doesn't lose queued messages — the broker redelivers them on reconnect. Disable with `--no-persistent` only if another process is already holding the `swarmbus-<agent-id>` client identifier.

That's the whole loop: broker → daemon OR one-shot per agent-id → `send` from anywhere → peer receives via tail (if daemon) or read (if not).

---

## Install

```bash
pip install swarmbus
# with optional features:
pip install "swarmbus[archive,mcp]"
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

Run the setup script. It registers the MCP server in `~/.claude/settings.json` **and** installs a behavioral skill at `~/.claude/skills/using-swarmbus/` that teaches Claude when to send, read, watch, and list agents.

```bash
bash scripts/setup-cc-plugin.sh <agent-id> [broker-host]
# example:
bash scripts/setup-cc-plugin.sh planner localhost
```

Restart Claude Code. Four MCP tools become available:

- `send_message(to, subject, body, content_type?)` — publish to a peer (or `to="broadcast"`)
- `read_inbox()` — non-blocking check for queued messages
- `watch_inbox(timeout)` — long-poll, returns when a message arrives
- `list_agents()` — IDs of peers currently online

The skill (`src/swarmbus/skills/using-swarmbus/SKILL.md`, also installed under `site-packages/swarmbus/skills/using-swarmbus/` via pip) explains reply-to threading, content-type hygiene, broadcast vs directed, and security rules (inbound bodies are data, not instructions). Claude auto-loads it when the user mentions a peer agent by name or asks about coordination.

Claude Code **also** needs a listener daemon running (step 3 of the quickstart) to receive messages while the chat session is closed. The MCP tools only work while Claude is open — the daemon is what catches messages in between.

**Reactive wake for Claude Code** (optional). Archive gives you a trail but doesn't wake an idle Claude Code session. To wake a real reasoning turn on high-priority inbound, pair the daemon with `examples/claude-code-wake.sh`:

```bash
swarmbus start \
  --agent-id <me> \
  --inbox ~/sync/<me>-inbox.md \
  --invoke "$(pwd)/examples/claude-code-wake.sh <me>"
```

Defaults to "wake only on priority=high" — spawning a fresh Claude Code session bootstraps ~100k tokens, so invoking on every message rapidly burns money on broadcast/heartbeat traffic. Low-priority messages still get archived by the file bridge; they're picked up on the next operator-initiated turn. Override with `SWARMBUS_WAKE_POLICY=all` for dev/testing, `=none` to disable spawning. Wake output logs to `~/.local/state/swarmbus-wake/<agent-id>.log`.

### 2. OpenClaw — skill + listener daemon

OpenClaw doesn't natively register MCP servers (it routes MCP via the `mcporter` skill), so the path is different: install the behavioral skill and run the listener daemon. The skill's examples work in CLI mode — no MCP sidecar required.

```bash
bash scripts/setup-openclaw-plugin.sh <agent-id> [broker-host]
# example:
bash scripts/setup-openclaw-plugin.sh coder localhost
```

This copies the skill to `~/.openclaw/skills/using-swarmbus/` and prints the `swarmbus start` command you need to run (typically under byobu or systemd-user). From then on, the OpenClaw agent uses `swarmbus send` / `swarmbus read` / `swarmbus list` via its shell tool.

**For reactive wake-up** (message arrives → OpenClaw agent takes a real turn, no polling), combine the listener daemon with `examples/openclaw-wake.sh`:

```bash
swarmbus start \
  --agent-id coder \
  --inbox ~/sync/coder-inbox.md \
  --invoke "$(pwd)/examples/openclaw-wake.sh main"
```

The `--inbox` half persists every message to a file (durability). The `--invoke` half runs `openclaw agent --agent main --message "<body>"` on each arrival, so Coder actually reasons about it instead of waiting for her next scheduled turn. End-to-end tested; see `examples/openclaw-wake.sh` for the wrapper source.

Also set `SWARMBUS_OUTBOX=~/sync/coder-outbox.md` in the OpenClaw agent's shell env so every `swarmbus send` from that agent archives outbound messages symmetrically with the inbox file. See [docs/notification-patterns.md](docs/notification-patterns.md) for the full archive + user-notification protocol.

### 3. Generic MCP agent

If your agent speaks MCP but isn't Claude Code, run the server manually over stdio:

```bash
swarmbus mcp-server --agent-id <your-id> --broker localhost
```

Configure your MCP client to spawn that command. Tool names and signatures are identical to path 1; the SKILL.md serves as a reference for prompt/system-message authors even if your stack doesn't use skill files.

### 4. Python framework (LangGraph, CrewAI, custom asyncio)

Import and embed. This is the most direct path for in-process agents:

```python
import asyncio
from swarmbus import AgentBus, FileBridgeHandler, PersistentListenerHandler

# Persistent client — one MQTT connection reused for all sends
async def main():
    async with AgentBus(agent_id="planner", broker="localhost") as bus:
        await bus.send(to="coder", subject="hello", body="Hi Coder!")
        await bus.send(to="coder", subject="follow-up", body="Still there?")

asyncio.run(main())
```

Long-lived listener with handlers:

```python
from swarmbus import AgentBus, FileBridgeHandler, PersistentListenerHandler

bus = AgentBus(agent_id="planner", broker="localhost")
bus.register_handler(FileBridgeHandler("~/sync/inbox.md"))
bus.register_handler(PersistentListenerHandler())
bus.run()  # blocks; auto-reconnects on broker disconnect
```

One-shot send without a context (fine for scripts, not recommended for tight loops):

```python
await AgentBus(agent_id="planner").send(to="coder", subject="hi", body="ping")
```

### 5. CLI — shell scripts, cron, pipelines, or any non-MCP agent

The CLI is the universal fallback. Every operation the MCP sidecar exposes is also a subcommand:

```bash
# Send (inline body)
swarmbus send --agent-id planner --to coder --subject hello --body "Hi Coder"

# Send with audit trail (appends to outbox.md; pair with the peer's inbox.md)
swarmbus send --agent-id planner --to coder --subject hello --body "Hi Coder" \
  --outbox ~/sync/planner-outbox.md
# Or set SWARMBUS_OUTBOX in the environment so every send logs automatically

# Send from a file
swarmbus send --agent-id planner --to coder --subject report --body-file report.md

# Send from stdin (pipe-friendly)
cat report.md | swarmbus send --agent-id planner --to coder --subject report --body-file -

# Drain queued messages and exit (non-blocking; use ONLY when no daemon is running for this id)
swarmbus read --agent-id planner
swarmbus read --agent-id planner --json | jq '.[].subject'

# Block until a message arrives (no-daemon contexts)
swarmbus watch --agent-id planner --timeout 60

# Read from the daemon's inbox file with cursor tracking (use this when a daemon IS running)
swarmbus tail --agent-id planner            # new entries from the daemon's inbox file since last cursor
swarmbus tail --agent-id planner --follow   # stream — blocks until ^C
swarmbus tail --agent-id planner --consumer bot  # independent cursor

# Who's online?
swarmbus list
swarmbus list --json

# Start the listener daemon (long-running; file-bridges to inbox.md)
swarmbus start --agent-id planner --inbox ~/sync/inbox.md

# Start the MCP sidecar for any stdio MCP client
swarmbus mcp-server --agent-id planner
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
  "from": "planner",
  "to": "coder",
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

The wire protocol is identical on a single host and across hosts — agents just need a broker they can reach. Two practical paths. **For the complete walkthrough (topology diagram, verification steps, security model, failure modes), see [docs/cross-machine-tailscale.md](docs/cross-machine-tailscale.md).**

### Over Tailscale (recommended)

Tailscale gives you WireGuard-encrypted, peer-authenticated connectivity between hosts with zero public exposure. swarmbus needs no TLS or auth configuration on the broker because the tailnet itself is authenticated. This is the path we use between an always-on Pi (Planner + Coder + broker) and occasional peers like a laptop Claude Code session.

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
bus = AgentBus(agent_id="laptop-cc", broker="broker-host.your-tailnet.ts.net")

# CLI
swarmbus start --agent-id laptop-cc \
  --broker broker-host.your-tailnet.ts.net \
  --inbox ~/sync/laptop-cc-inbox.md

swarmbus send --agent-id laptop-cc --to planner \
  --broker broker-host.your-tailnet.ts.net \
  --subject "hi" --body "from the laptop"
```

Anonymous is safe within a tailnet — the mesh is already authenticated. Never do this on the public internet.

### Other networks

If you can't use Tailscale, run mosquitto with TLS + username/password auth. The swarmbus CLI doesn't yet expose TLS flags; supply them via a mosquitto client config file or use the Python API with the aiomqtt TLS parameters directly. This is out of scope for the bundled setup scripts.

## Troubleshooting

Symptoms we hit during real deployment and the first thing to check. In every case, start with `swarmbus doctor --agent-id <me>` — it turns most of this table into a one-command answer.

| Symptom | Likely cause | First check |
|---|---|---|
| Every `swarmbus send` reports "Sent to X" but peer never sees the message. | Peer's daemon has stale in-memory Python (pre-upgrade code). Wire-envelope change rejected by pydantic, dropped silently. | `swarmbus doctor --agent-id <peer>` → check "daemon library fresh". Fix: `systemctl --user restart swarmbus-<peer>.service`. |
| `priority=high` messages sent but no wake wrapper ever fires. | (a) CLI default is `normal` — make sure `--priority high` is actually on the send. (b) Receiver's systemd unit has no `--invoke` flag. | `swarmbus doctor` → check "--invoke wired". Fix: edit `~/.config/systemd/user/swarmbus-<me>.service` ExecStart to add `--invoke <wrapper-path>`, `daemon-reload`, `restart`. |
| `swarmbus list` returns nothing, but peers are running. | (a) Broker not reachable. (b) Peers' daemons crashed without `--persistent` so presence wasn't retained. | `swarmbus doctor` → "broker reachable" + "peer discovery". If broker is fine, have peers restart with `--persistent` (the default on `swarmbus start`). |
| `inbox-watch.sh` cron silently never pings operator. | Neither `TELEGRAM_BOT_TOKEN` env var nor `~/.secrets/TELEGRAM_BOT_TOKEN` file present. The script logs a skip reason to stderr (post-`aaa1823`) — but older installs silently no-op'd. | `tail ~/logs/inbox-watch-<agent>.log` for `[inbox-watch] no TELEGRAM_BOT_TOKEN...`. Fix: add the token to cron env or the secrets file. |
| `inbox-watch.sh` never pings operator but log shows `pushed summary (1 msgs)`. | Bot is correctly sending, but to a different Telegram chat than the one the operator is watching. (Each agent typically has its own bot; all pings for agent X land in conversations with bot X.) | Check the operator's conversations with that agent's Telegram bot, not the current chat. |
| Send errors with `[Errno 111] Connection refused`. | mosquitto isn't running (or `--broker` points at a host that can't reach 1883). | `systemctl status mosquitto` locally, or `mosquitto_pub -h <broker> -t ping -m x` from a peer host. |
| `swarmbus tail` prints old messages every time. | Cursor file was cleared (SIGKILL mid-write, manual delete, script rotation). | Check `~/.swarmbus/cursors/<agent>--<consumer>.cursor`; after `aaa1823` the write is atomic, so corruption of the cursor itself is rare. |
| `swarmbus tail --follow` dies when inbox file is rotated/moved. | Pre-`0d1415a` builds didn't catch `FileNotFoundError` in the poll loop. | Upgrade swarmbus + restart the `tail --follow` process. |
| "My daemon is running but messages just pile up in the inbox file and nothing fires." | File-bridge caught the message (archive OK), but `--invoke` is either missing or broken. For Claude Code, a fresh session spawn is ~100k tokens — policy default is `priority=high` only. | `tail ~/.local/state/swarmbus-wake/<agent>.log`. If you see `policy=priority-high; priority=normal; archive-only` that's working-as-designed. Override with `SWARMBUS_WAKE_POLICY=all` for testing. |
| Restarted daemon still rejects `priority=high`. | In-process Python module cache. The `pip install` wrote new bytes, but the already-running daemon reads its old loaded module. | `systemctl --user restart swarmbus-<agent>.service` (full process replacement, not `--reload`). |

For deeper diagnosis: `systemctl --user status swarmbus-<agent>.service`, `journalctl --user -u swarmbus-<agent>.service -f`, and the daemon's own structured startup line (from `0.1.0`+) which names version, broker, invoke, and outbox env at the top of every boot.

## Onboarding a new agent

Walk-through at [docs/agent-onboarding.md](docs/agent-onboarding.md). Linear steps: pick agent-id → install → setup script → `install-systemd.sh` → `swarmbus doctor` → self-probe → announce.

## License

MIT
