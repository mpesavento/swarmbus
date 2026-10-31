# Configuration file

Every `swarmbus` command can read its host settings (broker, agent id, limits, outbox) from one INI file, so a wrapper or a systemd unit does not repeat them for each agent. The file is optional. Nothing in it is required.

## Location

- `swarmbus --config <path> <command> …`, or the `SWARMBUS_CONFIG` env var. The file must exist: a missing one exits 2.
- Otherwise `~/.swarmbus/config.ini`, when it is there. A missing default file is ignored silently.
- An empty `SWARMBUS_CONFIG` counts as unset.
- swarmbus only reads the file. It never creates or edits it.
- `--config` belongs to the group, so write `swarmbus --config X send …`, not `swarmbus send --config X`.

## Precedence

**CLI flag > env var > config file > built-in default.**

For the outbox path: `--outbox` > `SWARMBUS_OUTBOX_<ID>` > `SWARMBUS_OUTBOX` > `[outbox] path`.

## Example

```ini
# ~/.swarmbus/config.ini
[broker]
host = 100.64.0.7
port = 1883
username = sparrow
# password: allowed only when this file is mode 0600; prefer SWARMBUS_BROKER_PASSWORD
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

## Keys

A key sets the default of the CLI option in the table, on the commands that have that option. The env var column is the variable that beats the file; "none" means the option has no env var.

| Key | Type | Default of | Env var that beats it | Commands |
|---|---|---|---|---|
| `[broker] host` | string | `--broker` | none | `send`, `start`, `read`, `watch`, `list`, `doctor`, `mcp-server`, `init` |
| `[broker] port` | port | `--port` | none | `send`, `start`, `read`, `watch`, `list`, `doctor`, `mcp-server` |
| `[broker] username` | string | `--username` | `SWARMBUS_BROKER_USERNAME` | the commands above |
| `[broker] password` | string | `--password` | `SWARMBUS_BROKER_PASSWORD` | the commands above (see "Password rule") |
| `[broker] ca_cert` | path | `--ca-cert` | `SWARMBUS_BROKER_CA_CERT` | the commands above |
| `[broker] client_cert` | path | `--client-cert` | `SWARMBUS_BROKER_CLIENT_CERT` | the commands above |
| `[broker] client_key` | path | `--client-key` | `SWARMBUS_BROKER_CLIENT_KEY` | the commands above |
| `[broker] tls` | boolean | `--tls` | `SWARMBUS_BROKER_TLS` | the commands above |
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

## Value rules

- Integers and ports are whole numbers: ports run from 1 to 65535, other integers are ≥ 0.
- Numbers are finite and ≥ 0.
- Booleans are `1/yes/true/on` or `0/no/false/off`, in any case.
- `~` is expanded in path keys. It is not expanded inside `invoke` or `on_trip`, so write absolute paths there.
- An empty value counts as unset.

## INI rules

- No quoting: quote marks become part of the value.
- Comments go on their own line and start with `#` or `;`. There are no inline comments.
- `%`, `#` and `;` inside a value are literal. A password of `pa%ss#1;x` is read as written.
- `[DEFAULT]` is not special. It is an unknown section and gets a warning.
- Key names are case-insensitive. Section names are case-sensitive.
- A duplicate key or a duplicate section is an error.

## Errors

- An unknown section or key prints one warning on stderr and is ignored. The command runs.
- A parse error, a wrong type or range, or a missing explicit file stops the command with exit 2. The message names the file, and for a bad value also the section and key. It never prints the value.
- `swarmbus send --help` with a malformed file exits 2. `swarmbus --help` does not read the file.

## Password rule

`[broker] password` is accepted only when the file's mode is 0600 or 0400. If group or others can read the file, the command exits 2 with a `chmod 600` hint, even when `SWARMBUS_BROKER_PASSWORD` or `--password` is also set. Prefer `SWARMBUS_BROKER_PASSWORD`: see [security.md](security.md). A file without a password needs no special mode.

## Not covered

The file sets defaults for host settings only. It does not cover:

- per-call options: `send --to/--subject/--body/...`, `read --max/--json`, `watch --timeout/--json`, `list --json`, `tail --consumer/--follow/--reset`, `held --json/--reset/--reset-all`, `init --host-type/--invoke/--skip-broker/--skip-plugin/--dry-run/--yes`;
- `--persistent`, whose default differs by command;
- `mcp-server --presence` and `tail --cursor-dir`;
- QoS and the topic root, which have no CLI option;
- the wake-context env vars (`SWARMBUS_ID`, `SWARMBUS_HOPS`, `SWARMBUS_ORIGIN`, `SWARMBUS_TRIP_*`);
- the wake scripts' own `SWARMBUS_WAKE_*` and `SWARMBUS_RECEIVE_PROTOCOL` settings.

## The wake scripts do not read the file

A `swarmbus send` from inside a wake reads `~/.swarmbus/config.ini`, or the `SWARMBUS_CONFIG` in the daemon's environment. `swarmbus --config X start` does not pass `X` to the wake, so set `SWARMBUS_CONFIG` in the unit to share a non-default file. The Python API (`AgentBus`, `run_mcp_server`, `ManagedMCPRuntime`) does not read the file either.

## Several agents on one host

Give each agent its own file through `SWARMBUS_CONFIG` in its unit or MCP config. Keep `[agent] id` out of a file that several agents share. An explicit `--agent-id` always wins.

## Restarts

A daemon or sidecar reads the file once, at start. Restart it after you change the file.
