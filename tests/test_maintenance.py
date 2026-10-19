import asyncio
import inspect
import json
from datetime import datetime, timedelta, timezone
from typing import get_args
from unittest.mock import AsyncMock, patch

import aiomqtt
import pytest
from click.testing import CliRunner

from swarmbus.bus import _legacy_presence_payload
from swarmbus.cli import main
from swarmbus.maintenance import (
    OrphanPresenceEntry,
    RegistryForgetReport,
    RegistryGCReport,
    RegistryListReport,
    RegistryMaintenance,
)
from swarmbus.registry import PresenceRecord, RegistryRecord
from swarmbus.topics import DEFAULT_TOPICS, TopicMap


class _Message:
    def __init__(self, topic, payload):
        self.topic = topic
        self.payload = payload.encode() if isinstance(payload, str) else payload


class _FakeClient:
    def __init__(self, messages):
        self._source = list(messages)
        self.subscribe = AsyncMock()
        self.publish = AsyncMock()

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return None

    @property
    def messages(self):
        async def stream():
            for message in self._source:
                yield message
            await asyncio.Event().wait()

        return stream()


class _ClientFactory:
    def __init__(self, message_batches, publish_side_effects=None):
        self._message_batches = list(message_batches)
        self._publish_side_effects = publish_side_effects or {}
        self.clients = []
        self.calls = []

    def __call__(self, hostname, **kwargs):
        self.calls.append((hostname, kwargs))
        messages = self._message_batches.pop(0)
        client = _FakeClient(messages)
        index = len(self.clients)
        if index in self._publish_side_effects:
            client.publish.side_effect = self._publish_side_effects[index]
        self.clients.append(client)
        return client


def _registry(
    agent_id,
    *,
    now,
    age_seconds,
    lifecycle="transient",
    durability="ephemeral",
):
    return RegistryRecord(
        agent_id=agent_id,
        lifecycle=lifecycle,
        durability=durability,
        started_at=now - timedelta(hours=2),
        last_seen=now - timedelta(seconds=age_seconds),
    )


def _presence(agent_id, state, *, first_seen_offline=None):
    return PresenceRecord(
        agent_id=agent_id,
        state=state,
        first_seen_offline=first_seen_offline,
    )


def _message(record):
    if isinstance(record, RegistryRecord):
        return _Message(
            DEFAULT_TOPICS.registry(record.agent_id),
            record.to_json(),
        )
    return _Message(
        DEFAULT_TOPICS.presence(record.agent_id),
        record.to_json(),
    )


def _assert_nothing_published(factory):
    """Assert that no client published during the operation."""

    for index, client in enumerate(factory.clients):
        assert client.publish.await_count == 0, (
            f"client {index} published during an operation that must not "
            f"write: {client.publish.await_args_list}"
        )


def _presence_writes(factory, agent_id):
    """Payloads published to one agent's presence topic, in order."""

    topic = DEFAULT_TOPICS.presence(agent_id)
    return [
        call.args[1]
        for client in factory.clients
        for call in client.publish.await_args_list
        if call.args[0] == topic
    ]


def _assert_gc_identities(report):
    """Assert the GC report counter identities."""

    assert report.scanned == report.retained + report.eligible, (
        f"scanned {report.scanned} != retained {report.retained} + "
        f"eligible {report.eligible}"
    )
    assert report.selected == (
        report.deleted + report.would_delete + report.skipped
    ), (
        f"selected {report.selected} != deleted {report.deleted} + "
        f"would_delete {report.would_delete} + skipped {report.skipped}"
    )
    orphan_dispositions = (
        report.orphan_online
        + report.orphan_stamped
        + report.orphan_waiting
        + report.orphan_collected
        + report.orphan_would_stamp
        + report.orphan_would_collect
        + report.orphan_skipped
        + report.orphan_deferred
    )
    assert report.orphan_presence == orphan_dispositions, (
        f"orphan_presence {report.orphan_presence} != the sum of the "
        f"per-orphan dispositions {orphan_dispositions}"
    )


async def _gc_once(factory, *, now):
    """One default-bounds GC run, for tests that compare two runs."""
    maintenance = RegistryMaintenance(
        client_factory=factory,
        snapshot_seconds=0.001,
    )
    return await maintenance.gc_transient(
        now=now,
        stale_after_seconds=60,
        retention_seconds=300,
        batch_size=10,
    )


@pytest.mark.asyncio
async def test_registry_list_joins_registry_and_presence_without_writes():
    now = datetime(2026, 10, 1, tzinfo=timezone.utc)
    online = _registry(
        "online",
        now=now,
        age_seconds=30,
        lifecycle="named",
    )
    offline = _registry("offline", now=now, age_seconds=300)
    unknown = _registry("unknown", now=now, age_seconds=300)
    factory = _ClientFactory(
        [[
            _message(unknown),
            _message(_presence("orphan", "offline")),
            _message(offline),
            _message(_presence("offline", "offline")),
            _message(online),
            _message(_presence("online", "online")),
        ]]
    )
    maintenance = RegistryMaintenance(
        client_factory=factory,
        snapshot_seconds=0.001,
    )

    report = await maintenance.list_registry(
        now=now,
        stale_after_seconds=60,
    )

    assert report.scanned == 3
    assert report.orphan_presence == 1
    assert report.orphan_presence_agent_ids == ["orphan"]
    assert [identity.agent_id for identity in report.identities] == [
        "offline",
        "online",
        "unknown",
    ]
    assert [identity.online for identity in report.identities] == [
        False,
        True,
        None,
    ]
    assert [
        identity.presence_evidence for identity in report.identities
    ] == ["offline", "online", "unconfirmed"]
    assert report.identities[1].lifecycle == "named"
    _assert_nothing_published(factory)


@pytest.mark.asyncio
async def test_registry_list_surfaces_orphan_stamps_and_waiting_age():
    """An orphan appears only as an orphan, so its wait has to be reported
    outside `identities` -- it has no registry record to carry it."""
    now = datetime(2026, 10, 1, tzinfo=timezone.utc)
    stamp = now - timedelta(seconds=450)
    identity = _registry("worker", now=now, age_seconds=30)
    factory = _ClientFactory(
        [[
            _message(identity),
            _message(_presence("worker", "online")),
            _message(
                _presence("stamped", "offline", first_seen_offline=stamp)
            ),
            _message(_presence("unstamped", "offline")),
            _message(_presence("live", "online")),
        ]]
    )
    maintenance = RegistryMaintenance(
        client_factory=factory,
        snapshot_seconds=0.001,
    )

    report = await maintenance.list_registry(
        now=now,
        stale_after_seconds=60,
    )

    orphans = {orphan.agent_id: orphan for orphan in report.orphans}
    assert set(orphans) == {"stamped", "unstamped", "live"}
    # The operator question is how long this one has been waiting.
    assert orphans["stamped"].first_seen_offline == stamp
    assert orphans["stamped"].offline_age_seconds == (
        now - stamp
    ).total_seconds()
    assert orphans["unstamped"].first_seen_offline is None
    assert orphans["unstamped"].offline_age_seconds is None
    assert orphans["live"].presence_state == "online"
    assert orphans["live"].first_seen_offline is None
    # Synthesizing a registry record for an orphan would make the listing
    # claim an identity that does not exist.
    assert [entry.agent_id for entry in report.identities] == ["worker"]
    assert report.orphan_presence == 3
    assert report.orphan_presence_agent_ids == sorted(orphans)
    _assert_nothing_published(factory)


@pytest.mark.asyncio
async def test_gc_preserves_unknown_durability_with_explicit_reason():
    now = datetime(2026, 10, 1, tzinfo=timezone.utc)
    record = _registry(
        "legacy",
        now=now,
        age_seconds=900,
        durability="unknown",
    )
    factory = _ClientFactory([
        [_message(record), _message(_presence("legacy", "offline"))]
    ])
    maintenance = RegistryMaintenance(
        client_factory=factory,
        snapshot_seconds=0.001,
    )

    report = await maintenance.gc_transient(
        now=now,
        stale_after_seconds=60,
        retention_seconds=300,
        batch_size=1,
    )

    assert report.eligible == 0
    assert report.retained == 1
    assert report.preserved_by_reason == {"unknown-durability": 1}
    _assert_nothing_published(factory)


@pytest.mark.asyncio
async def test_gc_deletes_oldest_eligible_transient_in_bounded_batch():
    now = datetime(2026, 10, 1, tzinfo=timezone.utc)
    old = _registry("old", now=now, age_seconds=900)
    older = _registry("older", now=now, age_seconds=1200)
    persistent = _registry(
        "keeper",
        now=now,
        age_seconds=5000,
        lifecycle="named",
    )
    fresh = _registry("fresh", now=now, age_seconds=30)
    factory = _ClientFactory(
        [
            [
                _message(old),
                _message(_presence("old", "offline")),
                _message(older),
                _message(_presence("older", "offline")),
                _message(persistent),
                _message(fresh),
                _message(_presence("fresh", "offline")),
            ],
            [_message(older), _message(_presence("older", "offline"))],
        ]
    )
    audit_events = []
    maintenance = RegistryMaintenance(
        client_factory=factory,
        snapshot_seconds=0.001,
        audit_sink=audit_events.append,
    )

    report = await maintenance.gc_transient(
        now=now,
        stale_after_seconds=60,
        retention_seconds=300,
        batch_size=1,
    )

    assert report.scanned == 4
    assert report.eligible == 2
    assert report.selected == 1
    assert report.deleted == 1
    assert report.deleted_agent_ids == ["older"]
    assert report.failed == 0
    assert report.skipped == 0
    assert report.retained == 2
    _assert_gc_identities(report)
    assert audit_events[0]["operation"] == "transient-gc"
    assert audit_events[0]["agent_id"] == "older"
    assert audit_events[0]["outcome"] == "authorized"
    assert audit_events[0]["presence_state"] == "offline"
    assert audit_events[0]["online"] is False
    assert report.preserved_by_reason == {
        "named": 1,
        "within-retention": 1,
    }
    client = factory.clients[1]
    assert [call.args for call in client.publish.await_args_list] == [
        (DEFAULT_TOPICS.presence("older"), b""),
        (DEFAULT_TOPICS.registry("older"), b""),
    ]
    assert all(call.kwargs == {"qos": 1, "retain": True}
               for call in client.publish.await_args_list)


@pytest.mark.asyncio
async def test_gc_fresh_reread_preserves_agent_that_resumed():
    now = datetime(2026, 10, 1, tzinfo=timezone.utc)
    stale = _registry("worker", now=now, age_seconds=900)
    resumed = _registry("worker", now=now, age_seconds=5)
    factory = _ClientFactory(
        [
            [_message(stale), _message(_presence("worker", "offline"))],
            [_message(resumed), _message(_presence("worker", "online"))],
        ]
    )
    maintenance = RegistryMaintenance(
        client_factory=factory,
        snapshot_seconds=0.001,
    )

    report = await maintenance.gc_transient(
        now=now,
        stale_after_seconds=60,
        retention_seconds=300,
        batch_size=10,
    )

    assert report.eligible == 1
    assert report.deleted == 0
    assert report.raced == 1
    assert report.skipped == 1
    _assert_gc_identities(report)
    assert factory.clients[1].publish.await_count == 0


@pytest.mark.asyncio
async def test_gc_dry_run_rechecks_but_never_publishes():
    now = datetime(2026, 10, 1, tzinfo=timezone.utc)
    stale = _registry("worker", now=now, age_seconds=900)
    factory = _ClientFactory(
        [
            [_message(stale), _message(_presence("worker", "offline"))],
            [_message(stale), _message(_presence("worker", "offline"))],
        ]
    )
    maintenance = RegistryMaintenance(
        client_factory=factory,
        snapshot_seconds=0.001,
    )

    report = await maintenance.gc_transient(
        now=now,
        stale_after_seconds=60,
        retention_seconds=300,
        batch_size=10,
        dry_run=True,
    )

    assert report.would_delete == 1
    assert report.would_delete_agent_ids == ["worker"]
    assert report.deleted == 0
    assert report.skipped == 0
    _assert_gc_identities(report)
    assert factory.clients[1].publish.await_count == 0


@pytest.mark.parametrize("dry_run", [False, True])
@pytest.mark.asyncio
async def test_gc_never_touches_a_stale_online_identity(dry_run):
    now = datetime(2026, 10, 1, tzinfo=timezone.utc)
    stale = _registry("worker", now=now, age_seconds=900)
    stale.durability = "durable"
    factory = _ClientFactory(
        [[_message(stale), _message(_presence("worker", "online"))]]
    )
    audit_events = []
    maintenance = RegistryMaintenance(
        client_factory=factory,
        snapshot_seconds=0.001,
        audit_sink=audit_events.append,
    )

    report = await maintenance.gc_transient(
        now=now,
        stale_after_seconds=60,
        retention_seconds=300,
        batch_size=10,
        dry_run=dry_run,
    )

    assert report.eligible == 0
    assert report.selected == 0
    assert report.deleted == 0
    assert report.would_delete == 0
    assert report.sessions_destroyed == 0
    assert report.preserved_by_reason == {"unconfirmed": 1}
    assert audit_events == []
    assert len(factory.clients) == 1
    assert all(
        kwargs.get("identifier") != "Worker/client:2"
        for _, kwargs in factory.calls
    )
    _assert_nothing_published(factory)
    _assert_gc_identities(report)


@pytest.mark.asyncio
async def test_gc_partial_failure_leaves_registry_for_next_repair():
    now = datetime(2026, 10, 1, tzinfo=timezone.utc)
    stale = _registry("worker", now=now, age_seconds=900)
    failure = aiomqtt.MqttError("registry tombstone failed")
    factory = _ClientFactory(
        [
            [_message(stale), _message(_presence("worker", "offline"))],
            [_message(stale), _message(_presence("worker", "offline"))],
        ],
        publish_side_effects={1: [None, failure]},
    )
    maintenance = RegistryMaintenance(
        client_factory=factory,
        snapshot_seconds=0.001,
    )

    report = await maintenance.gc_transient(
        now=now,
        stale_after_seconds=60,
        retention_seconds=300,
        batch_size=10,
    )

    assert report.deleted == 0
    assert report.failed == 1
    assert report.partial == 1
    assert report.selected == 1
    assert report.skipped == 1
    _assert_gc_identities(report)
    assert [call.args for call in factory.clients[1].publish.await_args_list] == [
        (DEFAULT_TOPICS.presence("worker"), b""),
        (DEFAULT_TOPICS.registry("worker"), b""),
    ]


@pytest.mark.asyncio
async def test_gc_audit_failure_preserves_retained_state():
    now = datetime(2026, 10, 1, tzinfo=timezone.utc)
    stale = _registry("worker", now=now, age_seconds=900)
    factory = _ClientFactory(
        [
            [_message(stale), _message(_presence("worker", "offline"))],
            [_message(stale), _message(_presence("worker", "offline"))],
        ]
    )

    def broken_audit(_event):
        raise OSError("audit unavailable")

    maintenance = RegistryMaintenance(
        client_factory=factory,
        snapshot_seconds=0.001,
        audit_sink=broken_audit,
    )

    report = await maintenance.gc_transient(
        now=now,
        stale_after_seconds=60,
        retention_seconds=300,
        batch_size=10,
    )

    assert report.failed == 1
    assert report.deleted == 0
    assert report.skipped == 1
    _assert_gc_identities(report)
    assert factory.clients[1].publish.await_count == 0


@pytest.mark.asyncio
async def test_gc_counts_malformed_records_and_fails_closed():
    factory = _ClientFactory(
        [[_Message(DEFAULT_TOPICS.registry("worker"), b"{not-json")]]
    )
    maintenance = RegistryMaintenance(
        client_factory=factory,
        snapshot_seconds=0.001,
    )

    report = await maintenance.gc_transient(
        stale_after_seconds=60,
        retention_seconds=300,
        batch_size=10,
    )

    assert report.malformed == 1
    assert report.scanned == 0
    assert report.deleted == 0
    _assert_gc_identities(report)


@pytest.mark.asyncio
async def test_gc_counts_presence_without_registry_record():
    """Presence-only agents are outside the registry scan, so they must be
    reported or they accumulate invisibly. Reported and aged on their own
    pass, which leaves the registry pass unchanged."""
    now = datetime(2026, 10, 1, tzinfo=timezone.utc)
    stale = _registry("worker", now=now, age_seconds=900)
    factory = _ClientFactory(
        [
            [
                _message(stale),
                _message(_presence("worker", "offline")),
                _message(_presence("legacy-offline", "offline")),
                _message(_presence("legacy-online", "online")),
            ],
            [_message(stale), _message(_presence("worker", "offline"))],
            [_message(_presence("legacy-offline", "offline"))],
            # Spare: fixture exhaustion would fail this test before any
            # assertion runs.
            [],
        ]
    )
    maintenance = RegistryMaintenance(
        client_factory=factory,
        snapshot_seconds=0.001,
    )

    report = await maintenance.gc_transient(
        now=now,
        stale_after_seconds=60,
        retention_seconds=300,
        batch_size=10,
    )

    assert report.orphan_presence == 2
    assert report.orphan_online == 1
    assert report.orphan_stamped == 1
    assert report.scanned == 1
    assert report.deleted_agent_ids == ["worker"]
    _assert_gc_identities(report)
    assert [call.args for call in factory.clients[1].publish.await_args_list] == [
        (DEFAULT_TOPICS.presence("worker"), b""),
        (DEFAULT_TOPICS.registry("worker"), b""),
    ]
    assert _presence_writes(factory, "legacy-online") == []


@pytest.mark.asyncio
async def test_gc_stamps_presence_only_orphans_on_first_sighting():
    """An orphan has no registry last_seen to age against, so the first
    sighting stamps the retained payload instead of collecting it."""
    now = datetime(2026, 10, 1, tzinfo=timezone.utc)
    factory = _ClientFactory(
        [
            [
                _message(_presence("legacy-one", "offline")),
                _message(_presence("legacy-two", "offline")),
            ],
            [_message(_presence("legacy-one", "offline"))],
            [_message(_presence("legacy-two", "offline"))],
            [],
        ]
    )
    maintenance = RegistryMaintenance(
        client_factory=factory,
        snapshot_seconds=0.001,
    )

    report = await maintenance.gc_transient(
        now=now,
        stale_after_seconds=60,
        retention_seconds=300,
        batch_size=10,
    )

    assert report.orphan_presence == 2
    assert report.orphan_stamped == 2
    assert report.orphan_stamped_agent_ids == ["legacy-one", "legacy-two"]
    assert report.orphan_collected == 0
    assert report.orphan_waiting == 0
    assert report.orphan_failed == 0
    assert report.scanned == 0
    assert report.eligible == 0
    assert report.selected == 0
    assert report.deleted == 0
    assert report.would_delete == 0
    assert report.preserved_by_reason == {}
    _assert_gc_identities(report)
    for agent_id in ("legacy-one", "legacy-two"):
        topic = DEFAULT_TOPICS.presence(agent_id)
        written = _presence_writes(factory, agent_id)
        assert len(written) == 1, written
        assert written[0] not in (b"", ""), "orphan tombstoned, not stamped"
        stamped = PresenceRecord.from_mqtt(topic, written[0])
        assert stamped.first_seen_offline == now
        assert stamped.state == "offline"


@pytest.mark.asyncio
async def test_gc_never_restamps_an_orphan_and_collects_it_past_retention():
    """The stamp is written once and only aged afterwards.

    Rewriting it on a later run would reset the clock every pass, so the
    record's age could never exceed one GC interval and it would never be
    collectable -- worse than leaving it alone, and invisible without the
    orphan counters.
    """
    now = datetime(2026, 10, 1, tzinfo=timezone.utc)
    stamp = now - timedelta(seconds=100)
    stamped = _presence("legacy", "offline", first_seen_offline=stamp)
    # Spare batches repeat the record so a run that wrongly decides to
    # stamp fails on the write assertions, not on fixture exhaustion.
    waiting_factory = _ClientFactory([[_message(stamped)]] * 3)
    collecting_factory = _ClientFactory([[_message(stamped)]] * 3)

    waiting = await _gc_once(waiting_factory, now=now)
    collected = await _gc_once(
        collecting_factory,
        now=stamp + timedelta(seconds=301),
    )

    assert waiting.orphan_waiting == 1
    assert waiting.orphan_stamped == 0
    assert waiting.orphan_collected == 0
    # No write at all leaves the stamp on the broker exactly as it was.
    _assert_nothing_published(waiting_factory)
    _assert_gc_identities(waiting)

    assert collected.orphan_collected == 1
    assert collected.orphan_collected_agent_ids == ["legacy"]
    assert collected.orphan_stamped == 0
    assert collected.orphan_waiting == 0
    # The tombstone is the only write the record ever receives.
    assert _presence_writes(collecting_factory, "legacy") == [b""]
    _assert_gc_identities(collected)


@pytest.mark.asyncio
async def test_gc_never_writes_to_an_orphan_that_says_online():
    """An online presence record is live state, whatever the registry says."""
    now = datetime(2026, 10, 1, tzinfo=timezone.utc)
    factory = _ClientFactory([[_message(_presence("live", "online"))]] * 3)

    report = await _gc_once(factory, now=now)

    assert report.orphan_presence == 1
    assert report.orphan_online == 1
    assert report.orphan_stamped == 0
    assert report.orphan_collected == 0
    _assert_nothing_published(factory)
    _assert_gc_identities(report)


@pytest.mark.asyncio
async def test_gc_abandons_an_orphan_stamp_when_the_reread_says_online():
    """An agent can reconnect between the scan and the write, so the write
    is decided on a fresh read: otherwise a stale offline payload would
    land on top of a live record."""
    now = datetime(2026, 10, 1, tzinfo=timezone.utc)
    factory = _ClientFactory(
        [
            [_message(_presence("waking", "offline"))],
            [_message(_presence("waking", "online"))],
            [],
        ]
    )

    report = await _gc_once(factory, now=now)

    assert report.orphan_online == 1
    assert report.orphan_stamped == 0
    assert report.orphan_stamped_agent_ids == []
    _assert_nothing_published(factory)
    _assert_gc_identities(report)


@pytest.mark.asyncio
async def test_gc_orphan_stamp_preserves_a_legacy_payload_as_observed():
    """A legacy payload has no reason key and different field names.

    The stamp is added to the payload that is retained; rebuilding one from
    the current schema would rewrite the record while claiming only to add
    a timestamp.
    """
    now = datetime(2026, 10, 1, tzinfo=timezone.utc)
    topic = DEFAULT_TOPICS.presence("legacy")
    original = _legacy_presence_payload("legacy", "offline")
    factory = _ClientFactory([[_Message(topic, original)]] * 3)

    report = await _gc_once(factory, now=now)

    assert report.orphan_stamped == 1
    written = _presence_writes(factory, "legacy")
    assert len(written) == 1, written
    payload = json.loads(written[0])
    observed = json.loads(original)
    assert {key: payload[key] for key in observed} == observed
    assert set(payload) == set(observed) | {"first_seen_offline"}
    # The stamp has to survive being read back, or it is rewritten on
    # every run.
    assert PresenceRecord.from_mqtt(topic, written[0]).first_seen_offline == now


@pytest.mark.asyncio
async def test_gc_orphan_counters_separate_stamped_from_waiting():
    """Stamped-this-run and already-waiting are different populations: the
    first rising while the second never drains is the re-stamp failure."""
    now = datetime(2026, 10, 1, tzinfo=timezone.utc)
    fresh = _presence("fresh-orphan", "offline")
    waiting = _presence(
        "waiting-orphan",
        "offline",
        first_seen_offline=now - timedelta(seconds=30),
    )
    live = _presence("live-orphan", "online")
    factory = _ClientFactory(
        [
            [_message(fresh), _message(waiting), _message(live)],
            [_message(fresh)],
            [],
        ]
    )

    report = await _gc_once(factory, now=now)

    assert report.orphan_presence == 3
    assert report.orphan_stamped == 1
    assert report.orphan_stamped_agent_ids == ["fresh-orphan"]
    assert report.orphan_waiting == 1
    assert report.orphan_online == 1
    assert report.orphan_collected == 0
    assert report.orphan_deferred == 0
    _assert_gc_identities(report)
    assert _presence_writes(factory, "waiting-orphan") == []
    assert _presence_writes(factory, "live-orphan") == []


@pytest.mark.asyncio
async def test_gc_dry_run_never_stamps_or_collects_an_orphan():
    now = datetime(2026, 10, 1, tzinfo=timezone.utc)
    unstamped = _presence("fresh-orphan", "offline")
    expired = _presence(
        "old-orphan",
        "offline",
        first_seen_offline=now - timedelta(seconds=900),
    )
    factory = _ClientFactory(
        [
            [_message(unstamped), _message(expired)],
            [_message(unstamped)],
            [_message(expired)],
            [],
        ]
    )
    maintenance = RegistryMaintenance(
        client_factory=factory,
        snapshot_seconds=0.001,
    )

    report = await maintenance.gc_transient(
        now=now,
        stale_after_seconds=60,
        retention_seconds=300,
        batch_size=10,
        dry_run=True,
    )

    assert report.orphan_would_stamp == 1
    assert report.orphan_would_collect == 1
    assert report.orphan_stamped == 0
    assert report.orphan_collected == 0
    _assert_nothing_published(factory)
    _assert_gc_identities(report)


@pytest.mark.asyncio
async def test_gc_orphan_batch_bound_defers_the_rest():
    """Orphan writes are bounded per run like registry deletions are."""
    now = datetime(2026, 10, 1, tzinfo=timezone.utc)
    factory = _ClientFactory(
        [
            [
                _message(_presence("orphan-one", "offline")),
                _message(_presence("orphan-two", "offline")),
            ],
            [_message(_presence("orphan-one", "offline"))],
            [],
        ]
    )
    maintenance = RegistryMaintenance(
        client_factory=factory,
        snapshot_seconds=0.001,
    )

    report = await maintenance.gc_transient(
        now=now,
        stale_after_seconds=60,
        retention_seconds=300,
        batch_size=1,
    )

    assert report.orphan_presence == 2
    assert report.orphan_stamped == 1
    assert report.orphan_stamped_agent_ids == ["orphan-one"]
    assert report.orphan_deferred == 1
    _assert_gc_identities(report)
    assert _presence_writes(factory, "orphan-two") == []


@pytest.mark.asyncio
async def test_gc_orphan_write_failure_is_recorded_and_batch_continues():
    """One orphan's transport fault must not abandon the others: the
    failure is recorded and the sweep carries on to the next."""
    now = datetime(2026, 10, 1, tzinfo=timezone.utc)
    one = _presence("orphan-one", "offline")
    two = _presence("orphan-two", "offline")
    factory = _ClientFactory(
        [
            [_message(one), _message(two)],
            [_message(one)],
            [_message(two)],
            [],
        ],
        # Not an MqttError: a socket, DNS or TLS fault can surface raw.
        publish_side_effects={1: [OSError("connection reset by peer")]},
    )
    maintenance = RegistryMaintenance(
        client_factory=factory,
        snapshot_seconds=0.001,
    )

    report = await maintenance.gc_transient(
        now=now,
        stale_after_seconds=60,
        retention_seconds=300,
        batch_size=10,
    )

    assert report.orphan_presence == 2
    assert report.orphan_failed == 1
    assert report.orphan_skipped == 1
    assert report.orphan_deferred == 0, "the rest of the batch was abandoned"
    assert report.orphan_stamped == 1
    assert report.orphan_stamped_agent_ids == ["orphan-two"]
    _assert_gc_identities(report)
    assert factory.clients[1].publish.await_count == 1
    written = _presence_writes(factory, "orphan-two")
    assert len(written) == 1, written
    assert (
        PresenceRecord.from_mqtt(
            DEFAULT_TOPICS.presence("orphan-two"),
            written[0],
        ).first_seen_offline
        == now
    )


@pytest.mark.asyncio
async def test_gc_orphan_write_records_a_publish_timeout():
    """asyncio.TimeoutError is only an OSError subclass from 3.11 on, so a
    timed-out write has to be caught by name to stay recoverable."""
    now = datetime(2026, 10, 1, tzinfo=timezone.utc)
    orphan = _presence("orphan", "offline")
    factory = _ClientFactory(
        [[_message(orphan)]] * 3,
        publish_side_effects={1: [asyncio.TimeoutError()]},
    )

    report = await _gc_once(factory, now=now)

    assert report.orphan_failed == 1
    assert report.orphan_skipped == 1
    assert report.orphan_stamped == 0
    assert report.orphan_stamped_agent_ids == []
    _assert_gc_identities(report)


@pytest.mark.asyncio
async def test_gc_orphan_write_lets_cancellation_propagate():
    """Cancellation is not a transport fault. Recording it as one would
    keep a cancelled sweep writing to the broker."""
    now = datetime(2026, 10, 1, tzinfo=timezone.utc)
    orphan = _presence("orphan", "offline")
    factory = _ClientFactory(
        [[_message(orphan)]] * 3,
        publish_side_effects={1: [asyncio.CancelledError()]},
    )

    with pytest.raises(asyncio.CancelledError):
        await _gc_once(factory, now=now)


@pytest.mark.asyncio
async def test_gc_counter_identities_hold_across_a_mixed_batch():
    """Exercise all GC disposition counters together."""
    now = datetime(2026, 10, 1, tzinfo=timezone.utc)
    keeper = _registry(
        "keeper",
        now=now,
        age_seconds=5000,
        lifecycle="named",
    )
    fresh = _registry("fresh", now=now, age_seconds=30)
    oldest = _registry("oldest", now=now, age_seconds=1200)
    middle = _registry("middle", now=now, age_seconds=900)
    spare = _registry("spare", now=now, age_seconds=600)
    factory = _ClientFactory(
        [
            [
                _message(keeper),
                _message(fresh),
                _message(_presence("fresh", "offline")),
                _message(oldest),
                _message(_presence("oldest", "offline")),
                _message(middle),
                _message(_presence("middle", "offline")),
                _message(spare),
                _message(_presence("spare", "offline")),
            ],
            [],
            [_message(middle), _message(_presence("middle", "offline"))],
        ]
    )
    maintenance = RegistryMaintenance(
        client_factory=factory,
        snapshot_seconds=0.001,
    )

    report = await maintenance.gc_transient(
        now=now,
        stale_after_seconds=60,
        retention_seconds=300,
        batch_size=2,
    )

    assert report.scanned == 5
    assert report.retained == 2
    assert report.eligible == 3
    # Batch truncation is not an act-phase skip.
    assert report.selected == 2
    assert report.disappeared == 1
    assert report.skipped == 1
    assert report.deleted_agent_ids == ["middle"]
    assert report.failed == 0
    _assert_gc_identities(report)


@pytest.mark.asyncio
async def test_gc_skips_record_tombstoned_between_scan_and_delete():
    """A re-read tombstone marks the selected record as disappeared."""
    now = datetime(2026, 10, 1, tzinfo=timezone.utc)
    stale = _registry("worker", now=now, age_seconds=900)
    factory = _ClientFactory(
        [
            [_message(stale), _message(_presence("worker", "offline"))],
            [_Message(DEFAULT_TOPICS.registry("worker"), b"")],
        ]
    )
    maintenance = RegistryMaintenance(
        client_factory=factory,
        snapshot_seconds=0.001,
    )

    report = await maintenance.gc_transient(
        now=now,
        stale_after_seconds=60,
        retention_seconds=300,
        batch_size=10,
    )

    assert report.disappeared == 1
    assert report.skipped == 1
    assert report.deleted == 0
    assert report.failed == 0
    assert report.malformed == 0
    _assert_gc_identities(report)
    assert factory.clients[1].publish.await_count == 0


@pytest.mark.asyncio
async def test_snapshot_registry_tombstone_deletes_the_record_it_read():
    """A registry tombstone removes the record during snapshot ingest."""
    now = datetime(2026, 10, 1, tzinfo=timezone.utc)
    stale = _registry("worker", now=now, age_seconds=900)
    offline = _presence("worker", "offline")
    control = _ClientFactory(
        [
            [_message(stale), _message(offline)],
            [_message(stale), _message(offline)],
        ]
    )
    tombstoned = _ClientFactory(
        [
            [
                _message(stale),
                _message(offline),
                _Message(DEFAULT_TOPICS.registry("worker"), b""),
            ],
            # Presence outliving its registry record is an orphan, so the
            # orphan pass re-reads it.
            [_message(offline)],
            [],
        ]
    )

    control_report = await _gc_once(control, now=now)
    report = await _gc_once(tombstoned, now=now)

    assert control_report.scanned == 1
    assert control_report.deleted == 1
    assert control_report.orphan_presence == 0
    assert report.scanned == 0
    assert report.eligible == 0
    assert report.malformed == 0
    assert report.orphan_presence == 1
    assert report.orphan_stamped == 1
    _assert_gc_identities(report)
    assert tombstoned.clients[0].publish.await_count == 0


@pytest.mark.asyncio
async def test_snapshot_presence_tombstone_drops_the_online_observation():
    """A presence tombstone removes the online observation."""
    now = datetime(2026, 10, 1, tzinfo=timezone.utc)
    recent = _registry("worker", now=now, age_seconds=10)
    online = _message(_presence("worker", "online"))
    control = _ClientFactory([[_message(recent), online]])
    tombstoned = _ClientFactory(
        [
            [
                _message(recent),
                online,
                _Message(DEFAULT_TOPICS.presence("worker"), b""),
            ]
        ]
    )

    control_report = await _gc_once(control, now=now)
    report = await _gc_once(tombstoned, now=now)

    assert control_report.online == 1
    assert control_report.preserved_by_reason == {"online": 1}
    assert report.online == 0
    assert report.preserved_by_reason == {"unconfirmed": 1}
    assert report.scanned == 1
    assert report.malformed == 0
    _assert_gc_identities(report)


@pytest.mark.asyncio
async def test_snapshot_counts_tombstones_on_unparseable_topics():
    """An unattributable tombstone is counted as malformed."""
    factory = _ClientFactory(
        [
            [
                _Message("swarmbus/registry/nested/worker", b""),
                _Message("agents/nested/worker/presence", b""),
            ]
        ]
    )
    maintenance = RegistryMaintenance(
        client_factory=factory,
        snapshot_seconds=0.001,
    )

    report = await maintenance.gc_transient(
        stale_after_seconds=60,
        retention_seconds=300,
        batch_size=10,
    )

    assert report.malformed == 2
    assert report.scanned == 0
    assert report.orphan_presence == 0
    assert report.deleted == 0
    _assert_gc_identities(report)


@pytest.mark.asyncio
async def test_snapshot_counts_topic_in_neither_tree_as_invalid():
    """Unclassified topics do not abort snapshot ingest."""
    now = datetime(2026, 10, 1, tzinfo=timezone.utc)
    keeper = _registry(
        "keeper",
        now=now,
        age_seconds=5000,
        lifecycle="named",
    )
    factory = _ClientFactory(
        [
            [
                _Message(DEFAULT_TOPICS.inbox("keeper"), b'{"not": "a record"}'),
                _message(keeper),
            ]
        ]
    )

    report = await _gc_once(factory, now=now)

    assert report.malformed == 1
    assert report.scanned == 1
    assert report.retained == 1
    _assert_gc_identities(report)
    _assert_nothing_published(factory)


@pytest.mark.asyncio
async def test_snapshot_counts_rooted_map_reading_unrooted_topics():
    """A non-default root reaches the unclassified-topic path."""
    now = datetime(2026, 10, 1, tzinfo=timezone.utc)
    stale = _registry("worker", now=now, age_seconds=900)
    factory = _ClientFactory([[_message(stale)]])
    maintenance = RegistryMaintenance(
        client_factory=factory,
        snapshot_seconds=0.001,
        topics=TopicMap(root="fleet"),
    )

    report = await maintenance.gc_transient(
        now=now,
        stale_after_seconds=60,
        retention_seconds=300,
        batch_size=10,
    )

    assert report.malformed == 1
    assert report.scanned == 0
    assert report.orphan_presence == 0
    assert report.deleted == 0
    _assert_gc_identities(report)
    _assert_nothing_published(factory)


@pytest.mark.asyncio
async def test_forget_tombstones_state_and_destroys_durable_session():
    """The happy path: presence positively observed offline, twice."""
    now = datetime(2026, 10, 1, tzinfo=timezone.utc)
    record = _registry(
        "abandoned-session",
        now=now,
        age_seconds=900,
        lifecycle="named",
    )
    record = RegistryRecord.model_validate(
        {**record.model_dump(), "mqtt_client_id": "another-session"}
    )
    offline = _presence(record.agent_id, "offline")
    factory = _ClientFactory(
        [
            [_message(record), _message(offline)],
            [_message(record), _message(offline)],
            [],
        ]
    )
    audit_events = []
    maintenance = RegistryMaintenance(
        client_factory=factory,
        snapshot_seconds=0.001,
        audit_sink=audit_events.append,
    )

    result = await maintenance.forget(
        "abandoned-session",
        now=now,
        stale_after_seconds=60,
    )

    assert result.deleted is True
    assert result.session_destroyed is True
    assert result.online is False
    assert result.presence_evidence == "offline"
    assert result.rechecked is True
    assert result.refused is False
    assert result.refused_reason is None
    assert result.already_absent is False
    assert audit_events[0]["operation"] == "explicit-forget"
    assert audit_events[0]["lifecycle"] == "named"
    assert audit_events[0]["online"] is False
    # The anonymous re-read precedes the destructive clean-session client.
    assert len(factory.calls) == 3
    assert "identifier" not in factory.calls[1][1]
    assert factory.calls[2][1]["identifier"] == "swarmbus-abandoned-session"
    assert factory.calls[2][1]["clean_session"] is True
    assert factory.clients[0].publish.await_count == 0
    assert factory.clients[1].publish.await_count == 0
    assert [call.args for call in factory.clients[2].publish.await_args_list] == [
        (DEFAULT_TOPICS.presence("abandoned-session"), b""),
        (DEFAULT_TOPICS.registry("abandoned-session"), b""),
    ]


@pytest.mark.asyncio
async def test_forget_refuses_online_identity_without_override():
    now = datetime(2026, 10, 1, tzinfo=timezone.utc)
    record = _registry(
        "active-worker",
        now=now,
        age_seconds=5,
        lifecycle="named",
    )
    factory = _ClientFactory(
        [[_message(record), _message(_presence(record.agent_id, "online"))]]
    )
    maintenance = RegistryMaintenance(
        client_factory=factory,
        snapshot_seconds=0.001,
    )

    result = await maintenance.forget(
        "active-worker",
        now=now,
        stale_after_seconds=60,
    )

    assert result.refused is True
    assert result.refused_reason == "online"
    assert result.online is True
    assert result.presence_evidence == "online"
    assert result.rechecked is False
    assert len(factory.calls) == 1
    _assert_nothing_published(factory)


@pytest.mark.asyncio
async def test_forget_refuses_when_presence_was_never_observed():
    """Missing presence is unknown and fails the offline interlock."""
    now = datetime(2026, 10, 1, tzinfo=timezone.utc)
    record = _registry(
        "live-worker",
        now=now,
        age_seconds=5,
        lifecycle="named",
    )
    factory = _ClientFactory([[_message(record)]])
    audit_events = []
    maintenance = RegistryMaintenance(
        client_factory=factory,
        snapshot_seconds=0.001,
        audit_sink=audit_events.append,
    )

    result = await maintenance.forget(
        "live-worker",
        now=now,
        stale_after_seconds=60,
    )

    assert result.refused is True
    assert result.refused_reason == "unconfirmed-offline"
    assert result.online is None
    assert result.presence_evidence == "unconfirmed"
    assert result.rechecked is False
    assert len(factory.calls) == 1
    assert audit_events == []
    _assert_nothing_published(factory)


@pytest.mark.asyncio
async def test_forget_reports_already_absent_after_two_empty_snapshots():
    factory = _ClientFactory([[], []])
    audit_events = []
    maintenance = RegistryMaintenance(
        client_factory=factory,
        snapshot_seconds=0.001,
        audit_sink=audit_events.append,
    )

    result = await maintenance.forget("ghost-session")

    assert result.already_absent is True
    assert result.lifecycle == "unknown"
    assert result.online is None
    assert result.rechecked is True
    assert result.refused is False
    assert result.deleted is False
    assert result.session_destroyed is False
    assert audit_events == []
    assert len(factory.calls) == 2
    assert all("identifier" not in kwargs for _, kwargs in factory.calls)
    _assert_nothing_published(factory)


@pytest.mark.parametrize(
    ("presence_state", "age_seconds", "expected_reason"),
    [
        ("offline", 900, "unconfirmed-offline"),
        ("online", 5, "online"),
    ],
)
@pytest.mark.asyncio
async def test_forget_refuses_state_that_appears_after_empty_snapshot(
    presence_state,
    age_seconds,
    expected_reason,
):
    now = datetime(2026, 10, 1, tzinfo=timezone.utc)
    record = _registry(
        "starting-session",
        now=now,
        age_seconds=age_seconds,
        lifecycle="named",
    )
    presence = _presence(record.agent_id, presence_state)
    factory = _ClientFactory([[], [_message(record), _message(presence)]])
    maintenance = RegistryMaintenance(
        client_factory=factory,
        snapshot_seconds=0.001,
    )

    result = await maintenance.forget(
        record.agent_id,
        now=now,
        stale_after_seconds=60,
    )

    assert result.refused is True
    assert result.refused_reason == expected_reason
    assert result.already_absent is False
    assert result.online is (presence_state == "online")
    assert result.deleted is False
    assert result.session_destroyed is False
    assert len(factory.calls) == 2
    _assert_nothing_published(factory)


@pytest.mark.asyncio
async def test_forget_refuses_malformed_evidence_after_empty_snapshot():
    agent_id = "malformed-session"
    malformed = _Message(DEFAULT_TOPICS.registry(agent_id), b"{")
    factory = _ClientFactory([[], [malformed]])
    maintenance = RegistryMaintenance(
        client_factory=factory,
        snapshot_seconds=0.001,
    )

    result = await maintenance.forget(agent_id)

    assert result.refused is True
    assert result.refused_reason == "unconfirmed-offline"
    assert result.already_absent is False
    assert result.deleted is False
    assert len(factory.calls) == 2
    _assert_nothing_published(factory)


@pytest.mark.asyncio
async def test_forget_proceeds_on_unknown_presence_with_force_online():
    """force_online is the operator accepting an unprovable eviction."""
    now = datetime(2026, 10, 1, tzinfo=timezone.utc)
    record = _registry(
        "stuck-session",
        now=now,
        age_seconds=5,
        lifecycle="named",
    )
    factory = _ClientFactory([[_message(record)], [_message(record)], []])
    audit_events = []
    maintenance = RegistryMaintenance(
        client_factory=factory,
        snapshot_seconds=0.001,
        audit_sink=audit_events.append,
    )

    result = await maintenance.forget(
        "stuck-session",
        now=now,
        force_online=True,
        stale_after_seconds=60,
    )

    assert result.deleted is True
    assert result.session_destroyed is True
    assert result.refused is False
    assert result.rechecked is True
    assert result.online is None
    assert audit_events[0]["online"] is None
    assert audit_events[0]["presence_state"] == "unknown"
    assert audit_events[0]["force_online"] is True
    assert [call.args for call in factory.clients[2].publish.await_args_list] == [
        (DEFAULT_TOPICS.presence("stuck-session"), b""),
        (DEFAULT_TOPICS.registry("stuck-session"), b""),
    ]


@pytest.mark.asyncio
async def test_forget_evicts_observed_online_identity_with_force_online():
    """force_online permits eviction of an observed live identity."""
    now = datetime(2026, 10, 1, tzinfo=timezone.utc)
    record = _registry(
        "runaway-worker",
        now=now,
        age_seconds=5,
        lifecycle="named",
    )
    online = _presence(record.agent_id, "online")
    factory = _ClientFactory(
        [
            [_message(record), _message(online)],
            [_message(record), _message(online)],
            [],
        ]
    )
    audit_events = []
    maintenance = RegistryMaintenance(
        client_factory=factory,
        snapshot_seconds=0.001,
        audit_sink=audit_events.append,
    )

    result = await maintenance.forget(
        "runaway-worker",
        now=now,
        force_online=True,
        stale_after_seconds=60,
    )

    assert result.deleted is True
    assert result.session_destroyed is True
    assert result.refused is False
    assert result.refused_reason is None
    assert result.online is True
    assert audit_events[0]["online"] is True
    assert audit_events[0]["presence_state"] == "online"
    assert audit_events[0]["force_online"] is True
    assert factory.calls[2][1]["identifier"] == "swarmbus-runaway-worker"
    assert factory.calls[2][1]["clean_session"] is True
    assert [call.args for call in factory.clients[2].publish.await_args_list] == [
        (DEFAULT_TOPICS.presence("runaway-worker"), b""),
        (DEFAULT_TOPICS.registry("runaway-worker"), b""),
    ]


@pytest.mark.asyncio
async def test_forget_force_online_does_not_impersonate_absent_identity():
    factory = _ClientFactory([[], []])
    audit_events = []
    maintenance = RegistryMaintenance(
        client_factory=factory,
        snapshot_seconds=0.001,
        audit_sink=audit_events.append,
    )

    result = await maintenance.forget("ghost-session", force_online=True)

    assert result.already_absent is True
    assert result.lifecycle == "unknown"
    assert result.online is None
    assert result.deleted is False
    assert result.session_destroyed is False
    assert audit_events == []
    assert len(factory.calls) == 2
    assert all("identifier" not in kwargs for _, kwargs in factory.calls)
    _assert_nothing_published(factory)


@pytest.mark.asyncio
async def test_forget_audit_failure_preserves_retained_state():
    """Audit before publish, same settled policy as gc_transient: no
    provenance, no delete, and no destructive client either."""
    now = datetime(2026, 10, 1, tzinfo=timezone.utc)
    record = _registry(
        "worker",
        now=now,
        age_seconds=900,
        lifecycle="named",
    )
    offline = _presence("worker", "offline")
    factory = _ClientFactory(
        [
            [_message(record), _message(offline)],
            [_message(record), _message(offline)],
        ]
    )

    def broken_audit(_event):
        raise OSError("audit unavailable")

    maintenance = RegistryMaintenance(
        client_factory=factory,
        snapshot_seconds=0.001,
        audit_sink=broken_audit,
    )

    result = await maintenance.forget(
        "worker",
        now=now,
        stale_after_seconds=60,
    )

    assert result.failed is True
    assert result.deleted is False
    assert result.session_destroyed is False
    assert result.partial is False
    assert result.refused is False
    # Audit failure stops before constructing the destructive client.
    assert len(factory.calls) == 2
    _assert_nothing_published(factory)


@pytest.mark.asyncio
async def test_forget_partial_failure_reports_presence_already_gone():
    """Report a partial delete when only the presence tombstone lands."""
    now = datetime(2026, 10, 1, tzinfo=timezone.utc)
    record = _registry(
        "worker",
        now=now,
        age_seconds=900,
        lifecycle="named",
    )
    offline = _presence("worker", "offline")
    failure = aiomqtt.MqttError("registry tombstone failed")
    factory = _ClientFactory(
        [
            [_message(record), _message(offline)],
            [_message(record), _message(offline)],
            [],
        ],
        publish_side_effects={2: [None, failure]},
    )
    maintenance = RegistryMaintenance(
        client_factory=factory,
        snapshot_seconds=0.001,
    )

    result = await maintenance.forget(
        "worker",
        now=now,
        stale_after_seconds=60,
    )

    assert result.failed is True
    assert result.partial is True
    assert result.deleted is False
    assert result.session_destroyed is False
    assert [call.args for call in factory.clients[2].publish.await_args_list] == [
        (DEFAULT_TOPICS.presence("worker"), b""),
        (DEFAULT_TOPICS.registry("worker"), b""),
    ]


@pytest.mark.asyncio
async def test_forget_presence_failure_reports_no_partial_delete():
    """A first-tombstone failure is not a partial delete."""
    now = datetime(2026, 10, 1, tzinfo=timezone.utc)
    record = _registry(
        "worker",
        now=now,
        age_seconds=900,
        lifecycle="named",
    )
    offline = _presence("worker", "offline")
    failure = aiomqtt.MqttError("presence tombstone failed")
    factory = _ClientFactory(
        [
            [_message(record), _message(offline)],
            [_message(record), _message(offline)],
            [],
        ],
        publish_side_effects={2: [failure]},
    )
    maintenance = RegistryMaintenance(
        client_factory=factory,
        snapshot_seconds=0.001,
    )

    result = await maintenance.forget(
        "worker",
        now=now,
        stale_after_seconds=60,
    )

    assert result.failed is True
    assert result.partial is False
    assert result.deleted is False
    assert result.session_destroyed is False
    assert [call.args for call in factory.clients[2].publish.await_args_list] == [
        (DEFAULT_TOPICS.presence("worker"), b""),
    ]


@pytest.mark.asyncio
async def test_forget_aborts_when_reread_shows_agent_back_online():
    """A re-read that finds the agent online aborts deletion."""
    now = datetime(2026, 10, 1, tzinfo=timezone.utc)
    record = _registry(
        "resumed-worker",
        now=now,
        age_seconds=5,
        lifecycle="named",
    )
    factory = _ClientFactory(
        [
            [_message(record), _message(_presence(record.agent_id, "offline"))],
            [_message(record), _message(_presence(record.agent_id, "online"))],
            [],
        ]
    )
    audit_events = []
    maintenance = RegistryMaintenance(
        client_factory=factory,
        snapshot_seconds=0.001,
        audit_sink=audit_events.append,
    )

    result = await maintenance.forget(
        "resumed-worker",
        now=now,
        stale_after_seconds=60,
    )

    assert result.refused is True
    assert result.refused_reason == "online"
    assert result.online is True
    assert result.rechecked is True
    assert result.deleted is False
    assert result.session_destroyed is False
    assert result.failed is False
    assert audit_events == []
    assert len(factory.calls) == 2
    _assert_nothing_published(factory)


@pytest.mark.asyncio
async def test_forget_aborts_when_reread_loses_presence_evidence():
    """Evidence that evaporates between reads blocks, same as online."""
    now = datetime(2026, 10, 1, tzinfo=timezone.utc)
    record = _registry(
        "flaky-worker",
        now=now,
        age_seconds=900,
        lifecycle="named",
    )
    factory = _ClientFactory(
        [
            [_message(record), _message(_presence(record.agent_id, "offline"))],
            [_message(record)],
            [],
        ]
    )
    audit_events = []
    maintenance = RegistryMaintenance(
        client_factory=factory,
        snapshot_seconds=0.001,
        audit_sink=audit_events.append,
    )

    result = await maintenance.forget(
        "flaky-worker",
        now=now,
        stale_after_seconds=60,
    )

    assert result.refused is True
    assert result.refused_reason == "unconfirmed-offline"
    assert result.online is None
    assert result.rechecked is True
    assert result.deleted is False
    assert result.session_destroyed is False
    assert audit_events == []
    assert len(factory.calls) == 2
    _assert_nothing_published(factory)


@pytest.mark.asyncio
async def test_forget_dry_run_rechecks_but_never_publishes():
    """A rehearsal takes the same two reads a real run would."""
    now = datetime(2026, 10, 1, tzinfo=timezone.utc)
    record = _registry("worker", now=now, age_seconds=900)
    offline = _presence("worker", "offline")
    factory = _ClientFactory(
        [
            [_message(record), _message(offline)],
            [_message(record), _message(offline)],
            [],
        ]
    )
    audit_events = []
    maintenance = RegistryMaintenance(
        client_factory=factory,
        snapshot_seconds=0.001,
        audit_sink=audit_events.append,
    )

    result = await maintenance.forget(
        "worker",
        now=now,
        dry_run=True,
        stale_after_seconds=60,
    )

    assert result.dry_run is True
    assert result.rechecked is True
    assert result.online is False
    assert result.refused is False
    assert result.deleted is False
    assert result.session_destroyed is False
    assert audit_events == []
    assert len(factory.calls) == 2
    _assert_nothing_published(factory)


@pytest.mark.asyncio
async def test_forget_rejects_reserved_agent_id_without_connecting():
    factory = _ClientFactory([])
    maintenance = RegistryMaintenance(client_factory=factory)

    with pytest.raises(ValueError, match="reserved"):
        await maintenance.forget("broadcast")

    assert factory.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "override, match",
    [
        ({"batch_size": 0}, "batch_size must be positive"),
        ({"stale_after_seconds": 0}, "stale_after_seconds must be positive"),
        ({"retention_seconds": 60}, "retention_seconds must be longer"),
        ({"now": datetime(2026, 10, 1)}, "now must be timezone-aware"),
    ],
)
async def test_gc_rejects_bad_bounds_before_connecting(override, match):
    """Bad bounds are a caller bug, so they must not reach the broker."""
    factory = _ClientFactory([])
    maintenance = RegistryMaintenance(
        client_factory=factory,
        snapshot_seconds=0.001,
    )
    kwargs = {
        "now": datetime(2026, 10, 1, tzinfo=timezone.utc),
        "stale_after_seconds": 60,
        "retention_seconds": 300,
        "batch_size": 10,
        **override,
    }

    with pytest.raises(ValueError, match=match):
        await maintenance.gc_transient(**kwargs)

    assert factory.calls == []


@pytest.mark.asyncio
async def test_gc_durable_transient_tombstones_on_its_own_client_id():
    """GC retires persistent identities using their stable client ID."""
    now = datetime(2026, 10, 1, tzinfo=timezone.utc)
    rec = _registry("ghost", now=now, age_seconds=1200)
    rec.durability = "durable"
    rec = RegistryRecord.model_validate(
        {**rec.model_dump(), "mqtt_client_id": "another-session"}
    )
    factory = _ClientFactory(
        [
            [
                _message(rec),
                _message(_presence("ghost", "offline")),
            ],                                                  # scan
            [_message(rec), _message(_presence("ghost", "offline"))],
            [],                                                # per-agent
        ]
    )
    maintenance = RegistryMaintenance(
        client_factory=factory,
        snapshot_seconds=0.001,
        audit_sink=[].append,
    )

    report = await maintenance.gc_transient(
        now=now,
        stale_after_seconds=60,
        retention_seconds=300,
        batch_size=1,
    )

    assert report.deleted == 1
    assert report.sessions_destroyed == 1
    assert len(factory.clients) == 3
    hostname, kwargs = factory.calls[2]
    assert kwargs["identifier"] == "swarmbus-ghost"
    assert kwargs["clean_session"] is True
    assert [c.args for c in factory.clients[2].publish.await_args_list] == [
        (DEFAULT_TOPICS.presence("ghost"), b""),
        (DEFAULT_TOPICS.registry("ghost"), b""),
    ]
    _assert_gc_identities(report)


@pytest.mark.asyncio
async def test_gc_keeps_a_durable_session_when_presence_is_unconfirmed():
    """Absent presence is unconfirmed, not offline: clear records, keep the
    session. A live agent whose presence missed the snapshot window is
    indistinguishable from a retired one, and session destruction discards
    queued messages irreversibly."""
    now = datetime(2026, 10, 1, tzinfo=timezone.utc)
    rec = _registry("ghost", now=now, age_seconds=1200)
    rec.durability = "durable"
    factory = _ClientFactory(
        [
            [_message(rec)],   # scan
            [_message(rec)],   # per-agent recheck, no presence retained
            # Spare: without it, dropping the guard fails on fixture
            # exhaustion rather than on the assertions below.
            [],
        ]
    )
    maintenance = RegistryMaintenance(
        client_factory=factory,
        snapshot_seconds=0.001,
        audit_sink=[].append,
    )

    report = await maintenance.gc_transient(
        now=now,
        stale_after_seconds=60,
        retention_seconds=300,
        batch_size=1,
    )

    assert report.deleted == 0
    assert report.eligible == 0
    assert report.sessions_destroyed == 0
    assert report.sessions_preserved == 0
    assert report.preserved_by_reason == {"unconfirmed": 1}
    assert len(factory.clients) == 1
    assert all(
        "identifier" not in kwargs or kwargs["identifier"] != "swarmbus-ghost"
        for _, kwargs in factory.calls
    )
    _assert_nothing_published(factory)
    _assert_gc_identities(report)


@pytest.mark.asyncio
async def test_gc_ephemeral_transient_stays_on_the_anonymous_client():
    """GC keeps ephemeral identities on the shared client."""
    now = datetime(2026, 10, 1, tzinfo=timezone.utc)
    rec = _registry("plain", now=now, age_seconds=1200)
    rec.durability = "ephemeral"
    factory = _ClientFactory(
        [
            [_message(rec), _message(_presence("plain", "offline"))],
            [_message(rec), _message(_presence("plain", "offline"))],
        ]
    )
    maintenance = RegistryMaintenance(
        client_factory=factory,
        snapshot_seconds=0.001,
        audit_sink=[].append,
    )

    report = await maintenance.gc_transient(
        now=now,
        stale_after_seconds=60,
        retention_seconds=300,
        batch_size=1,
    )

    assert report.deleted == 1
    assert report.sessions_destroyed == 0
    assert len(factory.clients) == 2
    _assert_gc_identities(report)


class _BrokerDiesMidBatch(_ClientFactory):
    """Raises MqttError when asked for the Nth client."""

    def __init__(self, message_batches, *, fail_at_client, error):
        super().__init__(message_batches)
        self._fail_at_client = fail_at_client
        self._error = error

    def __call__(self, hostname, **kwargs):
        if len(self.clients) == self._fail_at_client:
            raise self._error
        return super().__call__(hostname, **kwargs)


@pytest.mark.asyncio
async def test_gc_transport_failure_preserves_report_of_prior_deletions():
    """A broker failure preserves the partial batch report."""
    now = datetime(2026, 10, 1, tzinfo=timezone.utc)
    aaa = _registry("aaa", now=now, age_seconds=1200)
    bbb = _registry("bbb", now=now, age_seconds=1100)
    ccc = _registry("ccc", now=now, age_seconds=1000)
    factory = _BrokerDiesMidBatch(
        [
            [
                _message(aaa),
                _message(_presence("aaa", "offline")),
                _message(bbb),
                _message(_presence("bbb", "offline")),
                _message(ccc),
                _message(_presence("ccc", "offline")),
            ],
            [_message(aaa), _message(_presence("aaa", "offline"))],
        ],
        fail_at_client=2,
        error=aiomqtt.MqttError("broker went away"),
    )
    audit_events = []
    maintenance = RegistryMaintenance(
        client_factory=factory,
        snapshot_seconds=0.001,
        audit_sink=audit_events.append,
    )

    report = await maintenance.gc_transient(
        now=now,
        stale_after_seconds=60,
        retention_seconds=300,
        batch_size=3,
    )

    assert report.selected == 3
    assert report.deleted == 1
    assert report.deleted_agent_ids == ["aaa"]

    assert report.failed == 1
    assert report.failed_agent_ids == ["bbb"]

    assert report.skipped == 2

    assert len(factory.clients) == 2

    _assert_gc_identities(report)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "override, match",
    [
        ({"stale_after_seconds": 0}, "stale_after_seconds must be positive"),
        ({"now": datetime(2026, 10, 1)}, "now must be timezone-aware"),
    ],
)
async def test_forget_rejects_bad_bounds_before_connecting(override, match):
    """Same as gc: validate the call, then touch the broker."""
    factory = _ClientFactory([])
    maintenance = RegistryMaintenance(
        client_factory=factory,
        snapshot_seconds=0.001,
    )
    kwargs = {
        "now": datetime(2026, 10, 1, tzinfo=timezone.utc),
        "stale_after_seconds": 60,
        **override,
    }

    with pytest.raises(ValueError, match=match):
        await maintenance.forget("worker", **kwargs)

    assert factory.calls == []


def test_maintenance_rejects_non_positive_snapshot_window():
    """A zero-length window yields an empty snapshot, which reads as
    "nothing retained" -- the exact observation forget refuses on."""
    with pytest.raises(ValueError, match="snapshot_seconds must be positive"):
        RegistryMaintenance(snapshot_seconds=0)


def test_registry_list_cli_prints_full_directory():
    report = RegistryListReport(scanned=1)
    with patch("swarmbus.cli.RegistryMaintenance") as maintenance:
        maintenance.return_value.list_registry = AsyncMock(return_value=report)
        result = CliRunner().invoke(
            main,
            [
                "registry-list",
                "--stale-after-seconds",
                "60",
                "--snapshot-seconds",
                "1.5",
            ],
        )

    assert result.exit_code == 0, result.output
    assert '"scanned": 1' in result.output
    maintenance.return_value.list_registry.assert_awaited_once_with(
        stale_after_seconds=60.0,
    )
    assert maintenance.call_args.kwargs["snapshot_seconds"] == 1.5


def test_registry_list_cli_renders_orphan_stamps():
    """Reading the stamp off the MQTT topic by hand is the thing this
    replaces, so it has to reach the command's output."""
    stamp = datetime(2026, 10, 1, tzinfo=timezone.utc) - timedelta(
        seconds=450
    )
    report = RegistryListReport(
        scanned=0,
        orphan_presence=2,
        orphans=[
            OrphanPresenceEntry(
                agent_id="stamped",
                presence_state="offline",
                first_seen_offline=stamp,
                offline_age_seconds=450.0,
            ),
            OrphanPresenceEntry(
                agent_id="unstamped",
                presence_state="offline",
            ),
        ],
        orphan_presence_agent_ids=["stamped", "unstamped"],
    )
    with patch("swarmbus.cli.RegistryMaintenance") as maintenance:
        maintenance.return_value.list_registry = AsyncMock(return_value=report)
        result = CliRunner().invoke(main, ["registry-list"])

    assert result.exit_code == 0, result.output
    rendered = {
        orphan["agent_id"]: orphan
        for orphan in json.loads(result.output)["orphans"]
    }
    assert set(rendered) == {"stamped", "unstamped"}
    # Read back through the model rather than pattern-matched: what matters
    # is that the values survive, not how they are spelled.
    stamped = OrphanPresenceEntry.model_validate(rendered["stamped"])
    assert stamped.first_seen_offline == stamp
    assert stamped.offline_age_seconds == 450.0
    unstamped = OrphanPresenceEntry.model_validate(rendered["unstamped"])
    assert unstamped.first_seen_offline is None
    assert unstamped.offline_age_seconds is None


def test_registry_gc_cli_is_dry_run_by_default_and_threads_bounds():
    runner = CliRunner()
    report = RegistryGCReport(
        dry_run=True,
        scanned=2,
        eligible=1,
        selected=1,
        would_delete=1,
        would_delete_agent_ids=["worker"],
    )
    with patch("swarmbus.cli.RegistryMaintenance") as maintenance:
        maintenance.return_value.gc_transient = AsyncMock(return_value=report)
        result = runner.invoke(
            main,
            [
                "registry-gc",
                "--broker",
                "mqtt.example",
                "--stale-after-seconds",
                "60",
                "--retention-seconds",
                "600",
                "--batch-size",
                "5",
                "--snapshot-seconds",
                "0.25",
            ],
        )

    assert result.exit_code == 0, result.output
    assert '"dry_run": true' in result.output
    maintenance.assert_called_once_with(
        broker="mqtt.example",
        port=1883,
        username=None,
        password=None,
        tls=False,
        ca_cert=None,
        client_cert=None,
        client_key=None,
        snapshot_seconds=0.25,
    )
    maintenance.return_value.gc_transient.assert_awaited_once_with(
        stale_after_seconds=60.0,
        retention_seconds=600.0,
        batch_size=5,
        dry_run=True,
    )


def test_registry_gc_cli_reads_password_file(tmp_path):
    password_file = tmp_path / "mqtt-password"
    password_file.write_text("secret\n")
    report = RegistryGCReport(
        dry_run=True,
        scanned=0,
        eligible=0,
        selected=0,
    )

    with patch("swarmbus.cli.RegistryMaintenance") as maintenance:
        maintenance.return_value.gc_transient = AsyncMock(return_value=report)
        result = CliRunner().invoke(
            main,
            ["registry-gc", "--password-file", str(password_file)],
        )

    assert result.exit_code == 0, result.output
    assert maintenance.call_args.kwargs["password"] == "secret"


def test_registry_gc_cli_rejects_two_password_sources(tmp_path):
    password_file = tmp_path / "mqtt-password"
    password_file.write_text("secret\n")

    result = CliRunner().invoke(
        main,
        [
            "registry-gc",
            "--password",
            "inline",
            "--password-file",
            str(password_file),
        ],
    )

    assert result.exit_code == 2
    assert "mutually exclusive" in result.output


def test_registry_gc_cli_requires_explicit_delete_switch_to_mutate():
    runner = CliRunner()
    report = RegistryGCReport(
        dry_run=False,
        scanned=1,
        eligible=1,
        selected=1,
        deleted=1,
        deleted_agent_ids=["worker"],
    )
    with patch("swarmbus.cli.RegistryMaintenance") as maintenance:
        maintenance.return_value.gc_transient = AsyncMock(return_value=report)
        result = runner.invoke(main, ["registry-gc", "--delete"])

    assert result.exit_code == 0, result.output
    assert (
        maintenance.return_value.gc_transient.await_args.kwargs["dry_run"]
        is False
    )


def test_registry_forget_prompts_before_connecting():
    runner = CliRunner()
    with patch("swarmbus.cli.RegistryMaintenance") as maintenance:
        result = runner.invoke(
            main,
            ["registry-forget", "--agent-id", "old-session"],
            input="n\n",
        )

    assert result.exit_code == 1
    assert "Forget 'old-session'" in result.output
    maintenance.assert_not_called()


def test_registry_forget_confirmation_allows_mutation():
    report = RegistryForgetReport(
        agent_id="old-session",
        dry_run=False,
        deleted=True,
        session_destroyed=True,
    )
    with patch("swarmbus.cli.RegistryMaintenance") as maintenance:
        maintenance.return_value.forget = AsyncMock(return_value=report)
        result = CliRunner().invoke(
            main,
            ["registry-forget", "--agent-id", "old-session"],
            input="y\n",
        )

    assert result.exit_code == 0, result.output
    maintenance.return_value.forget.assert_awaited_once()


def test_registry_forget_tombstones_exact_agent_with_yes():
    runner = CliRunner()
    with patch("swarmbus.cli.RegistryMaintenance") as maintenance:
        maintenance.return_value.forget = AsyncMock(
            return_value=RegistryForgetReport(
                agent_id="old-session",
                deleted=True,
                session_destroyed=True,
                dry_run=False,
            )
        )
        result = runner.invoke(
            main,
            ["registry-forget", "--agent-id", "old-session", "--yes"],
        )

    assert result.exit_code == 0, result.output
    maintenance.return_value.forget.assert_awaited_once_with(
        "old-session",
        dry_run=False,
        force_online=False,
        stale_after_seconds=180,
    )


def _snapshot_seconds_default(command_name):
    """Read a command's --snapshot-seconds default off its click option."""
    option = next(
        param
        for param in main.commands[command_name].params
        if param.name == "snapshot_seconds"
    )
    return option.default


def _refusal_reasons():
    """Read the refusal vocabulary off the report model that declares it."""
    annotation = RegistryForgetReport.model_fields["refused_reason"].annotation
    return tuple(
        reason
        for member in get_args(annotation)
        for reason in get_args(member)
    )


def test_registry_forget_snapshot_default_mirrors_registry_gc():
    """GC and forget share the snapshot-window default."""
    forget_default = _snapshot_seconds_default("registry-forget")

    assert forget_default == _snapshot_seconds_default("registry-gc")
    assert forget_default == (
        inspect.signature(RegistryMaintenance.__init__)
        .parameters["snapshot_seconds"]
        .default
    )


def test_registry_forget_threads_snapshot_seconds_to_maintenance():
    """registry-forget passes its snapshot window to maintenance."""
    runner = CliRunner()
    report = RegistryForgetReport(
        agent_id="old-session",
        dry_run=True,
        online=False,
        rechecked=True,
    )
    with patch("swarmbus.cli.RegistryMaintenance") as maintenance:
        maintenance.return_value.forget = AsyncMock(return_value=report)
        explicit = runner.invoke(
            main,
            [
                "registry-forget",
                "--agent-id",
                "old-session",
                "--dry-run",
                "--snapshot-seconds",
                "2.5",
            ],
        )
        implicit = runner.invoke(
            main,
            ["registry-forget", "--agent-id", "old-session", "--dry-run"],
        )

    assert explicit.exit_code == 0, explicit.output
    assert implicit.exit_code == 0, implicit.output
    assert [
        call.kwargs["snapshot_seconds"]
        for call in maintenance.call_args_list
    ] == [2.5, _snapshot_seconds_default("registry-forget")]


@pytest.mark.parametrize("refused_reason", _refusal_reasons())
def test_registry_forget_refusal_exits_non_zero(refused_reason):
    """registry-forget exits non-zero for every refusal reason."""
    runner = CliRunner()
    report = RegistryForgetReport(
        agent_id="old-session",
        dry_run=False,
        rechecked=True,
        refused=True,
        refused_reason=refused_reason,
        online=True if refused_reason == "online" else None,
    )
    with patch("swarmbus.cli.RegistryMaintenance") as maintenance:
        maintenance.return_value.forget = AsyncMock(return_value=report)
        result = runner.invoke(
            main,
            ["registry-forget", "--agent-id", "old-session", "--yes"],
        )

    assert result.exit_code != 0
    assert refused_reason in result.output
    assert '"refused": true' in result.output
