import pytest
from unittest.mock import AsyncMock, patch, MagicMock
from agentbus.mcp_server import create_mcp_app


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
    with patch("agentbus.mcp_server.AgentBus"):
        app = create_mcp_app(agent_id="sparrow", broker="localhost")
        list_fn = app._tool_fns["list_agents"]
        result = await list_fn()
        assert isinstance(result, list)
