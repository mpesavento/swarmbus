from __future__ import annotations

import asyncio
import json
import os
import shlex
import sys

import aiomqtt
import click

from .bus import AgentBus
from .handlers.file_bridge import FileBridgeHandler
from .handlers.direct_invoke import DirectInvocationHandler
from .handlers.persistent import PersistentListenerHandler


@click.group()
def main() -> None:
    """agentbus — reactive MQTT messaging for AI agents."""


def _resolve_outbox(explicit: str | None, agent_id: str) -> str | None:
    """Resolve the outbox path with this precedence:

    1. `--outbox` flag (passed in `explicit`) — highest.
    2. `AGENTBUS_OUTBOX_<UPPER_AGENT_ID>` — agent-scoped env var. Hyphens in
       the agent-id become underscores so `wren-beta` → `AGENTBUS_OUTBOX_WREN_BETA`.
    3. `AGENTBUS_OUTBOX` — shared env var. Supports `{agent_id}` template.
    4. None — no archive.

    The agent-scoped form exists so shells that leak a plain `AGENTBUS_OUTBOX`
    to multiple agent processes can still pin each agent to its own file.
    """
    if explicit is not None:
        return explicit
    scoped_key = "AGENTBUS_OUTBOX_" + agent_id.replace("-", "_").upper()
    scoped = os.environ.get(scoped_key)
    if scoped is not None:
        return scoped
    return os.environ.get("AGENTBUS_OUTBOX")


@main.command()
@click.option("--agent-id", required=True, help="This agent's ID")
@click.option("--to", "to_agent", required=True, help="Target agent ID")
@click.option("--subject", required=True, help="Message subject")
@click.option("--body", default=None, help="Message body as a string")
@click.option(
    "--body-file",
    type=click.File("r"),
    default=None,
    help="Read message body from a file (use '-' for stdin)",
)
@click.option("--broker", default="localhost", show_default=True)
@click.option("--port", default=1883, show_default=True)
@click.option("--content-type", default="text/plain", show_default=True)
@click.option("--reply-to", "reply_to", default=None, help="Agent id for the peer to reply to (defaults to unset)")
@click.option(
    "--outbox",
    "outbox",
    default=None,
    help="Append each sent message to this file (audit trail). Supports "
         "`{agent_id}` template. Resolution order: --outbox > "
         "AGENTBUS_OUTBOX_<UPPER_AGENT_ID> > AGENTBUS_OUTBOX.",
)
@click.pass_context
def send(
    ctx: click.Context,
    agent_id: str,
    to_agent: str,
    subject: str,
    body: str | None,
    body_file: click.File | None,
    broker: str,
    port: int,
    content_type: str,
    reply_to: str | None,
    outbox: str | None,
) -> None:
    """Send a message to another agent.

    Body can be supplied inline (--body) or read from a file (--body-file).
    Use '--body-file -' to read from stdin:

    \b
    agentbus send --agent-id sparrow --to wren --subject report --body "short text"
    agentbus send --agent-id sparrow --to wren --subject report --body-file report.md
    agentbus send --agent-id sparrow --to wren --subject report --body-file -
    cat report.md | agentbus send --agent-id sparrow --to wren --subject report --body-file -
    """
    if body is not None and body_file is not None:
        raise click.UsageError("--body and --body-file are mutually exclusive")
    if body is None and body_file is None:
        raise click.UsageError("One of --body or --body-file is required")

    if body_file is not None:
        body = body_file.read()

    bus = AgentBus(agent_id=agent_id, broker=broker, port=port)
    resolved_outbox = _resolve_outbox(outbox, agent_id)
    try:
        asyncio.run(bus.send(
            to=to_agent,
            subject=subject,
            body=body,
            content_type=content_type,
            reply_to=reply_to,
            outbox_path=resolved_outbox,
        ))
    except aiomqtt.MqttError as exc:
        click.echo(f"[agentbus] broker unreachable ({broker}:{port}): {exc}", err=True)
        sys.exit(2)
    click.echo(f"Sent to {to_agent}")


@main.command()
@click.option("--agent-id", required=True, help="This agent's ID")
@click.option("--broker", default="localhost", show_default=True)
@click.option("--port", default=1883, show_default=True)
@click.option("--inbox", default=None, help="Path for file bridge (inbox.md)")
@click.option("--invoke", "invoke_cmd", default=None, help="Command to invoke on message")
@click.option(
    "--persistent/--no-persistent",
    default=True,
    show_default=True,
    help="Use an MQTT persistent session so queued QoS1 messages survive "
         "daemon restarts. Disable only if another process holds the same "
         "`agentbus-<id>` client identifier.",
)
def start(
    agent_id: str,
    broker: str,
    port: int,
    inbox: str | None,
    invoke_cmd: str | None,
    persistent: bool,
) -> None:
    """Start the agentbus listener daemon."""
    bus = AgentBus(agent_id=agent_id, broker=broker, port=port, persistent=persistent)

    if inbox:
        bus.register_handler(FileBridgeHandler(inbox))
    if invoke_cmd:
        bus.register_handler(DirectInvocationHandler(command=shlex.split(invoke_cmd)))

    persistent = PersistentListenerHandler()
    bus.register_handler(persistent)

    click.echo(f"[agentbus] {agent_id} listening on {broker}:{port}")
    try:
        bus.run()
    except KeyboardInterrupt:
        click.echo("\n[agentbus] shutting down")


@main.command()
@click.option("--agent-id", required=True, help="This agent's ID")
@click.option("--broker", default="localhost", show_default=True)
@click.option("--port", default=1883, show_default=True)
@click.option("--max", "max_messages", default=10, show_default=True, help="Max messages to drain")
@click.option("--json", "as_json", is_flag=True, help="Emit raw JSON array (default: pretty)")
def read(agent_id: str, broker: str, port: int, max_messages: int, as_json: bool) -> None:
    """Drain retained messages from your inbox and exit.

    Non-blocking: returns immediately with whatever's waiting. Catches
    only messages sent with `retain=True` — non-retained directed sends
    (the default) that arrived while no subscriber was connected are
    already gone. For durable delivery, keep a listener daemon up:
    `agentbus start --agent-id <me> --inbox <path>`.

    Exit 0 always (empty inbox is not an error).

    \b
    agentbus read --agent-id sparrow
    agentbus read --agent-id sparrow --json | jq '.[] | .subject'
    """
    bus = AgentBus(agent_id=agent_id, broker=broker, port=port)
    try:
        messages = asyncio.run(bus.read_inbox(max_messages=max_messages))
    except aiomqtt.MqttError as exc:
        click.echo(f"[agentbus] broker unreachable ({broker}:{port}): {exc}", err=True)
        sys.exit(2)
    if as_json:
        click.echo(json.dumps(messages, indent=2))
        return
    if not messages:
        click.echo(f"[agentbus] {agent_id}: inbox empty")
        return
    for m in messages:
        click.echo(f"--- from {m['from']} @ {m['ts']} ---")
        click.echo(f"subject: {m['subject']}")
        if m.get("reply_to"):
            click.echo(f"reply_to: {m['reply_to']}")
        click.echo(f"content-type: {m.get('content_type', 'text/plain')}")
        click.echo("")
        click.echo(m["body"])
        click.echo("")


@main.command()
@click.option("--agent-id", required=True, help="This agent's ID")
@click.option("--broker", default="localhost", show_default=True)
@click.option("--port", default=1883, show_default=True)
@click.option("--timeout", default=30.0, show_default=True, help="Seconds to wait")
@click.option("--json", "as_json", is_flag=True, help="Emit raw JSON (default: pretty)")
def watch(agent_id: str, broker: str, port: int, timeout: float, as_json: bool) -> None:
    """Block until one message arrives, print it, exit.

    Catches messages published while this call is active. If a listener
    daemon is already running for the same agent-id, the two will race
    for each incoming message — use one mechanism per id at a time.

    Exit 0 on message, exit 1 on timeout, exit 2 on broker error. Use in
    shell pipelines when you want to wait for a specific reply.

    \b
    agentbus watch --agent-id sparrow --timeout 60
    """
    bus = AgentBus(agent_id=agent_id, broker=broker, port=port)
    try:
        msg = asyncio.run(bus.watch_inbox(timeout=timeout))
    except aiomqtt.MqttError as exc:
        click.echo(f"[agentbus] broker unreachable ({broker}:{port}): {exc}", err=True)
        sys.exit(2)
    if msg is None:
        click.echo(f"[agentbus] {agent_id}: timeout after {timeout}s", err=True)
        sys.exit(1)
    if as_json:
        click.echo(json.dumps(msg, indent=2))
        return
    click.echo(f"--- from {msg['from']} @ {msg['ts']} ---")
    click.echo(f"subject: {msg['subject']}")
    if msg.get("reply_to"):
        click.echo(f"reply_to: {msg['reply_to']}")
    click.echo("")
    click.echo(msg["body"])


@main.command("list")
@click.option("--broker", default="localhost", show_default=True)
@click.option("--port", default=1883, show_default=True)
@click.option("--json", "as_json", is_flag=True, help="Emit JSON array")
def list_agents_cmd(broker: str, port: int, as_json: bool) -> None:
    """List agent IDs currently online on the broker.

    \b
    agentbus list
    agentbus list --json
    """
    bus = AgentBus.probe(broker=broker, port=port)
    try:
        agents = asyncio.run(bus.list_agents())
    except aiomqtt.MqttError as exc:
        click.echo(f"[agentbus] broker unreachable ({broker}:{port}): {exc}", err=True)
        sys.exit(2)
    if as_json:
        click.echo(json.dumps(agents))
        return
    if not agents:
        click.echo("[agentbus] no agents online")
        return
    for a in agents:
        click.echo(a)


@main.command()
@click.option("--agent-id", required=True, help="This agent's ID")
@click.option(
    "--inbox",
    default=None,
    help="Path to the inbox file written by the listener daemon. "
         "Defaults to ~/sync/<agent-id>-inbox.md.",
)
@click.option(
    "--consumer",
    default="default",
    show_default=True,
    help="Consumer name — identifies this reader's cursor. Different names "
         "give independent read positions (two scripts both reading the "
         "same inbox without colliding).",
)
@click.option(
    "--cursor-dir",
    default=None,
    help="Directory for cursor files. Defaults to ~/.agentbus/cursors/.",
)
@click.option(
    "--follow", "-f",
    is_flag=True,
    help="Keep polling for new content (0.5s interval). Blocks until Ctrl+C.",
)
@click.option(
    "--reset",
    is_flag=True,
    help="Reset cursor to start of file before reading.",
)
def tail(
    agent_id: str,
    inbox: str | None,
    consumer: str,
    cursor_dir: str | None,
    follow: bool,
    reset: bool,
) -> None:
    """Read new entries from the daemon's inbox file, advancing a cursor.

    Companion to `agentbus start --inbox`: the daemon is the sole MQTT
    subscriber, `agentbus tail` reads the file it produces. This is how
    you read messages when a daemon is already running — avoids the
    race that `agentbus read` would create by subscribing to the same
    topic as the daemon.

    Polling-based --follow (0.5s interval) is intentional: we stay
    zero-dep across Linux/macOS/BSD instead of pulling in inotify/watchdog.
    The cost is ~2 syscalls/sec per follower — noise floor in practice,
    and the tail use case is "pick up on next agent turn", not
    sub-100ms UI refresh.

    \b
    agentbus tail --agent-id sparrow              # read new lines since last call
    agentbus tail --agent-id sparrow --follow     # block; stream new content
    agentbus tail --agent-id sparrow --consumer bot   # separate cursor
    """
    import os
    import re
    from pathlib import Path

    # Reject path-traversal-shaped consumer names — the value flows into a
    # cursor filename. Today --consumer is operator-set, but future scripted
    # callers might pass user-derived strings; cheap defence in depth.
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", consumer):
        click.echo(
            f"[agentbus] invalid --consumer {consumer!r}: must match "
            f"[A-Za-z0-9_-]{{1,64}}",
            err=True,
        )
        sys.exit(2)

    inbox_path = Path(inbox).expanduser() if inbox else Path.home() / "sync" / f"{agent_id}-inbox.md"
    cursors_root = Path(cursor_dir).expanduser() if cursor_dir else Path.home() / ".agentbus" / "cursors"
    cursors_root.mkdir(parents=True, exist_ok=True)
    cursor_file = cursors_root / f"{agent_id}--{consumer}.cursor"

    if not inbox_path.exists():
        click.echo(f"[agentbus] inbox does not exist: {inbox_path}", err=True)
        sys.exit(2)

    def _read_cursor() -> tuple[int, int | None]:
        """Return (offset, stored_inode). stored_inode is None on cold-start
        or legacy cursor formats so the first call falls through to emit
        everything from offset 0."""
        if reset or not cursor_file.exists():
            return 0, None
        try:
            raw = cursor_file.read_text().strip()
            parts = raw.split()
            offset = int(parts[0])
            inode = int(parts[1]) if len(parts) >= 2 else None
            return offset, inode
        except (ValueError, OSError):
            # Corrupt/empty cursor — re-emit from start. Loud enough to notice
            # if it happens, quiet enough not to crash a follower loop.
            click.echo(
                f"[agentbus] cursor {cursor_file} unreadable; restarting from offset 0",
                err=True,
            )
            return 0, None

    def _write_cursor(offset: int, inode: int) -> None:
        # Atomic write so a SIGKILL between truncate and write can't leave
        # an empty cursor file (which would cause the next call to re-emit
        # the entire inbox). Format: "<offset> <inode>". Whitespace-separated
        # for trivial backward compat with legacy "<offset>" cursor files.
        tmp = cursor_file.with_suffix(".tmp")
        tmp.write_text(f"{offset} {inode}")
        os.replace(tmp, cursor_file)

    def _emit_new() -> int | None:
        """Read from cursor → EOF, print, advance cursor.

        Returns the new offset, or None if the inbox is currently missing
        (rotated/deleted). Atomicity note: the daemon's FileBridgeHandler
        appends one whole entry per syscall in O_APPEND mode, so the file
        size we see is always at an entry boundary even if we read while
        the daemon is mid-burst.

        Rotation detection: we persist the file's st_ino alongside the
        offset. If the inode changes (logrotate, mv, rm+recreate,
        restart+refill), we reset to offset 0 so the new file is re-read
        from its beginning rather than silently seeking mid-file.
        """
        try:
            stat = inbox_path.stat()
        except FileNotFoundError:
            return None
        size = stat.st_size
        current_inode = stat.st_ino
        start, stored_inode = _read_cursor()
        if stored_inode is not None and stored_inode != current_inode:
            click.echo(
                f"[agentbus] inbox inode changed "
                f"({stored_inode} → {current_inode}); re-reading from 0",
                err=True,
            )
            start = 0
        elif size < start:
            # Same inode but file was truncated in place (copytruncate style).
            click.echo(f"[agentbus] inbox shrank ({size} < cursor {start}); resetting", err=True)
            start = 0
        if size == start:
            # Still update the cursor to record the current inode in case this
            # is a cold-start against an existing cursor with no inode field.
            if stored_inode != current_inode:
                _write_cursor(start, current_inode)
            return start
        try:
            with inbox_path.open("rb") as f:
                f.seek(start)
                chunk = f.read(size - start)
        except FileNotFoundError:
            return None
        click.echo(chunk.decode("utf-8", errors="replace"), nl=False)
        _write_cursor(size, current_inode)
        return size

    _emit_new()
    if not follow:
        return

    import time
    try:
        while True:
            time.sleep(0.5)
            _emit_new()
    except KeyboardInterrupt:
        click.echo("", err=True)  # newline after ^C


@main.command("mcp-server")
@click.option("--agent-id", required=True, help="This agent's ID")
@click.option("--broker", default="localhost", show_default=True)
@click.option("--port", default=1883, show_default=True)
def mcp_server(agent_id: str, broker: str, port: int) -> None:
    """Start the MCP sidecar for this agent."""
    from .mcp_server import run_mcp_server
    run_mcp_server(agent_id=agent_id, broker=broker, port=port)
