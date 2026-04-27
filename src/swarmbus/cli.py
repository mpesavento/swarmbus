from __future__ import annotations

import asyncio
import json
import os
import re as _re
import shlex
import shutil
import subprocess
import sys

import aiomqtt
import click

from .bus import AgentBus
from .handlers.file_bridge import FileBridgeHandler
from .handlers.direct_invoke import DirectInvocationHandler
from .handlers.persistent import PersistentListenerHandler


@click.group()
def main() -> None:
    """swarmbus — reactive MQTT messaging for AI agents."""


def _resolve_outbox(explicit: str | None, agent_id: str) -> str | None:
    """Resolve the outbox path with this precedence:

    1. `--outbox` flag (passed in `explicit`) — highest.
    2. `SWARMBUS_OUTBOX_<UPPER_AGENT_ID>` — agent-scoped env var. Hyphens in
       the agent-id become underscores so `coder-beta` → `SWARMBUS_OUTBOX_CODER_BETA`.
    3. `SWARMBUS_OUTBOX` — shared env var. Supports `{agent_id}` template.
    4. None — no archive.

    The agent-scoped form exists so shells that leak a plain `SWARMBUS_OUTBOX`
    to multiple agent processes can still pin each agent to its own file.
    """
    if explicit is not None:
        return explicit
    scoped_key = "SWARMBUS_OUTBOX_" + agent_id.replace("-", "_").upper()
    scoped = os.environ.get(scoped_key)
    if scoped is not None:
        return scoped
    return os.environ.get("SWARMBUS_OUTBOX")


@main.command()
@click.option("--agent-id", required=True, help="This agent's ID")
@click.option(
    "--to",
    "to_agent",
    required=True,
    help="Target agent ID, or the literal 'broadcast' to publish to every "
         "listening peer.",
)
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
@click.option(
    "--priority",
    type=click.Choice(["low", "normal", "high"], case_sensitive=False),
    default="normal",
    show_default=True,
    help="Envelope priority. `high` is the gate for reactive wake wrappers "
         "(examples/claude-code-wake.sh, examples/openclaw-wake.sh) — use "
         "sparingly: it triggers a real reasoning turn on the recipient.",
)
@click.option("--reply-to", "reply_to", default=None, help="Agent id for the peer to reply to (defaults to unset)")
@click.option(
    "--outbox",
    "outbox",
    default=None,
    help="Append each sent message to this file (audit trail). Supports "
         "`{agent_id}` template. Resolution order: --outbox > "
         "SWARMBUS_OUTBOX_<UPPER_AGENT_ID> > SWARMBUS_OUTBOX.",
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
    priority: str,
    reply_to: str | None,
    outbox: str | None,
) -> None:
    """Send a message to another agent.

    Body can be supplied inline (--body) or read from a file (--body-file).
    Use '--body-file -' to read from stdin:

    \b
    swarmbus send --agent-id planner --to coder --subject report --body "short text"
    swarmbus send --agent-id planner --to coder --subject report --body-file report.md
    swarmbus send --agent-id planner --to coder --subject report --body-file -
    cat report.md | swarmbus send --agent-id planner --to coder --subject report --body-file -
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
            priority=priority.lower(),
            reply_to=reply_to,
            outbox_path=resolved_outbox,
        ))
    except aiomqtt.MqttError as exc:
        click.echo(f"[swarmbus] broker unreachable ({broker}:{port}): {exc}", err=True)
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
         "`swarmbus-<id>` client identifier.",
)
def start(
    agent_id: str,
    broker: str,
    port: int,
    inbox: str | None,
    invoke_cmd: str | None,
    persistent: bool,
) -> None:
    """Start the swarmbus listener daemon."""
    from . import __version__

    bus = AgentBus(agent_id=agent_id, broker=broker, port=port, persistent=persistent)

    if inbox:
        bus.register_handler(FileBridgeHandler(inbox))
    if invoke_cmd:
        bus.register_handler(DirectInvocationHandler(command=shlex.split(invoke_cmd)))

    bus.register_handler(PersistentListenerHandler())

    # Verbose startup line — all the state a reader of journalctl needs to
    # diagnose "why didn't my agent react" mysteries without a manual
    # inspection of the systemd unit. Pulled from the same args the
    # daemon was actually launched with.
    click.echo(f"[swarmbus] {agent_id} ready")
    click.echo(f"  version:     {__version__}")
    click.echo(f"  broker:      {broker}:{port}")
    click.echo(f"  persistent:  {'yes' if persistent else 'no'}")
    click.echo(f"  inbox:       {inbox or '(unset — no file bridge)'}")
    click.echo(f"  invoke:      {invoke_cmd or '(unset — no reactive wake)'}")
    scoped_key = "SWARMBUS_OUTBOX_" + agent_id.replace("-", "_").upper()
    outbox_env = os.environ.get(scoped_key) or os.environ.get("SWARMBUS_OUTBOX") or "(unset)"
    click.echo(f"  outbox env:  {outbox_env}")
    try:
        bus.run()
    except KeyboardInterrupt:
        click.echo("\n[swarmbus] shutting down")


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
    `swarmbus start --agent-id <me> --inbox <path>`.

    Exit 0 always (empty inbox is not an error).

    \b
    swarmbus read --agent-id planner
    swarmbus read --agent-id planner --json | jq '.[] | .subject'
    """
    bus = AgentBus(agent_id=agent_id, broker=broker, port=port)
    try:
        messages = asyncio.run(bus.read_inbox(max_messages=max_messages))
    except aiomqtt.MqttError as exc:
        click.echo(f"[swarmbus] broker unreachable ({broker}:{port}): {exc}", err=True)
        sys.exit(2)
    if as_json:
        click.echo(json.dumps(messages, indent=2))
        return
    if not messages:
        click.echo(f"[swarmbus] {agent_id}: inbox empty")
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
    swarmbus watch --agent-id planner --timeout 60
    """
    bus = AgentBus(agent_id=agent_id, broker=broker, port=port)
    try:
        msg = asyncio.run(bus.watch_inbox(timeout=timeout))
    except aiomqtt.MqttError as exc:
        click.echo(f"[swarmbus] broker unreachable ({broker}:{port}): {exc}", err=True)
        sys.exit(2)
    if msg is None:
        click.echo(f"[swarmbus] {agent_id}: timeout after {timeout}s", err=True)
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
    swarmbus list
    swarmbus list --json
    """
    bus = AgentBus.probe(broker=broker, port=port)
    try:
        agents = asyncio.run(bus.list_agents())
    except aiomqtt.MqttError as exc:
        click.echo(f"[swarmbus] broker unreachable ({broker}:{port}): {exc}", err=True)
        sys.exit(2)
    if as_json:
        click.echo(json.dumps(agents))
        return
    if not agents:
        click.echo("[swarmbus] no agents online")
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
    help="Directory for cursor files. Defaults to ~/.swarmbus/cursors/.",
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

    Companion to `swarmbus start --inbox`: the daemon is the sole MQTT
    subscriber, `swarmbus tail` reads the file it produces. This is how
    you read messages when a daemon is already running — avoids the
    race that `swarmbus read` would create by subscribing to the same
    topic as the daemon.

    Polling-based --follow (0.5s interval) is intentional: we stay
    zero-dep across Linux/macOS/BSD instead of pulling in inotify/watchdog.
    The cost is ~2 syscalls/sec per follower — noise floor in practice,
    and the tail use case is "pick up on next agent turn", not
    sub-100ms UI refresh.

    \b
    swarmbus tail --agent-id planner              # read new lines since last call
    swarmbus tail --agent-id planner --follow     # block; stream new content
    swarmbus tail --agent-id planner --consumer bot   # separate cursor
    """
    import os
    import re
    from pathlib import Path

    # Reject path-traversal-shaped consumer names — the value flows into a
    # cursor filename. Today --consumer is operator-set, but future scripted
    # callers might pass user-derived strings; cheap defence in depth.
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", consumer):
        click.echo(
            f"[swarmbus] invalid --consumer {consumer!r}: must match "
            f"[A-Za-z0-9_-]{{1,64}}",
            err=True,
        )
        sys.exit(2)

    inbox_path = Path(inbox).expanduser() if inbox else Path.home() / "sync" / f"{agent_id}-inbox.md"
    cursors_root = Path(cursor_dir).expanduser() if cursor_dir else Path.home() / ".swarmbus" / "cursors"
    cursors_root.mkdir(parents=True, exist_ok=True)
    cursor_file = cursors_root / f"{agent_id}--{consumer}.cursor"

    if not inbox_path.exists():
        click.echo(f"[swarmbus] inbox does not exist: {inbox_path}", err=True)
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
                f"[swarmbus] cursor {cursor_file} unreadable; restarting from offset 0",
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
                f"[swarmbus] inbox inode changed "
                f"({stored_inode} → {current_inode}); re-reading from 0",
                err=True,
            )
            start = 0
        elif size < start:
            # Same inode but file was truncated in place (copytruncate style).
            click.echo(f"[swarmbus] inbox shrank ({size} < cursor {start}); resetting", err=True)
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


@main.command()
@click.option("--agent-id", default=None, help="Agent id to audit. Defaults to auto-detect from systemd unit or env.")
@click.option("--broker", default="localhost", show_default=True)
@click.option("--port", default=1883, show_default=True)
def doctor(agent_id: str | None, broker: str, port: int) -> None:
    """Run a self-diagnosis of the local swarmbus install + daemon state.

    Prints a checklist of 7 probes: CLI version, broker reachability, my
    systemd unit state, daemon library freshness (catches stale in-memory
    Python — the root cause of the 2026-04-14 priority-field incident),
    --invoke wired, outbox env resolvable, peer discovery. Each failure
    prints a one-line fix hint. Exits 0 if every check is green, 1 if
    any is red, 2 if the doctor itself couldn't run.

    Use after any `pip install -U`, after tweaking a systemd unit, or
    when something "just stopped working" and you want a fast pass/fail
    signal before diving into logs.
    """
    import glob
    import subprocess
    from pathlib import Path
    from . import __version__

    try:
        agent_id_resolved = agent_id or _detect_agent_id()
    except Exception as exc:
        click.echo(f"[doctor] could not detect agent-id: {exc}", err=True)
        click.echo("[doctor] pass --agent-id <me> to proceed.", err=True)
        sys.exit(2)

    results: list[tuple[str, str, str | None]] = []
    # Each tuple: (label, status, fix_hint). status ∈ {"ok","warn","fail","skip"}

    # 1. CLI version
    try:
        import swarmbus as _ab
        pkg_path = Path(_ab.__file__).parent
        results.append((
            f"swarmbus CLI version.... {__version__} at {pkg_path}",
            "ok",
            None,
        ))
    except Exception as exc:
        results.append((f"swarmbus CLI version.... ERROR {exc}", "fail",
                        "pip install -e /path/to/swarmbus (editable install recommended)"))

    # 2. Broker reachability
    try:
        async def _probe_broker():
            async with aiomqtt.Client(broker, port=port, timeout=2.0):
                return True
        asyncio.run(_probe_broker())
        results.append((f"broker reachable........ {broker}:{port}", "ok", None))
    except Exception as exc:
        results.append((f"broker reachable........ {broker}:{port}: {exc}",
                        "fail",
                        f"systemctl status mosquitto  (or point --broker at a reachable host)"))

    # 3. Systemd daemon state
    unit_name = f"swarmbus-{agent_id_resolved}.service"
    try:
        env = {**os.environ, "XDG_RUNTIME_DIR": f"/run/user/{os.getuid()}",
               "DBUS_SESSION_BUS_ADDRESS": f"unix:path=/run/user/{os.getuid()}/bus"}
        probe = subprocess.run(
            ["systemctl", "--user", "show", unit_name,
             "--property=ActiveState,SubState,MainPID,ExecMainStartTimestamp,ExecStart"],
            capture_output=True, text=True, env=env, timeout=3,
        )
        if probe.returncode != 0 or not probe.stdout.strip():
            results.append((
                f"systemd user unit....... {unit_name} NOT FOUND",
                "warn",
                f"scripts/install-systemd.sh {agent_id_resolved} "
                f"(or leave as-is if you run a bare daemon)",
            ))
            active_state = None
            main_pid = None
            exec_start = None
            start_ts = None
        else:
            props = dict(line.split("=", 1) for line in probe.stdout.strip().splitlines() if "=" in line)
            active_state = props.get("ActiveState", "?")
            main_pid = props.get("MainPID", "0")
            start_ts = props.get("ExecMainStartTimestamp", "")
            exec_start = props.get("ExecStart", "")
            if active_state == "active":
                results.append((
                    f"systemd user unit....... active (PID {main_pid}, since {start_ts})",
                    "ok", None,
                ))
            else:
                results.append((
                    f"systemd user unit....... {active_state}",
                    "fail",
                    f"systemctl --user status {unit_name}  (and restart if needed)",
                ))
    except FileNotFoundError:
        results.append(("systemd user unit....... (systemctl not found)", "skip", None))
        main_pid = None
        exec_start = None
        start_ts = None
    except Exception as exc:
        results.append((f"systemd user unit....... ERROR {exc}", "warn", None))
        main_pid = None
        exec_start = None
        start_ts = None

    # 4. Library freshness — does the running daemon's start time predate
    # the on-disk package source? If so, stale in-memory Python is possible.
    # This is the exact check that would have saved hours on 2026-04-14.
    try:
        if main_pid and main_pid != "0":
            # Compare daemon process start time to the source file mtime.
            import datetime
            try:
                proc_stat = Path(f"/proc/{main_pid}/stat").read_text().split()
                # Field 22 is start time in clock ticks since boot; convert.
                boot_ts = float(Path("/proc/uptime").read_text().split()[0])
                clock_ticks = os.sysconf("SC_CLK_TCK")
                start_since_boot = float(proc_stat[21]) / clock_ticks
                now = datetime.datetime.now()
                proc_started = now - datetime.timedelta(seconds=boot_ts - start_since_boot)
                source_mtime = datetime.datetime.fromtimestamp(
                    Path(_ab.__file__).parent.joinpath("message.py").stat().st_mtime
                )
                if source_mtime > proc_started:
                    results.append((
                        f"daemon library fresh.... STALE — source modified "
                        f"{source_mtime:%Y-%m-%d %H:%M:%S} > daemon started "
                        f"{proc_started:%Y-%m-%d %H:%M:%S}",
                        "fail",
                        f"systemctl --user restart {unit_name}  "
                        f"(running daemon holds old code in memory)",
                    ))
                else:
                    results.append((
                        f"daemon library fresh.... ok (started "
                        f"{proc_started:%Y-%m-%d %H:%M:%S}, source last "
                        f"modified {source_mtime:%Y-%m-%d %H:%M:%S})",
                        "ok", None,
                    ))
            except (FileNotFoundError, PermissionError, IndexError) as exc:
                results.append((f"daemon library fresh.... could not verify ({exc})",
                                "skip", None))
        else:
            results.append(("daemon library fresh.... (no daemon to check)", "skip", None))
    except Exception as exc:
        results.append((f"daemon library fresh.... ERROR {exc}", "warn", None))

    # 5. --invoke wired
    if exec_start is not None:
        if "--invoke" in (exec_start or ""):
            import re as _re
            m = _re.search(r"--invoke[= ]+\"?([^\"\\s]+)\"?", exec_start)
            invoke_path = m.group(1) if m else "(parsed)"
            results.append((f"--invoke wired.......... yes ({invoke_path})", "ok", None))
        else:
            results.append((
                "--invoke wired.......... no reactive wake",
                "warn",
                f"edit {Path.home()}/.config/systemd/user/{unit_name} ExecStart "
                f"to add --invoke <path-to-wake-wrapper>; systemctl --user "
                f"daemon-reload && restart",
            ))
    else:
        results.append(("--invoke wired.......... (no unit to inspect)", "skip", None))

    # 6. Outbox env var resolvable
    scoped_key = "SWARMBUS_OUTBOX_" + agent_id_resolved.replace("-", "_").upper()
    outbox_env = os.environ.get(scoped_key) or os.environ.get("SWARMBUS_OUTBOX")
    if outbox_env:
        resolved = outbox_env.replace("{agent_id}", agent_id_resolved)
        try:
            p = Path(resolved).expanduser()
            p.parent.mkdir(parents=True, exist_ok=True)
            p.touch(exist_ok=True)
            results.append((
                f"outbox env resolvable... {resolved}",
                "ok", None,
            ))
        except Exception as exc:
            results.append((
                f"outbox env resolvable... {resolved}: NOT WRITABLE ({exc})",
                "fail",
                "check parent dir permissions",
            ))
    else:
        results.append((
            "outbox env resolvable... (unset)",
            "warn",
            f"export SWARMBUS_OUTBOX_{agent_id_resolved.upper()}=~/sync/"
            f"{agent_id_resolved}-outbox.md   (or SWARMBUS_OUTBOX with "
            f"{{agent_id}} template)",
        ))

    # 7. Peer discovery
    try:
        bus = AgentBus.probe(broker=broker, port=port)
        peers = asyncio.run(bus.list_agents())
        if agent_id_resolved in peers:
            others = [p for p in peers if p != agent_id_resolved]
            results.append((
                f"peer discovery.......... I'm visible; {len(others)} other peer(s): "
                f"{', '.join(others) or '(none)'}",
                "ok", None,
            ))
        else:
            results.append((
                f"peer discovery.......... I'm NOT in the online list "
                f"({len(peers)} peers visible: {', '.join(peers) or '(none)'})",
                "fail",
                f"systemctl --user restart {unit_name}  "
                f"(daemon may not have announced presence)",
            ))
    except Exception as exc:
        results.append((f"peer discovery.......... ERROR {exc}", "fail", None))

    # Render
    click.echo(f"\n[doctor] swarmbus health check for agent-id={agent_id_resolved}\n")
    icon_char = {"ok": "✓", "warn": "⚠", "fail": "✗", "skip": "·"}
    color_map = {"ok": "green", "warn": "yellow", "fail": "red", "skip": None}
    fails = warns = 0
    for i, (label, status, hint) in enumerate(results, 1):
        char = icon_char[status]
        fg = color_map[status]
        line = f"  [{char}] {i}. {label}"
        click.echo(click.style(line, fg=fg, bold=(status == "fail")))
        if hint:
            click.echo(f"        fix: {hint}")
        if status == "fail":
            fails += 1
        elif status == "warn":
            warns += 1
    click.echo("")
    if fails:
        click.echo(f"[doctor] {fails} failure(s), {warns} warning(s) — some checks are red.")
        sys.exit(1)
    elif warns:
        click.echo(f"[doctor] all critical checks passed; {warns} warning(s) above.")
        sys.exit(0)
    else:
        click.echo("[doctor] all green.")
        sys.exit(0)


def _detect_agent_id() -> str:
    """Best-effort agent-id detection: look for an active systemd user
    unit named swarmbus-*.service. Error out if ambiguous or none."""
    import subprocess
    env = {**os.environ, "XDG_RUNTIME_DIR": f"/run/user/{os.getuid()}",
           "DBUS_SESSION_BUS_ADDRESS": f"unix:path=/run/user/{os.getuid()}/bus"}
    res = subprocess.run(
        ["systemctl", "--user", "list-units", "--type=service", "--no-legend",
         "--no-pager", "swarmbus-*.service"],
        capture_output=True, text=True, env=env, timeout=3,
    )
    units = [line.split()[0] for line in res.stdout.strip().splitlines() if line.strip()]
    candidates = []
    for u in units:
        if u.startswith("swarmbus-") and u.endswith(".service"):
            candidates.append(u[len("swarmbus-"):-len(".service")])
    if len(candidates) == 1:
        return candidates[0]
    if len(candidates) > 1:
        raise RuntimeError(f"multiple swarmbus units running ({candidates}); pass --agent-id")
    raise RuntimeError("no swarmbus-*.service unit detected")


@main.command("mcp-server")
@click.option("--agent-id", required=True, help="This agent's ID")
@click.option("--broker", default="localhost", show_default=True)
@click.option("--port", default=1883, show_default=True)
def mcp_server(agent_id: str, broker: str, port: int) -> None:
    """Start the MCP sidecar for this agent."""
    from .mcp_server import run_mcp_server
    run_mcp_server(agent_id=agent_id, broker=broker, port=port)


# ---------------------------------------------------------------------------
# swarmbus init
# ---------------------------------------------------------------------------

from .platform import detect_platform, find_repo_root, resolve_broker_addr


_AGENT_ID_RE = _re.compile(r"^[a-z0-9][a-z0-9_-]{0,62}$")
_RESERVED_AGENT_IDS = frozenset({"broadcast", "system"})


def _derive_invoke(host_type: str, agent_id: str, repo_root: str | None) -> str | None:
    """Derive the --invoke wrapper path from host_type and repo_root.

    Returns None when:
    - host_type is "none" (archive-only, no reactive wake needed)
    - repo_root is None (PyPI install; user must supply --invoke manually)
    """
    if repo_root is None or host_type == "none":
        return None
    if host_type == "cc":
        return f"{repo_root}/examples/claude-code-wake.sh {agent_id}"
    if host_type == "openclaw":
        return f"{repo_root}/examples/openclaw-wake.sh {agent_id}"
    return None


def _run_step(label: str, cmd: list[str], dry_run: bool) -> bool:
    """Run a single init step, printing progress and result.

    In dry-run mode prints the command instead of executing it.
    On failure prints captured stdout + stderr so the operator sees
    what went wrong (setup scripts emit to stdout, not just stderr).

    Returns True on success, False on failure.
    """
    import subprocess as _sp

    padding = max(0, 40 - len(label))
    dots = "." * padding
    click.echo(f"  {label} {dots} ", nl=False)

    if dry_run:
        click.echo(click.style("(dry-run)", fg="cyan"))
        click.echo(f"    would run: {' '.join(cmd)}")
        return True

    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode == 0:
        click.echo(click.style("✓", fg="green"))
        return True
    else:
        click.echo(click.style("✗ failed", fg="red", bold=True))
        if result.stdout.strip():
            click.echo(f"    stdout: {result.stdout.strip()}")
        if result.stderr.strip():
            click.echo(f"    stderr: {result.stderr.strip()}")
        return False


def _step_broker(
    plt: str,
    broker: str,
    scripts_dir: str,
    dry_run: bool,
    yes: bool,
) -> bool:
    """Step 1: install / verify the MQTT broker."""
    label = "Broker"

    if plt == "debian":
        # resolve_broker_addr already called in init() before reaching here;
        # broker here is still the raw value. Pass the right flag to the script.
        if broker == "tailscale":
            cmd = [f"{scripts_dir}/setup-mosquitto.sh", "--tailscale"]
        else:
            cmd = [f"{scripts_dir}/setup-mosquitto.sh"]
        return _run_step(label, cmd, dry_run)

    if plt == "macos":
        if shutil.which("brew"):
            if not yes and not dry_run:
                click.echo(f"  {label}: install mosquitto via brew? [Y/n] ", nl=False)
                ans = sys.stdin.readline().strip().lower()
                if ans in ("n", "no"):
                    click.echo(click.style(f"  {label} ... skipped (no broker)", fg="yellow"))
                    return True
            return _run_step(
                label,
                ["brew", "install", "mosquitto"],
                dry_run,
            ) and _run_step(
                "Broker start",
                ["brew", "services", "start", "mosquitto"],
                dry_run,
            )
        else:
            # No brew — print manual instructions
            padding = max(0, 40 - len(label))
            dots = "." * padding
            click.echo(f"  {label} {dots} ", nl=False)
            click.echo(click.style("⚠ manual install needed", fg="yellow"))
            click.echo("    Install mosquitto manually:")
            click.echo("      https://mosquitto.org/download/")
            click.echo("    Then start it and press Enter to continue.")
            if not yes and not dry_run:
                sys.stdin.readline()
            return True

    # Unknown platform — skip with warning
    padding = max(0, 40 - len(label))
    dots = "." * padding
    click.echo(f"  {label} {dots} ", nl=False)
    click.echo(click.style("⚠ unknown platform — skipped", fg="yellow"))
    return True


def _step_package(dry_run: bool) -> bool:
    """Step 2: verify swarmbus package is importable."""
    from . import __version__
    label = "Package"
    padding = max(0, 40 - len(label))
    dots = "." * padding
    click.echo(f"  {label} {dots} ", nl=False)
    try:
        import swarmbus as _sb
        from pathlib import Path as _Path
        pkg_path = _Path(_sb.__file__).parent
        click.echo(click.style(f"✓ swarmbus {__version__} at {pkg_path}", fg="green"))
        return True
    except ImportError as exc:
        click.echo(click.style(f"⚠ could not import swarmbus: {exc}", fg="yellow"))
        click.echo("    hint: pip install swarmbus")
        return True  # warn only — don't fail init on self-check


def _step_systemd(
    agent_id: str,
    broker_addr: str,
    inbox: str,
    invoke: str | None,
    scripts_dir: str,
    dry_run: bool,
) -> bool:
    """Step 3: install the systemd user unit via install-systemd.sh."""
    label = "Systemd unit"
    cmd = [f"{scripts_dir}/install-systemd.sh", agent_id,
           "--broker", broker_addr,
           "--inbox", inbox]
    if invoke:
        cmd += ["--invoke", invoke]
    return _run_step(label, cmd, dry_run)


def _step_wake_wrapper(
    invoke: str | None,
    dry_run: bool,
) -> bool:
    """Step 4: verify the wake wrapper script exists and is executable.

    This is a verification step — the script itself is not installed here,
    it is referenced by the systemd unit's --invoke arg. When invoke is None
    (PyPI install with no --invoke supplied, or host-type=none), we warn
    instead of failing so the rest of init can complete.
    """
    label = "Wake wrapper"
    padding = max(0, 40 - len(label))
    dots = "." * padding
    click.echo(f"  {label} {dots} ", nl=False)

    if invoke is None:
        click.echo(click.style("⚠ not wired (no --invoke; no reactive wake)", fg="yellow"))
        click.echo("    hint: pass --invoke <path/to/wake.sh agent-id> to enable reactive wake")
        return True  # warn, not fail

    # invoke may be "<path> <agent-id>" — extract path using shlex so quoted
    # paths with spaces are handled correctly.
    from pathlib import Path as _Path
    try:
        invoke_path = shlex.split(invoke)[0]
    except ValueError:
        invoke_path = invoke.split()[0]
    p = _Path(invoke_path)
    if not p.exists():
        click.echo(click.style(f"⚠ script not found: {invoke_path}", fg="yellow"))
        click.echo("    hint: use --invoke to point to a different wake wrapper path")
        return True  # warn, not fail
    if not p.stat().st_mode & 0o100:
        click.echo(click.style(f"⚠ not executable: {invoke_path}", fg="yellow"))
        click.echo(f"    hint: chmod +x {invoke_path}")
        return True

    click.echo(click.style(f"✓ {invoke}", fg="green"))
    return True


def _step_plugin(
    agent_id: str,
    host_type: str,
    broker: str,
    scripts_dir: str,
    skip_plugin: bool,
    dry_run: bool,
) -> bool:
    """Step 5: install the host plugin (Claude Code or OpenClaw)."""
    label = "Host plugin"
    padding = max(0, 40 - len(label))
    dots = "." * padding

    if skip_plugin or host_type == "none":
        click.echo(f"  {label} {dots} ", nl=False)
        click.echo(click.style("· skipped", fg=None))
        return True

    if host_type == "cc":
        # positional args: <agent-id> [broker-host]
        return _run_step(label, [f"{scripts_dir}/setup-cc-plugin.sh", agent_id, broker], dry_run)
    if host_type == "openclaw":
        return _run_step(label, [f"{scripts_dir}/setup-openclaw-plugin.sh", agent_id, broker], dry_run)

    click.echo(f"  {label} {dots} ", nl=False)
    click.echo(click.style("· skipped", fg=None))
    return True


def _step_doctor(agent_id: str, dry_run: bool) -> bool:
    """Step 6: run swarmbus doctor and surface the result."""
    label = "Doctor"
    swarmbus_on_path = shutil.which("swarmbus")
    if swarmbus_on_path:
        cmd = [swarmbus_on_path, "doctor", "--agent-id", agent_id]
    else:
        # Fallback: run as a Python module. sys.executable is always a single
        # path, never a compound string, so it's safe as a list element.
        cmd = [sys.executable, "-m", "swarmbus", "doctor", "--agent-id", agent_id]
    return _run_step(label, cmd, dry_run)


@main.command()
@click.option("--agent-id", required=True,
              help="Agent identifier (lowercase alphanumeric, hyphens, underscores)")
@click.option(
    "--host-type",
    type=click.Choice(["cc", "openclaw", "none"], case_sensitive=False),
    default="none",
    show_default=True,
    help="Host type: cc (Claude Code), openclaw, none (archive-only).",
)
@click.option("--broker", default="localhost", show_default=True,
              help="Broker address. Use 'tailscale' to auto-resolve Tailscale IP.")
@click.option("--invoke", "invoke_override", default=None,
              help="Override the invoke wrapper path passed to install-systemd.")
@click.option("--inbox", default=None,
              help="Inbox file path. Defaults to ~/sync/<agent-id>-inbox.md.")
@click.option("--skip-broker", is_flag=True, help="Skip broker install.")
@click.option("--skip-plugin", is_flag=True, help="Skip host plugin install.")
@click.option("--dry-run", is_flag=True, help="Print what would run without executing.")
@click.option("--yes", is_flag=True, help="Non-interactive; accept all prompts.")
def init(
    agent_id: str,
    host_type: str,
    broker: str,
    invoke_override: str | None,
    inbox: str | None,
    skip_broker: bool,
    skip_plugin: bool,
    dry_run: bool,
    yes: bool,
) -> None:
    """One-command agent setup: broker, daemon, plugin, doctor.

    \b
    swarmbus init --agent-id sparrow
    swarmbus init --agent-id wren --host-type openclaw
    swarmbus init --agent-id coder --host-type cc --broker tailscale
    swarmbus init --agent-id coder --invoke "/path/to/wake.sh coder" --skip-broker
    """
    from pathlib import Path as _Path

    # --- Validate agent-id
    if not _AGENT_ID_RE.match(agent_id):
        raise click.BadParameter(
            f"{agent_id!r} is not valid. Use lowercase letters, digits, hyphens, "
            "underscores (1–63 chars); must start with a letter or digit.",
            param_hint="--agent-id",
        )
    if agent_id in _RESERVED_AGENT_IDS:
        raise click.BadParameter(
            f"{agent_id!r} is reserved and cannot be used as an agent id.",
            param_hint="--agent-id",
        )

    # --- Resolve broker address (may shell out for tailscale)
    try:
        broker_addr = resolve_broker_addr(broker)
    except RuntimeError as exc:
        click.echo(click.style(f"[swarmbus init] error: {exc}", fg="red", bold=True), err=True)
        sys.exit(1)

    # --- Detect platform + repo
    plt = detect_platform()
    repo_root = find_repo_root()

    # --- Resolve scripts dir (where the bash setup scripts live)
    scripts_dir = f"{repo_root}/scripts" if repo_root else None
    if scripts_dir is None and not skip_broker:
        # Can't call setup scripts without a repo — warn and continue where possible
        click.echo(click.style(
            "[swarmbus init] scripts/ not found (PyPI install). "
            "Broker install, systemd install, and plugin install require the repo. "
            "Re-run with --skip-broker --skip-plugin and wire systemd manually, "
            "or use an editable install: pip install -e /path/to/swarmbus",
            fg="yellow",
        ))
        # Don't hard-fail — let the steps surface individual warnings

    # --- Resolve inbox
    resolved_inbox = inbox or str(_Path.home() / "sync" / f"{agent_id}-inbox.md")

    # --- Derive invoke wrapper
    if invoke_override:
        invoke = invoke_override
    else:
        invoke = _derive_invoke(host_type, agent_id, repo_root)

    # --- Header
    click.echo(f"\n[swarmbus init] agent-id={agent_id} host-type={host_type} broker={broker_addr}")
    if dry_run:
        click.echo(click.style("  (dry run — no changes will be made)\n", fg="cyan"))
    else:
        click.echo("")

    results: list[tuple[str, bool]] = []

    # --- Step 1: Broker
    if skip_broker:
        padding = max(0, 40 - len("Broker"))
        dots = "." * padding
        click.echo(f"  Broker {dots} ", nl=False)
        click.echo(click.style("· skipped (--skip-broker)", fg=None))
        broker_ok = True
    else:
        if scripts_dir is None:
            # No scripts dir — skip with warning
            padding = max(0, 40 - len("Broker"))
            dots = "." * padding
            click.echo(f"  Broker {dots} ", nl=False)
            click.echo(click.style("⚠ skipped (no scripts dir; PyPI install)", fg="yellow"))
            broker_ok = True
        else:
            broker_ok = _step_broker(plt, broker, scripts_dir, dry_run, yes)

    results.append(("Broker", broker_ok))

    if not broker_ok:
        click.echo(click.style("\n[swarmbus init] broker setup failed — aborting.", fg="red", bold=True))
        sys.exit(1)

    # --- Step 2: Package check
    pkg_ok = _step_package(dry_run)
    results.append(("Package", pkg_ok))

    # --- Step 3: Systemd
    if scripts_dir is None:
        padding = max(0, 40 - len("Systemd unit"))
        dots = "." * padding
        click.echo(f"  Systemd unit {dots} ", nl=False)
        click.echo(click.style("⚠ skipped (no scripts dir; PyPI install)", fg="yellow"))
        systemd_ok = True
    else:
        systemd_ok = _step_systemd(agent_id, broker_addr, resolved_inbox, invoke, scripts_dir, dry_run)
    results.append(("Systemd unit", systemd_ok))

    # --- Step 4: Wake wrapper
    wake_ok = _step_wake_wrapper(invoke, dry_run)
    results.append(("Wake wrapper", wake_ok))

    # --- Step 5: Plugin
    if scripts_dir is None and not skip_plugin and host_type != "none":
        padding = max(0, 40 - len("Host plugin"))
        dots = "." * padding
        click.echo(f"  Host plugin {dots} ", nl=False)
        click.echo(click.style("⚠ skipped (no scripts dir; PyPI install)", fg="yellow"))
        plugin_ok = True
    else:
        plugin_ok = _step_plugin(agent_id, host_type, broker_addr, scripts_dir or "", skip_plugin, dry_run)
    results.append(("Host plugin", plugin_ok))

    # --- Step 6: Doctor
    doctor_ok = _step_doctor(agent_id, dry_run)
    results.append(("Doctor", doctor_ok))

    # --- Summary
    failed = [name for name, ok in results if not ok]
    click.echo("")
    click.echo("─" * 60)

    if not failed:
        click.echo(click.style(f"✓  {agent_id} is ready.", fg="green", bold=True))
        click.echo("")
        upper_id = agent_id.replace("-", "_").upper()
        click.echo("To set outbox archiving in your shell:")
        click.echo(f'  export SWARMBUS_OUTBOX_{upper_id}="$HOME/sync/{agent_id}-outbox.md"')
        click.echo("")
        click.echo("The daemon is running. Your agent can now receive messages.")
        click.echo(f"Send a test:")
        click.echo(f"  swarmbus send --agent-id probe --to {agent_id} --subject hello --body 'init test'")
        click.echo("─" * 60)
        sys.exit(0)
    else:
        click.echo(click.style(f"✗  {len(failed)} step(s) failed: {', '.join(failed)}", fg="red", bold=True))
        click.echo("")
        click.echo("Fix the errors above and re-run:")
        click.echo(f"  swarmbus init --agent-id {agent_id} --host-type {host_type} --skip-broker")
        click.echo("─" * 60)
        sys.exit(1)
