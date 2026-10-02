import asyncio
import json
import logging
import sqlite3
from unittest.mock import AsyncMock

import aiomqtt
import pytest

from swarmbus.message import AgentMessage
from swarmbus.runtime import (
    AcknowledgementError,
    ManagedMCPRuntime,
    RuntimeFatalError,
    RuntimeState,
    StoreUnavailable,
    TransportUnavailable,
    _ManualAckAdapter,
)


class _FakePaho:
    def __init__(self):
        self.enabled = None
        self.acks = []

    def manual_ack_set(self, enabled):
        self.enabled = enabled

    def ack(self, mid, qos):
        self.acks.append((mid, qos))
        return 0


class _FakeClient:
    def __init__(self):
        self._client = _FakePaho()
        self.publish = AsyncMock()


class _FakeStore:
    def __init__(self, events):
        self.events = events
        self.messages = []
        self.opened = False
        self.closed = False

    async def open(self):
        self.opened = True

    async def close(self):
        self.closed = True

    async def store(self, msg, *, source_topic):
        self.events.append("commit")
        self.messages.append(msg)
        return True

    async def read(self, max_messages=10):
        return [
            json.loads(msg.to_json())
            for msg in self.messages[:max_messages]
        ]

    async def ack(self, message_ids):
        acknowledged = 0
        remaining = []
        for message in self.messages:
            if message.id in message_ids:
                acknowledged += 1
            else:
                remaining.append(message)
        self.messages = remaining
        return acknowledged


class _FakeMqttMessage:
    def __init__(self, msg):
        self.topic = "agents/foo/inbox"
        self.payload = msg.to_json().encode()
        self.mid = 42
        self.qos = 1


class _RecordingAck:
    def __init__(self, events=None):
        self.events = events
        self.messages = []

    def ack(self, message):
        if self.events is not None:
            self.events.append("ack")
        self.messages.append(message)


def _runtime(store, **kwargs):
    return ManagedMCPRuntime(
        agent_id="foo",
        broker="localhost",
        state_path="unused.sqlite3",
        store=store,
        **kwargs,
    )


def _message():
    return AgentMessage.create(
        from_="wren",
        to="foo",
        subject="hey",
        body="yo",
    )


def test_manual_ack_adapter_enables_paho_and_acks_message():
    client = _FakeClient()
    adapter = _ManualAckAdapter(client)

    adapter.enable()
    adapter.ack(_FakeMqttMessage(_message()))

    assert client._client.enabled is True
    assert client._client.acks == [(42, 1)]


def test_manual_ack_adapter_rejects_unsupported_client():
    class _UnsupportedClient:
        _client = object()

    with pytest.raises(RuntimeError, match="does not support manual ACK"):
        _ManualAckAdapter(_UnsupportedClient())


@pytest.mark.asyncio
async def test_inbox_commit_happens_before_qos1_ack():
    events = []
    runtime = _runtime(_FakeStore(events))
    ack = _RecordingAck(events)

    await runtime._handle_message(_FakeMqttMessage(_message()), ack)

    assert events == ["commit", "ack"]


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [OSError("disk full"), sqlite3.OperationalError("locked")])
async def test_store_failure_leaves_message_unacked(error):
    class _BrokenStore(_FakeStore):
        async def store(self, msg, *, source_topic):
            raise error

    ack = _RecordingAck()
    runtime = _runtime(_BrokenStore([]), persistent=True)

    with pytest.raises(type(error), match=str(error)):
        await runtime._handle_message(_FakeMqttMessage(_message()), ack)

    assert ack.messages == []


@pytest.mark.asyncio
async def test_live_session_retries_held_message_with_capped_backoff(monkeypatch):
    class _FlakyStore(_FakeStore):
        def __init__(self, events):
            super().__init__(events)
            self.failures_remaining = 7

        async def store(self, msg, *, source_topic):
            if self.failures_remaining:
                self.failures_remaining -= 1
                raise sqlite3.OperationalError("locked")
            return await super().store(msg, source_topic=source_topic)

    delays = []

    async def record_sleep(delay):
        delays.append(delay)

    events = []
    runtime = _runtime(_FlakyStore(events))
    ack = _RecordingAck(events)
    monkeypatch.setattr("swarmbus.runtime.asyncio.sleep", record_sleep)

    await runtime._handle_message(_FakeMqttMessage(_message()), ack)

    assert delays == [1.0, 2.0, 4.0, 8.0, 16.0, 32.0, 60.0]
    assert events == ["commit", "ack"]
    assert runtime.state is RuntimeState.CONNECTED


@pytest.mark.asyncio
async def test_invalid_envelope_is_acked_without_being_stored():
    store = _FakeStore([])
    runtime = _runtime(store)
    ack = _RecordingAck()
    message = _FakeMqttMessage(_message())
    message.payload = b"not-json"

    await runtime._handle_message(message, ack)

    assert store.messages == []
    assert ack.messages == [message]


@pytest.mark.asyncio
async def test_presence_updates_list_agents_from_retained_protocol():
    runtime = _runtime(_FakeStore([]))
    runtime._client = _FakeClient()
    ack = _RecordingAck()

    online = _FakeMqttMessage(_message())
    online.topic = "agents/wren/presence"
    online.payload = json.dumps({"agent": "wren", "status": "online"}).encode()
    await runtime._handle_message(online, ack)

    assert await runtime.list_agents() == ["wren"]

    offline = _FakeMqttMessage(_message())
    offline.topic = "agents/wren/presence"
    offline.payload = json.dumps({"agent": "wren", "status": "offline"}).encode()
    await runtime._handle_message(offline, ack)

    assert await runtime.list_agents() == []


@pytest.mark.asyncio
async def test_presence_identity_mismatch_is_acked_and_ignored():
    runtime = _runtime(_FakeStore([]))
    runtime._client = _FakeClient()
    ack = _RecordingAck()
    message = _FakeMqttMessage(_message())
    message.topic = "agents/wren/presence"
    message.payload = json.dumps({"agent": "sparrow", "status": "online"}).encode()

    await runtime._handle_message(message, ack)

    assert await runtime.list_agents() == []
    assert ack.messages == [message]


@pytest.mark.asyncio
async def test_read_inbox_long_poll_wakes_after_committed_message():
    events = []
    store = _FakeStore(events)
    runtime = _runtime(store)
    runtime._client = _FakeClient()
    waiter = asyncio.create_task(runtime.read_inbox(wait_seconds=1))

    await asyncio.sleep(0)
    await runtime._handle_message(
        _FakeMqttMessage(_message()),
        _RecordingAck(events),
    )
    result = await waiter

    assert result[0]["body"] == "yo"
    assert events == ["commit", "ack"]


@pytest.mark.asyncio
async def test_read_inbox_waits_for_startup_replay_commit():
    store = _FakeStore([])
    runtime = _runtime(store, persistent=True)
    runtime._client = _FakeClient()
    runtime._replay_activity_at = asyncio.get_running_loop().time()
    runtime._replay_settled = False

    read_task = asyncio.create_task(runtime.read_inbox())
    await asyncio.sleep(0)
    assert read_task.done() is False

    message = _message()
    store.messages.append(message)
    result = await asyncio.wait_for(read_task, timeout=1)

    assert [item["id"] for item in result] == [message.id]
    assert runtime._replay_settled is True


@pytest.mark.asyncio
async def test_read_inbox_requires_explicit_ack():
    message = _message()
    runtime = _runtime(_FakeStore([]))
    runtime.store.messages.append(message)
    runtime._client = _FakeClient()

    first = await runtime.read_inbox()
    second = await runtime.read_inbox()
    third = await runtime.read_inbox(ack_ids=[message.id])

    assert [item["id"] for item in first] == [message.id]
    assert [item["id"] for item in second] == [message.id]
    assert third == []


@pytest.mark.asyncio
async def test_read_inbox_ack_only_succeeds_while_disconnected():
    message = _message()
    store = _FakeStore([])
    store.messages.append(message)
    runtime = _runtime(store)

    result = await runtime.read_inbox(
        ack_ids=[message.id],
        max_messages=0,
        wait_seconds=30,
    )

    assert result == []
    assert store.messages == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("kwargs", "match"),
    [
        ({"max_messages": -1}, "max_messages"),
        ({"wait_seconds": -1}, "wait_seconds"),
    ],
)
async def test_read_inbox_validates_before_ack(kwargs, match):
    message = _message()
    store = _FakeStore([])
    store.messages.append(message)
    runtime = _runtime(store)

    with pytest.raises(ValueError, match=match):
        await runtime.read_inbox(ack_ids=[message.id], **kwargs)

    assert store.messages == [message]


@pytest.mark.asyncio
async def test_empty_inbox_reports_disconnected_transport():
    runtime = _runtime(_FakeStore([]))

    with pytest.raises(TransportUnavailable, match="disconnected"):
        await runtime.read_inbox()
    with pytest.raises(TransportUnavailable, match="disconnected"):
        await runtime.watch_inbox(timeout=0.01)
    with pytest.raises(TransportUnavailable, match="disconnected"):
        await runtime.list_agents()


@pytest.mark.asyncio
async def test_store_degraded_empty_reads_surface_failure():
    runtime = _runtime(_FakeStore([]))
    runtime._client = _FakeClient()
    runtime._state = RuntimeState.STORE_DEGRADED
    runtime._last_store_error = sqlite3.OperationalError("disk full")

    with pytest.raises(StoreUnavailable, match="disk full"):
        await runtime.read_inbox()
    with pytest.raises(StoreUnavailable, match="disk full"):
        await runtime.watch_inbox(timeout=0.01)


@pytest.mark.asyncio
async def test_long_poll_surfaces_store_failure_that_begins_while_waiting():
    store = _FakeStore([])
    store.store = AsyncMock(side_effect=sqlite3.OperationalError("disk full"))
    runtime = _runtime(store)
    runtime._client = _FakeClient()
    waiter = asyncio.create_task(runtime.read_inbox(wait_seconds=10))

    await asyncio.sleep(0)
    handler = asyncio.create_task(
        runtime._handle_message(
            _FakeMqttMessage(_message()),
            _RecordingAck(),
        )
    )
    try:
        with pytest.raises(StoreUnavailable, match="disk full"):
            await asyncio.wait_for(waiter, timeout=0.2)
    finally:
        handler.cancel()
        await asyncio.gather(handler, return_exceptions=True)


@pytest.mark.asyncio
async def test_start_opens_store_without_waiting_for_broker():
    store = _FakeStore([])
    runtime = _runtime(store)
    connection_started = asyncio.Event()
    keep_running = asyncio.Event()

    async def _connection_loop():
        connection_started.set()
        await keep_running.wait()

    runtime._connection_loop = _connection_loop
    await asyncio.wait_for(runtime.start(), timeout=0.1)
    await asyncio.wait_for(connection_started.wait(), timeout=0.1)

    assert store.opened is True
    assert runtime._connection_task is not None
    await runtime.stop()
    assert store.closed is True


@pytest.mark.asyncio
async def test_start_rejects_missing_manual_ack_before_opening_store(monkeypatch):
    class _UnsupportedClient:
        def __init__(self, *args, **kwargs):
            self._client = object()

    store = _FakeStore([])
    runtime = _runtime(store)
    monkeypatch.setattr("swarmbus.runtime.aiomqtt.Client", _UnsupportedClient)

    with pytest.raises(RuntimeFatalError, match="manual ACK"):
        await runtime.start()

    assert runtime.state is RuntimeState.FATAL
    assert runtime.last_fatal_error is not None
    assert store.opened is False
    assert runtime._connection_task is None
    await runtime.stop()
    await runtime.stop()
    assert store.closed is True


@pytest.mark.asyncio
async def test_background_connection_failure_is_recorded_and_stop_is_safe(caplog):
    store = _FakeStore([])
    runtime = _runtime(store)

    async def _broken_connection_loop():
        raise RuntimeError("receiver exploded")

    runtime._connection_loop = _broken_connection_loop
    with caplog.at_level(logging.ERROR, logger="swarmbus.runtime"):
        await runtime.start()
        await asyncio.sleep(0)
        await asyncio.sleep(0)

    assert "managed MQTT runtime stopped: receiver exploded" in caplog.text
    assert runtime.state is RuntimeState.FATAL
    assert str(runtime.last_fatal_error) == "receiver exploded"
    with pytest.raises(RuntimeFatalError, match="receiver exploded"):
        await runtime.read_inbox()
    await runtime.stop()
    await runtime.stop()
    assert store.closed is True


@pytest.mark.asyncio
async def test_ack_failure_reconnects_instead_of_killing_receiver(monkeypatch):
    class _Messages:
        def __init__(self, messages):
            self._messages = iter(messages)

        def __aiter__(self):
            return self

        async def __anext__(self):
            try:
                return next(self._messages)
            except StopIteration:
                raise StopAsyncIteration

    class _AckPaho(_FakePaho):
        def __init__(self, result):
            super().__init__()
            self.result = result

        def ack(self, mid, qos):
            super().ack(mid, qos)
            return self.result

    class _LoopClient:
        def __init__(self, paho, messages, on_enter=None):
            self._client = paho
            self.messages = _Messages(messages)
            self.subscribe = AsyncMock()
            self.on_enter = on_enter

        async def __aenter__(self):
            if self.on_enter is not None:
                self.on_enter()
            return self

        async def __aexit__(self, exc_type, exc, traceback):
            return False

    store = _FakeStore([])
    runtime = _runtime(store)
    first = _LoopClient(
        _AckPaho(1),
        [_FakeMqttMessage(_message())],
    )
    second = _LoopClient(
        _AckPaho(0),
        [],
        on_enter=runtime._stopping.set,
    )
    clients = iter([first, second])
    monkeypatch.setattr(
        "swarmbus.runtime.aiomqtt.Client",
        lambda *args, **kwargs: next(clients),
    )
    monkeypatch.setattr("swarmbus.runtime.asyncio.sleep", AsyncMock())

    await runtime._connection_loop()

    assert first._client.acks == [(42, 1)]
    assert runtime.last_fatal_error is None


@pytest.mark.asyncio
async def test_store_failure_marks_runtime_degraded_before_reconnect(monkeypatch):
    class _BrokenStore(_FakeStore):
        async def store(self, msg, *, source_topic):
            raise sqlite3.OperationalError("locked")

    class _Messages:
        def __init__(self, messages):
            self._messages = iter(messages)

        def __aiter__(self):
            return self

        async def __anext__(self):
            try:
                return next(self._messages)
            except StopIteration:
                raise StopAsyncIteration

    class _LoopClient:
        def __init__(self):
            self._client = _FakePaho()
            self.messages = _Messages([_FakeMqttMessage(_message())])
            self.subscribe = AsyncMock()

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, traceback):
            return False

    runtime = _runtime(_BrokenStore([]), persistent=True)
    observed_states = []

    async def stop_after_backoff(delay):
        observed_states.append(runtime.state)
        runtime._stopping.set()

    monkeypatch.setattr(
        "swarmbus.runtime.aiomqtt.Client",
        lambda *args, **kwargs: _LoopClient(),
    )
    monkeypatch.setattr("swarmbus.runtime.asyncio.sleep", stop_after_backoff)

    await runtime._connection_loop()

    assert observed_states == [RuntimeState.STORE_DEGRADED]
    assert runtime.last_fatal_error is None


@pytest.mark.asyncio
async def test_reconnect_backoff_does_not_reset_on_short_connections(monkeypatch):
    class _FailingClient:
        def __init__(self):
            self._client = _FakePaho()

        async def __aenter__(self):
            raise aiomqtt.MqttError("connection failed")

        async def __aexit__(self, exc_type, exc, traceback):
            return False

    runtime = _runtime(_FakeStore([]))
    delays = []

    async def record_sleep(delay):
        delays.append(delay)
        if len(delays) == 7:
            runtime._stopping.set()

    monkeypatch.setattr(
        "swarmbus.runtime.aiomqtt.Client",
        lambda *args, **kwargs: _FailingClient(),
    )
    monkeypatch.setattr("swarmbus.runtime.asyncio.sleep", record_sleep)
    monkeypatch.setattr("swarmbus.runtime.random.uniform", lambda *args: 0)

    await runtime._connection_loop()

    assert delays == [1.0, 2.0, 4.0, 8.0, 16.0, 32.0, 60.0]


@pytest.mark.asyncio
async def test_send_message_uses_held_connection():
    runtime = _runtime(_FakeStore([]))
    runtime._client = _FakeClient()

    await runtime.send_message(
        to="wren",
        subject="hello",
        body="ping",
        content_type="text/markdown",
    )

    topic, payload = runtime._client.publish.await_args.args
    message = AgentMessage.from_json(payload)
    assert topic == "agents/wren/inbox"
    assert message.content_type == "text/markdown"


def test_persistent_runtime_uses_stable_session():
    runtime = _runtime(_FakeStore([]), persistent=True)

    kwargs = runtime._client_kwargs()

    assert kwargs["identifier"] == "swarmbus-foo"
    assert kwargs["clean_session"] is False


def test_presence_controls_last_will():
    without_presence = _runtime(_FakeStore([]))
    with_presence = _runtime(_FakeStore([]), presence=True)

    assert "will" not in without_presence._client_kwargs()
    will = with_presence._client_kwargs()["will"]
    assert str(will.topic) == "agents/foo/presence"
    assert json.loads(will.payload)["status"] == "offline"
