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

Do not run two persistent receivers for the same ID. They would claim the same MQTT client identifier and disconnect each other.

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

Persistent sessions always use the canonical `swarmbus-<agent-id>` MQTT client identifier. This keeps maintenance targeting derived from the validated identity rather than peer-authored registry data.

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

## Registry operations

All three commands default to broker `localhost:1883`. Override that with `--broker HOST --port PORT`.

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

Maintenance derives the persistent session identifier canonically as `swarmbus-<agent-id>`; registry payloads cannot redirect session destruction.

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
- QoS and the topic root, which have no CLI option;
- the wake-context env vars (`SWARMBUS_ID`, `SWARMBUS_HOPS`, `SWARMBUS_ORIGIN`, `SWARMBUS_TRIP_*`);
- the wake scripts' own `SWARMBUS_WAKE_*` and `SWARMBUS_RECEIVE_PROTOCOL` settings.

### The wake scripts do not read the file

A `swarmbus send` from inside a wake reads `~/.swarmbus/config.ini`, or the `SWARMBUS_CONFIG` in the daemon's environment. `swarmbus --config X start` does not pass `X` to the wake, so set `SWARMBUS_CONFIG` in the unit to share a non-default file. The Python API (`AgentBus`, `run_mcp_server`, `ManagedMCPRuntime`) does not read the file either.

### Several agents on one host

Give each agent its own file through `SWARMBUS_CONFIG` in its unit or MCP config. Keep `[agent] id` out of a file that several agents share. An explicit `--agent-id` always wins.

### Restarts

A daemon or sidecar reads the file once, at start. Restart it after you change the file.

