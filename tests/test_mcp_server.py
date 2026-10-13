from unittest.mock import AsyncMock, MagicMock

import aiomqtt
import pytest

from swarmbus.mcp_server import create_mcp_app
from swarmbus.message import AgentMessage
from swarmbus.runtime import (
    ManagedMCPRuntime,
    RuntimeFatalError,
    StoreUnavailable,
)


class _FakeRuntime:
    broker = "localhost"
    port = 1883

    def __init__(self):
        self.send_message = AsyncMock()
        self.read_inbox = AsyncMock(return_value=[])
        self.list_agents = AsyncMock(return_value=[])


@pytest.mark.asyncio
async def test_send_message_tool_calls_runtime():
    runtime = _FakeRuntime()
    app = create_mcp_app(runtime)

    result = await app._tool_fns["send_message"](
        to="wren",
        subject="hello",
        body="world",
    )

    assert result == "Sent to wren"
    runtime.send_message.assert_awaited_once_with(
        to="wren",
        subject="hello",
        body="world",
        content_type="text/plain",
    )


@pytest.mark.asyncio
async def test_read_inbox_uses_runtime():
    runtime = _FakeRuntime()
    runtime.read_inbox.return_value = [{"id": "message-1"}]
    app = create_mcp_app(runtime)

    result = await app._tool_fns["read_inbox"]()

    assert result == [{"id": "message-1"}]
    runtime.read_inbox.assert_awaited_once_with(
        ack_ids=None,
        max_messages=10,
        wait_seconds=0.0,
    )


@pytest.mark.asyncio
async def test_read_inbox_combines_ack_limit_and_wait():
    runtime = _FakeRuntime()
    runtime.read_inbox.return_value = [{"id": "message-3"}]
    app = create_mcp_app(runtime)

    result = await app._tool_fns["read_inbox"](
        ack_ids=["message-1", "message-2"],
        max_messages=4,
        wait_seconds=12.5,
    )

    assert result == [{"id": "message-3"}]
    runtime.read_inbox.assert_awaited_once_with(
        ack_ids=["message-1", "message-2"],
        max_messages=4,
        wait_seconds=12.5,
    )


@pytest.mark.asyncio
async def test_read_inbox_supports_ack_only():
    runtime = _FakeRuntime()
    app = create_mcp_app(runtime)

    result = await app._tool_fns["read_inbox"](
        ack_ids=["message-1"],
        max_messages=0,
    )

    assert result == []
    runtime.read_inbox.assert_awaited_once_with(
        ack_ids=["message-1"],
        max_messages=0,
        wait_seconds=0.0,
    )


@pytest.mark.asyncio
async def test_list_agents_uses_runtime():
    runtime = _FakeRuntime()
    runtime.list_agents.return_value = ["sparrow", "wren"]
    app = create_mcp_app(runtime)

    result = await app._tool_fns["list_agents"]()

    assert result == ["sparrow", "wren"]
    runtime.list_agents.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_read_inbox_logs_broker_error(caplog):
    runtime = _FakeRuntime()
    runtime.read_inbox.side_effect = aiomqtt.MqttError("connection refused")
    app = create_mcp_app(runtime)

    with caplog.at_level("ERROR", logger="swarmbus.mcp_server"):
        result = await app._tool_fns["read_inbox"]()

    assert result == []
    assert any("broker error" in record.message for record in caplog.records)


@pytest.mark.asyncio
@pytest.mark.parametrize("tool_name", ["read_inbox", "list_agents"])
async def test_runtime_fatal_error_surfaces_as_mcp_tool_failure(tool_name):
    runtime = _FakeRuntime()
    getattr(runtime, tool_name).side_effect = RuntimeFatalError(
        "managed MQTT runtime failed"
    )
    app = create_mcp_app(runtime)

    with pytest.raises(RuntimeFatalError, match="managed MQTT runtime failed"):
        await app._tool_fns[tool_name]()


@pytest.mark.asyncio
async def test_store_degraded_error_surfaces_as_mcp_tool_failure():
    runtime = _FakeRuntime()
    runtime.read_inbox.side_effect = StoreUnavailable(
        "durable inbox is degraded"
    )
    app = create_mcp_app(runtime)

    with pytest.raises(StoreUnavailable, match="durable inbox is degraded"):
        await app._tool_fns["read_inbox"]()


class _RuntimeMessage:
    def __init__(self, message: AgentMessage):
        self.topic = "agents/foo/inbox"
        self.payload = message.to_json().encode()
        self.mid = 7
        self.qos = 1


class _RuntimeAck:
    def __init__(self):
        self.acked = []

    def ack(self, message):
        self.acked.append(message.mid)


@pytest.mark.asyncio
async def test_managed_runtime_ingestion_survives_restart_and_is_queryable(tmp_path):
    state_path = tmp_path / "foo.sqlite3"
    receiver = ManagedMCPRuntime(agent_id="foo", state_path=state_path)
    await receiver.store.open()
    message = AgentMessage.create(
        from_="wren",
        to="foo",
        subject="persisted",
        body="read through MCP",
    )
    ack = _RuntimeAck()

    await receiver._handle_message(_RuntimeMessage(message), ack)
    await receiver.store.close()

    restarted = ManagedMCPRuntime(agent_id="foo", state_path=state_path)
    await restarted.store.open()
    restarted._client = MagicMock()
    app = create_mcp_app(restarted)
    first = await app._tool_fns["read_inbox"]()
    second = await app._tool_fns["read_inbox"]()
    third = await app._tool_fns["read_inbox"](
        ack_ids=[message.id],
        max_messages=0,
    )

    assert [item["id"] for item in first] == [message.id]
    assert [item["id"] for item in second] == [message.id]
    assert third == []
    assert ack.acked == [7]
    await restarted.store.close()
