# Configuration

This guide covers identity, durable delivery, directory presence, and registry maintenance. Run `swarmbus <command> --help` for the complete option list.

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
