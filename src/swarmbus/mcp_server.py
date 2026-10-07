"""MCP sidecar exposing swarmbus tools over a lifespan-managed runtime."""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator, Literal, Protocol

import aiomqtt

from .runtime import ManagedMCPRuntime, TransportUnavailable

logger = logging.getLogger(__name__)

try:
    from mcp.server.fastmcp import FastMCP
    _MCP_AVAILABLE = True
except ImportError:
    _MCP_AVAILABLE = False
    FastMCP = None  # type: ignore[assignment,misc]


class _MCPApp:
    """Thin wrapper that tracks registered tool functions for testing."""

    def __init__(self) -> None:
        self._tool_fns: dict[str, Any] = {}

    def tool(self, fn=None, *, name: str | None = None):
        def decorator(f):
            key = name or f.__name__
            self._tool_fns[key] = f
            return f

        return decorator(fn) if fn else decorator


class _MCPRuntime(Protocol):
    broker: str
    port: int

    async def send_message(
        self,
        *,
        to: str,
        subject: str,
        body: str,
        content_type: str,
    ) -> None: ...

    async def read_inbox(
        self,
        *,
        ack_ids: list[str] | None,
        max_messages: int,
        wait_seconds: float,
    ) -> list[dict]: ...

    async def list_states(
        self,
        *,
        include_offline: bool,
        lifecycle: Literal["persistent", "transient"] | None,
    ) -> list[dict]: ...

    async def get_state(self, agent_id: str) -> dict: ...

    async def update_state(
        self,
        *,
        status: str | None,
        working_set: list[str] | None,
        capabilities: list[str] | None,
    ) -> dict: ...


def create_mcp_app(runtime: _MCPRuntime) -> _MCPApp:
    """Create the MCP tool surface over the lifespan-owned runtime."""
    app = _MCPApp()

    @app.tool(name="send_message")
    async def send_message(
        to: str,
        subject: str,
        body: str,
        content_type: str = "text/plain",
    ) -> str:
        """Send a message to another agent."""
        await runtime.send_message(
            to=to,
            subject=subject,
            body=body,
            content_type=content_type,
        )
        return f"Sent to {to}"

    @app.tool(name="read_inbox")
    async def read_inbox(
        ack_ids: list[str] | None = None,
        max_messages: int = 10,
        wait_seconds: float = 0.0,
    ) -> list[dict]:
        """Acknowledge handled messages, then read or wait for pending ones.

        Messages remain pending until a later call includes their IDs in
        ack_ids. Set max_messages to zero for an acknowledgement-only call.
        Set wait_seconds above zero to wait when the inbox is empty.
        """
        try:
            return await runtime.read_inbox(
                ack_ids=ack_ids,
                max_messages=max_messages,
                wait_seconds=wait_seconds,
            )
        except (aiomqtt.MqttError, TransportUnavailable) as exc:
            logger.error(
                "read_inbox: broker error (%s:%d): %s",
                runtime.broker,
                runtime.port,
                exc,
            )
            return []

    @app.tool(name="agent_state")
    async def agent_state(
        action: Literal["list", "get", "update"],
        agent_id: str | None = None,
        status: str | None = None,
        working_set: list[str] | None = None,
        capabilities: list[str] | None = None,
        include_offline: bool = False,
        lifecycle: Literal["persistent", "transient"] | None = None,
    ) -> list[dict] | dict:
        """List, get, or update registry state; requires presence and a live transport."""
        if action == "list":
            if (
                agent_id is not None
                or status is not None
                or working_set is not None
                or capabilities is not None
            ):
                raise ValueError(
                    "list only accepts include_offline and lifecycle"
                )
            return await runtime.list_states(
                include_offline=include_offline,
                lifecycle=lifecycle,
            )
        if action == "get":
            if agent_id is None:
                raise ValueError("get requires agent_id")
            if (
                status is not None
                or working_set is not None
                or capabilities is not None
                or include_offline
                or lifecycle is not None
            ):
                raise ValueError("get only accepts agent_id")
            return await runtime.get_state(agent_id)
        if action == "update":
            if agent_id is not None:
                raise ValueError("update cannot target another agent")
            if include_offline or lifecycle is not None:
                raise ValueError(
                    "update does not accept include_offline or lifecycle"
                )
            if (
                status is None
                and working_set is None
                and capabilities is None
            ):
                raise ValueError(
                    "update requires status, working_set, or capabilities"
                )
            return await runtime.update_state(
                status=status,
                working_set=working_set,
                capabilities=capabilities,
            )
        raise ValueError(f"unknown action {action!r}")

    return app


def run_mcp_server(
    agent_id: str,
    broker: str = "localhost",
    port: int = 1883,
    *,
    durable: bool = False,
    presence: bool = False,
    lifecycle: Literal["persistent", "transient"] = "persistent",
    client_id: str | None = None,
    capabilities: tuple[str, ...] = (),
    state_dir: str = "~/.local/state/swarmbus",
    registry_heartbeat_seconds: float = 60,
    registry_stale_after_seconds: float = 180,
    username: str | None = None,
    password: str | None = None,
    tls: bool = False,
    ca_cert: str | None = None,
    client_cert: str | None = None,
    client_key: str | None = None,
) -> None:
    """Start the MCP sidecar with one managed MQTT connection."""
    if not _MCP_AVAILABLE:
        raise RuntimeError(
            "mcp package not installed. Run: uv pip install 'swarmbus[mcp]'"
        )

    from mcp.server.fastmcp import FastMCP

    state_path = Path(state_dir).expanduser() / f"{agent_id}.sqlite3"
    runtime = ManagedMCPRuntime(
        agent_id=agent_id,
        broker=broker,
        port=port,
        durable=durable,
        presence=presence,
        lifecycle=lifecycle,
        client_id=client_id,
        heartbeat_seconds=registry_heartbeat_seconds,
        stale_after_seconds=registry_stale_after_seconds,
        capabilities=capabilities,
        state_path=state_path,
        username=username,
        password=password,
        tls=tls,
        ca_cert=ca_cert,
        client_cert=client_cert,
        client_key=client_key,
    )

    @asynccontextmanager
    async def lifespan(server: FastMCP) -> AsyncIterator[dict]:
        await runtime.start()
        try:
            yield {"runtime": runtime}
        finally:
            await runtime.stop()

    mcp = FastMCP("swarmbus", lifespan=lifespan)
    app = create_mcp_app(runtime)
    for name, fn in app._tool_fns.items():
        mcp.tool(name=name)(fn)

    mcp.run(transport="stdio")
