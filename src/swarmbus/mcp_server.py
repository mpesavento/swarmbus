"""MCP sidecar — exposes swarmbus as MCP tools for CC/LLM integration.

Usage: swarmbus mcp-server --agent-id planner --broker localhost
Register in .claude/settings.json:
  "mcpServers": {
    "swarmbus": {
      "command": "swarmbus",
      "args": ["mcp-server", "--agent-id", "planner", "--broker", "localhost"]
    }
  }
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

import aiomqtt

from .bus import AgentBus

logger = logging.getLogger(__name__)

try:
    from mcp.server.fastmcp import FastMCP
    _MCP_AVAILABLE = True
except ImportError:
    _MCP_AVAILABLE = False
    FastMCP = None  # type: ignore[assignment,misc]


class _MCPApp:
    """Thin wrapper that tracks registered tool functions for testing."""
    def __init__(self):
        self._tool_fns: dict[str, Any] = {}

    def tool(self, fn=None, *, name: str | None = None):
        def decorator(f):
            key = name or f.__name__
            self._tool_fns[key] = f
            return f
        return decorator(fn) if fn else decorator


def create_mcp_app(
    agent_id: str,
    broker: str = "localhost",
    port: int = 1883,
    *,
    persistent: bool = False,
    presence: bool = False,
    username: str | None = None,
    password: str | None = None,
    tls: bool = False,
    ca_cert: str | None = None,
    client_cert: str | None = None,
    client_key: str | None = None,
) -> _MCPApp:
    """Create and return the MCP app (testable without running the server)."""
    bus = AgentBus(
        agent_id=agent_id,
        broker=broker,
        port=port,
        persistent=persistent,
        username=username,
        password=password,
        tls=tls,
        ca_cert=ca_cert,
        client_cert=client_cert,
        client_key=client_key,
    )
    app = _MCPApp()

    @app.tool(name="send_message")
    async def send_message(
        to: str,
        subject: str,
        body: str,
        content_type: str = "text/plain",
    ) -> str:
        """Send a message to another agent."""
        await bus.send(to=to, subject=subject, body=body, content_type=content_type)
        return f"Sent to {to}"

    # MCP tools intentionally degrade to empty/None on broker failure and log
    # at ERROR — LLM callers have no useful recovery path and benefit from a
    # uniform return contract. The CLI has the opposite contract (propagate
    # and exit 2), so the swallow lives here, not in AgentBus.

    @app.tool(name="read_inbox")
    async def read_inbox() -> list[dict]:
        """Poll for queued messages. Returns up to 10 recent messages."""
        try:
            return await bus.read_inbox()
        except aiomqtt.MqttError as exc:
            logger.error("read_inbox: broker error (%s:%d): %s", broker, port, exc)
            return []

    @app.tool(name="watch_inbox")
    async def watch_inbox(timeout: float = 30.0) -> dict | None:
        """Long-poll — blocks until a message arrives, then returns it."""
        try:
            return await bus.watch_inbox(timeout=timeout)
        except aiomqtt.MqttError as exc:
            logger.error("watch_inbox: broker error (%s:%d): %s", broker, port, exc)
            return None

    @app.tool(name="list_agents")
    async def list_agents() -> list[str]:
        """Return IDs of agents currently online."""
        try:
            return await bus.list_agents()
        except aiomqtt.MqttError as exc:
            logger.warning("list_agents: broker error: %s", exc)
            return []

    return app


def run_mcp_server(
    agent_id: str,
    broker: str = "localhost",
    port: int = 1883,
    *,
    persistent: bool = False,
    presence: bool = False,
    username: str | None = None,
    password: str | None = None,
    tls: bool = False,
    ca_cert: str | None = None,
    client_cert: str | None = None,
    client_key: str | None = None,
) -> None:
    """Start the MCP sidecar. Called by CLI `swarmbus mcp-server`.

    When ``presence`` is True, publishes a retained "online" message to
    ``agents/<agent_id>/presence`` on startup and "offline" on shutdown.
    This makes the agent visible to ``list_agents`` without requiring a
    separate listener daemon.
    """
    if not _MCP_AVAILABLE:
        raise RuntimeError(
            "mcp package not installed. Run: uv pip install 'swarmbus[mcp]''"
        )

    from mcp.server.fastmcp import FastMCP

    mcp = FastMCP("swarmbus")
    app = create_mcp_app(
        agent_id=agent_id,
        broker=broker,
        port=port,
        persistent=persistent,
        presence=presence,
        username=username,
        password=password,
        tls=tls,
        ca_cert=ca_cert,
        client_cert=client_cert,
        client_key=client_key,
    )

    # Register tool functions with the real FastMCP instance
    for name, fn in app._tool_fns.items():
        mcp.tool(name=name)(fn)

    # Presence lifecycle: announce online before serving, offline on exit.
    bus: AgentBus | None = None
    if presence:
        bus = AgentBus(
            agent_id=agent_id,
            broker=broker,
            port=port,
            persistent=False,  # presence uses a separate ephemeral connection
            username=username,
            password=password,
            tls=tls,
            ca_cert=ca_cert,
            client_cert=client_cert,
            client_key=client_key,
        )
        try:
            asyncio.run(bus.announce())
            logger.info("Published online presence for %s", agent_id)
        except Exception as exc:
            logger.warning("Failed to publish online presence: %s", exc)

    try:
        mcp.run(transport="stdio")
    finally:
        if bus is not None:
            try:
                asyncio.run(bus.disconnect())
                logger.info("Published offline presence for %s", agent_id)
            except Exception as exc:
                logger.warning("Failed to publish offline presence: %s", exc)
