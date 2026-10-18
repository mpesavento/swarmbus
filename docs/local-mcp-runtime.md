# Shared local MCP runtime

Use the shared local runtime when a harness starts a fresh stdio MCP process for
each session but one agent identity must keep a single MQTT connection and
durable inbox.

For the usual one-process case, keep using `swarmbus mcp-server`. The daemon
and proxy commands are an optional deployment shape, not a replacement for the
direct server.

## Start the runtime and proxies

Create a private directory, then start one runtime owner:

```bash
install -d -m 700 ~/.local/state/swarmbus
swarmbus mcp-daemon --socket ~/.local/state/swarmbus/planner.sock \
  --agent-id planner --broker mqtt.example.com \
  --port 8883 --tls --persistent --presence
```

Configure each local MCP client to spawn only the credential-free frontend:

```bash
swarmbus mcp-proxy --socket ~/.local/state/swarmbus/planner.sock
```

Any number of proxies may share the daemon. They expose the same three MCP
tools as `mcp-server`; the private protocol is not an additional model-facing
tool surface.

Do not run another listener daemon or persistent MCP runtime for the same agent
ID. Two clients using the same durable MQTT identity will disconnect each
other.

## Runtime options

The three mode controls are independent:

| Option | Controls |
|---|---|
| `--lifecycle named|transient` | How the retained registry identity is retired |
| `--presence` | Presence publication and directory subscriptions |
| `--persistent` | MQTT session durability and offline QoS1 queueing |

A persistent MQTT session requires presence so its queued identity remains
discoverable and can be retired. A transient lifecycle also requires presence
because it governs cleanup of the retained directory record.

## Local security and ownership

The daemon uses an AF_UNIX socket and a sibling lock file. The socket parent
must already exist, be owned by the daemon UID, and have no group or other
permissions. The socket and lock are mode `0600`; peer credentials are checked
where the platform provides them.

Startup refuses unsafe ownership, a non-socket path, and an active socket.
A stale socket is removed only after the daemon holds the owner lock and proves
that no process accepts connections on the path. Shutdown removes only the
socket inode created by that daemon.

The local runtime is supported on Linux and macOS. Native Windows is not
supported; use WSL when a Windows host must run it.

## Failure behavior

There is no automatic fallback from a proxy to another broker connection.
If the daemon disappears, calls fail through the normal MCP error behavior
instead of creating a second runtime.

The private `ping` operation distinguishes local process availability from
broker readiness. It returns `ready`, `connected`, and `state`; `ready` is
true only while the managed runtime is connected. Runtime states are
`stopped`, `connecting`, `connected`, `store-degraded`, and `fatal`.

The public `read_inbox` MCP tool preserves its existing contract: a transport
failure is logged and returned as an empty list. Directory operations fail
with `TransportUnavailable` rather than returning stale state. Fatal runtime
and durable-store failures remain explicit errors.

## Controller inbox state

The private protocol also exposes `inbox_state` for local controllers. It is
not registered as an MCP tool and never returns message subjects or bodies.

A snapshot contains:

- a runtime-instance ID and monotonically changing generation;
- `runtime_state` and `connected`;
- pending count and truncation status;
- bounded pending message metadata: ID, sender, received time, and priority.

The generation changes after a committed insertion, acknowledgement, or
quarantine, and on broker connect, disconnect, or fatal failure. A waiting
controller therefore wakes for both durable inbox changes and source
availability changes. A daemon restart changes the instance ID and returns the current
snapshot with `reset: true`.

Snapshots do not consume or acknowledge mail. The agent acknowledges handled
messages later through `read_inbox(ack_ids=...)`.

For a concrete controller contract and Codex app-server dispatch semantics, see
[Using Swarmbus with Codex](codex.md).
