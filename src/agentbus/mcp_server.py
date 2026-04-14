"""MCP sidecar — exposes agentbus as MCP tools for CC/LLM integration.

Usage: agentbus mcp-server --agent-id sparrow --broker localhost
Register in .claude/settings.json:
  "mcpServers": {
    "agentbus": {
      "command": "agentbus",
      "args": ["mcp-server", "--agent-id", "sparrow", "--broker", "localhost"]
    }
  }
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

import aiomqtt
from pydantic import ValidationError

from .bus import AgentBus
from .message import AgentMessage

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


def create_mcp_app(agent_id: str, broker: str = "localhost", port: int = 1883) -> _MCPApp:
    """Create and return the MCP app (testable without running the server)."""
    bus = AgentBus(agent_id=agent_id, broker=broker, port=port)
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

    @app.tool(name="read_inbox")
    async def read_inbox() -> list[dict]:
        """Poll for queued messages (retain=True). Returns up to 10 recent messages.

        Broker errors are logged and surface as an empty list so a caller
        can distinguish "no messages" from "broker unreachable" only via
        the server log. Malformed envelopes are skipped, not fatal.
        """
        messages: list[dict] = []
        try:
            async with aiomqtt.Client(broker, port=port) as client:
                await client.subscribe(f"agents/{agent_id}/inbox", qos=1)
                try:
                    async with asyncio.timeout(1.0):
                        async for mqtt_msg in client.messages:
                            try:
                                msg = AgentMessage.from_json(mqtt_msg.payload)
                                messages.append(json.loads(msg.to_json()))
                            except (ValidationError, json.JSONDecodeError, ValueError) as exc:
                                logger.warning("read_inbox: skipping bad envelope: %s", exc)
                            if len(messages) >= 10:
                                break
                except asyncio.TimeoutError:
                    pass
        except aiomqtt.MqttError as exc:
            logger.error("read_inbox: broker error (%s:%d): %s", broker, port, exc)
        return messages

    @app.tool(name="watch_inbox")
    async def watch_inbox(timeout: float = 30.0) -> dict | None:
        """Long-poll — blocks until a message arrives, then returns it.

        Returns None on timeout or broker unavailability (the latter is
        logged at ERROR for operator visibility).
        """
        try:
            async with aiomqtt.Client(broker, port=port) as client:
                await client.subscribe(f"agents/{agent_id}/inbox", qos=1)
                try:
                    async with asyncio.timeout(timeout):
                        async for mqtt_msg in client.messages:
                            try:
                                msg = AgentMessage.from_json(mqtt_msg.payload)
                                return json.loads(msg.to_json())
                            except (ValidationError, json.JSONDecodeError, ValueError) as exc:
                                logger.warning("watch_inbox: skipping bad envelope: %s", exc)
                                continue
                except asyncio.TimeoutError:
                    return None
        except aiomqtt.MqttError as exc:
            logger.error("watch_inbox: broker error (%s:%d): %s", broker, port, exc)
        return None

    @app.tool(name="list_agents")
    async def list_agents() -> list[str]:
        """Return IDs of agents currently online.

        Subscribes briefly to `agents/+/presence` and collects retained
        messages. Agents are reported only when their latest retained
        presence message is status=online.
        """
        online: set[str] = set()
        try:
            async with aiomqtt.Client(broker, port=port) as client:
                await client.subscribe("agents/+/presence", qos=0)
                # Retained messages arrive on SUBACK; collect for a short
                # window, then return what we have.
                try:
                    async with asyncio.timeout(0.5):
                        async for mqtt_msg in client.messages:
                            try:
                                payload = json.loads(mqtt_msg.payload)
                            except (json.JSONDecodeError, TypeError, ValueError):
                                continue
                            name = payload.get("agent")
                            status = payload.get("status")
                            if not name:
                                continue
                            if status == "online":
                                online.add(name)
                            else:
                                online.discard(name)
                except asyncio.TimeoutError:
                    pass
        except aiomqtt.MqttError as exc:
            logger.warning("list_agents: broker error: %s", exc)
        return sorted(online)

    return app


def run_mcp_server(agent_id: str, broker: str = "localhost", port: int = 1883) -> None:
    """Start the MCP sidecar. Called by CLI `agentbus mcp-server`."""
    if not _MCP_AVAILABLE:
        raise RuntimeError(
            "mcp package not installed. Run: uv pip install 'agentbus[mcp]'"
        )

    from mcp.server.fastmcp import FastMCP

    mcp = FastMCP("agentbus")
    app = create_mcp_app(agent_id=agent_id, broker=broker, port=port)

    # Register tool functions with the real FastMCP instance
    for name, fn in app._tool_fns.items():
        mcp.tool(name=name)(fn)

    mcp.run(transport="stdio")
