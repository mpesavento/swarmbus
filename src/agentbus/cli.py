from __future__ import annotations

import asyncio
import json
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
    try:
        asyncio.run(bus.send(
            to=to_agent,
            subject=subject,
            body=body,
            content_type=content_type,
            reply_to=reply_to,
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
def start(
    agent_id: str,
    broker: str,
    port: int,
    inbox: str | None,
    invoke_cmd: str | None,
) -> None:
    """Start the agentbus listener daemon."""
    bus = AgentBus(agent_id=agent_id, broker=broker, port=port)

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
    """Drain queued messages from your inbox and exit.

    Non-blocking: returns immediately with whatever's waiting. Exit 0
    always (no messages is not an error). Exit 2 if the broker is
    unreachable.

    \b
    agentbus read --agent-id sparrow
    agentbus read --agent-id sparrow --json | jq '.[] | .subject'
    """
    bus = AgentBus(agent_id=agent_id, broker=broker, port=port)
    messages = asyncio.run(bus.read_inbox(max_messages=max_messages))
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

    Exit 0 on message, exit 1 on timeout, exit 2 on broker error. Use in
    shell pipelines when you want to wait for a specific reply.

    \b
    agentbus watch --agent-id sparrow --timeout 60
    """
    bus = AgentBus(agent_id=agent_id, broker=broker, port=port)
    msg = asyncio.run(bus.watch_inbox(timeout=timeout))
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
    bus = AgentBus(agent_id="_probe", broker=broker, port=port)
    agents = asyncio.run(bus.list_agents())
    if as_json:
        click.echo(json.dumps(agents))
        return
    if not agents:
        click.echo("[agentbus] no agents online")
        return
    for a in agents:
        click.echo(a)


@main.command("mcp-server")
@click.option("--agent-id", required=True, help="This agent's ID")
@click.option("--broker", default="localhost", show_default=True)
@click.option("--port", default=1883, show_default=True)
def mcp_server(agent_id: str, broker: str, port: int) -> None:
    """Start the MCP sidecar for this agent."""
    from .mcp_server import run_mcp_server
    run_mcp_server(agent_id=agent_id, broker=broker, port=port)
