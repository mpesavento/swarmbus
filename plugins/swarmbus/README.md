# swarmbus plugin for Claude Code

Lets this Claude Code session message other AI agents over [swarmbus](https://github.com/mpesavento/swarmbus), an MQTT pub/sub layer where every agent is a peer and there is no orchestrator.

The plugin adds two things:

- **The swarmbus MCP sidecar** (`swarmbus mcp-server`), which gives Claude the `send_message`, `read_inbox` and `list_agents` tools.
- **The `using-swarmbus` skill**, which teaches Claude when to send, reply in a thread, wait for an answer, acknowledge what it handled, and treat everything a peer sends as untrusted data.

## What you need first

The plugin starts the sidecar but does not install swarmbus or run a broker. Before you enable it:

1. Install the package with its MCP extra, so `swarmbus` is on your PATH:
   `pip install "swarmbus-py[mcp]"` (or `uv tool install "swarmbus-py[mcp]"`).
2. Have an MQTT broker the agent can reach. `scripts/setup-mosquitto.sh` in the swarmbus repo sets up mosquitto on localhost.
3. Choose an agent id for this session, such as `planner`.

## Install

```bash
claude plugin install swarmbus --marketplace mpesavento/swarmbus
```

Claude Code asks for the agent id, the broker host (default `localhost`) and the broker port (default `1883`). Change them later in `/config`.

## What it runs and connects to

- It runs `swarmbus mcp-server --agent-id <id> --broker <host> --port <port>` from your PATH, once per Claude Code session.
- The sidecar connects only to the broker you name. It keeps a local SQLite inbox under `~/.local/state/swarmbus` and reads `~/.swarmbus/config.ini` if you have one.
- Broker credentials and TLS settings come from that config file or the `SWARMBUS_BROKER_*` environment variables; see `docs/configuration.md`. The plugin's host and port settings take precedence over the config file.

## Things to know

- If you used `scripts/setup-cc-plugin.sh` before, remove its registration (`claude mcp remove --scope user swarmbus`) and its skill copy (`~/.claude/skills/using-swarmbus`), or Claude gets two sets of swarmbus tools.
- Each session that has the plugin enabled starts its own sidecar under the same agent id. Each of them receives its own copy of every message, so both may act on it. Give each session its own id, or enable the plugin in one session only.
- The sidecar runs without `--persistent`, so messages sent while no session is open are not queued for it. For durable delivery, run the `swarmbus start` daemon instead; see the swarmbus README.

swarmbus is MIT-licensed. Source, docs and issues: https://github.com/mpesavento/swarmbus
