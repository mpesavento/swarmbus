# tests/test_integration.py
"""End-to-end tests against a real mosquitto broker (via the
`mosquitto_broker` session fixture in conftest.py).

These tests exercise the wire protocol, retained-message semantics, and
handler side-effects -- things mocks cannot verify. Each test spins up
agents, drives a scenario, then cancels listeners cleanly.

Scenarios not yet covered (deliberately -- see README or issues):
  * Reconnect after unclean broker restart -- unit-tested in test_bus.py
  * LWT on unclean TCP abort -- cannot reliably force from test code
"""
import asyncio
import inspect
import json
import os
import sqlite3
import stat
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import aiomqtt
import pytest

from swarmbus.archive import SQLiteArchive, SQLiteMessageStore
from swarmbus.bus import AgentBus
from swarmbus.handlers.base import BaseHandler
from swarmbus._compat import asyncio_timeout
from swarmbus.handlers.direct_invoke import DirectInvocationHandler
from swarmbus.handlers.file_bridge import FileBridgeHandler
from swarmbus.maintenance import RegistryMaintenance
from swarmbus.mcp_server import create_mcp_app
from swarmbus.message import AgentMessage
from swarmbus.registry import PresenceRecord, RegistryRecord
from swarmbus.runtime import ManagedMCPRuntime, PresenceRequiredError
from swarmbus.topics import DEFAULT_TOPICS, TopicMap


class CollectingHandler(BaseHandler):
    def __init__(self):
        self.received: list[AgentMessage] = []
        self._event = asyncio.Event()

    async def handle(self, msg: AgentMessage) -> None:
        self.received.append(msg)
        self._event.set()

    async def wait_for_message(self, timeout: float = 3.0) -> None:
        await asyncio.wait_for(self._event.wait(), timeout=timeout)
        self._event.clear()


async def _stop(task: asyncio.Task) -> None:
    """Cancel a listen task and swallow the resulting CancelledError/MqttError."""
    task.cancel()
    try:
        await task
    except (asyncio.CancelledError, Exception):
        pass


async def _clear_retained(host: str, port: int, topics: list[str]) -> None:
    """Publish empty retained payloads to clear state between tests.

    Retained messages survive broker restarts and can leak between tests
    when the session-scoped broker fixture is shared. This is idempotent.
    """
    async with aiomqtt.Client(host, port=port) as client:
        for topic in topics:
            await client.publish(topic, payload=b"", qos=1, retain=True)


@pytest.fixture
async def listening_agent(mosquitto_broker):
    """Start listeners and clean up their retained presence."""
    host, port = mosquitto_broker
    started: list[tuple[str, asyncio.Task]] = []

    async def start(agent_id, *handlers, settle=0.3, **kwargs):
        bus = AgentBus(agent_id=agent_id, broker=host, port=port, **kwargs)
        for handler in handlers:
            bus.register_handler(handler)
        started.append((agent_id, asyncio.create_task(bus.listen())))
        await asyncio.sleep(settle)  # let the subscription establish
        return bus

    yield start

    for _, task in reversed(started):
        await _stop(task)
    await _clear_retained(
        host,
        port,
        [DEFAULT_TOPICS.presence(agent_id) for agent_id, _ in started],
    )


@pytest.mark.asyncio
async def test_send_receive_roundtrip(mosquitto_broker, listening_agent):
    host, port = mosquitto_broker
    handler = CollectingHandler()
    await listening_agent("sparrow", handler)

    sender = AgentBus(agent_id="wren", broker=host, port=port, retain=False)
    await sender.send(to="sparrow", subject="ping", body="hello from wren")
    await handler.wait_for_message(timeout=3.0)

    assert len(handler.received) == 1
    msg = handler.received[0]
    assert msg.from_agent == "wren"
    assert msg.subject == "ping"
    assert msg.body == "hello from wren"


@pytest.mark.asyncio
async def test_markdown_body_preserved(mosquitto_broker, listening_agent):
    host, port = mosquitto_broker
    handler = CollectingHandler()
    await listening_agent("sparrow", handler)

    sender = AgentBus(agent_id="wren", broker=host, port=port, retain=False)
    body = "# Report\n```python\nprint('hi')\n```\n> Note: tested."

    await sender.send(
        to="sparrow", subject="report",
        body=body, content_type="text/markdown",
    )
    await handler.wait_for_message(timeout=3.0)

    assert handler.received[0].body == body
    assert handler.received[0].content_type == "text/markdown"


# ---------------------------------------------------------------------------
# Protocol-level e2e
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_broadcast_delivered_to_all_subscribers(mosquitto_broker):
    """One `to='broadcast'` publish must reach every listening agent."""
    host, port = mosquitto_broker
    h_a, h_b = CollectingHandler(), CollectingHandler()

    agent_a = AgentBus(agent_id="alpha", broker=host, port=port, retain=False)
    agent_b = AgentBus(agent_id="beta", broker=host, port=port, retain=False)
    agent_a.register_handler(h_a)
    agent_b.register_handler(h_b)

    sender = AgentBus(agent_id="gamma", broker=host, port=port, retain=False)

    t_a = asyncio.create_task(agent_a.listen())
    t_b = asyncio.create_task(agent_b.listen())
    await asyncio.sleep(0.3)  # allow both subscriptions to establish

    try:
        await sender.send(to="broadcast", subject="all-hands", body="everyone read this")
        await asyncio.gather(
            h_a.wait_for_message(timeout=3.0),
            h_b.wait_for_message(timeout=3.0),
        )
        assert h_a.received[0].body == "everyone read this"
        assert h_b.received[0].body == "everyone read this"
        assert h_a.received[0].to == "broadcast"
    finally:
        await _stop(t_a)
        await _stop(t_b)
        await _clear_retained(host, port, [
            "agents/alpha/presence", "agents/beta/presence",
        ])


@pytest.mark.asyncio
async def test_reply_to_roundtrip(mosquitto_broker):
    """A sends with reply_to=A; B receives, replies via reply_to; A gets reply."""
    host, port = mosquitto_broker
    h_a, h_b = CollectingHandler(), CollectingHandler()

    agent_a = AgentBus(agent_id="ayy", broker=host, port=port, retain=False)
    agent_a.register_handler(h_a)
    agent_b = AgentBus(agent_id="bee", broker=host, port=port, retain=False)
    agent_b.register_handler(h_b)

    t_a = asyncio.create_task(agent_a.listen())
    t_b = asyncio.create_task(agent_b.listen())
    await asyncio.sleep(0.3)

    try:
        # A asks B a question, stamped with reply_to
        await agent_a.send(
            to="bee", subject="question",
            body="what's the ETA?", reply_to="ayy",
        )
        await h_b.wait_for_message(timeout=3.0)

        inbound = h_b.received[0]
        assert inbound.reply_to == "ayy"

        # B replies to the reply_to address
        await agent_b.send(
            to=inbound.reply_to,
            subject=f"re: {inbound.subject}",
            body="ETA 2026-04-20",
        )
        await h_a.wait_for_message(timeout=3.0)

        reply = h_a.received[0]
        assert reply.from_agent == "bee"
        assert reply.subject == "re: question"
        assert reply.body == "ETA 2026-04-20"
    finally:
        await _stop(t_a)
        await _stop(t_b)
        await _clear_retained(host, port, [
            "agents/ayy/presence", "agents/bee/presence",
        ])


@pytest.mark.asyncio
async def test_persistent_client_multiple_sends(mosquitto_broker):
    """`async with AgentBus()` reuses one client across sends; all arrive."""
    host, port = mosquitto_broker
    handler = CollectingHandler()
    received_bodies: list[str] = []

    class _AllMessages(BaseHandler):
        async def handle(self, msg: AgentMessage) -> None:
            received_bodies.append(msg.body)
            handler._event.set()  # reuse event for progress

    receiver = AgentBus(agent_id="rx", broker=host, port=port, retain=False)
    receiver.register_handler(_AllMessages())

    t = asyncio.create_task(receiver.listen())
    await asyncio.sleep(0.3)

    try:
        async with AgentBus(agent_id="tx", broker=host, port=port) as sender:
            for i in range(5):
                await sender.send(to="rx", subject=f"m{i}", body=f"body-{i}")

        # Wait until all 5 arrive (or timeout at 3s)
        deadline = asyncio.get_event_loop().time() + 3.0
        while len(received_bodies) < 5 and asyncio.get_event_loop().time() < deadline:
            await asyncio.sleep(0.05)

        assert received_bodies == [f"body-{i}" for i in range(5)]
    finally:
        await _stop(t)
        await _clear_retained(host, port, ["agents/rx/presence"])


@pytest.mark.asyncio
async def test_durable_runtime_replays_offline_delivery_across_restarts(
    mosquitto_broker,
    tmp_path,
):
    """A durable consumer must durably receive an offline delivery."""
    host, port = mosquitto_broker
    state_path = tmp_path / "consumer.sqlite3"
    runtimes: list[ManagedMCPRuntime] = []

    def new_runtime() -> ManagedMCPRuntime:
        runtime = ManagedMCPRuntime(
            agent_id="consumer",
            broker=host,
            port=port,
            durable=True,
            presence=True,
            state_path=state_path,
        )
        runtimes.append(runtime)
        return runtime

    try:
        subscriber = new_runtime()
        await subscriber.start()
        await subscriber.wait_until_ready()
        await subscriber.stop()

        sender = AgentBus(
            agent_id="sender",
            broker=host,
            port=port,
            retain=False,
        )
        await sender.send(
            to="consumer",
            subject="offline",
            body="queued while receiver was offline",
        )

        replayer = new_runtime()
        await replayer.start()
        await replayer.wait_until_ready()

        deadline = asyncio.get_running_loop().time() + 3.0
        unread = 0
        while asyncio.get_running_loop().time() < deadline:
            with sqlite3.connect(state_path) as db:
                unread = db.execute(
                    """SELECT COUNT(*) FROM inbox_messages
                       WHERE acknowledged_at IS NULL"""
                ).fetchone()[0]
            if unread == 1:
                break
            await asyncio.sleep(0.05)
        assert unread == 1

        await replayer.stop()

        drainer = new_runtime()
        await drainer.start()
        await drainer.wait_until_ready()
        app = create_mcp_app(drainer)
        messages = await app._tool_fns["read_inbox"]()

        assert len(messages) == 1
        assert messages[0]["from"] == "sender"
        assert messages[0]["to"] == "consumer"
        assert messages[0]["subject"] == "offline"
        assert messages[0]["body"] == "queued while receiver was offline"
    finally:
        for runtime in reversed(runtimes):
            await runtime.stop()
        async with aiomqtt.Client(
            host,
            port=port,
            identifier="swarmbus-consumer",
            clean_session=True,
        ):
            pass
        await _clear_retained(
            host,
            port,
            [
                DEFAULT_TOPICS.registry("consumer"),
                DEFAULT_TOPICS.presence("consumer"),
            ],
        )


@pytest.mark.asyncio
async def test_durable_runtime_redelivers_after_first_store_failure(
    mosquitto_broker,
    tmp_path,
):
    """A failed durable write must remain unacked and redeliver exactly once."""
    host, port = mosquitto_broker
    state_path = tmp_path / "receiver.sqlite3"

    class _FailOnceStore(SQLiteMessageStore):
        def __init__(self, path):
            super().__init__(path)
            self.attempts = 0

        async def store(
            self,
            msg,
            *,
            source_topic,
            received_at=None,
            sender_provenance=None,
        ):
            self.attempts += 1
            if self.attempts == 1:
                raise sqlite3.OperationalError("injected first-write failure")
            return await super().store(
                msg,
                source_topic=source_topic,
                received_at=received_at,
                sender_provenance=sender_provenance,
            )

    store = _FailOnceStore(state_path)
    runtime = ManagedMCPRuntime(
        agent_id="receiver",
        broker=host,
        port=port,
        durable=True,
        presence=True,
        state_path=state_path,
        store=store,
    )
    try:
        await runtime.start()
        await runtime.wait_until_ready()
        sender = AgentBus(
            agent_id="sender",
            broker=host,
            port=port,
            retain=False,
        )
        await sender.send(
            to="receiver",
            subject="retry",
            body="commit after broker redelivery",
        )

        deadline = asyncio.get_running_loop().time() + 5.0
        row_count = 0
        while asyncio.get_running_loop().time() < deadline:
            with sqlite3.connect(state_path) as db:
                row_count = db.execute(
                    "SELECT COUNT(*) FROM inbox_messages"
                ).fetchone()[0]
            if store.attempts >= 2 and row_count == 1:
                break
            await asyncio.sleep(0.05)

        assert store.attempts >= 2
        assert row_count == 1
        messages = await runtime.read_inbox()
        assert len(messages) == 1
        assert messages[0]["body"] == "commit after broker redelivery"
    finally:
        await runtime.stop()
        async with aiomqtt.Client(
            host,
            port=port,
            identifier="swarmbus-receiver",
            clean_session=True,
        ):
            pass
        await _clear_retained(
            host,
            port,
            [
                DEFAULT_TOPICS.registry("receiver"),
                DEFAULT_TOPICS.presence("receiver"),
            ],
        )


@pytest.mark.asyncio
async def test_retained_presence_late_subscriber(mosquitto_broker):
    """Late subscriber to agents/+/presence must see who's currently online."""
    host, port = mosquitto_broker

    agent = AgentBus(agent_id="earlybird", broker=host, port=port, retain=False)
    t = asyncio.create_task(agent.listen())
    await asyncio.sleep(0.3)  # let it publish retained "online"

    try:
        # A separate client subscribes *after* agent is online.
        seen: dict[str, str] = {}
        async with aiomqtt.Client(host, port=port) as client:
            await client.subscribe("agents/+/presence", qos=0)
            try:
                async with asyncio_timeout(1.0):
                    async for msg in client.messages:
                        data = json.loads(msg.payload)
                        seen[data["agent"]] = data["status"]
                        if "earlybird" in seen:
                            break
            except asyncio.TimeoutError:
                pass

        assert seen.get("earlybird") == "online"
    finally:
        await _stop(t)
        await _clear_retained(host, port, ["agents/earlybird/presence"])


@pytest.mark.asyncio
async def test_agent_state_mcp_tool_sees_online_agents(
    mosquitto_broker,
    tmp_path,
):
    """End-to-end: the managed runtime exposes retained online presence."""
    host, port = mosquitto_broker

    a = AgentBus(agent_id="one", broker=host, port=port, retain=False)
    b = AgentBus(agent_id="two", broker=host, port=port, retain=False)
    t_a = asyncio.create_task(a.listen())
    t_b = asyncio.create_task(b.listen())
    await asyncio.sleep(0.4)

    runtime = ManagedMCPRuntime(
        agent_id="observer",
        broker=host,
        port=port,
        presence=True,
        state_path=tmp_path / "observer.sqlite3",
    )
    try:
        await runtime.start()
        await runtime.wait_until_ready()
        app = create_mcp_app(runtime)
        for _ in range(20):
            states = await app._tool_fns["agent_state"](action="list")
            result = {state["agent_id"] for state in states}
            if {"one", "two"}.issubset(result):
                break
            await asyncio.sleep(0.05)
        assert {"one", "two"}.issubset(result)

        # Keep the MCP replacement aligned with the runtime projection.
        states = await app._tool_fns["agent_state"](action="list")
        assert sorted(
            state["agent_id"] for state in states
        ) == await runtime.list_agents()
    finally:
        await runtime.stop()
        await _stop(t_a)
        await _stop(t_b)
        await _clear_retained(host, port, [
            "agents/one/presence", "agents/two/presence",
            DEFAULT_TOPICS.registry("observer"),
            DEFAULT_TOPICS.presence("observer"),
        ])


@pytest.mark.asyncio
async def test_messaging_only_runtime_gets_no_directory_on_real_broker(
    mosquitto_broker,
    tmp_path,
):
    """Messaging-only runtimes exchange mail without directory state."""
    host, port = mosquitto_broker

    collected = CollectingHandler()
    peer = AgentBus(
        agent_id="visible-peer",
        broker=host,
        port=port,
        retain=False,
    )
    peer.register_handler(collected)
    listener = asyncio.create_task(peer.listen())
    await asyncio.sleep(0.4)

    runtime = ManagedMCPRuntime(
        agent_id="quiet-peer",
        broker=host,
        port=port,
        state_path=tmp_path / "quiet-peer.sqlite3",
    )
    try:
        await runtime.start()
        await runtime.wait_until_ready()
        app = create_mcp_app(runtime)

        await app._tool_fns["send_message"](
            to="visible-peer",
            subject="still routable",
            body="messaging survives the directory refusal",
        )
        await collected.wait_for_message(timeout=5.0)

        with pytest.raises(PresenceRequiredError):
            await runtime.list_agents()
        with pytest.raises(PresenceRequiredError):
            await app._tool_fns["agent_state"](action="list")

        assert collected.received[0].subject == "still routable"
        assert runtime.registry.list_states(include_offline=True) == []
    finally:
        await runtime.stop()
        await _stop(listener)
        await _clear_retained(host, port, [
            DEFAULT_TOPICS.presence("visible-peer"),
            DEFAULT_TOPICS.registry("quiet-peer"),
            DEFAULT_TOPICS.presence("quiet-peer"),
        ])


# ---------------------------------------------------------------------------
# Handler e2e -- side effects on disk / DB / subprocess
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_file_bridge_handler_writes_to_disk(mosquitto_broker, tmp_path):
    """FileBridgeHandler appends received messages to a real file."""
    host, port = mosquitto_broker
    inbox = tmp_path / "inbox.md"

    receiver = AgentBus(agent_id="fb-rx", broker=host, port=port, retain=False)
    receiver.register_handler(FileBridgeHandler(str(inbox)))
    # Need a way to know the write landed -- add a barrier handler.
    barrier = CollectingHandler()
    receiver.register_handler(barrier)

    sender = AgentBus(agent_id="fb-tx", broker=host, port=port, retain=False)
    t = asyncio.create_task(receiver.listen())
    await asyncio.sleep(0.3)

    try:
        await sender.send(to="fb-rx", subject="hello", body="written to disk")
        await barrier.wait_for_message(timeout=3.0)

        text = inbox.read_text()
        assert "written to disk" in text
        assert "From: fb-tx" in text
        assert "hello" in text
    finally:
        await _stop(t)
        await _clear_retained(host, port, ["agents/fb-rx/presence"])


@pytest.mark.asyncio
async def test_sqlite_archive_handler_persists_message(mosquitto_broker, tmp_path):
    """SQLiteArchive logs each received message to SQLite."""
    import sqlite3

    host, port = mosquitto_broker
    db_path = tmp_path / "archive.db"

    receiver = AgentBus(agent_id="sql-rx", broker=host, port=port, retain=False)
    receiver.register_handler(SQLiteArchive(str(db_path)))
    barrier = CollectingHandler()
    receiver.register_handler(barrier)

    sender = AgentBus(agent_id="sql-tx", broker=host, port=port, retain=False)
    t = asyncio.create_task(receiver.listen())
    await asyncio.sleep(0.3)

    try:
        await sender.send(to="sql-rx", subject="archived", body="row in sqlite")
        await barrier.wait_for_message(timeout=3.0)

        # Small grace for async archive commit
        await asyncio.sleep(0.1)

        with sqlite3.connect(db_path) as con:
            rows = con.execute(
                "SELECT from_agent, to_agent, subject, body, direction FROM messages"
            ).fetchall()
        assert len(rows) == 1
        from_agent, to_agent, subject, body, direction = rows[0]
        assert from_agent == "sql-tx"
        assert to_agent == "sql-rx"
        assert subject == "archived"
        assert body == "row in sqlite"
        assert direction == "received"
    finally:
        await _stop(t)
        await _clear_retained(host, port, ["agents/sql-rx/presence"])


@pytest.mark.asyncio
async def test_direct_invocation_handler_fires_subprocess(mosquitto_broker, tmp_path):
    """DirectInvocationHandler runs a command with body on stdin and env vars set."""
    host, port = mosquitto_broker
    out_file = tmp_path / "invocation.log"

    # Use a trivial shell command: cat stdin + env var into a file.
    # shell=False is enforced in the handler, so argv is a real exec.
    script = tmp_path / "capture.sh"
    script.write_text(
        "#!/bin/bash\n"
        f'echo "FROM=$SWARMBUS_FROM SUBJECT=$SWARMBUS_SUBJECT" >> "{out_file}"\n'
        f'cat >> "{out_file}"\n'
        f'echo >> "{out_file}"\n'
    )
    script.chmod(script.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)

    receiver = AgentBus(agent_id="di-rx", broker=host, port=port, retain=False)
    receiver.register_handler(DirectInvocationHandler(command=[str(script)]))
    barrier = CollectingHandler()
    receiver.register_handler(barrier)

    sender = AgentBus(agent_id="di-tx", broker=host, port=port, retain=False)
    t = asyncio.create_task(receiver.listen())
    await asyncio.sleep(0.3)

    try:
        await sender.send(to="di-rx", subject="trigger", body="payload-on-stdin")
        await barrier.wait_for_message(timeout=3.0)
        await asyncio.sleep(0.2)  # give subprocess time to flush

        text = out_file.read_text()
        assert "FROM=di-tx SUBJECT=trigger" in text
        assert "payload-on-stdin" in text
    finally:
        await _stop(t)
        await _clear_retained(host, port, ["agents/di-rx/presence"])


@pytest.mark.asyncio
async def test_presence_lifecycle_online_then_offline(mosquitto_broker):
    """Happy-path LWT / presence test: agent publishes online on subscribe,
    then publishes offline when the listen loop is cancelled cleanly.

    We can't force an unclean TCP abort from test code, but we can verify
    the graceful path end-to-end against a real broker: announce, yield
    the online presence to a probe subscriber, shut down, yield the offline
    presence retained."""
    host, port = mosquitto_broker

    agent = AgentBus(agent_id="presence-tester", broker=host, port=port)
    listen_task = asyncio.create_task(agent.listen())
    await asyncio.sleep(0.3)  # let presence announce

    # Probe: subscribe to the presence topic and read the retained payload.
    async def _read_presence():
        async with aiomqtt.Client(host, port=port) as client:
            await client.subscribe("agents/presence-tester/presence", qos=0)
            try:
                async with asyncio_timeout(0.5):
                    async for mqtt_msg in client.messages:
                        return json.loads(mqtt_msg.payload)
            except asyncio.TimeoutError:
                return None
        return None

    online_payload = await _read_presence()
    assert online_payload is not None
    assert online_payload.get("agent") == "presence-tester"
    assert online_payload.get("status") == "online"

    # Graceful shutdown -- exercises the close path, not LWT.
    await _stop(listen_task)
    await agent.disconnect()
    await asyncio.sleep(0.2)

    offline_payload = await _read_presence()
    assert offline_payload is not None
    assert offline_payload.get("agent") == "presence-tester"
    assert offline_payload.get("status") == "offline"

    await _clear_retained(host, port, ["agents/presence-tester/presence"])


@pytest.mark.asyncio
async def test_non_retained_message_lost_when_no_subscriber(
    mosquitto_broker,
    listening_agent,
):
    """Documented invariant: directed messages default to retain=False,
    so sends that arrive while no subscriber is connected are dropped.
    This is the entire reason we recommend running a listener daemon.

    The second send is the control. Zero received is also what a listener
    that never subscribed, a send that silently failed, or a sender and
    receiver disagreeing about the topic produce, so the absence above
    only means anything once the same bus and the same handler have been
    shown to deliver."""
    host, port = mosquitto_broker

    # Send to an agent with no listener running.
    sender = AgentBus(agent_id="lost-sender", broker=host, port=port, retain=False)
    await sender.send(to="lost-receiver", subject="lost", body="never seen")
    await asyncio.sleep(0.2)  # let the publish complete

    handler = CollectingHandler()
    await listening_agent("lost-receiver", handler, settle=0.5)

    assert len(handler.received) == 0, (
        f"Expected 0 messages for non-retained lost send, got "
        f"{len(handler.received)}. This would mean the default retain "
        f"semantics have changed, which is a breaking wire protocol change."
    )

    await sender.send(to="lost-receiver", subject="live", body="arrives now")
    await handler.wait_for_message(timeout=3.0)

    assert [msg.subject for msg in handler.received] == ["live"]


@pytest.mark.asyncio
async def test_send_receive_large_body_at_limit(mosquitto_broker):
    """64KB is the documented envelope body cap. Verify a body at that
    limit round-trips through MQTT + handler cleanly. This catches any
    regression that would silently truncate or reject large bodies on the
    wire (distinct from the unit test in test_message.py which only
    validates the size-check on construct)."""
    host, port = mosquitto_broker
    handler = CollectingHandler()

    receiver = AgentBus(agent_id="big-rx", broker=host, port=port, retain=False)
    receiver.register_handler(handler)

    sender = AgentBus(agent_id="big-tx", broker=host, port=port, retain=False)

    listen_task = asyncio.create_task(receiver.listen())
    await asyncio.sleep(0.2)

    # Leave room for envelope overhead -- body alone can be the full 64KB.
    body = "x" * (64 * 1024)

    try:
        await sender.send(to="big-rx", subject="big", body=body)
        await handler.wait_for_message(timeout=5.0)
        assert len(handler.received) == 1
        assert len(handler.received[0].body) == 64 * 1024
        assert handler.received[0].body == body
    finally:
        await _stop(listen_task)
        await _clear_retained(host, port, ["agents/big-rx/presence"])


@pytest.mark.asyncio
async def test_transient_registry_gc_removes_retained_state_on_real_broker(
    mosquitto_broker,
):
    host, port = mosquitto_broker
    topics = TopicMap(root="gc-acceptance")
    now = datetime.now(timezone.utc)
    registry = RegistryRecord(
        agent_id="expired-worker",
        lifecycle="transient",
        started_at=now - timedelta(hours=1),
        last_seen=now - timedelta(minutes=20),
    )
    presence = PresenceRecord(
        agent_id="expired-worker",
        state="offline",
        reason="shutdown",
    )
    retained_topics = [
        topics.registry("expired-worker"),
        topics.presence("expired-worker"),
    ]
    await _clear_retained(host, port, retained_topics)

    try:
        async with aiomqtt.Client(host, port=port) as client:
            await client.publish(
                retained_topics[0],
                registry.to_json(),
                qos=1,
                retain=True,
            )
            await client.publish(
                retained_topics[1],
                presence.to_json(),
                qos=1,
                retain=True,
            )

        maintenance = RegistryMaintenance(
            broker=host,
            port=port,
            topics=topics,
            snapshot_seconds=0.2,
        )
        report = await maintenance.gc_transient(
            now=now,
            stale_after_seconds=60,
            retention_seconds=300,
            batch_size=10,
        )

        assert report.deleted_agent_ids == ["expired-worker"]

        async with aiomqtt.Client(host, port=port) as client:
            for topic in retained_topics:
                await client.subscribe(topic, qos=1)
            with pytest.raises(asyncio.TimeoutError):
                await asyncio.wait_for(
                    anext(client.messages),
                    timeout=0.2,
                )
    finally:
        await _clear_retained(host, port, retained_topics)


@pytest.mark.asyncio
async def test_durable_session_queues_message_until_reconnect(
    mosquitto_broker,
):
    """Confirm durable persistent identities receive queued mail."""
    host, port = mosquitto_broker
    agent_id = "queued-session-control"
    client_id = f"swarmbus-{agent_id}"
    inbox_topic = DEFAULT_TOPICS.inbox(agent_id)

    async with aiomqtt.Client(
        host,
        port=port,
        identifier=client_id,
        clean_session=True,
    ):
        pass

    try:
        async with aiomqtt.Client(
            host,
            port=port,
            identifier=client_id,
            clean_session=False,
        ) as client:
            await client.subscribe(inbox_topic, qos=1)

        async with aiomqtt.Client(host, port=port) as publisher:
            await publisher.publish(
                inbox_topic,
                AgentMessage.create(
                    from_="sender",
                    to=agent_id,
                    subject="queued",
                    body="must survive the disconnect",
                ).to_json(),
                qos=1,
                retain=False,
            )

        async with aiomqtt.Client(
            host,
            port=port,
            identifier=client_id,
            clean_session=False,
        ) as client:
            delivered = await asyncio.wait_for(
                anext(client.messages),
                timeout=3.0,
            )

        replayed = AgentMessage.from_json(delivered.payload)
        assert str(delivered.topic) == inbox_topic
        assert replayed.subject == "queued"
        assert replayed.body == "must survive the disconnect"
    finally:
        async with aiomqtt.Client(
            host,
            port=port,
            identifier=client_id,
            clean_session=True,
        ):
            pass


@pytest.mark.asyncio
async def test_transient_durable_shutdown_destroys_queued_session(
    mosquitto_broker,
    tmp_path,
):
    """Clean transient shutdown destroys queued durable-session mail."""
    host, port = mosquitto_broker
    agent_id = "transient-durable-exit"
    client_id = f"swarmbus-{agent_id}"
    inbox_topic = DEFAULT_TOPICS.inbox(agent_id)
    runtime = ManagedMCPRuntime(
        agent_id=agent_id,
        broker=host,
        port=port,
        durable=True,
        presence=True,
        lifecycle="transient",
        state_path=tmp_path / "transient.sqlite3",
    )

    try:
        await runtime.start()
        await runtime.wait_until_ready()
        await runtime.stop()

        async with aiomqtt.Client(host, port=port) as publisher:
            await publisher.publish(
                inbox_topic,
                AgentMessage.create(
                    from_="sender",
                    to=agent_id,
                    subject="queued",
                    body="must not outlive the erased identity",
                ).to_json(),
                qos=1,
                retain=False,
            )

        async with aiomqtt.Client(
            host,
            port=port,
            identifier=client_id,
            clean_session=False,
        ) as client:
            with pytest.raises(asyncio.TimeoutError):
                await asyncio.wait_for(
                    anext(client.messages),
                    timeout=0.5,
                )
    finally:
        await runtime.stop()
        async with aiomqtt.Client(
            host,
            port=port,
            identifier=client_id,
            clean_session=True,
        ):
            pass
        await _clear_retained(
            host,
            port,
            [
                DEFAULT_TOPICS.registry(agent_id),
                DEFAULT_TOPICS.presence(agent_id),
            ],
        )


@pytest.mark.asyncio
async def test_registry_forget_destroys_queued_durable_session(
    mosquitto_broker,
):
    """The absence here is only meaningful against the control above,
    which proves this broker queues QoS1 mail for an offline session."""
    host, port = mosquitto_broker
    agent_id = "retired-session-acceptance"
    client_id = f"swarmbus-{agent_id}"
    inbox_topic = DEFAULT_TOPICS.inbox(agent_id)

    async with aiomqtt.Client(
        host,
        port=port,
        identifier=client_id,
        clean_session=True,
    ):
        pass

    try:
        async with aiomqtt.Client(
            host,
            port=port,
            identifier=client_id,
            clean_session=False,
        ) as client:
            await client.subscribe(inbox_topic, qos=1)

        async with aiomqtt.Client(host, port=port) as publisher:
            await publisher.publish(
                inbox_topic,
                AgentMessage.create(
                    from_="sender",
                    to=agent_id,
                    subject="queued",
                    body="must be destroyed with retired session",
                ).to_json(),
                qos=1,
                retain=False,
            )
            # Supply offline evidence so forget uses the normal interlock path.
            await publisher.publish(
                DEFAULT_TOPICS.presence(agent_id),
                PresenceRecord(agent_id=agent_id, state="offline").to_json(),
                qos=1,
                retain=True,
            )

        maintenance = RegistryMaintenance(
            broker=host,
            port=port,
            snapshot_seconds=0.2,
        )
        report = await maintenance.forget(agent_id)

        assert report.session_destroyed is True
        assert report.deleted is True

        async with aiomqtt.Client(
            host,
            port=port,
            identifier=client_id,
            clean_session=False,
        ) as client:
            with pytest.raises(asyncio.TimeoutError):
                await asyncio.wait_for(
                    anext(client.messages),
                    timeout=0.2,
                )
    finally:
        async with aiomqtt.Client(
            host,
            port=port,
            identifier=client_id,
            clean_session=True,
        ):
            pass
        await _clear_retained(
            host,
            port,
            [
                DEFAULT_TOPICS.registry(agent_id),
                DEFAULT_TOPICS.presence(agent_id),
            ],
        )


@pytest.mark.asyncio
async def test_mcp_tools_expose_expected_signatures(tmp_path):
    """MCP tool signatures are a public contract -- anything consuming
    swarmbus via MCP (Claude Code sidecar, Cursor, custom clients) breaks
    if a tool name or parameter name changes. Assert the full tool shape."""
    assert list(inspect.signature(create_mcp_app).parameters) == ["runtime"]

    runtime = ManagedMCPRuntime(
        agent_id="sig-check",
        state_path=tmp_path / "sig-check.sqlite3",
    )
    app = create_mcp_app(runtime)

    expected = {
        "send_message",
        "read_inbox",
        "agent_state",
    }
    assert set(app._tool_fns.keys()) == expected

    send_sig = inspect.signature(app._tool_fns["send_message"])
    # Positional/keyword params the LLM wire protocol depends on.
    assert set(send_sig.parameters.keys()) >= {"to", "subject", "body", "content_type"}

    read_sig = inspect.signature(app._tool_fns["read_inbox"])
    assert list(read_sig.parameters) == [
        "ack_ids",
        "max_messages",
        "wait_seconds",
    ]
    assert read_sig.parameters["ack_ids"].default is None
    assert read_sig.parameters["max_messages"].default == 10
    assert read_sig.parameters["wait_seconds"].default == 0.0

    state_sig = inspect.signature(app._tool_fns["agent_state"])
    assert list(state_sig.parameters) == [
        "action",
        "agent_id",
        "status",
        "working_set",
        "capabilities",
        "include_offline",
        "lifecycle",
    ]


def test_list_agents_tool_is_gone(tmp_path):
    """The MCP surface exposes agent_state instead of list_agents."""
    runtime = ManagedMCPRuntime(
        agent_id="gone-check",
        state_path=tmp_path / "gone-check.sqlite3",
    )

    assert "list_agents" not in create_mcp_app(runtime)._tool_fns
    assert inspect.iscoroutinefunction(runtime.list_agents)
    assert inspect.iscoroutinefunction(AgentBus.probe().list_agents)
