# Using Swarmbus with Codex

Codex can use Swarmbus in two modes:

1. **MCP only** — Codex can send, read, acknowledge, and discover peers during
   a turn. This works with stock Codex CLI.
2. **Reactive attention** — an external controller notices pending mail and
   starts or steers a Codex turn. Swarmbus supplies the durable inbox and a
   private metadata-only change feed; the controller is a separate integration.

Swarmbus does not ship a Codex attention controller. The MCP-only setup below is
turnkey. The reactive section documents the contract needed to build or operate
a controller without depending on a particular launcher.

## MCP-only setup

Install Codex CLI and Swarmbus with its MCP dependency:

```bash
python -m pip install "swarmbus-py[mcp]"
```

Register one Swarmbus stdio server with Codex:

```bash
codex mcp add swarmbus -- \
  swarmbus mcp-server \
  --agent-id planner \
  --broker mqtt.example.com \
  --port 8883 --tls \
  --persistent --presence

codex mcp list
```

Codex stores MCP configuration in `~/.codex/config.toml`; a trusted project
can instead carry `.codex/config.toml`. See the official
[Codex MCP documentation](https://developers.openai.com/codex/mcp).

Use a broker account and TLS configuration appropriate for your deployment.
Prefer `--password-file` or client certificates over putting a password in
Codex configuration. See [Security](security.md) for broker ACL and credential
examples.

### Install the behavioral skill

The MCP server exposes the tools. The bundled skill teaches Codex when to read,
acknowledge, reply, and publish state. From a Swarmbus checkout, install it for
your user with ordinary shell commands:

```bash
mkdir -p ~/.agents/skills/using-swarmbus
cp src/swarmbus/skills/using-swarmbus/SKILL.md \
  ~/.agents/skills/using-swarmbus/
```

The skill is optional; MCP works without it. For a repository-scoped install,
copy the file to `.agents/skills/using-swarmbus/SKILL.md` instead. Codex
discovers skills from these locations; see the official
[Codex skills documentation](https://developers.openai.com/codex/skills).

Restart Codex if the server or skill does not appear. A useful first prompt is:

> Load the using-swarmbus skill. List the online agents, read any pending
> messages, and acknowledge only messages you have handled.

The registered server exposes:

- `send_message` to send or reply;
- `read_inbox` to read and acknowledge durable messages;
- `agent_state` to list peers and publish status.

A read is non-destructive. After handling message IDs, acknowledge them with
`read_inbox(ack_ids=[...], max_messages=0)`. If Codex exits before that call,
the same IDs appear again on the next read.

### What MCP-only does not do

MCP tools are callable only while Codex is running a turn. A long-polling
`read_inbox(wait_seconds=30)` can wait inside an active turn, but a new
message does not wake an idle Codex session and cannot steer a turn already in
progress.

Use MCP-only mode when the operator starts turns, or when an existing scheduler
already invokes Codex. Use a reactive attention controller when inbound work
must become a turn without operator input.

## Shared runtime for multiple Codex sessions

A direct `mcp-server` owns one MQTT connection for the lifetime of the Codex
process. If several local Codex sessions share one logical agent identity, run
one supervised `mcp-daemon` and configure every Codex session to spawn a
credential-free `mcp-proxy`:

```bash
install -d -m 700 ~/.local/state/swarmbus

swarmbus mcp-daemon \
  --socket ~/.local/state/swarmbus/planner.sock \
  --agent-id planner \
  --broker mqtt.example.com \
  --port 8883 --tls \
  --persistent --presence
```

Then register the proxy:

```bash
codex mcp add swarmbus -- \
  swarmbus mcp-proxy \
  --socket ~/.local/state/swarmbus/planner.sock
```

Supervise the daemon independently of Codex. Do not run another persistent
receiver for the same agent ID; two MQTT clients claiming the same durable
identity disconnect each other. See [Shared local MCP runtime](local-mcp-runtime.md)
for ownership, health, and failure semantics.

All proxies share one inbox and acknowledgement state. If several Codex threads
may consume work concurrently, the launcher must choose which thread receives
each notification. Swarmbus does not assign messages to local threads.

## Reactive attention architecture

A reactive integration keeps message transport separate from Codex turn
control:

```text
                                     +--------------------+
Codex TUI <-> controller/proxy <----> Codex app-server    |
                    |                |  MCP: mcp-proxy ----+--+
                    |                +--------------------+  |
                    |                                          |
                    +-- inbox_state -- mcp-daemon <------------+
                                           |
                                           +-- MQTT broker
```

The two daemon clients have different purposes:

- Codex calls the public MCP tools through `mcp-proxy`.
- The controller calls the daemon's private `inbox_state` operation over the
  same local socket.

`inbox_state` returns only runtime health, a restart-aware cursor, pending
count, and bounded metadata: message ID, sender, receive time, and priority. It
does not return subjects or bodies, and it cannot consume or acknowledge mail.
Codex reads the actual envelopes through `read_inbox`.

The controller must either be the Codex app-server client or transparently
proxy the app-server connection used by the TUI. It needs current thread and
turn state; a second client that guesses from process state cannot safely decide
whether to start or steer a turn.

## Controller dispatch contract

A controller should implement this loop:

1. Initialize the Codex app-server client and track thread, turn, and pending
   human-input state from app-server traffic.
2. Call `inbox_state` with the last cursor and a bounded `wait_seconds`.
3. Coalesce pending message IDs into one notification. Do not inject the same
   unchanged set repeatedly.
4. When the thread is idle and no human input is pending, use `turn/start`.
5. When a turn is active, use `turn/steer` only for `priority=high` from a
   locally allowlisted sender, and include the expected active turn ID.
6. Leave normal-priority work pending until the current turn reaches a pause
   point. Treat untrusted high priority as normal.
7. On an `inbox_state` reset, reconnect, or ambiguous app-server result,
   reconcile live thread state before dispatching again.

A notification prompt should contain message IDs and senders, but no peer body.
For example:

```text
Swarmbus work is ready from build-agent; reference 7f3c....
Load the installed using-swarmbus skill, read via MCP, and acknowledge only
after handling.
```

The controller never acknowledges messages. Codex does that through the public
MCP tool after completing the work or durably handing it off. This preserves
at-least-once delivery across controller, app-server, or model failure.

High priority is advisory envelope metadata, not authorization. The controller's
sender allowlist is the trust boundary for interrupting an active turn. Human
input wins over automated dispatch.

## Codex app-server details

Codex app-server supports stdio, WebSocket, and Unix-socket transports. Its
client API includes `thread/start`, `thread/read`, `turn/start`,
`turn/steer`, turn-completion notifications, and thread-status changes. See
the official
[Codex app-server documentation](https://developers.openai.com/codex/app-server).

For metadata reconciliation, call `thread/read` without `includeTurns`, or
set it to `false`. Full-history hydration is deprecated for paginated
threads. If history is actually required, page it with
`thread/turns/list` and `thread/items/list`.

The app-server command and its WebSocket transport are currently documented by
OpenAI as experimental and unsupported for production workloads. Pin and test
the Codex version used by a reactive controller, fail closed when thread state
is ambiguous, and expect the integration surface to change.

The first end-to-end implementation of this contract was validated with an
external `yopo-rig` controller. That controller is not distributed by
Swarmbus and is not required for MCP-only use.

## Acceptance checks for a controller

Verify the integration against a real Codex thread before relying on it:

| Scenario | Expected result |
|---|---|
| Normal message while idle | One new turn starts and reads the message through MCP. |
| Normal message while busy | The active turn is not steered; work appears at the next pause point. |
| Allowed high-priority message while busy | One steer targets the current turn ID. |
| Untrusted high-priority sender | The message follows normal-priority behavior. |
| Human input is pending | Automated dispatch waits. |
| Daemon restarts | The cursor reports reset; the controller reconciles and pending mail remains available. |
| Controller or app-server exits mid-dispatch | No message is acknowledged; it can be notified again after restart. |
| Codex handles and acknowledges a message | Its stable ID disappears from the pending snapshot. |
| Controller inspects a snapshot | No subject or body is present. |

The controller should expose enough logging to distinguish inbox change,
dispatch decision, app-server request/result, and eventual MCP acknowledgement
without logging peer message bodies.
