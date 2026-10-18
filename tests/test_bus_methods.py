"""Direct tests for AgentBus.read_inbox / watch_inbox / list_agents.

Before the refactor these methods only lived on the MCP server and were
tested indirectly. Now that they're first-class on AgentBus, cover the
timeout and malformed-envelope branches directly.
"""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
import re
import pytest
from unittest.mock import AsyncMock, patch

import aiomqtt

from swarmbus.bus import AgentBus, _legacy_presence_payload
from swarmbus.message import AgentMessage
from swarmbus.registry import PresenceRecord
from swarmbus.topics import DEFAULT_TOPICS

#: Topic the inbox fixtures arrive on; every inbox test uses agent_id "me".
_INBOX_TOPIC = DEFAULT_TOPICS.inbox("me")


class _FakeMsg:
    """Minimal aiomqtt message carrying transport and topic metadata."""
    def __init__(
        self,
        payload: bytes,
        topic: str = _INBOX_TOPIC,
        mid: int = 0,
        qos: int = 1,
    ):
        self.payload = payload
        self.topic = topic
        self.mid = mid
        self.qos = qos


class _FakePaho:
    """Stands in for the paho client aiomqtt wraps; records manual-ack use."""
    def __init__(self, events: list):
        self._events = events
        self.manual_ack = None
        self.acks: list[tuple[int, int]] = []

    def manual_ack_set(self, on: bool):
        self.manual_ack = on
        self._events.append(("manual_ack_set", on))

    def ack(self, mid: int, qos: int):
        self.acks.append((mid, qos))
        self._events.append(("ack", mid))
        return 0


def _legacy_presence(agent_id: str, status: str) -> _FakeMsg:
    """Presence message exactly as AgentBus.listen/disconnect publish it."""
    return _FakeMsg(
        _legacy_presence_payload(agent_id, status).encode(),
        DEFAULT_TOPICS.presence(agent_id),
    )


def _record_presence(
    agent_id: str, state: str, *, topic_owner: str | None = None
) -> _FakeMsg:
    """Build a managed-runtime presence message."""
    record = PresenceRecord(
        agent_id=agent_id,
        state=state,
        connected_at=(
            datetime.now(timezone.utc) if state == "online" else None
        ),
    )
    return _FakeMsg(
        record.to_json().encode(),
        DEFAULT_TOPICS.presence(topic_owner or agent_id),
    )


class _FakeClient:
    """Replay preset payloads until the caller times out."""
    def __init__(
        self,
        payloads: list[bytes] | list[_FakeMsg],
        *,
        topic: str = _INBOX_TOPIC,
    ):
        self._payloads = [
            p if isinstance(p, _FakeMsg) else _FakeMsg(p, topic, mid=i)
            for i, p in enumerate(payloads, start=1)
        ]
        self.events: list = []
        self._client = _FakePaho(self.events)

    async def __aenter__(self):
        self.events.append(("enter",))
        return self

    async def __aexit__(self, *_):
        pass

    async def subscribe(self, *_args, **_kwargs):
        pass

    async def publish(self, topic, payload, **kwargs):
        self.events.append(("publish", str(topic), payload, kwargs))

    @property
    def messages(self):
        async def _gen():
            for p in self._payloads:
                yield p
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
    with patch("swarmbus.bus.aiomqtt.Client", return_value=_FakeClient(payloads)):
        bus = AgentBus(agent_id="me")
        result = await bus.read_inbox(drain_timeout=0.1)
    assert len(result) == 2
    assert result[0]["subject"] == "one"
    assert result[1]["subject"] == "two"


@pytest.mark.asyncio
async def test_read_inbox_respects_max_messages():
    payloads = [_envelope(subject=f"m{i}") for i in range(5)]
    with patch("swarmbus.bus.aiomqtt.Client", return_value=_FakeClient(payloads)):
        bus = AgentBus(agent_id="me")
        result = await bus.read_inbox(max_messages=3, drain_timeout=0.2)
    assert len(result) == 3


@pytest.mark.asyncio
async def test_read_inbox_skips_malformed_envelopes():
    payloads = [b"not even json", _envelope(subject="good"), b'{"partial": 1}']
    with patch("swarmbus.bus.aiomqtt.Client", return_value=_FakeClient(payloads)):
        bus = AgentBus(agent_id="me")
        result = await bus.read_inbox(drain_timeout=0.1)
    assert len(result) == 1
    assert result[0]["subject"] == "good"


@pytest.mark.asyncio
async def test_read_inbox_broker_error_raises():
    """CLI contract depends on this: MqttError must propagate so the CLI
    layer can print a clean error and exit 2 (instead of confusing broker-down
    with empty-inbox). MCP callers catch it at the tool boundary."""
    with patch("swarmbus.bus.aiomqtt.Client", return_value=_BadClient()):
        bus = AgentBus(agent_id="me")
        with pytest.raises(aiomqtt.MqttError):
            await bus.read_inbox(drain_timeout=0.1)


@pytest.mark.asyncio
async def test_read_inbox_timeout_returns_what_it_has():
    payloads: list[bytes] = []  # nothing to yield → hits timeout
    with patch("swarmbus.bus.aiomqtt.Client", return_value=_FakeClient(payloads)):
        bus = AgentBus(agent_id="me")
        result = await bus.read_inbox(drain_timeout=0.05)
    assert result == []


# --------------------------------------------------------------------------
# watch_inbox
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_watch_inbox_returns_first_valid_envelope():
    payloads = [_envelope(subject="first"), _envelope(subject="second")]
    with patch("swarmbus.bus.aiomqtt.Client", return_value=_FakeClient(payloads)):
        bus = AgentBus(agent_id="me")
        result = await bus.watch_inbox(timeout=0.2)
    assert result is not None
    assert result["subject"] == "first"


@pytest.mark.asyncio
async def test_watch_inbox_skips_malformed_then_returns_good():
    payloads = [b"garbage", _envelope(subject="good")]
    with patch("swarmbus.bus.aiomqtt.Client", return_value=_FakeClient(payloads)):
        bus = AgentBus(agent_id="me")
        result = await bus.watch_inbox(timeout=0.2)
    assert result is not None
    assert result["subject"] == "good"


@pytest.mark.asyncio
async def test_watch_inbox_timeout_returns_none():
    with patch("swarmbus.bus.aiomqtt.Client", return_value=_FakeClient([])):
        bus = AgentBus(agent_id="me")
        result = await bus.watch_inbox(timeout=0.05)
    assert result is None


@pytest.mark.asyncio
async def test_watch_inbox_broker_error_raises():
    """Same contract as read_inbox: MqttError propagates."""
    with patch("swarmbus.bus.aiomqtt.Client", return_value=_BadClient()):
        bus = AgentBus(agent_id="me")
        with pytest.raises(aiomqtt.MqttError):
            await bus.watch_inbox(timeout=0.1)


@pytest.mark.asyncio
async def test_list_agents_broker_error_raises():
    with patch("swarmbus.bus.aiomqtt.Client", return_value=_BadClient()):
        bus = AgentBus.probe()
        with pytest.raises(aiomqtt.MqttError):
            await bus.list_agents(collect_window=0.1)


# --------------------------------------------------------------------------
# list_agents (test_mcp_server covers the tool surface; these are direct)
# --------------------------------------------------------------------------


async def _listed(messages: list[_FakeMsg]) -> list[str]:
    """Run list_agents over a canned presence tree."""
    with patch("swarmbus.bus.aiomqtt.Client", return_value=_FakeClient(messages)):
        return await AgentBus.probe().list_agents(collect_window=0.1)


@pytest.mark.asyncio
async def test_list_agents_filters_offline():
    assert await _listed([
        _legacy_presence("sparrow", "online"),
        _legacy_presence("wren", "online"),
        _legacy_presence("ghost", "offline"),
    ]) == ["sparrow", "wren"]


@pytest.mark.asyncio
async def test_list_agents_reads_presence_record_shape():
    """AgentBus accepts managed-runtime presence records."""
    assert await _listed([
        _record_presence("sparrow", "online"),
        _record_presence("ghost", "offline"),
    ]) == ["sparrow"]


@pytest.mark.asyncio
async def test_list_agents_mixes_legacy_and_record_shapes():
    """Both shapes coexist on the presence tree during the transition."""
    assert await _listed([
        _legacy_presence("legacy-daemon", "online"),
        _record_presence("managed-agent", "online"),
    ]) == ["legacy-daemon", "managed-agent"]


@pytest.mark.asyncio
async def test_list_agents_trusts_topic_owner_not_payload_claim():
    """Presence topic ownership overrides payload claims."""
    assert await _listed([
        _record_presence("sparrow", "online", topic_owner="wren"),
    ]) == ["wren"]


@pytest.mark.asyncio
async def test_list_agents_skips_unattributable_presence_topic():
    """Unattributable presence topics do not abort listing."""
    assert await _listed([
        _record_presence("imposter", "online", topic_owner="outer/inner/too-deep"),
        _legacy_presence("sparrow", "online"),
    ]) == ["sparrow"]


@pytest.mark.parametrize("junk", [b"[]", b"null", b"3"])
@pytest.mark.asyncio
async def test_list_agents_skips_non_dict_payloads(junk: bytes):
    """Non-object JSON does not abort presence listing."""
    assert await _listed([
        _FakeMsg(junk, DEFAULT_TOPICS.presence("junk-publisher")),
        _legacy_presence("sparrow", "online"),
    ]) == ["sparrow"]


@pytest.mark.asyncio
async def test_list_agents_applies_last_presence_write_per_agent():
    """Retained presence is last-write-wins, in both directions."""
    assert await _listed([
        _legacy_presence("sparrow", "offline"),
        _legacy_presence("sparrow", "online"),
    ]) == ["sparrow"]
    assert await _listed([
        _legacy_presence("sparrow", "online"),
        _legacy_presence("sparrow", "offline"),
    ]) == []


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
    with patch("swarmbus.bus.aiomqtt.Client", return_value=_NullPublishClient()):
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
    with patch("swarmbus.bus.aiomqtt.Client", return_value=_NullPublishClient()):
        bus = AgentBus(agent_id="sparrow")
        await bus.send(to="wren", subject="x", body="y", outbox_path=str(outbox))
    assert outbox.exists()


@pytest.mark.asyncio
async def test_send_outbox_disabled_when_unset(tmp_path):
    """No outbox_path → no file written; confirms the append is opt-in."""
    outbox = tmp_path / "should_not_exist.md"
    with patch("swarmbus.bus.aiomqtt.Client", return_value=_NullPublishClient()):
        bus = AgentBus(agent_id="sparrow")
        await bus.send(to="wren", subject="x", body="y")
    assert not outbox.exists()


ROTATED = re.compile(r"sparrow-outbox\.\d{8}T\d{12}Z\.md")


async def _send_n(outbox, n, **kw):
    with patch("swarmbus.bus.aiomqtt.Client", return_value=_NullPublishClient()):
        bus = AgentBus(agent_id="sparrow")
        for i in range(n):
            await bus.send(to="wren", subject=f"s{i}", body="x" * 100,
                           outbox_path=str(outbox), **kw)


def _rotated(dir_):
    return sorted(p for p in dir_.iterdir() if ROTATED.fullmatch(p.name))


@pytest.mark.asyncio
async def test_send_outbox_rotates_by_size_and_keeps_the_newest(tmp_path):
    outbox = tmp_path / "sparrow-outbox.md"
    other = tmp_path / "coder-outbox.20200101T000000000000Z.md"
    other.write_text("coder's")
    loose = tmp_path / "sparrow-outbox.old.md"
    loose.write_text("hand-made")

    # One entry is about 138 bytes, so 150 holds one entry per file.
    await _send_n(outbox, 5, outbox_max_bytes=150, outbox_keep=2)

    assert "s4" in outbox.read_text() and "s3" not in outbox.read_text()
    kept = _rotated(tmp_path)
    assert [p.read_text().count("## [") for p in kept] == [1, 1]
    assert ["s2" in kept[0].read_text(), "s3" in kept[1].read_text()] == [True, True]
    assert other.read_text() == "coder's" and loose.read_text() == "hand-made"


@pytest.mark.asyncio
async def test_send_outbox_rotation_off_keep_all_and_oversized_entries(tmp_path):
    off = tmp_path / "off" / "sparrow-outbox.md"
    await _send_n(off, 4, outbox_max_bytes=0)
    assert off.read_text().count("## [") == 4 and _rotated(off.parent) == []

    keep_all = tmp_path / "all" / "sparrow-outbox.md"
    await _send_n(keep_all, 4, outbox_max_bytes=150, outbox_keep=0)
    assert len(_rotated(keep_all.parent)) == 3

    big = tmp_path / "big" / "sparrow-outbox.md"
    await _send_n(big, 3, outbox_max_bytes=50, outbox_keep=0)
    files = [big, *_rotated(big.parent)]
    assert [p.read_text().count("## [") for p in files] == [1, 1, 1]
    assert all(p.read_text().count("x" * 100) == 1 for p in files)


@pytest.mark.asyncio
async def test_send_outbox_rotation_loses_nothing_a_stale_writer_appends(tmp_path):
    outbox = tmp_path / "sparrow-outbox.md"
    await _send_n(outbox, 1, outbox_max_bytes=150)
    stale = outbox.open("ab")            # a sender that opened the file before the rotation
    await _send_n(outbox, 1, outbox_max_bytes=150)
    stale.write(b"\n## [late] To: wren | late\nlate\n")
    stale.close()
    (rotated,) = _rotated(tmp_path)
    assert "| late" in rotated.read_text() and "| late" not in outbox.read_text()


@pytest.mark.asyncio
async def test_send_rejects_negative_outbox_rotation_before_publishing(tmp_path):
    client = _NullPublishClient()
    client.publish = AsyncMock()
    with patch("swarmbus.bus.aiomqtt.Client", return_value=client):
        bus = AgentBus(agent_id="sparrow")
        with pytest.raises(ValueError):
            await bus.send(to="wren", subject="x", body="y",
                           outbox_path=str(tmp_path / "o.md"), outbox_keep=-1)
    client.publish.assert_not_called()


@pytest.mark.asyncio
async def test_send_outbox_skips_the_entry_when_the_file_keeps_rotating(tmp_path, monkeypatch, caplog):
    import fcntl
    import itertools
    import logging
    import os

    outbox = tmp_path / "sparrow-outbox.md"
    real_flock = fcntl.flock
    rotations = itertools.count()

    def rotating_flock(fd, op):
        # Another sender rotates the file each time this one gets the lock.
        real_flock(fd, op)
        if op & fcntl.LOCK_EX:
            os.rename(outbox, tmp_path / f"gone-{next(rotations)}.md")
            outbox.touch()

    monkeypatch.setattr(fcntl, "flock", rotating_flock)
    with caplog.at_level(logging.WARNING, logger="swarmbus.bus"):
        await _send_n(outbox, 1)
    assert "file kept rotating" in caplog.text
    assert next(rotations) == 100
    assert not any("| s0" in p.read_text() for p in tmp_path.iterdir() if p.is_file())


@pytest.mark.asyncio
async def test_listen_durable_passes_stable_identifier_and_clean_session():
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

    with patch("swarmbus.bus.aiomqtt.Client", _ClientStub):
        bus = AgentBus(agent_id="sparrow", persistent=True)
        await bus.listen()
    assert captured_kwargs.get("identifier") == "swarmbus-sparrow"
    assert captured_kwargs.get("clean_session") is False


@pytest.mark.asyncio
async def test_listen_non_durable_omits_identifier():
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

    with patch("swarmbus.bus.aiomqtt.Client", _ClientStub):
        bus = AgentBus(agent_id="sparrow", persistent=False)
        await bus.listen()
    assert "identifier" not in captured_kwargs
    assert "clean_session" not in captured_kwargs


@pytest.mark.asyncio
async def test_send_outbox_agent_id_template_substitutes(tmp_path):
    """`{agent_id}` in outbox_path → replaced with the bus's agent_id."""
    template = str(tmp_path / "{agent_id}-outbox.md")
    with patch("swarmbus.bus.aiomqtt.Client", return_value=_NullPublishClient()):
        bus_s = AgentBus(agent_id="sparrow")
        await bus_s.send(to="wren", subject="s", body="b1", outbox_path=template)
        bus_w = AgentBus(agent_id="wren")
        await bus_w.send(to="sparrow", subject="w", body="b2", outbox_path=template)
    assert (tmp_path / "sparrow-outbox.md").read_text().startswith("\n## ")
    assert (tmp_path / "wren-outbox.md").read_text().startswith("\n## ")
    assert "b1" in (tmp_path / "sparrow-outbox.md").read_text()
    assert "b2" in (tmp_path / "wren-outbox.md").read_text()
    assert "b2" not in (tmp_path / "sparrow-outbox.md").read_text()


# --------------------------------------------------------------------------
# read_inbox / watch_inbox persistent session kwargs
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_read_inbox_durable_passes_identifier_and_clean_session():
    """persistent=True → stable client-id + clean_session=False on read_inbox."""
    captured_kwargs = {}
    payloads = [_envelope(subject="queued")]

    class _CapturingClient(_FakeClient):
        def __init__(self, *args, **kwargs):
            captured_kwargs.update(kwargs)
            super().__init__(payloads)

    with patch("swarmbus.bus.aiomqtt.Client", _CapturingClient):
        bus = AgentBus(agent_id="sparrow", persistent=True)
        result = await bus.read_inbox(drain_timeout=0.1)
    assert captured_kwargs.get("identifier") == "swarmbus-sparrow"
    assert captured_kwargs.get("clean_session") is False
    assert len(result) == 1


@pytest.mark.asyncio
async def test_read_inbox_non_durable_omits_identifier():
    """persistent=False (default) → no identifier or clean_session set."""
    captured_kwargs = {}

    class _CapturingClient(_FakeClient):
        def __init__(self, *args, **kwargs):
            captured_kwargs.update(kwargs)
            super().__init__([])

    with patch("swarmbus.bus.aiomqtt.Client", _CapturingClient):
        bus = AgentBus(agent_id="sparrow", persistent=False)
        await bus.read_inbox(drain_timeout=0.1)
    assert "identifier" not in captured_kwargs
    assert "clean_session" not in captured_kwargs


@pytest.mark.asyncio
async def test_watch_inbox_durable_passes_identifier_and_clean_session():
    """persistent=True → stable client-id + clean_session=False on watch_inbox."""
    captured_kwargs = {}
    payloads = [_envelope(subject="live")]

    class _CapturingClient(_FakeClient):
        def __init__(self, *args, **kwargs):
            captured_kwargs.update(kwargs)
            super().__init__(payloads)

    with patch("swarmbus.bus.aiomqtt.Client", _CapturingClient):
        bus = AgentBus(agent_id="sparrow", persistent=True)
        result = await bus.watch_inbox(timeout=0.1)
    assert captured_kwargs.get("identifier") == "swarmbus-sparrow"
    assert captured_kwargs.get("clean_session") is False
    assert result is not None


@pytest.mark.asyncio
async def test_watch_inbox_non_durable_omits_identifier():
    """persistent=False (default) → no identifier or clean_session set."""
    captured_kwargs = {}

    class _CapturingClient(_FakeClient):
        def __init__(self, *args, **kwargs):
            captured_kwargs.update(kwargs)
            super().__init__([])

    with patch("swarmbus.bus.aiomqtt.Client", _CapturingClient):
        bus = AgentBus(agent_id="sparrow", persistent=False)
        await bus.watch_inbox(timeout=0.1)
    assert "identifier" not in captured_kwargs
    assert "clean_session" not in captured_kwargs


@pytest.mark.asyncio
async def test_watch_inbox_persistent_acks_only_the_returned_message():
    """persistent=True → manual ack on before connect; only the returned mid is acked."""
    fake = _FakeClient([_envelope(subject="first"), _envelope(subject="second")])
    with patch("swarmbus.bus.aiomqtt.Client", return_value=fake):
        bus = AgentBus(agent_id="sparrow", persistent=True)
        result = await bus.watch_inbox(timeout=0.2)
    assert result is not None
    assert result["subject"] == "first"
    assert fake._client.acks == [(1, 1)]
    assert fake._client.manual_ack is True
    assert fake.events.index(("manual_ack_set", True)) < fake.events.index(("enter",))


@pytest.mark.asyncio
async def test_watch_inbox_persistent_acks_skipped_bad_envelope():
    """A skipped malformed payload is acked too, in order, before the returned one."""
    fake = _FakeClient([b"garbage", _envelope(subject="good")])
    with patch("swarmbus.bus.aiomqtt.Client", return_value=fake):
        bus = AgentBus(agent_id="sparrow", persistent=True)
        result = await bus.watch_inbox(timeout=0.2)
    assert result is not None
    assert result["subject"] == "good"
    assert fake._client.acks == [(1, 1), (2, 1)]


@pytest.mark.asyncio
async def test_watch_inbox_non_persistent_never_touches_manual_ack():
    """persistent=False → manual ack stays untouched and nothing is acked."""
    fake = _FakeClient([b"garbage", _envelope(subject="good")])
    with patch("swarmbus.bus.aiomqtt.Client", return_value=fake):
        bus = AgentBus(agent_id="sparrow", persistent=False)
        result = await bus.watch_inbox(timeout=0.2)
    assert result is not None
    assert result["subject"] == "good"
    assert fake._client.manual_ack is None
    assert fake._client.acks == []

@pytest.mark.asyncio
async def test_persistent_watch_publishes_named_registry_and_presence():
    captured_kwargs = {}
    fake = _FakeClient([_envelope(subject="live")])

    def client(*args, **kwargs):
        captured_kwargs.update(kwargs)
        return fake

    with patch("swarmbus.bus.aiomqtt.Client", client):
        result = await AgentBus(
            agent_id="sparrow",
            persistent=True,
        ).watch_inbox(timeout=0.1)

    assert result is not None
    will = captured_kwargs["will"]
    assert str(will.topic) == DEFAULT_TOPICS.presence("sparrow")
    assert json.loads(will.payload)["state"] == "offline"

    publishes = [event for event in fake.events if event[0] == "publish"]
    assert [event[1] for event in publishes] == [
        DEFAULT_TOPICS.registry("sparrow"),
        DEFAULT_TOPICS.presence("sparrow"),
        DEFAULT_TOPICS.presence("sparrow"),
    ]
    registry = json.loads(publishes[0][2])
    assert registry["lifecycle"] == "named"
    assert registry["durability"] == "durable"
    assert json.loads(publishes[1][2])["state"] == "online"
    assert json.loads(publishes[2][2])["state"] == "offline"


def _topic_client(payloads):
    class _Msg:
        def __init__(self, payload):
            self.topic = "agents/me/inbox"
            self.payload = payload

    class _Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            pass

        async def subscribe(self, *_args, **_kwargs):
            pass

        @property
        def messages(self):
            async def _gen():
                for p in payloads:
                    yield _Msg(p)
            return _gen()

    return _Client()


@pytest.mark.asyncio
async def test_read_inbox_skips_held_messages_and_records_admitted_ones(tmp_path):
    from swarmbus.archive import SQLiteMessageStore

    store = SQLiteMessageStore(tmp_path / "me.sqlite3", managed=False, hop_limit=4)
    bus = AgentBus(agent_id="me", broker="localhost", hold_store=store)
    past = AgentMessage.create(from_="wren", to="me", subject="past", body="b", hops=5)
    tripped = AgentMessage.create(from_="wren", to="me", subject="tripped", body="b")
    ok = AgentMessage.create(from_="robin", to="me", subject="ok", body="b", hops=2, origin="planner")
    payloads = [m.to_json().encode() for m in (past, tripped, ok)]

    with patch("swarmbus.bus.aiomqtt.Client", return_value=_topic_client(payloads)):
        result = await bus.read_inbox(max_messages=1, drain_timeout=30)

    assert [m["subject"] for m in result] == ["ok"]
    holds = await store.list_holds()
    assert [t["sender"] for t in holds["tripped"]] == ["wren"]
    assert holds["held_count"] == 2
    assert await store.lookup(ok.id) == {"from": "robin", "hops": 2, "origin": "planner"}

    empty = SQLiteMessageStore(tmp_path / "empty" / "me.sqlite3", managed=False)
    bus = AgentBus(agent_id="me", broker="localhost", hold_store=empty)
    with patch("swarmbus.bus.aiomqtt.Client", return_value=_topic_client([])):
        assert await bus.read_inbox(drain_timeout=0.1) == []
    assert (tmp_path / "empty" / "me.sqlite3").exists()

