import asyncio
import inspect
import json
import logging
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import get_args
from unittest.mock import AsyncMock

import aiomqtt
import pytest

from swarmbus.bus import _legacy_presence_payload
from swarmbus.message import AgentMessage
from swarmbus.registry import AgentLifecycle, RegistryRecord, presence_payload
from swarmbus.runtime import (
    AcknowledgementError,
    ManagedMCPRuntime,
    PresenceRequiredError,
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
        self.sender_provenance = []
        self.opened = False
        self.closed = False

    async def open(self):
        self.opened = True

    async def close(self):
        self.closed = True

    async def store(
        self,
        msg,
        *,
        source_topic,
        sender_provenance=None,
    ):
        self.events.append("commit")
        self.messages.append(msg)
        self.sender_provenance.append(sender_provenance)
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


def test_runtime_normalizes_declared_capabilities_at_construction():
    runtime = _runtime(
        _FakeStore([]),
        capabilities=[
            " development.files.write ",
            "development.files.write",
            "messaging",
        ],
    )

    assert runtime.declared_capabilities == ["development.files.write"]


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
async def test_inbox_commit_snapshots_sender_registry_state():
    store = _FakeStore([])
    # Sender provenance is available only to presence-enabled runtimes.
    runtime = _runtime(store, presence=True)
    now = datetime.now(timezone.utc)
    record = RegistryRecord(
        agent_id="wren",
        lifecycle="transient",
        started_at=now - timedelta(minutes=10),
        last_seen=now,
        capabilities=["messaging"],
    )
    runtime.registry.update_registry(
        "swarmbus/registry/wren",
        record.to_json(),
    )
    runtime.registry.update_presence(
        runtime.topics.presence("wren"),
        presence_payload("wren", "online"),
    )

    await runtime._handle_message(
        _FakeMqttMessage(_message()),
        _RecordingAck(),
    )

    provenance = store.sender_provenance[0]
    assert provenance["lifecycle"] == "transient"
    assert provenance["online"] is True
    assert provenance["started_at"] == record.started_at.isoformat()
    assert provenance["capabilities"] == ["messaging"]
    assert provenance["observed_at"] is not None


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [OSError("disk full"), sqlite3.OperationalError("locked")])
async def test_store_failure_leaves_message_unacked(error):
    class _BrokenStore(_FakeStore):
        async def store(self, msg, *, source_topic, sender_provenance=None):
            raise error

    ack = _RecordingAck()
    runtime = _runtime(_BrokenStore([]), durable=True, presence=True)

    with pytest.raises(type(error), match=str(error)):
        await runtime._handle_message(_FakeMqttMessage(_message()), ack)

    assert ack.messages == []


@pytest.mark.asyncio
async def test_live_session_retries_held_message_with_capped_backoff(monkeypatch):
    class _FlakyStore(_FakeStore):
        def __init__(self, events):
            super().__init__(events)
            self.failures_remaining = 7

        async def store(self, msg, *, source_topic, sender_provenance=None):
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
    runtime = _runtime(_FakeStore([]), presence=True)
    runtime._client = _FakeClient()
    ack = _RecordingAck()

    online = _FakeMqttMessage(_message())
    online.topic = runtime.topics.presence("wren")
    online.payload = _legacy_presence_payload("wren", "online").encode()
    await runtime._handle_message(online, ack)

    assert await runtime.list_agents() == ["wren"]

    offline = _FakeMqttMessage(_message())
    offline.topic = runtime.topics.presence("wren")
    offline.payload = _legacy_presence_payload("wren", "offline").encode()
    await runtime._handle_message(offline, ack)

    assert await runtime.list_agents() == []


@pytest.mark.asyncio
async def test_presence_identity_mismatch_is_acked_and_ignored():
    runtime = _runtime(_FakeStore([]), presence=True)
    runtime._client = _FakeClient()
    ack = _RecordingAck()
    message = _FakeMqttMessage(_message())
    message.topic = runtime.topics.presence("wren")
    message.payload = _legacy_presence_payload("sparrow", "online").encode()

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
    runtime = _runtime(store, durable=True, presence=True)
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
    # Presence lets list_agents reach the shared transport check.
    runtime = _runtime(_FakeStore([]), presence=True)

    with pytest.raises(TransportUnavailable, match="disconnected"):
        await runtime.read_inbox()
    with pytest.raises(TransportUnavailable, match="disconnected"):
        await runtime.watch_inbox(timeout=0.01)
    with pytest.raises(TransportUnavailable, match="disconnected"):
        await runtime.list_agents()


@pytest.mark.asyncio
async def test_list_states_refuses_instead_of_reading_a_stale_cache():
    """Disconnected directory lists reject cached results."""
    runtime = _runtime(_FakeStore([]), presence=True)
    runtime._client = _FakeClient()
    presence = _FakeMqttMessage(_message())
    presence.topic = runtime.topics.presence("wren")
    presence.payload = presence_payload("wren", "online").encode()
    await runtime._handle_message(presence, _RecordingAck())

    assert await runtime.list_states() == [await runtime.get_state("wren")]

    runtime._client = None

    with pytest.raises(TransportUnavailable, match="disconnected"):
        await runtime.list_states()
    with pytest.raises(TransportUnavailable, match="disconnected"):
        await runtime.list_states(include_offline=True)


@pytest.mark.asyncio
async def test_list_states_still_answers_empty_for_an_empty_swarm():
    """Connected empty directory lists return an empty result."""
    runtime = _runtime(_FakeStore([]), presence=True)
    runtime._client = _FakeClient()

    assert await runtime.list_states(include_offline=True) == []


@pytest.mark.asyncio
async def test_get_state_refuses_instead_of_reading_a_stale_cache():
    """Disconnected directory gets reject cached and uncached peers."""
    runtime = _runtime(_FakeStore([]), presence=True)
    runtime._client = _FakeClient()
    presence = _FakeMqttMessage(_message())
    presence.topic = runtime.topics.presence("wren")
    presence.payload = presence_payload("wren", "online").encode()
    await runtime._handle_message(presence, _RecordingAck())

    assert (await runtime.get_state("wren"))["online"] is True
    with pytest.raises(ValueError, match="no registry record"):
        await runtime.get_state("sparrow")

    runtime._client = None

    with pytest.raises(TransportUnavailable, match="disconnected"):
        await runtime.get_state("wren")
    with pytest.raises(TransportUnavailable, match="disconnected"):
        await runtime.get_state("sparrow")


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
            self.publish = AsyncMock()
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
        async def store(self, msg, *, source_topic, sender_provenance=None):
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
            self.publish = AsyncMock()

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, traceback):
            return False

    runtime = _runtime(_BrokenStore([]), durable=True, presence=True)
    observed_states = []

    async def idle_heartbeat():
        await asyncio.Event().wait()

    # Keep the real heartbeat from consuming the patched sleep probe.
    runtime._heartbeat_loop = idle_heartbeat

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


def test_durable_runtime_uses_stable_session():
    runtime = _runtime(_FakeStore([]), durable=True, presence=True)

    kwargs = runtime._client_kwargs()

    assert kwargs["identifier"] == "swarmbus-foo"
    assert kwargs["clean_session"] is False


def test_presence_controls_last_will():
    without_presence = _runtime(_FakeStore([]))
    with_presence = _runtime(_FakeStore([]), presence=True)

    assert "will" not in without_presence._client_kwargs()
    will = with_presence._client_kwargs()["will"]
    assert str(will.topic) == "agents/foo/presence"
    payload = json.loads(will.payload)
    assert payload["state"] == "offline"
    assert payload["reason"] == "connection-lost"
    assert "connected_at" not in payload


@pytest.mark.asyncio
async def test_published_presence_timestamps_only_the_online_record():
    """Offline presence omits connected_at."""
    runtime = _runtime(_FakeStore([]), presence=True)
    runtime._client = _FakeClient()

    await runtime._publish_presence("online")
    await runtime._publish_presence("offline", reason="clean-shutdown")

    online, offline = (
        json.loads(call.args[1])
        for call in runtime._client.publish.await_args_list
    )
    assert online["connected_at"] is not None
    assert "connected_at" not in offline
    assert offline["reason"] == "clean-shutdown"


@pytest.mark.asyncio
async def test_runtime_registry_reads_and_updates_use_held_connection():
    runtime = _runtime(_FakeStore([]), presence=True)
    runtime._client = _FakeClient()

    await runtime._publish_presence("online")
    initial = await runtime.update_state(
        status="working",
        working_set=[" repo ", "broker", "repo"],
        capabilities=["development.files.write", "agent-state"],
    )
    listed = await runtime.list_states(include_offline=True)
    fetched = await runtime.get_state("foo")

    assert initial["status"] == "working"
    assert initial["working_set"] == ["repo", "broker"]
    assert initial["capabilities"] == [
        "messaging",
        "durable-inbox",
        "agent-state",
        "development.files.write",
    ]
    assert listed == [fetched]
    assert fetched["online"] is True


@pytest.mark.asyncio
async def test_runtime_merges_retained_registry_and_presence_messages():
    runtime = _runtime(_FakeStore([]), presence=True)
    runtime._client = _FakeClient()
    ack = _RecordingAck()
    record = RegistryRecord(
        agent_id="wren",
        lifecycle="transient",
        durability="ephemeral",
        started_at=datetime.now(timezone.utc),
        last_seen=datetime.now(timezone.utc),
    )
    registry_message = _FakeMqttMessage(_message())
    registry_message.topic = "swarmbus/registry/wren"
    registry_message.payload = record.to_json().encode()
    presence_message = _FakeMqttMessage(_message())
    presence_message.topic = runtime.topics.presence("wren")
    presence_message.payload = presence_payload("wren", "online").encode()

    await runtime._handle_message(registry_message, ack)
    await runtime._handle_message(presence_message, ack)

    assert await runtime.list_agents() == ["wren"]
    assert (await runtime.get_state("wren"))["lifecycle"] == "transient"


@pytest.mark.asyncio
async def test_heartbeat_loop_survives_registry_publish_failure():
    runtime = _runtime(_FakeStore([]), presence=True)
    runtime._client = _FakeClient()
    runtime.heartbeat_seconds = 0.001
    calls = 0
    recovered = asyncio.Event()

    async def _flaky_publish():
        nonlocal calls
        calls += 1
        if calls <= 2:
            raise RuntimeError("swarmbus MQTT runtime is not connected")
        recovered.set()

    runtime._publish_registry = _flaky_publish
    task = asyncio.create_task(runtime._heartbeat_loop())
    try:
        await asyncio.wait_for(recovered.wait(), timeout=1)
        assert task.done() is False
    finally:
        runtime._stopping.set()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


_ACCEPTED_LIFECYCLE_PRESENCE = [
    ("transient", True),
    ("persistent", False),
    ("persistent", True),
]
_REJECTED_LIFECYCLE_PRESENCE = [
    ("transient", False),
]


def test_lifecycle_presence_matrix_is_exhaustive():
    """Cover every lifecycle/presence constructor combination."""
    covered = set(_ACCEPTED_LIFECYCLE_PRESENCE) | set(
        _REJECTED_LIFECYCLE_PRESENCE
    )

    assert covered == {
        (lifecycle, presence)
        for lifecycle in get_args(AgentLifecycle)
        for presence in (False, True)
    }


@pytest.mark.parametrize(
    ("lifecycle", "presence"),
    _ACCEPTED_LIFECYCLE_PRESENCE,
)
def test_accepted_lifecycle_presence_combinations_construct(
    lifecycle,
    presence,
):
    runtime = _runtime(
        _FakeStore([]),
        lifecycle=lifecycle,
        presence=presence,
    )

    assert runtime.lifecycle == lifecycle
    assert runtime.presence is presence


@pytest.mark.parametrize(
    ("lifecycle", "presence"),
    _REJECTED_LIFECYCLE_PRESENCE,
)
def test_transient_lifecycle_without_presence_is_rejected(
    lifecycle,
    presence,
):
    """Transient lifecycle requires presence."""
    with pytest.raises(ValueError) as excinfo:
        _runtime(
            _FakeStore([]),
            lifecycle=lifecycle,
            presence=presence,
        )

    message = str(excinfo.value)
    assert "transient" in message
    assert "presence" in message


def test_default_lifecycle_and_presence_stay_accepted():
    """The default persistent lifecycle remains valid without presence."""
    parameters = inspect.signature(ManagedMCPRuntime.__init__).parameters
    defaults = (
        parameters["lifecycle"].default,
        parameters["presence"].default,
    )

    assert defaults in _ACCEPTED_LIFECYCLE_PRESENCE

    runtime = _runtime(_FakeStore([]))

    assert (runtime.lifecycle, runtime.presence) == defaults


_ACCEPTED_DURABLE_PRESENCE = [
    (False, False),
    (False, True),
    (True, True),
]
_REJECTED_DURABLE_PRESENCE = [
    (True, False),
]


def test_durable_presence_matrix_is_exhaustive():
    """Guard the combination tests below against an untested pair."""
    covered = set(_ACCEPTED_DURABLE_PRESENCE) | set(
        _REJECTED_DURABLE_PRESENCE
    )

    assert covered == {
        (durable, presence)
        for durable in (False, True)
        for presence in (False, True)
    }


@pytest.mark.parametrize(
    ("durable", "presence"),
    _ACCEPTED_DURABLE_PRESENCE,
)
def test_accepted_durable_presence_combinations_construct(
    durable,
    presence,
):
    runtime = _runtime(
        _FakeStore([]),
        durable=durable,
        presence=presence,
    )

    assert runtime.durable is durable
    assert runtime.presence is presence


@pytest.mark.parametrize(
    ("durable", "presence"),
    _REJECTED_DURABLE_PRESENCE,
)
def test_durable_session_without_presence_is_rejected(durable, presence):
    """Durable sessions require presence."""
    with pytest.raises(ValueError) as excinfo:
        _runtime(
            _FakeStore([]),
            durable=durable,
            presence=presence,
        )

    message = str(excinfo.value)
    assert "durable" in message
    assert "presence" in message


def test_durable_is_independent_of_lifecycle():
    """Durability and lifecycle remain independent fields."""
    runtime = _runtime(
        _FakeStore([]),
        durable=True,
        presence=True,
        lifecycle="transient",
    )

    assert (runtime.durable, runtime.lifecycle) == (True, "transient")


def test_default_durable_and_presence_stay_accepted():
    """The default messaging-only runtime remains valid."""
    parameters = inspect.signature(ManagedMCPRuntime.__init__).parameters
    defaults = (
        parameters["durable"].default,
        parameters["presence"].default,
    )

    assert defaults in _ACCEPTED_DURABLE_PRESENCE

    runtime = _runtime(_FakeStore([]))

    assert (runtime.durable, runtime.presence) == defaults


_DIRECTORY_OPERATIONS = (
    ("list_agents", lambda runtime: runtime.list_agents()),
    ("list_states", lambda runtime: runtime.list_states()),
    ("get_state", lambda runtime: runtime.get_state("wren")),
    (
        "update_state",
        lambda runtime: runtime.update_state(status="busy", working_set=None),
    ),
)
_MESSAGING_OPERATIONS = frozenset(
    {"send_message", "read_inbox", "ack_inbox", "watch_inbox"}
)
_LIFECYCLE_OPERATIONS = frozenset({"start", "wait_until_ready", "stop"})


def test_directory_operation_matrix_covers_the_mcp_runtime_contract():
    """Cover every public directory method with presence-refusal tests."""
    declared = {
        name
        for name, _ in inspect.getmembers(
            ManagedMCPRuntime,
            inspect.iscoroutinefunction,
        )
        if not name.startswith("_")
    }

    assert {name for name, _ in _DIRECTORY_OPERATIONS} == (
        declared - _MESSAGING_OPERATIONS - _LIFECYCLE_OPERATIONS
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("name", "call"),
    _DIRECTORY_OPERATIONS,
    ids=[name for name, _ in _DIRECTORY_OPERATIONS],
)
async def test_directory_operations_refuse_without_presence(name, call):
    """Messaging-only runtimes reject directory reads."""
    runtime = _runtime(_FakeStore([]))
    runtime._client = _FakeClient()

    with pytest.raises(PresenceRequiredError) as excinfo:
        await call(runtime)

    assert not isinstance(excinfo.value, TransportUnavailable)
    message = str(excinfo.value)
    assert runtime.agent_id in message
    assert "presence" in message


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("name", "call"),
    _DIRECTORY_OPERATIONS,
    ids=[name for name, _ in _DIRECTORY_OPERATIONS],
)
async def test_directory_operations_refuse_presence_before_transport(
    name,
    call,
):
    """Presence errors take precedence over transport errors."""
    runtime = _runtime(_FakeStore([]))

    assert runtime.connected is False

    with pytest.raises(PresenceRequiredError):
        await call(runtime)


@pytest.mark.asyncio
@pytest.mark.parametrize("presence", [False, True])
async def test_directory_subscriptions_follow_presence(presence, monkeypatch):
    """Presence-free runtimes subscribe only to messaging topics."""

    class _StopOnDrain:
        """Empty inbox that ends the connection loop once drained."""

        def __init__(self, runtime):
            self._runtime = runtime

        def __aiter__(self):
            return self

        async def __anext__(self):
            self._runtime._stopping.set()
            raise StopAsyncIteration

    class _LoopClient:
        def __init__(self, runtime):
            self._client = _FakePaho()
            self.messages = _StopOnDrain(runtime)
            self.subscribe = AsyncMock()
            self.publish = AsyncMock()

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, traceback):
            return False

    runtime = _runtime(_FakeStore([]), presence=presence)
    clients = []

    def _connect(*args, **kwargs):
        client = _LoopClient(runtime)
        clients.append(client)
        return client

    monkeypatch.setattr("swarmbus.runtime.aiomqtt.Client", _connect)

    await runtime._connection_loop()

    assert len(clients) == 1
    subscribed = [
        call.args[0] for call in clients[0].subscribe.await_args_list
    ]
    directory_filters = {
        runtime.topics.any_presence_filter(),
        runtime.topics.any_registry_filter(),
    }
    assert runtime.topics.inbox(runtime.agent_id) in subscribed
    assert runtime.topics.broadcast in subscribed
    if presence:
        assert directory_filters.issubset(subscribed)
    else:
        assert directory_filters.isdisjoint(subscribed)
        assert runtime.registry.list_states(include_offline=True) == []


@pytest.mark.asyncio
async def test_messaging_survives_the_directory_refusal():
    """Presence-free runtimes still send and receive messages."""
    store = _FakeStore([])
    runtime = _runtime(store)
    runtime._client = _FakeClient()

    await runtime.send_message(to="wren", subject="hello", body="ping")
    await runtime._handle_message(
        _FakeMqttMessage(_message()),
        _RecordingAck(),
    )
    delivered = await runtime.read_inbox()

    topic, _ = runtime._client.publish.await_args.args
    assert topic == runtime.topics.inbox("wren")
    assert [item["subject"] for item in delivered] == ["hey"]
    assert store.sender_provenance[0]["lifecycle"] == "unknown"
    assert store.sender_provenance[0]["online"] is None


def test_transient_runtime_uses_explicit_mqtt_client_id():
    runtime = _runtime(
        _FakeStore([]),
        lifecycle="transient",
        presence=True,
        client_id="session-deadbeef",
    )

    kwargs = runtime._client_kwargs()

    assert kwargs["identifier"] == "session-deadbeef"
    assert "clean_session" not in kwargs


@pytest.mark.asyncio
async def test_connect_publishes_registry_before_presence(monkeypatch):
    """Startup publishes registry before presence."""

    class _StopOnDrain:
        """Empty inbox that ends the connection loop once drained."""

        def __init__(self, runtime):
            self._runtime = runtime

        def __aiter__(self):
            return self

        async def __anext__(self):
            self._runtime._stopping.set()
            raise StopAsyncIteration

    class _LoopClient:
        def __init__(self, runtime):
            self._client = _FakePaho()
            self.messages = _StopOnDrain(runtime)
            self.subscribe = AsyncMock()
            self.publish = AsyncMock()

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, traceback):
            return False

    runtime = _runtime(
        _FakeStore([]),
        lifecycle="transient",
        presence=True,
    )
    clients = []

    def _connect(*args, **kwargs):
        client = _LoopClient(runtime)
        clients.append(client)
        return client

    monkeypatch.setattr("swarmbus.runtime.aiomqtt.Client", _connect)

    await runtime._connection_loop()

    assert len(clients) == 1
    retained_topics = [
        call.args[0]
        for call in clients[0].publish.await_args_list
        if call.kwargs.get("retain") is True
    ]
    assert retained_topics == [
        "swarmbus/registry/foo",
        "agents/foo/presence",
    ]


@pytest.mark.asyncio
async def test_transient_shutdown_stops_heartbeat_before_tombstoning():
    runtime = _runtime(
        _FakeStore([]),
        lifecycle="transient",
        presence=True,
    )
    runtime._client = _FakeClient()
    runtime._connection_task = asyncio.create_task(asyncio.sleep(60))
    events = []

    async def heartbeat():
        try:
            await asyncio.Event().wait()
        finally:
            events.append("heartbeat-stopped")

    async def clear():
        events.append("tombstoned")

    runtime._heartbeat_task = asyncio.create_task(heartbeat())
    await asyncio.sleep(0)
    runtime._clear_transient_retained_state = clear

    await runtime.stop()

    assert events == ["heartbeat-stopped", "tombstoned"]


@pytest.mark.asyncio
async def test_clean_transient_shutdown_tombstones_presence_then_registry():
    """Shutdown tombstones presence before registry."""
    runtime = _runtime(
        _FakeStore([]),
        lifecycle="transient",
        presence=True,
    )
    client = _FakeClient()
    runtime._client = client
    runtime._connection_task = asyncio.create_task(asyncio.sleep(60))
    await runtime._publish_registry()

    await runtime.stop()

    tombstones = [
        call.args[:2]
        for call in client.publish.await_args_list
        if len(call.args) >= 2 and call.args[1] == b""
    ]
    assert tombstones == [
        ("agents/foo/presence", b""),
        ("swarmbus/registry/foo", b""),
    ]
    assert runtime.registry.list_states(include_offline=True) == []


def _shutdown_runtime(events, **kwargs):
    """A connected runtime whose shutdown publishes are recorded in order."""
    runtime = _runtime(_FakeStore([]), presence=True, **kwargs)
    client = _FakeClient()

    async def record(topic, payload, **publish_kwargs):
        events.append((str(topic), payload))

    client.publish = AsyncMock(side_effect=record)
    runtime._client = client
    runtime._connection_task = asyncio.create_task(asyncio.sleep(60))
    return runtime


def _capture_session_destroy(monkeypatch, events, *, error=None):
    """Record the connect kwargs of every client shutdown opens."""
    connects = []

    class _Session:
        async def __aenter__(self):
            events.append(("destroy-session", None))
            if error is not None:
                raise error
            return self

        async def __aexit__(self, exc_type, exc, traceback):
            return False

    def connect(*args, **kwargs):
        connects.append(kwargs)
        return _Session()

    monkeypatch.setattr("swarmbus.runtime.aiomqtt.Client", connect)
    return connects


@pytest.mark.asyncio
async def test_transient_durable_shutdown_destroys_the_mqtt_session(monkeypatch):
    """Transient durable shutdown destroys the session after tombstones."""
    events = []
    runtime = _shutdown_runtime(events, durable=True, lifecycle="transient")
    connects = _capture_session_destroy(monkeypatch, events)

    await runtime.stop()

    assert events == [
        ("agents/foo/presence", b""),
        ("swarmbus/registry/foo", b""),
        ("destroy-session", None),
    ]
    assert len(connects) == 1
    assert connects[0]["identifier"] == "swarmbus-foo"
    assert connects[0]["clean_session"] is True


@pytest.mark.asyncio
async def test_transient_shutdown_without_durable_destroys_no_session(monkeypatch):
    """A live-session runtime leaves no broker-side session behind, so
    shutdown must not spend a reconnect discovering that."""
    events = []
    runtime = _shutdown_runtime(events, lifecycle="transient")
    connects = _capture_session_destroy(monkeypatch, events)

    await runtime.stop()

    assert events == [
        ("agents/foo/presence", b""),
        ("swarmbus/registry/foo", b""),
    ]
    assert connects == []


@pytest.mark.asyncio
async def test_persistent_durable_shutdown_keeps_the_mqtt_session(monkeypatch):
    """Persistent durable shutdown keeps the broker session."""
    events = []
    runtime = _shutdown_runtime(events, durable=True)
    connects = _capture_session_destroy(monkeypatch, events)

    await runtime.stop()

    assert [topic for topic, _ in events] == ["agents/foo/presence"]
    assert json.loads(events[0][1])["state"] == "offline"
    assert connects == []


@pytest.mark.asyncio
async def test_session_destroy_carries_no_last_will(monkeypatch):
    """An unclean drop of the destroying connection would republish the
    presence record just tombstoned, as an orphan nothing collects."""
    events = []
    runtime = _shutdown_runtime(events, durable=True, lifecycle="transient")
    connects = _capture_session_destroy(monkeypatch, events)

    await runtime.stop()

    assert "will" in runtime._client_kwargs()
    assert "will" not in connects[0]


@pytest.mark.asyncio
async def test_session_destroy_failure_does_not_crash_shutdown(
    monkeypatch,
    caplog,
):
    """Teardown runs on the way out, so a dead broker must not take the
    process with it -- matching the shutdown publish failure path."""
    events = []
    runtime = _shutdown_runtime(events, durable=True, lifecycle="transient")
    _capture_session_destroy(
        monkeypatch,
        events,
        error=aiomqtt.MqttError("broker gone"),
    )

    with caplog.at_level(logging.WARNING):
        await runtime.stop()

    assert "failed to destroy durable session: broker gone" in caplog.text
    assert runtime.state is RuntimeState.STOPPED
    assert runtime.store.closed is True


@pytest.mark.asyncio
async def test_disconnected_shutdown_destroys_no_session(monkeypatch):
    """With no connection there is nothing to publish on and nothing to
    reclaim the client id from; shutdown still has to finish."""
    events = []
    runtime = _shutdown_runtime(events, durable=True, lifecycle="transient")
    runtime._client = None
    connects = _capture_session_destroy(monkeypatch, events)

    await runtime.stop()

    assert events == []
    assert connects == []
    assert runtime.state is RuntimeState.STOPPED
