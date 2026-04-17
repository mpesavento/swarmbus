# tests/test_bus.py
import json
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from swarmbus.bus import AgentBus
from swarmbus.message import AgentMessage
from swarmbus.handlers.base import BaseHandler


class RecordingHandler(BaseHandler):
    def __init__(self):
        self.received = []

    async def handle(self, msg: AgentMessage) -> None:
        self.received.append(msg)


def test_invalid_agent_id_raises():
    with pytest.raises(ValueError):
        AgentBus(agent_id="BAD ID!", broker="localhost")


def test_reserved_agent_id_broadcast_rejected():
    with pytest.raises(ValueError, match="reserved"):
        AgentBus(agent_id="broadcast", broker="localhost")


def test_reserved_agent_id_system_rejected():
    with pytest.raises(ValueError, match="reserved"):
        AgentBus(agent_id="system", broker="localhost")


def test_register_handler():
    bus = AgentBus(agent_id="sparrow", broker="localhost")
    h = RecordingHandler()
    bus.register_handler(h)
    assert h in bus._handlers


@pytest.mark.asyncio
async def test_send_publishes_to_correct_topic():
    bus = AgentBus(agent_id="sparrow", broker="localhost")

    published = []

    class FakeClient:
        async def __aenter__(self):
            return self
        async def __aexit__(self, *_):
            pass
        async def publish(self, topic, payload, qos=0, retain=False):
            published.append((topic, payload))

    with patch("swarmbus.bus.aiomqtt.Client", return_value=FakeClient()):
        await bus.send(to="wren", subject="hello", body="world")

    assert len(published) == 1
    topic, payload = published[0]
    assert topic == "agents/wren/inbox"
    data = json.loads(payload)
    assert data["from"] == "sparrow"
    assert data["to"] == "wren"
    assert data["subject"] == "hello"
    assert data["body"] == "world"


@pytest.mark.asyncio
async def test_async_context_manager_reuses_single_client():
    """Two sends inside `async with` must share one MQTT client."""
    bus = AgentBus(agent_id="sparrow", broker="localhost")
    publish_calls = []
    aenter_calls = []

    class FakeClient:
        def __init__(self):
            aenter_calls.append(self)
        async def __aenter__(self):
            return self
        async def __aexit__(self, *_):
            pass
        async def publish(self, topic, payload, qos=0, retain=False):
            publish_calls.append(topic)

    with patch("swarmbus.bus.aiomqtt.Client", side_effect=lambda *a, **kw: FakeClient()):
        async with bus as b:
            await b.send(to="wren", subject="one", body="x")
            await b.send(to="wren", subject="two", body="y")

    assert len(aenter_calls) == 1  # only one client ever constructed
    assert publish_calls == ["agents/wren/inbox", "agents/wren/inbox"]


@pytest.mark.asyncio
async def test_one_shot_send_without_context_opens_its_own_client():
    """Without connect()/context-manager, send() still works (connect-per-call)."""
    bus = AgentBus(agent_id="sparrow", broker="localhost")
    constructions = []

    class FakeClient:
        def __init__(self):
            constructions.append(self)
        async def __aenter__(self):
            return self
        async def __aexit__(self, *_):
            pass
        async def publish(self, *args, **kwargs):
            pass

    with patch("swarmbus.bus.aiomqtt.Client", side_effect=lambda *a, **kw: FakeClient()):
        await bus.send(to="wren", subject="hi", body="x")
        await bus.send(to="wren", subject="hi", body="x")

    assert len(constructions) == 2  # one client per send()


@pytest.mark.asyncio
async def test_close_is_idempotent():
    bus = AgentBus(agent_id="sparrow", broker="localhost")
    await bus.close()  # no-op before connect
    await bus.close()  # still no-op


@pytest.mark.asyncio
async def test_send_default_retain_is_false():
    """Inbox messages must not be retained by default (would replay forever)."""
    bus = AgentBus(agent_id="sparrow", broker="localhost")
    captured = {}

    class FakeClient:
        async def __aenter__(self):
            return self
        async def __aexit__(self, *_):
            pass
        async def publish(self, topic, payload, qos=0, retain=False):
            captured["retain"] = retain

    with patch("swarmbus.bus.aiomqtt.Client", return_value=FakeClient()):
        await bus.send(to="wren", subject="hi", body="x")

    assert captured["retain"] is False


@pytest.mark.asyncio
async def test_listen_retains_online_presence():
    """Late subscribers must see current presence via retained messages."""
    bus = AgentBus(agent_id="sparrow", broker="localhost")
    published = []
    will_args = {}

    class FakeClient:
        async def __aenter__(self):
            return self
        async def __aexit__(self, *_):
            pass
        async def publish(self, topic, payload, qos=0, retain=False):
            published.append({"topic": topic, "retain": retain, "qos": qos})
        async def subscribe(self, *args, **kwargs):
            pass
        @property
        def messages(self):
            async def _gen():
                if False:
                    yield
            return _gen()

    def _capture_client(broker, port, will=None):
        will_args["payload"] = will.payload if will else None
        will_args["retain"] = will.retain if will else None
        return FakeClient()

    with patch("swarmbus.bus.aiomqtt.Client", side_effect=_capture_client):
        await bus.listen()

    online = [p for p in published if p["topic"] == "agents/sparrow/presence"]
    assert online, "expected an online presence publish"
    assert online[0]["retain"] is True
    assert will_args["retain"] is True  # LWT also retained


@pytest.mark.asyncio
async def test_send_broadcast_uses_correct_topic():
    bus = AgentBus(agent_id="sparrow", broker="localhost")
    published = []

    class FakeClient:
        async def __aenter__(self):
            return self
        async def __aexit__(self, *_):
            pass
        async def publish(self, topic, payload, qos=0, retain=False):
            published.append(topic)

    with patch("swarmbus.bus.aiomqtt.Client", return_value=FakeClient()):
        await bus.send(to="broadcast", subject="all", body="hello everyone")

    assert published[0] == "agents/broadcast"  # not agents/broadcast/inbox


@pytest.mark.asyncio
async def test_listen_dispatches_to_handlers():
    bus = AgentBus(agent_id="sparrow", broker="localhost")
    handler = RecordingHandler()
    bus.register_handler(handler)

    msg = AgentMessage.create(from_="wren", to="sparrow", subject="ping", body="hi")

    class FakeMessage:
        payload = msg.to_json().encode()

    class FakeClient:
        async def __aenter__(self):
            return self
        async def __aexit__(self, *_):
            pass
        async def publish(self, *args, **kwargs):
            pass
        async def subscribe(self, *args, **kwargs):
            pass
        @property
        def messages(self):
            async def _gen():
                yield FakeMessage()
            return _gen()

    with patch("swarmbus.bus.aiomqtt.Client", return_value=FakeClient()):
        await bus.listen()

    assert len(handler.received) == 1
    assert handler.received[0].subject == "ping"
    assert handler.received[0].from_agent == "wren"


@pytest.mark.asyncio
async def test_listen_skips_invalid_envelope():
    bus = AgentBus(agent_id="sparrow", broker="localhost")
    handler = RecordingHandler()
    bus.register_handler(handler)

    class FakeBadMessage:
        payload = b'{"not": "valid"}'

    class FakeClient:
        async def __aenter__(self):
            return self
        async def __aexit__(self, *_):
            pass
        async def publish(self, *args, **kwargs):
            pass
        async def subscribe(self, *args, **kwargs):
            pass
        @property
        def messages(self):
            async def _gen():
                yield FakeBadMessage()
            return _gen()

    with patch("swarmbus.bus.aiomqtt.Client", return_value=FakeClient()):
        await bus.listen()  # must not raise

    assert handler.received == []  # invalid message discarded


@pytest.mark.asyncio
async def test_listen_reconnects_on_mqtt_error():
    """If the broker drops, listen() reconnects rather than crashing."""
    import aiomqtt
    bus = AgentBus(agent_id="sparrow", broker="localhost")
    handler = RecordingHandler()
    bus.register_handler(handler)

    msg = AgentMessage.create(from_="wren", to="sparrow", subject="ping", body="hi")

    class FakeMessage:
        payload = msg.to_json().encode()

    class GoodClient:
        async def __aenter__(self):
            return self
        async def __aexit__(self, *_):
            pass
        async def publish(self, *args, **kwargs):
            pass
        async def subscribe(self, *args, **kwargs):
            pass
        @property
        def messages(self):
            async def _gen():
                yield FakeMessage()
            return _gen()

    class FlakyClient:
        async def __aenter__(self):
            raise aiomqtt.MqttError("connection refused")
        async def __aexit__(self, *_):
            pass

    call_sequence = [FlakyClient(), GoodClient()]

    def _client_factory(*args, **kwargs):
        return call_sequence.pop(0)

    with patch("swarmbus.bus.aiomqtt.Client", side_effect=_client_factory), \
         patch("swarmbus.bus.asyncio.sleep", new=AsyncMock()):  # don't actually wait
        await bus.listen(reconnect_initial=0.01)

    assert len(handler.received) == 1
    assert call_sequence == []  # both clients were consumed


@pytest.mark.asyncio
async def test_listen_continues_after_handler_exception():
    bus = AgentBus(agent_id="sparrow", broker="localhost")

    class CrashingHandler(BaseHandler):
        async def handle(self, msg):
            raise RuntimeError("boom")

    ok_handler = RecordingHandler()
    bus.register_handler(CrashingHandler())
    bus.register_handler(ok_handler)

    msg = AgentMessage.create(from_="wren", to="sparrow", subject="ping", body="hi")

    class FakeMessage:
        payload = msg.to_json().encode()

    class FakeClient:
        async def __aenter__(self):
            return self
        async def __aexit__(self, *_):
            pass
        async def publish(self, *args, **kwargs):
            pass
        async def subscribe(self, *args, **kwargs):
            pass
        @property
        def messages(self):
            async def _gen():
                yield FakeMessage()
            return _gen()

    with patch("swarmbus.bus.aiomqtt.Client", return_value=FakeClient()):
        await bus.listen()  # must not raise

    assert len(ok_handler.received) == 1  # ok_handler still ran


@pytest.mark.asyncio
async def test_listen_preserves_handler_registration_order():
    """Documented invariant: handlers run in registration order.
    Regressions here break anything that layers a logger before an
    effect handler, etc."""
    bus = AgentBus(agent_id="sparrow", broker="localhost")

    order_recorded: list[str] = []

    def make_handler(tag: str) -> BaseHandler:
        class H(BaseHandler):
            async def handle(self, msg):
                order_recorded.append(tag)
        return H()

    bus.register_handler(make_handler("A"))
    bus.register_handler(make_handler("B"))
    bus.register_handler(make_handler("C"))

    msg = AgentMessage.create(from_="wren", to="sparrow", subject="x", body="y")

    class FakeMessage:
        payload = msg.to_json().encode()

    class FakeClient:
        async def __aenter__(self): return self
        async def __aexit__(self, *_): pass
        async def publish(self, *args, **kwargs): pass
        async def subscribe(self, *args, **kwargs): pass
        @property
        def messages(self):
            async def _gen():
                yield FakeMessage()
            return _gen()

    with patch("swarmbus.bus.aiomqtt.Client", return_value=FakeClient()):
        await bus.listen()

    assert order_recorded == ["A", "B", "C"]
