import json
import pytest
from unittest.mock import AsyncMock, patch, MagicMock
from agentbus.mcp_server import create_mcp_app


class _FakePresenceMsg:
    def __init__(self, payload: dict):
        self.payload = json.dumps(payload).encode()


class _FakePresenceClient:
    """Fake aiomqtt client that replays a preset list of retained presence messages."""
    def __init__(self, retained: list[dict]):
        self._retained = retained
    async def __aenter__(self):
        return self
    async def __aexit__(self, *_):
        pass
    async def subscribe(self, *args, **kwargs):
        pass
    @property
    def messages(self):
        async def _gen():
            for p in self._retained:
                yield _FakePresenceMsg(p)
        return _gen()


@pytest.mark.asyncio
async def test_send_message_tool_calls_bus():
    with patch("agentbus.mcp_server.AgentBus") as MockBus:
        instance = MockBus.return_value
        instance.send = AsyncMock()

        app = create_mcp_app(agent_id="sparrow", broker="localhost")
        send_fn = app._tool_fns["send_message"]

        await send_fn(to="wren", subject="hello", body="world")

        instance.send.assert_called_once_with(
            to="wren", subject="hello", body="world",
            content_type="text/plain",
        )


@pytest.mark.asyncio
async def test_list_agents_returns_list():
    with patch("agentbus.mcp_server.AgentBus"), \
         patch("agentbus.mcp_server.aiomqtt.Client", return_value=_FakePresenceClient([])):
        app = create_mcp_app(agent_id="sparrow", broker="localhost")
        list_fn = app._tool_fns["list_agents"]
        result = await list_fn()
        assert isinstance(result, list)
        assert result == []


@pytest.mark.asyncio
async def test_list_agents_reports_online_only():
    retained = [
        {"agent": "sparrow", "status": "online"},
        {"agent": "wren", "status": "online"},
        {"agent": "ghost", "status": "offline"},  # should be filtered
    ]
    with patch("agentbus.mcp_server.AgentBus"), \
         patch("agentbus.mcp_server.aiomqtt.Client", return_value=_FakePresenceClient(retained)):
        app = create_mcp_app(agent_id="sparrow", broker="localhost")
        result = await app._tool_fns["list_agents"]()
    assert set(result) == {"sparrow", "wren"}
    assert result == sorted(result)  # sorted output


@pytest.mark.asyncio
async def test_list_agents_latest_status_wins():
    """If an agent has multiple retained presence messages, latest wins."""
    retained = [
        {"agent": "wren", "status": "online"},
        {"agent": "wren", "status": "offline"},  # supersedes
    ]
    with patch("agentbus.mcp_server.AgentBus"), \
         patch("agentbus.mcp_server.aiomqtt.Client", return_value=_FakePresenceClient(retained)):
        app = create_mcp_app(agent_id="sparrow", broker="localhost")
        result = await app._tool_fns["list_agents"]()
    assert result == []


@pytest.mark.asyncio
async def test_read_inbox_logs_broker_error(caplog):
    """Broker errors must log at ERROR, not silently return []."""
    import aiomqtt as _aiomqtt

    class _BadClient:
        async def __aenter__(self):
            raise _aiomqtt.MqttError("connection refused")
        async def __aexit__(self, *_):
            pass

    with patch("agentbus.mcp_server.AgentBus"), \
         patch("agentbus.mcp_server.aiomqtt.Client", return_value=_BadClient()):
        app = create_mcp_app(agent_id="sparrow", broker="localhost")
        with caplog.at_level("ERROR", logger="agentbus.mcp_server"):
            result = await app._tool_fns["read_inbox"]()
    assert result == []
    assert any("broker error" in r.message for r in caplog.records)


@pytest.mark.asyncio
async def test_list_agents_skips_malformed_payloads():
    class _BadPayloadMsg:
        payload = b"not json at all"
    class _MixedClient:
        async def __aenter__(self): return self
        async def __aexit__(self, *_): pass
        async def subscribe(self, *args, **kwargs): pass
        @property
        def messages(self):
            async def _gen():
                yield _BadPayloadMsg()
                yield _FakePresenceMsg({"agent": "sparrow", "status": "online"})
            return _gen()

    with patch("agentbus.mcp_server.AgentBus"), \
         patch("agentbus.mcp_server.aiomqtt.Client", return_value=_MixedClient()):
        app = create_mcp_app(agent_id="sparrow", broker="localhost")
        result = await app._tool_fns["list_agents"]()
    assert result == ["sparrow"]
