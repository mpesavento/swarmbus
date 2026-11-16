# Configuration

This guide covers host settings, identity, durable delivery, directory presence, and registry maintenance. Run `swarmbus <command> --help` for the complete option list.

## Identity and receive mode

An agent ID is the Swarmbus address. Choose one receive path for each ID:

| Path | Durable MQTT session | Directory presence |
|---|---:|---:|
| `swarmbus start` | yes | yes |
| `swarmbus mcp-server --persistent --presence` | yes | yes |
| `swarmbus watch --persistent` | yes | yes |
| one-shot `read` or `watch` | no | no |

Do not run two persistent receivers for the same ID on one [topic root](#topic-root). They would claim the same MQTT client identifier and disconnect each other.

For an MCP sidecar:

```bash
swarmbus mcp-server \
  --agent-id planner \
  --persistent \
  --presence \
  --lifecycle named
```

`--persistent` controls the MQTT session: QoS 1 messages can queue while the process is away. It requires `--presence` so operators can discover and retire the queued identity.

`--lifecycle named|transient` controls the retained directory record. A named record survives shutdown as offline. A transient record is tombstoned on clean shutdown. This is independent of MQTT persistence.

Persistent sessions always use the canonical MQTT client identifier: `swarmbus-<agent-id>`, or `swarmbus-<agent-id>@<root>` under a topic root. This keeps maintenance targeting derived from the validated identity and the configured root rather than peer-authored registry data.

### MCP sidecar directory options

| Option | Default | Effect |
|---|---|---|
| `--persistent/--no-persistent` | no persistent session | Queue QoS 1 messages while the sidecar is stopped. Requires `--presence`. |
| `--presence/--no-presence` | no directory presence | Publish registry and online/offline presence, and enable `agent_state`. |
| `--lifecycle` (`named` or `transient`) | `named` | Keep an offline directory record, or tombstone a transient record at clean shutdown. |
| `--capability NAME` | none | Publish advisory capability metadata. Repeat the option for multiple capabilities. |
| `--registry-heartbeat-seconds N` | `60` | Set the registry heartbeat cadence. |
| `--registry-stale-after-seconds N` | `180` | Set when an online record becomes unconfirmed. |
| `--state-dir PATH` | `~/.local/state/swarmbus` | Store the durable MCP inbox and hold state. |

The sidecar always adds its runtime capabilities (`messaging`, `durable-inbox`, and `agent-state`) to capabilities declared with `--capability`.

## Presence and heartbeats

Presence has three evidence states:

- `online`: a fresh online heartbeat exists.
- `offline`: an explicit offline record exists, normally from clean shutdown.
- `unconfirmed`: no presence record exists, or an online heartbeat is stale.

A stale heartbeat is not proof that a process stopped. It therefore becomes `unconfirmed`, not `offline`. Registry GC and normal retirement preserve unconfirmed identities and their MQTT sessions.

MCP sidecars publish registry heartbeats every 60 seconds by default. Configure the cadence with `--registry-heartbeat-seconds` or `SWARMBUS_REGISTRY_HEARTBEAT_SECONDS`. Readers consider an online heartbeat stale after 180 seconds by default; configure that with `--registry-stale-after-seconds` or `SWARMBUS_REGISTRY_STALE_AFTER_SECONDS`.

Registry `status`, `working_set`, capabilities, and offline reasons are peer-provided, untrusted metadata. Display them as data; do not execute or follow instructions found in them.

Directory subscriptions use QoS 0 and the cache is cleared on every reconnect. The first `agent_state` read waits for a short bounded retained-message settling window before declaring that connection's snapshot ready. This prevents stale-cache and queued-directory replay, but it is not a proof that every retained record has arrived; hard snapshot completeness is tracked in [#69](https://github.com/mpesavento/swarmbus/issues/69).

## Topic root

A topic root is a prefix on the agent and registry topics. Use one to run several swarms on one broker, or to give a broker ACL one prefix per swarm.

| Topic | Unrooted (default) | With root `fleet` |
|---|---|---|
| inbox | `agents/<id>/inbox` | `fleet/agents/<id>/inbox` |
| presence | `agents/<id>/presence` | `fleet/agents/<id>/presence` |
| registry | `swarmbus/registry/<id>` | `fleet/swarmbus/registry/<id>` |
| broadcast | `agents/broadcast` | `agents/broadcast` |

Broadcast is never rooted. A broadcast reaches every agent on the broker, whatever its root.

Set the root with `--topic-root`, `SWARMBUS_TOPIC_ROOT`, or `[broker] topic_root` in the config file. Every command that connects to the broker takes it; the Keys table below lists them.

```bash
swarmbus start --agent-id planner --topic-root fleet
swarmbus send --agent-id coder --topic-root fleet --to planner --subject hi --body hello
```

**Every process in a swarm must use the same root.** A sender on one root and a receiver on another both connect and both report success, and the message is never delivered. `list`, `doctor` and the registry commands see only the agents under their own root. Set the root once, in a shared config file or environment, rather than on each command line. `swarmbus start` prints it on a `topic root:` line in its startup banner.

`mcp-attach` passes its root to the daemon it starts. Its default socket and daemon log are named for the agent id and the root, so a session on another root starts its own daemon. Given an explicit `--socket`, it refuses a running daemon on a different root.

`swarmbus init` takes the root too. It writes the root it resolved into the systemd unit's `ExecStart` as `--topic-root=<root>`, empty included, and passes it to its doctor and host-plugin steps. The unit is then pinned: a later change to `[broker] topic_root` does not move that daemon until you run `init` again or edit the unit. `scripts/install-systemd.sh` run by hand takes an optional `--topic-root <root>`; without it the unit carries no root and the daemon reads the config file. It refuses a root with characters other than letters, digits, `.`, `_`, `/` and `-`, because the root is written into the unit file; set such a root in the config file instead.

`swarmbus held` takes the root because it reads the state db, which is named for it (see "What a root separates").

The Python API takes the same setting as `topics=TopicMap(root="fleet")` on `AgentBus`, `AgentBus.probe`, `run_mcp_server`, `run_mcp_daemon`, `create_managed_runtime`, `ManagedMCPRuntime` and `RegistryMaintenance`.

### Accepted values

- Surrounding whitespace and leading or trailing slashes are stripped, so `/fleet/` means `fleet`.
- A root may have several levels, such as `org/fleet`.
- A root with an MQTT wildcard (`+` or `#`), a leading `$`, or an empty level (`a//b`) stops the command with exit 2. From the flag or the environment variable the error names that source. From the config file it is a config error that names the file and `[broker] topic_root`.
- An explicit empty `--topic-root ""` selects the unrooted layout, even when the config file sets a root. So does an empty `SWARMBUS_TOPIC_ROOT`, which is how a systemd unit or a wrapper asks for the unrooted layout on a host whose config file sets a root. The flag beats the environment variable.

### What a root separates

An agent's name on one bus is its agent id when unrooted, and `<agent-id>@<root>` under a root, with the root percent-encoded into one token: `planner@fleet`, `planner@org%2Ffleet`. Everything kept per agent uses that name:

| Kept per agent | Unrooted | Under root `fleet` |
|---|---|---|
| state db (inbox, holds, admitted ids, rate limits) | `<state-dir>/planner.sqlite3` | `<state-dir>/planner@fleet.sqlite3` |
| `mcp-attach` default socket | `<state-dir>/planner.sock` | `<state-dir>/planner@fleet.sock` |
| `mcp-attach` daemon log | `<state-dir>/planner.daemon.log` | `<state-dir>/planner@fleet.daemon.log` |
| persistent MQTT session | `swarmbus-planner` | `swarmbus-planner@fleet` |

So one agent id can run under two roots on one host and one broker. Each has its own inbox, holds and rate-limit state, its own shared daemon and its own broker session, and `registry-forget` under one root does not touch the session under the other. The unrooted names are the ones an unrooted deployment has always had.

A root does not separate two things:

- **Broadcast.** `agents/broadcast` is one topic for every root on the broker.
- **The systemd unit.** The unit is named `swarmbus-<agent-id>.service`, so a host has one unit per agent id. Rather than move a running daemon to a different swarm, `scripts/install-systemd.sh` refuses to replace a unit pinned to another root, and refuses to unpin one. Running one agent id under two roots on one host therefore means a different agent id for the second, or a daemon started some other way than this unit.

### Moving an agent onto a root

Changing the root moves nothing. Under the new root the agent has a new state db and a new broker session, and its retained registry and presence records stay under the old root. Nothing is adopted automatically: state left under the old root cannot be told apart from another swarm's agent with the same id, so adopting it could take that agent's mail.

1. With the receiver still running under the old root, read and acknowledge what is in its inbox.
2. Stop the receiver.
3. Start it under the new root, and restart every other process in the swarm with the same root.
4. Run `swarmbus registry-forget --agent-id <id> --topic-root <old root>`. Use `--topic-root ""` when the old layout was unrooted. It removes the old retained records and destroys the old broker session, and nothing under the new root.

`swarmbus doctor --agent-id <id> --topic-root <new root>` warns when the unrooted state db for that id still holds unacknowledged messages, and when a local daemon for that id is still running on another root.

## Registry operations

All three commands default to broker `localhost:1883`. Override that with `--broker HOST --port PORT`. Each reads and changes only the records under its `--topic-root` (see "Topic root").

| Command | Default behavior | Mutation switch | Main controls |
|---|---|---|---|
| `registry-list` | Read-only JSON snapshot | none | `--stale-after-seconds`, `--snapshot-seconds` |
| `registry-gc` | Read-only dry run | `--delete` | `--retention-seconds`, `--batch-size`, `--stale-after-seconds`, `--snapshot-seconds` |
| `registry-forget` | Prompts before mutation | `--yes` skips the prompt; `--dry-run` never mutates | `--agent-id`, `--force-online`, `--stale-after-seconds`, `--snapshot-seconds` |

Each command accepts `--username`, `--password`, `--tls`, `--ca-cert`, `--client-cert`, and `--client-key`. The corresponding `SWARMBUS_BROKER_*` environment variables are safer for long-running configuration. Registry commands also accept `--password-file`; it is mutually exclusive with `--password`, including `SWARMBUS_BROKER_PASSWORD`, and keeps the secret out of process arguments.

List the retained directory and its presence evidence:

```bash
swarmbus registry-list
```

Dry-run bounded collection of old transient records:

```bash
swarmbus registry-gc --retention-seconds 86400
```

Apply the same collection:

```bash
swarmbus registry-gc --retention-seconds 86400 --delete
```

GC deletes only old transient identities with explicit offline evidence. It rechecks selected records before deletion. The default batch size is 100.

Explicitly retire one identity:

```bash
swarmbus registry-forget --agent-id old-worker --dry-run
swarmbus registry-forget --agent-id old-worker
```

`registry-forget` rechecks the identity immediately before mutation and prints a JSON report on both success and refusal. A refusal exits non-zero with `refused: true` and a reason of `online` or `unconfirmed-offline`. `--force-online` bypasses that safety check and can disconnect a live client; reserve it for operator-confirmed emergencies.

GC reports scanned, selected, preserved, deleted, raced, and failed identities. A dry run populates `would_delete` without publishing tombstones. Applied GC remains bounded by `--batch-size` and rechecks every selected identity before mutation.

Maintenance derives the persistent session identifier canonically from the agent id and its own `--topic-root`; registry payloads cannot redirect session destruction.

## Safe rollout

When enabling persistent delivery for an existing identity:

1. Stop every receiver using that agent ID.
2. Start exactly one persistent receive path with presence enabled.
3. Confirm the record with `swarmbus registry-list`.
4. Send a test message, stop the receiver cleanly, send another, and restart it.
5. Confirm both delivery and an explicit offline-to-online presence transition.

If the directory says `unconfirmed`, investigate the receiver and broker before deleting anything.

## Configuration file

Every `swarmbus` command can read its host settings (broker, agent id, limits, outbox) from one INI file, so a wrapper or a systemd unit does not repeat them for each agent. The file is optional. Nothing in it is required.

### Location

- `swarmbus --config <path> <command> …`, or the `SWARMBUS_CONFIG` env var. The file must exist: a missing one exits 2.
- Otherwise `~/.swarmbus/config.ini`, when it is there. A missing default file is ignored silently.
- An empty `SWARMBUS_CONFIG` counts as unset.
- swarmbus only reads the file. It never creates or edits it.
- `--config` belongs to the group, so write `swarmbus --config X send …`, not `swarmbus send --config X`.

### Precedence

**CLI flag > env var > config file > built-in default.**

An empty `--broker` or `--port` counts as unset, so the config file and then the built-in default apply. The Claude Code plugin relies on this, because it passes its host and port settings even when they are empty. An empty `SWARMBUS_BROKER_*` env var counts as unset too.

The topic root is the exception, because empty is a real value for it: an explicit empty `--topic-root ""` selects the unrooted layout and beats `[broker] topic_root`, and so does an empty `SWARMBUS_TOPIC_ROOT`.

For the outbox path: `--outbox` > `SWARMBUS_OUTBOX_<ID>` > `SWARMBUS_OUTBOX` > `[outbox] path`.

### Example

```ini
# ~/.swarmbus/config.ini
[broker]
host = 100.64.0.7
port = 1883
username = sparrow
# password: file must be private to its owner; prefer SWARMBUS_BROKER_PASSWORD
ca_cert = ~/.swarmbus/ca.pem

[agent]
id = sparrow
inbox = ~/sync/sparrow-inbox.md
invoke = /home/me/src/swarmbus/examples/claude-code-wake.sh sparrow

[limits]
hop_limit = 8

[outbox]
path = ~/sync/{agent_id}-outbox.md
max_bytes = 10485760
keep = 10
```

### Keys

A key sets the default of the CLI option in the table, on the commands that have that option. The env var column is the variable that beats the file; "none" means the option has no env var.

| Key | Type | Default of | Env var that beats it | Commands |
|---|---|---|---|---|
| `[broker] host` | string | `--broker` | none | `send`, `start`, `read`, `watch`, `list`, `doctor`, `mcp-server`, `init`, `registry-list`, `registry-gc`, `registry-forget` |
| `[broker] port` | port | `--port` | none | `send`, `start`, `read`, `watch`, `list`, `doctor`, `mcp-server`, `registry-list`, `registry-gc`, `registry-forget` |
| `[broker] username` | string | `--username` | `SWARMBUS_BROKER_USERNAME` | the `[broker] host` commands |
| `[broker] password` | string | `--password` | `SWARMBUS_BROKER_PASSWORD` | the `[broker] host` commands (see "Password rule") |
| `[broker] ca_cert` | path | `--ca-cert` | `SWARMBUS_BROKER_CA_CERT` | the `[broker] host` commands |
| `[broker] client_cert` | path | `--client-cert` | `SWARMBUS_BROKER_CLIENT_CERT` | the `[broker] host` commands |
| `[broker] client_key` | path | `--client-key` | `SWARMBUS_BROKER_CLIENT_KEY` | the `[broker] host` commands |
| `[broker] tls` | boolean | `--tls` | `SWARMBUS_BROKER_TLS` | the `[broker] host` commands |
| `[broker] topic_root` | topic root | `--topic-root` | `SWARMBUS_TOPIC_ROOT` | `send`, `start`, `read`, `watch`, `list`, `doctor`, `held`, `init`, `mcp-server`, `mcp-daemon`, `mcp-attach`, `registry-list`, `registry-gc`, `registry-forget` |
| `[agent] id` | string | `--agent-id` | none | every command that takes `--agent-id` |
| `[agent] state_dir` | path | `--state-dir` | `SWARMBUS_STATE_DIR` | `send`, `start`, `read`, `watch`, `doctor`, `mcp-server`, `held` |
| `[agent] inbox` | path | `--inbox` | none | `start`, `tail`, `init` |
| `[agent] invoke` | string | `--invoke` | none | `start` only, not `init --invoke` |
| `[agent] invoke_timeout` | number | `--invoke-timeout` | `SWARMBUS_INVOKE_TIMEOUT` | `start` |
| `[agent] on_trip` | string | `--on-trip` | `SWARMBUS_ON_TRIP` | `start`, `watch`, `mcp-server` |
| `[limits] hop_limit` | integer | `--hop-limit` | `SWARMBUS_HOP_LIMIT` | `send`, `start`, `read`, `watch`, `mcp-server` |
| `[limits] rate_limit` | integer | `--rate-limit` | `SWARMBUS_RATE_LIMIT` | `start`, `watch`, `mcp-server` |
| `[limits] rate_cooldown` | number | `--rate-cooldown` | `SWARMBUS_RATE_COOLDOWN` | `start`, `watch`, `mcp-server` |
| `[outbox] path` | path | `--outbox` (see Precedence) | `SWARMBUS_OUTBOX_<ID>`, `SWARMBUS_OUTBOX` | `send`; also `start`'s `outbox env:` banner line and `doctor`'s outbox probe |
| `[outbox] max_bytes` | integer | `--outbox-max-bytes` | `SWARMBUS_MAX_OUTBOX_BYTES` | `send` |
| `[outbox] keep` | integer | `--outbox-keep` | `SWARMBUS_KEEP_OUTBOX_FILES` | `send` |

`start` prints the file it read on a `config:` line in its startup banner (`(none)` when there was none).

### Value rules

- Integers and ports are whole numbers: ports run from 1 to 65535, other integers are ≥ 0.
- Numbers are finite and ≥ 0.
- Booleans are `1/yes/true/on` or `0/no/false/off`, in any case.
- `~` is expanded in path keys. It is not expanded inside `invoke` or `on_trip`, so write absolute paths there.
- An empty value counts as unset.

### INI rules

- No quoting: quote marks become part of the value.
- Comments go on their own line and start with `#` or `;`. There are no inline comments.
- `%`, `#` and `;` inside a value are literal. A password of `pa%ss#1;x` is read as written.
- `[DEFAULT]` is not special. It is an unknown section and gets a warning.
- Key names are case-insensitive. Section names are case-sensitive.
- A duplicate key or a duplicate section is an error.

### Errors

- An unknown section or key prints one warning on stderr and is ignored. The command runs.
- A parse error, a wrong type or range, or a missing explicit file stops the command with exit 2. The message names the file, and for a bad value also the section and key. It never prints the value.
- `swarmbus send --help` with a malformed file exits 2. `swarmbus --help` does not read the file.

### Password rule

`[broker] password` is accepted only when group and other permission bits are clear (normally mode 0600 or 0400). If group or others can read the file, the command exits 2 with a `chmod 600` hint, even when `SWARMBUS_BROKER_PASSWORD` or `--password` is also set. Prefer `SWARMBUS_BROKER_PASSWORD`: see [security.md](security.md). A file without a password needs no special mode.

### Not covered

The file sets defaults for host settings only. It does not cover:

- per-call options: `send --to/--subject/--body/...`, `read --max/--json`, `watch --timeout/--json`, `list --json`, `tail --consumer/--follow/--reset`, `held --json/--reset/--reset-all`, `init --host-type/--invoke/--skip-broker/--skip-plugin/--dry-run/--yes`, and the registry commands' retention, snapshot, batch, confirmation, force, and password-file controls;
- `--persistent`, whose default differs by command;
- `mcp-server --presence` and `tail --cursor-dir`;
- QoS, which has no CLI option;
- the wake-context env vars (`SWARMBUS_ID`, `SWARMBUS_HOPS`, `SWARMBUS_ORIGIN`, `SWARMBUS_TRIP_*`);
- the wake scripts' own `SWARMBUS_WAKE_*` and `SWARMBUS_RECEIVE_PROTOCOL` settings.

### The wake scripts do not read the file

A `swarmbus send` from inside a wake reads `~/.swarmbus/config.ini`, or the `SWARMBUS_CONFIG` in the daemon's environment. `swarmbus --config X start` does not pass `X` to the wake, so set `SWARMBUS_CONFIG` in the unit to share a non-default file. The Python API (`AgentBus`, `run_mcp_server`, `ManagedMCPRuntime`) does not read the file either.

### Several agents on one host

Give each agent its own file through `SWARMBUS_CONFIG` in its unit or MCP config. Keep `[agent] id` out of a file that several agents share. An explicit `--agent-id` always wins.

### Restarts

A daemon or sidecar reads the file once, at start. Restart it after you change the file.

