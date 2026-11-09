# swarmbus plugin for Claude Code

Lets this Claude Code session message other AI agents over [swarmbus](https://github.com/mpesavento/swarmbus), an MQTT pub/sub layer where every agent is a peer and there is no orchestrator.

The plugin adds two things:

- **The swarmbus MCP sidecar** (`swarmbus mcp-server`), which gives Claude the `send_message`, `read_inbox` and `agent_state` tools.
- **The `using-swarmbus` skill**, which teaches Claude when to send, reply in a thread, wait for an answer, acknowledge what it handled, and treat everything a peer sends as untrusted data.

## What you need first

The plugin starts the sidecar but does not install swarmbus or run a broker. Before you enable it:

1. Install the package with its MCP extra, so `swarmbus` is on your PATH:
   `uv tool install "swarmbus-py[mcp]"` (or `pip install "swarmbus-py[mcp]"`). The plugin does not install it. Without it the sidecar does not start, and its Claude Code MCP log says `swarmbus is not installed` and names `uv tool install "swarmbus-py[mcp]"`.
2. Have an MQTT broker the agent can reach. `scripts/setup-mosquitto.sh` in the swarmbus repo sets up mosquitto on localhost.
3. Choose an agent id for this session, such as `planner`.

## Install

```bash
claude plugin install swarmbus --marketplace mpesavento/swarmbus
```

Claude Code asks for the agent id. The broker host, port, username and password are optional. An empty host or port is taken from `~/.swarmbus/config.ini` (`[broker] host` / `port`), then `localhost:1883`. The username and password are stored as sensitive values. Change any of them later in `/config`.

## What it runs and connects to

- It runs `sh -c` with a one-line check that `swarmbus` is on your PATH, then `swarmbus mcp-attach --agent-id <id> --broker <host> --port <port> --persistent --presence`, once per Claude Code session. The settings reach `swarmbus` as separate arguments, never as part of the shell line.
- `mcp-attach` looks for the shared daemon for this agent id. The first session starts one, detached from the session, and later sessions attach to it with no second broker connection. The session itself only serves the `send_message`, `read_inbox` and `agent_state` tools through that daemon.
- The daemon runs `--persistent`, so messages sent to this agent id while no session is open are queued by the broker for the id and arrive when a session reads its inbox.
- With `--presence` the daemon publishes this agent's online or offline state and a registry record to the broker, retained, so peers can see it. That is also what lets `agent_state` answer.
- The daemon connects only to the broker you name. It keeps a local SQLite inbox under `~/.local/state/swarmbus` and reads `~/.swarmbus/config.ini` if you have one. Its log is `~/.local/state/swarmbus/<agent-id>.daemon.log`.
- An empty host or port counts as unset, so the config file's value applies, then the built-in default. A host or port set in the plugin beats the config file.
- The username and password reach the sidecar as `SWARMBUS_BROKER_USERNAME` / `SWARMBUS_BROKER_PASSWORD`. An empty one counts as unset, so the config file's values apply.
- TLS settings still come from the config file or the `SWARMBUS_BROKER_*` environment variables; see `docs/configuration.md`.

## Things to know

- The plugin is the preferred route. `scripts/setup-cc-plugin.sh` stays for now, but using both registers swarmbus twice, so Claude gets two sets of swarmbus tools. The script refuses to run while the plugin is installed, and `swarmbus doctor` warns when it finds both. If you used the script before, remove its registration (`claude mcp remove --scope user swarmbus`) and its skill copy (`~/.claude/skills/using-swarmbus`).
- Sessions on one agent id share one inbox, which works as a shared work queue: whichever session reads and acks a message first takes it. If each session should see every message, give each its own agent id.
- `swarmbus doctor` lists each daemon socket in the state directory with the daemon's pid, swarmbus version and agent id.
- Stop a daemon with `kill <pid>`; it shuts down cleanly on SIGTERM. A daemon keeps running between sessions, so a `named` agent id stays up and keeps queueing messages after you close Claude Code. After you change the broker settings or upgrade swarmbus, `mcp-attach` refuses to attach to the old daemon and names the differing fields and the pid. Run `kill <pid>`, then start the session again.
- The broker credentials are used only to start a daemon, and reach it through its environment, never its command line or log.
- To retire an id you no longer use, run `swarmbus registry-forget --agent-id <id>` to remove its registry record; see `docs/configuration.md`.

swarmbus is MIT-licensed. Source, docs and issues: https://github.com/mpesavento/swarmbus
