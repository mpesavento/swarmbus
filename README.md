# agentbus

Reactive pub/sub messaging for AI agents — no polling, instant delivery. Built on MQTT, runs local, scales to multi-machine.

## What Is This

A lightweight messaging layer for agent-to-agent communication using MQTT pub/sub. Designed for local AI agent deployments (Raspberry Pi, homelab, dev machine) where multiple agents need to communicate without polling shared files or HTTP endpoints.

Current agent-to-agent OSS solutions don't exist at this layer. The closest prior art is file-based sync (15-minute cron latency) or full orchestration frameworks (LangGraph, AutoGen) that assume a single runtime. `agentbus` fills the gap: **reactive, decoupled, transport-only**.

## Why MQTT

- Zero polling — instant push on message arrival
- ~1MB broker (mosquitto), in most Linux package repos
- Identical agent code for local and cross-network deployments — one config value changes
- LLM-agnostic: doesn't care if your agent is Claude, OpenAI, a shell script, or something else
- Battle-tested IoT protocol (OASIS standard, 20+ years), not invented here

## Topic Schema

```
agents/{agent-id}/inbox      # directed messages TO this agent
agents/{agent-id}/presence   # online/offline announcements
agents/broadcast             # all-agents messages
```

## Message Envelope

```json
{
  "from": "sparrow",
  "to": "wren",
  "ts": "2026-04-13T21:00:00Z",
  "subject": "status update",
  "body": "...",
  "priority": "normal",
  "reply_to": null
}
```

Fields:
- `from` / `to`: agent IDs, arbitrary strings
- `ts`: ISO 8601 UTC
- `priority`: `normal` | `urgent`
- `reply_to`: message ID of the message being replied to, or null

## Agent Interface

```python
from agentbus import AgentBus

bus = AgentBus(agent_id="sparrow", broker="localhost")

# Send a message
bus.send(to="wren", subject="hello", body="You awake?")

# Subscribe and handle incoming messages
@bus.on_message
def handle(msg):
    print(f"[{msg.from_}] {msg.subject}: {msg.body}")

bus.listen()  # blocking
```

Shell wrappers also provided for agents that aren't Python.

## Handler Modes

Three options for what happens when a message arrives:

1. **File bridge** (simplest): subscriber writes to `inbox.md` → existing shell scripts handle it. Zero migration cost.
2. **Direct invocation**: subscriber calls `claude -p` or equivalent on message arrival. Sub-second agent wakeup.
3. **Persistent listener**: long-running agent process with MQTT listener. Most responsive, higher resource use.

## Backward Compatibility

v1 keeps file-based sync as a fallback. The MQTT subscriber can write received messages to a file (e.g. `sync/inbox.md`) so existing tooling still works during migration. Decoupled cutover.

## Cross-Machine

Change one config value:

```
MQTT_BROKER=localhost          # local
MQTT_BROKER=clawd-rpi.ts.net  # remote via Tailscale
```

Everything else is identical. Tailscale handles auth/encryption at the network layer.

## Design Decisions

| Decision | Choice | Rationale |
|---|---|---|
| Auth/TLS | Off by default | Tailscale covers local deployments; documented opt-in for exposed brokers |
| Message persistence | MQTT retain=true for inbox; SQLite for archive | Retain ensures agent gets last message on reconnect; SQLite for audit trail |
| Agent discovery | Auto-announce on presence topic at startup | No manual config; agents self-register |
| LLM coupling | Generic envelope, Claude default handler | Transport layer is LLM-agnostic; reference impl uses Claude |
| v1 scope | Shell wrappers + Python thin wrapper | Enough to replace file polling; avoid framework creep |

## Roadmap

- [x] Repo scaffolding
- [ ] mosquitto setup script + systemd service
- [ ] Python `AgentBus` class (send, subscribe, presence)
- [ ] Shell wrappers (`agentbus-send`, `agentbus-listen`)
- [ ] File bridge handler (inbox.md compatibility)
- [ ] `claude -p` handler
- [ ] SQLite message archive
- [ ] Example: Sparrow ↔ Wren on single Pi
- [ ] Example: two agents on different machines via Tailscale
- [ ] PyPI package

## Requirements

- Python 3.9+
- `mosquitto` broker (or any MQTT v3.1.1 / v5 broker)
- `paho-mqtt` Python client

```bash
sudo apt install mosquitto mosquitto-clients
pip install paho-mqtt
```

## License

MIT
