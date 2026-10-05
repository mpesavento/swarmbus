from typing import get_args, get_type_hints
from unittest.mock import AsyncMock, MagicMock

import aiomqtt
import pytest

from swarmbus.mcp_server import create_mcp_app
from swarmbus.message import AgentMessage
from swarmbus.registry import presence_payload
from swarmbus.runtime import (
    ManagedMCPRuntime,
    PresenceRequiredError,
    RuntimeFatalError,
    StoreUnavailable,
    TransportUnavailable,
)


class _FakeRuntime:
    broker = "localhost"
    port = 1883

    def __init__(self):
        self.send_message = AsyncMock()
        self.read_inbox = AsyncMock(return_value=[])
        self.list_states = AsyncMock(return_value=[])
        self.get_state = AsyncMock(return_value={})
        self.update_state = AsyncMock(return_value={})


def _directory_runtime(tmp_path) -> ManagedMCPRuntime:
    """Connected, presence-enabled runtime holding one online peer."""
    runtime = ManagedMCPRuntime(
        agent_id="foo",
        presence=True,
        state_path=tmp_path / "foo.sqlite3",
    )
    runtime._client = MagicMock()
    runtime.registry.update_presence(
        runtime.topics.presence("wren"),
        presence_payload("wren", "online"),
    )
    return runtime


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
async def test_agent_state_consolidates_list_get_and_update_actions():
    runtime = _FakeRuntime()
    runtime.list_states.return_value = [{"agent_id": "wren"}]
    runtime.get_state.return_value = {"agent_id": "wren"}
    runtime.update_state.return_value = {"agent_id": "foo", "status": "working"}
    app = create_mcp_app(runtime)

    listed = await app._tool_fns["agent_state"](
        action="list",
        include_offline=True,
        lifecycle="transient",
    )
    fetched = await app._tool_fns["agent_state"](
        action="get",
        agent_id="wren",
    )
    updated = await app._tool_fns["agent_state"](
        action="update",
        status="working",
        capabilities=["development.files.write"],
    )

    assert listed == [{"agent_id": "wren"}]
    assert fetched == {"agent_id": "wren"}
    assert updated["status"] == "working"
    runtime.list_states.assert_awaited_once_with(
        include_offline=True,
        lifecycle="transient",
    )
    runtime.get_state.assert_awaited_once_with("wren")
    runtime.update_state.assert_awaited_once_with(
        status="working",
        working_set=None,
        capabilities=["development.files.write"],
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "kwargs",
    [
        {"action": "list", "agent_id": "wren"},
        {"action": "get"},
        {"action": "get", "agent_id": "wren", "include_offline": True},
        {"action": "update", "agent_id": "wren", "status": "nope"},
        {"action": "update"},
        {"action": "unknown"},
    ],
)
async def test_agent_state_rejects_invalid_action_arguments(kwargs):
    app = create_mcp_app(_FakeRuntime())

    with pytest.raises(ValueError):
        await app._tool_fns["agent_state"](**kwargs)


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
async def test_runtime_fatal_error_surfaces_as_mcp_tool_failure():
    runtime = _FakeRuntime()
    runtime.read_inbox.side_effect = RuntimeFatalError(
        "managed MQTT runtime failed"
    )
    app = create_mcp_app(runtime)

    with pytest.raises(RuntimeFatalError, match="managed MQTT runtime failed"):
        await app._tool_fns["read_inbox"]()


_AGENT_STATE_CALLS = {
    "list": {"action": "list"},
    "get": {"action": "get", "agent_id": "wren"},
    "update": {"action": "update", "status": "busy"},
}


def test_agent_state_action_matrix_covers_every_declared_action():
    """Cover every agent_state action with presence-refusal tests."""
    app = create_mcp_app(_FakeRuntime())
    hints = get_type_hints(app._tool_fns["agent_state"])

    assert set(_AGENT_STATE_CALLS) == set(get_args(hints["action"]))


@pytest.mark.asyncio
async def test_presence_free_runtime_refuses_every_directory_action(tmp_path):
    """A live messaging-only runtime rejects every directory action."""
    runtime = ManagedMCPRuntime(
        agent_id="foo",
        state_path=tmp_path / "foo.sqlite3",
    )
    runtime._client = MagicMock()
    app = create_mcp_app(runtime)

    for kwargs in _AGENT_STATE_CALLS.values():
        with pytest.raises(PresenceRequiredError):
            await app._tool_fns["agent_state"](**kwargs)


@pytest.mark.asyncio
async def test_agent_state_list_surfaces_transport_failure_to_the_caller(
    tmp_path,
):
    """agent_state list propagates presence and transport failures."""
    runtime = _directory_runtime(tmp_path)
    app = create_mcp_app(runtime)

    listed = await app._tool_fns["agent_state"](action="list")

    assert [state["agent_id"] for state in listed] == ["wren"]

    runtime._client = None

    with pytest.raises(TransportUnavailable, match="disconnected"):
        await app._tool_fns["agent_state"](action="list")


@pytest.mark.asyncio
async def test_agent_state_get_surfaces_transport_failure_to_the_caller(
    tmp_path,
):
    """agent_state get propagates transport failures."""
    runtime = _directory_runtime(tmp_path)
    app = create_mcp_app(runtime)

    fetched = await app._tool_fns["agent_state"](action="get", agent_id="wren")

    assert fetched["agent_id"] == "wren"
    with pytest.raises(ValueError, match="no registry record"):
        await app._tool_fns["agent_state"](action="get", agent_id="sparrow")

    runtime._client = None

    for agent_id in ("wren", "sparrow"):
        with pytest.raises(TransportUnavailable, match="disconnected"):
            await app._tool_fns["agent_state"](
                action="get",
                agent_id=agent_id,
            )


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
