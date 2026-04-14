"""Direct tests for AgentBus.read_inbox / watch_inbox / list_agents.

Before the refactor these methods only lived on the MCP server and were
tested indirectly. Now that they're first-class on AgentBus, cover the
timeout and malformed-envelope branches directly.
"""
from __future__ import annotations

import json
import pytest
from unittest.mock import patch

import aiomqtt

from agentbus.bus import AgentBus
from agentbus.message import AgentMessage


class _FakeMsg:
    def __init__(self, payload: bytes):
        self.payload = payload


class _FakeClient:
    """Replays a preset list of payloads, then hangs (caller relies on timeout)."""
    def __init__(self, payloads: list[bytes]):
        self._payloads = payloads

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        pass

    async def subscribe(self, *_args, **_kwargs):
        pass

    @property
    def messages(self):
        async def _gen():
            for p in self._payloads:
                yield _FakeMsg(p)
        return _gen()


class _BadClient:
    async def __aenter__(self):
        raise aiomqtt.MqttError("connection refused")

    async def __aexit__(self, *_):
        pass


def _envelope(**overrides) -> bytes:
    """Build a valid AgentMessage JSON payload."""
    msg = AgentMessage.create(
        from_=overrides.get("from_", "sender"),
        to=overrides.get("to", "me"),
        subject=overrides.get("subject", "hi"),
        body=overrides.get("body", "hello"),
    )
    return msg.to_json().encode()


# --------------------------------------------------------------------------
# read_inbox
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_read_inbox_returns_valid_envelopes():
    payloads = [_envelope(subject="one"), _envelope(subject="two")]
    with patch("agentbus.bus.aiomqtt.Client", return_value=_FakeClient(payloads)):
        bus = AgentBus(agent_id="me")
        result = await bus.read_inbox(drain_timeout=0.1)
    assert len(result) == 2
    assert result[0]["subject"] == "one"
    assert result[1]["subject"] == "two"


@pytest.mark.asyncio
async def test_read_inbox_respects_max_messages():
    payloads = [_envelope(subject=f"m{i}") for i in range(5)]
    with patch("agentbus.bus.aiomqtt.Client", return_value=_FakeClient(payloads)):
        bus = AgentBus(agent_id="me")
        result = await bus.read_inbox(max_messages=3, drain_timeout=0.2)
    assert len(result) == 3


@pytest.mark.asyncio
async def test_read_inbox_skips_malformed_envelopes():
    payloads = [b"not even json", _envelope(subject="good"), b'{"partial": 1}']
    with patch("agentbus.bus.aiomqtt.Client", return_value=_FakeClient(payloads)):
        bus = AgentBus(agent_id="me")
        result = await bus.read_inbox(drain_timeout=0.1)
    assert len(result) == 1
    assert result[0]["subject"] == "good"


@pytest.mark.asyncio
async def test_read_inbox_broker_error_raises():
    """CLI contract depends on this: MqttError must propagate so the CLI
    layer can print a clean error and exit 2 (instead of confusing broker-down
    with empty-inbox). MCP callers catch it at the tool boundary."""
    with patch("agentbus.bus.aiomqtt.Client", return_value=_BadClient()):
        bus = AgentBus(agent_id="me")
        with pytest.raises(aiomqtt.MqttError):
            await bus.read_inbox(drain_timeout=0.1)


@pytest.mark.asyncio
async def test_read_inbox_timeout_returns_what_it_has():
    payloads: list[bytes] = []  # nothing to yield → hits timeout
    with patch("agentbus.bus.aiomqtt.Client", return_value=_FakeClient(payloads)):
        bus = AgentBus(agent_id="me")
        result = await bus.read_inbox(drain_timeout=0.05)
    assert result == []


# --------------------------------------------------------------------------
# watch_inbox
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_watch_inbox_returns_first_valid_envelope():
    payloads = [_envelope(subject="first"), _envelope(subject="second")]
    with patch("agentbus.bus.aiomqtt.Client", return_value=_FakeClient(payloads)):
        bus = AgentBus(agent_id="me")
        result = await bus.watch_inbox(timeout=0.2)
    assert result is not None
    assert result["subject"] == "first"


@pytest.mark.asyncio
async def test_watch_inbox_skips_malformed_then_returns_good():
    payloads = [b"garbage", _envelope(subject="good")]
    with patch("agentbus.bus.aiomqtt.Client", return_value=_FakeClient(payloads)):
        bus = AgentBus(agent_id="me")
        result = await bus.watch_inbox(timeout=0.2)
    assert result is not None
    assert result["subject"] == "good"


@pytest.mark.asyncio
async def test_watch_inbox_timeout_returns_none():
    with patch("agentbus.bus.aiomqtt.Client", return_value=_FakeClient([])):
        bus = AgentBus(agent_id="me")
        result = await bus.watch_inbox(timeout=0.05)
    assert result is None


@pytest.mark.asyncio
async def test_watch_inbox_broker_error_raises():
    """Same contract as read_inbox: MqttError propagates."""
    with patch("agentbus.bus.aiomqtt.Client", return_value=_BadClient()):
        bus = AgentBus(agent_id="me")
        with pytest.raises(aiomqtt.MqttError):
            await bus.watch_inbox(timeout=0.1)


@pytest.mark.asyncio
async def test_list_agents_broker_error_raises():
    with patch("agentbus.bus.aiomqtt.Client", return_value=_BadClient()):
        bus = AgentBus.probe()
        with pytest.raises(aiomqtt.MqttError):
            await bus.list_agents(collect_window=0.1)


# --------------------------------------------------------------------------
# list_agents (existing coverage in test_mcp_server is solid; one direct test)
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_agents_filters_offline():
    payloads = [
        json.dumps({"agent": "sparrow", "status": "online"}).encode(),
        json.dumps({"agent": "wren", "status": "online"}).encode(),
        json.dumps({"agent": "ghost", "status": "offline"}).encode(),
    ]
    with patch("agentbus.bus.aiomqtt.Client", return_value=_FakeClient(payloads)):
        bus = AgentBus.probe()
        result = await bus.list_agents(collect_window=0.1)
    assert result == ["sparrow", "wren"]


@pytest.mark.asyncio
async def test_probe_bypasses_agent_id_validation():
    """AgentBus.probe() must not raise even though `_probe` starts with _."""
    bus = AgentBus.probe(broker="localhost")
    assert bus.agent_id == "_probe"
    assert bus.broker == "localhost"


# --------------------------------------------------------------------------
# outbox logging on send
# --------------------------------------------------------------------------


class _NullPublishClient:
    """aiomqtt stand-in that accepts publish() and is a no-op otherwise."""
    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        pass

    async def publish(self, *args, **kwargs):
        pass


@pytest.mark.asyncio
async def test_send_appends_to_outbox(tmp_path):
    outbox = tmp_path / "outbox.md"
    with patch("agentbus.bus.aiomqtt.Client", return_value=_NullPublishClient()):
        bus = AgentBus(agent_id="sparrow")
        await bus.send(
            to="wren",
            subject="hello",
            body="hi wren",
            outbox_path=str(outbox),
        )
    text = outbox.read_text()
    assert "To: wren" in text
    assert "hello" in text
    assert "hi wren" in text


@pytest.mark.asyncio
async def test_send_outbox_creates_parent_dirs(tmp_path):
    outbox = tmp_path / "nested" / "sparrow-outbox.md"
    with patch("agentbus.bus.aiomqtt.Client", return_value=_NullPublishClient()):
        bus = AgentBus(agent_id="sparrow")
        await bus.send(to="wren", subject="x", body="y", outbox_path=str(outbox))
    assert outbox.exists()


@pytest.mark.asyncio
async def test_send_outbox_disabled_when_unset(tmp_path):
    """No outbox_path → no file written; confirms the append is opt-in."""
    outbox = tmp_path / "should_not_exist.md"
    with patch("agentbus.bus.aiomqtt.Client", return_value=_NullPublishClient()):
        bus = AgentBus(agent_id="sparrow")
        await bus.send(to="wren", subject="x", body="y")
    assert not outbox.exists()


@pytest.mark.asyncio
async def test_listen_persistent_passes_stable_identifier_and_clean_session():
    """persistent=True → stable client-id + clean_session=False on the MQTT client."""
    captured_kwargs = {}

    class _ClientStub:
        def __init__(self, *args, **kwargs):
            captured_kwargs.update(kwargs)
        async def __aenter__(self):
            return self
        async def __aexit__(self, *_):
            pass
        async def publish(self, *_args, **_kwargs):
            pass
        async def subscribe(self, *_args, **_kwargs):
            pass
        @property
        def messages(self):
            async def _empty():
                if False:
                    yield  # make generator
            return _empty()

    with patch("agentbus.bus.aiomqtt.Client", _ClientStub):
        bus = AgentBus(agent_id="sparrow", persistent=True)
        await bus.listen()
    assert captured_kwargs.get("identifier") == "agentbus-sparrow"
    assert captured_kwargs.get("clean_session") is False


@pytest.mark.asyncio
async def test_listen_non_persistent_omits_identifier():
    captured_kwargs = {}

    class _ClientStub:
        def __init__(self, *args, **kwargs):
            captured_kwargs.update(kwargs)
        async def __aenter__(self):
            return self
        async def __aexit__(self, *_):
            pass
        async def publish(self, *_args, **_kwargs):
            pass
        async def subscribe(self, *_args, **_kwargs):
            pass
        @property
        def messages(self):
            async def _empty():
                if False:
                    yield
            return _empty()

    with patch("agentbus.bus.aiomqtt.Client", _ClientStub):
        bus = AgentBus(agent_id="sparrow", persistent=False)
        await bus.listen()
    assert "identifier" not in captured_kwargs
    assert "clean_session" not in captured_kwargs


@pytest.mark.asyncio
async def test_send_outbox_agent_id_template_substitutes(tmp_path):
    """`{agent_id}` in outbox_path → replaced with the bus's agent_id."""
    template = str(tmp_path / "{agent_id}-outbox.md")
    with patch("agentbus.bus.aiomqtt.Client", return_value=_NullPublishClient()):
        bus_s = AgentBus(agent_id="sparrow")
        await bus_s.send(to="wren", subject="s", body="b1", outbox_path=template)
        bus_w = AgentBus(agent_id="wren")
        await bus_w.send(to="sparrow", subject="w", body="b2", outbox_path=template)
    assert (tmp_path / "sparrow-outbox.md").read_text().startswith("\n## ")
    assert (tmp_path / "wren-outbox.md").read_text().startswith("\n## ")
    assert "b1" in (tmp_path / "sparrow-outbox.md").read_text()
    assert "b2" in (tmp_path / "wren-outbox.md").read_text()
    assert "b2" not in (tmp_path / "sparrow-outbox.md").read_text()
