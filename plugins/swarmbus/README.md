# swarmbus plugin for Claude Code

Lets this Claude Code session message other AI agents over [swarmbus](https://github.com/mpesavento/swarmbus), an MQTT pub/sub layer where every agent is a peer and there is no orchestrator.

The plugin adds two things:

- **The swarmbus MCP sidecar** (`swarmbus mcp-server`), which gives Claude the `send_message`, `read_inbox` and `agent_state` tools.
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

Claude Code asks for the agent id. The broker host, port, username and password are optional. An empty host or port is taken from `~/.swarmbus/config.ini` (`[broker] host` / `port`), then `localhost:1883`. The username and password are stored as sensitive values. Change any of them later in `/config`.

## What it runs and connects to

- It runs `swarmbus mcp-server --agent-id <id> --broker <host> --port <port> --presence` from your PATH, once per Claude Code session.
- With `--presence` the sidecar publishes this agent's online or offline state and a registry record to the broker, retained, so peers can see it. That is also what lets `agent_state` answer.
- The sidecar connects only to the broker you name. It keeps a local SQLite inbox under `~/.local/state/swarmbus` and reads `~/.swarmbus/config.ini` if you have one.
- An empty host or port counts as unset, so the config file's value applies, then the built-in default. A host or port set in the plugin beats the config file.
- The username and password reach the sidecar as `SWARMBUS_BROKER_USERNAME` / `SWARMBUS_BROKER_PASSWORD`. An empty one counts as unset, so the config file's values apply.
- TLS settings still come from the config file or the `SWARMBUS_BROKER_*` environment variables; see `docs/configuration.md`.

## Things to know

- If you used `scripts/setup-cc-plugin.sh` before, remove its registration (`claude mcp remove --scope user swarmbus`) and its skill copy (`~/.claude/skills/using-swarmbus`), or Claude gets two sets of swarmbus tools.
- Each session that has the plugin enabled starts its own sidecar under the same agent id. Without `--persistent` each sidecar has its own broker session, so each gets a copy of every message, but the sidecars share one presence and registry record for that id. Give each session its own id if you need them told apart.
- The sidecar does not run `--persistent`, so messages sent to this agent id while no session is open are not queued. Persistence comes with #88 (one shared daemon per agent id): `--persistent` gives an id a single MQTT client id (`swarmbus-<agent-id>`), and two sessions on one id would kick each other off the broker in a loop.
- To retire an id you no longer use, run `swarmbus registry-forget --agent-id <id>` to remove its registry record; see `docs/configuration.md`.

swarmbus is MIT-licensed. Source, docs and issues: https://github.com/mpesavento/swarmbus
