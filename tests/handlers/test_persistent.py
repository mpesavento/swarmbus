import asyncio
import pytest
from swarmbus.handlers.persistent import PersistentListenerHandler
from swarmbus.message import AgentMessage


@pytest.fixture
def msg():
    return AgentMessage.create(
        from_="wren", to="sparrow", subject="ping", body="hi",
    )


@pytest.mark.asyncio
async def test_tracks_message_count(msg):
    handler = PersistentListenerHandler()
    assert handler.stats()["messages_received"] == 0
    await handler.handle(msg)
    await handler.handle(msg)
    assert handler.stats()["messages_received"] == 2


@pytest.mark.asyncio
async def test_tracks_last_message_ts(msg):
    handler = PersistentListenerHandler()
    assert handler.stats()["last_message_ts"] is None
    await handler.handle(msg)
    assert handler.stats()["last_message_ts"] is not None


@pytest.mark.asyncio
async def test_heartbeat_calls_publish_fn():
    handler = PersistentListenerHandler(heartbeat_interval=0.05)
    calls = []

    async def fake_publish():
        calls.append(1)

    task = asyncio.create_task(handler.start_heartbeat(fake_publish))
    await asyncio.sleep(0.15)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    assert len(calls) >= 2  # fired at least twice in 0.15s with 0.05s interval


@pytest.mark.asyncio
async def test_heartbeat_sets_started_at():
    handler = PersistentListenerHandler(heartbeat_interval=10)
    assert handler.stats()["started_at"] is None

    async def noop():
        pass

    task = asyncio.create_task(handler.start_heartbeat(noop))
    await asyncio.sleep(0.01)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    assert handler.stats()["started_at"] is not None
