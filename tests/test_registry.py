from datetime import datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from swarmbus.bus import _legacy_presence_payload
from swarmbus.registry import (
    GCDecisionReason,
    PresenceRecord,
    RegistryCache,
    RegistryRecord,
    transient_gc_decision,
)
from swarmbus.topics import DEFAULT_TOPICS


def _presence_payload(
    agent_id: str,
    *,
    state: str = "online",
    reason: str | None = None,
) -> str:
    """Presence payload as ManagedMCPRuntime._publish_presence emits it."""
    return PresenceRecord(
        agent_id=agent_id,
        state=state,
        connected_at=(
            datetime.now(timezone.utc) if state == "online" else None
        ),
        reason=reason,
    ).to_json()


def _record(agent_id: str, *, age_seconds: float = 0) -> RegistryRecord:
    now = datetime.now(timezone.utc)
    return RegistryRecord(
        agent_id=agent_id,
        status="building the weird little bus 🚌",
        working_set=[" repo ", "broker ACLs", "repo"],
        capabilities=["messaging", "agent-state"],
        durability="durable",
        started_at=now - timedelta(minutes=5),
        last_seen=now - timedelta(seconds=age_seconds),
    )


def test_status_is_freeform_and_working_set_is_opaque_strings():
    record = _record("foo")

    assert record.status == "building the weird little bus 🚌"
    assert record.working_set == ["repo", "broker ACLs"]


def test_status_limit_is_enforced():
    with pytest.raises(ValidationError, match="280"):
        RegistryRecord.model_validate(
            {**_record("foo").model_dump(), "status": "x" * 281}
        )

    # Known bypass: pydantic v2 model_copy does not run validators.
    oversized = _record("foo").model_copy(update={"status": "x" * 281})
    assert len(oversized.status) == 281


def test_working_set_rejects_empty_entries():
    with pytest.raises(ValidationError, match="empty"):
        RegistryRecord.model_validate(
            {**_record("foo").model_dump(), "working_set": ["repo", " "]}
        )


@pytest.mark.parametrize("field", ["started_at", "last_seen"])
def test_registry_rejects_naive_timestamps(field):
    payload = _record("foo").model_dump()
    payload[field] = datetime(2026, 10, 1, 12, 0, 0)

    with pytest.raises(ValidationError, match="timezone-aware"):
        RegistryRecord.model_validate(payload)


def test_registry_topic_must_match_payload_agent():
    payload = _record("foo").to_json()

    with pytest.raises(ValueError, match="does not match"):
        RegistryRecord.from_mqtt(DEFAULT_TOPICS.registry("wren"), payload)


def test_presence_topic_must_match_payload_agent():
    with pytest.raises(ValueError, match="does not match"):
        PresenceRecord.from_mqtt(
            DEFAULT_TOPICS.presence("wren"), _presence_payload("foo")
        )


def test_cache_combines_presence_and_heartbeat_freshness():
    cache = RegistryCache(stale_after_seconds=180)
    fresh = _record("foo", age_seconds=30)
    stale = _record("wren", age_seconds=181)

    cache.update_registry(DEFAULT_TOPICS.registry("foo"), fresh.to_json())
    cache.update_registry(DEFAULT_TOPICS.registry("wren"), stale.to_json())
    cache.update_presence(
        DEFAULT_TOPICS.presence("foo"), _presence_payload("foo")
    )
    cache.update_presence(
        DEFAULT_TOPICS.presence("wren"), _presence_payload("wren")
    )

    assert cache.online_agent_ids() == ["foo"]
    assert [state["agent_id"] for state in cache.list_states()] == ["foo"]
    assert [state["agent_id"] for state in cache.list_states(include_offline=True)] == [
        "foo",
        "wren",
    ]


def test_legacy_presence_only_agent_remains_discoverable():
    cache = RegistryCache(stale_after_seconds=180)
    cache.update_presence(
        DEFAULT_TOPICS.presence("legacy"),
        _legacy_presence_payload("legacy", "online"),
    )

    assert cache.online_agent_ids() == ["legacy"]
    state = cache.get_state("legacy")
    assert state["status"] == ""
    assert state["working_set"] == []
    assert state["online"] is True


def test_offline_reason_is_exposed():
    cache = RegistryCache(stale_after_seconds=180)
    record = _record("foo")
    cache.update_registry(DEFAULT_TOPICS.registry("foo"), record.to_json())
    cache.update_presence(
        DEFAULT_TOPICS.presence("foo"),
        _presence_payload("foo", state="offline", reason="clean-shutdown"),
    )

    assert cache.get_state("foo")["offline_reason"] == "clean-shutdown"


@pytest.mark.parametrize(
    ("state", "reason"), [("offline", None), ("online", "clean-shutdown")]
)
def test_offline_reason_is_absent_unless_offline_with_reason(state, reason):
    """Absence proof; test_offline_reason_is_exposed shows this same path
    emitting the key."""
    cache = RegistryCache(stale_after_seconds=180)
    cache.update_presence(
        DEFAULT_TOPICS.presence("foo"),
        _presence_payload("foo", state=state, reason=reason),
    )

    assert "offline_reason" not in cache.get_state("foo")


def test_get_state_raises_for_unknown_agent():
    with pytest.raises(KeyError):
        RegistryCache(stale_after_seconds=180).get_state("nobody")


@pytest.mark.parametrize("stale_after_seconds", [0, -1])
def test_cache_rejects_non_positive_stale_threshold(stale_after_seconds):
    with pytest.raises(ValueError, match="positive"):
        RegistryCache(stale_after_seconds=stale_after_seconds)


@pytest.mark.parametrize("remover", ["remove_registry", "remove_presence"])
def test_tombstone_removal_rejects_foreign_topics(remover):
    """Dropping the wrong agent's record is worse than a loud failure."""
    cache = RegistryCache(stale_after_seconds=180)

    with pytest.raises(ValueError):
        getattr(cache, remover)(DEFAULT_TOPICS.inbox("foo"))


def test_capabilities_are_normalized_but_remain_open_vocabulary():
    record = RegistryRecord.model_validate(
        {
            **_record("foo").model_dump(),
            "capabilities": [
                " development.files.write ",
                "example.facet.healthcheck",
                "development.files.write",
            ],
        }
    )

    assert record.capabilities == [
        "development.files.write",
        "example.facet.healthcheck",
    ]

    with pytest.raises(ValidationError, match="128"):
        RegistryRecord.model_validate(
            {
                **_record("foo").model_dump(),
                "capabilities": ["x" * 129],
            }
        )


def test_lifecycle_defaults_persistent_for_schema_v1_compatibility():
    payload = _record("foo").model_dump()
    payload.pop("lifecycle")

    record = RegistryRecord.model_validate(payload)

    assert record.lifecycle == "persistent"


def test_lifecycle_filter_partitions_the_directory():
    cache = RegistryCache(stale_after_seconds=180)
    persistent = _record("healthcheck")
    transient = RegistryRecord.model_validate(
        {**_record("transient-session").model_dump(), "lifecycle": "transient"}
    )
    for record in (persistent, transient):
        cache.update_registry(
            DEFAULT_TOPICS.registry(record.agent_id), record.to_json()
        )
        cache.update_presence(
            DEFAULT_TOPICS.presence(record.agent_id),
            _presence_payload(record.agent_id),
        )

    assert [state["agent_id"] for state in cache.list_states()] == [
        "healthcheck",
        "transient-session",
    ]
    assert [
        state["agent_id"]
        for state in cache.list_states(lifecycle="persistent")
    ] == ["healthcheck"]
    assert [
        state["agent_id"]
        for state in cache.list_states(lifecycle="transient")
    ] == ["transient-session"]


def test_lifecycle_filter_still_lists_offline_persistent_agents():
    cache = RegistryCache(stale_after_seconds=180)
    record = _record("healthcheck", age_seconds=181)
    cache.update_registry(
        DEFAULT_TOPICS.registry("healthcheck"), record.to_json()
    )
    cache.update_presence(
        DEFAULT_TOPICS.presence("healthcheck"), _presence_payload("healthcheck")
    )

    assert cache.list_states(lifecycle="persistent") == []
    assert [
        state["agent_id"]
        for state in cache.list_states(
            lifecycle="persistent", include_offline=True
        )
    ] == ["healthcheck"]


def test_retained_tombstones_remove_transient_state():
    cache = RegistryCache(stale_after_seconds=180)
    record = RegistryRecord.model_validate(
        {**_record("transient-session").model_dump(), "lifecycle": "transient"}
    )
    registry_topic = DEFAULT_TOPICS.registry("transient-session")
    presence_topic = DEFAULT_TOPICS.presence("transient-session")
    cache.update_registry(registry_topic, record.to_json())
    cache.update_presence(
        presence_topic, _presence_payload("transient-session")
    )

    assert [
        state["agent_id"]
        for state in cache.list_states(include_offline=True)
    ] == ["transient-session"]

    cache.remove_registry(registry_topic)
    cache.remove_presence(presence_topic)

    assert cache.list_states(include_offline=True) == []


def test_registry_reads_do_not_refresh_last_seen():
    cache = RegistryCache(stale_after_seconds=180)
    record = _record("transient-session", age_seconds=90)
    cache.update_registry(
        DEFAULT_TOPICS.registry("transient-session"), record.to_json()
    )
    cache.update_presence(
        DEFAULT_TOPICS.presence("transient-session"),
        _presence_payload("transient-session"),
    )

    first = cache.get_state("transient-session")
    cache.list_states(include_offline=True)
    second = cache.get_state("transient-session")

    assert first["last_seen"] == record.last_seen.isoformat()
    assert second["last_seen"] == record.last_seen.isoformat()


def test_transient_gc_never_selects_persistent_records():
    now = datetime(2026, 10, 2, tzinfo=timezone.utc)
    record = RegistryRecord.model_validate(
        {
            **_record("relay", age_seconds=0).model_dump(),
            "last_seen": now - timedelta(days=30),
            "lifecycle": "persistent",
        }
    )

    decision = transient_gc_decision(
        record=record,
        presence=PresenceRecord(
            agent_id="relay",
            state="offline",
            reason="connection-lost",
        ),
        now=now,
        stale_after_seconds=180,
        retention_seconds=86400,
    )

    assert decision.eligible is False
    assert decision.reason is GCDecisionReason.PERSISTENT


@pytest.mark.parametrize(
    ("age_seconds", "presence_state", "expected_reason"),
    [
        (30, "online", GCDecisionReason.ONLINE),
        # Boundary: the staleness check is <=, so 180 is still online.
        (180, "online", GCDecisionReason.ONLINE),
        (181, "online", GCDecisionReason.WITHIN_RETENTION),
        (86400, "offline", GCDecisionReason.WITHIN_RETENTION),
        (86401, "offline", GCDecisionReason.ELIGIBLE),
        (86401, "online", GCDecisionReason.ELIGIBLE),
    ],
)
def test_transient_gc_uses_owner_heartbeat_and_retention(
    age_seconds: float,
    presence_state: str,
    expected_reason: GCDecisionReason,
):
    now = datetime(2026, 10, 2, tzinfo=timezone.utc)
    record = RegistryRecord.model_validate(
        {
            **_record("transient-session", age_seconds=0).model_dump(),
            "last_seen": now - timedelta(seconds=age_seconds),
            "lifecycle": "transient",
        }
    )

    decision = transient_gc_decision(
        record=record,
        presence=PresenceRecord(
            agent_id="transient-session",
            state=presence_state,
        ),
        now=now,
        stale_after_seconds=180,
        retention_seconds=86400,
    )

    assert decision.eligible is (expected_reason is GCDecisionReason.ELIGIBLE)
    assert decision.reason is expected_reason
    assert decision.age_seconds == age_seconds


def test_transient_gc_preserves_future_heartbeat_on_clock_skew():
    now = datetime(2026, 10, 2, tzinfo=timezone.utc)
    record = RegistryRecord.model_validate(
        {
            **_record("transient-session", age_seconds=0).model_dump(),
            "last_seen": now + timedelta(seconds=1),
            "lifecycle": "transient",
        }
    )

    decision = transient_gc_decision(
        record=record,
        presence=PresenceRecord(
            agent_id="transient-session",
            state="offline",
        ),
        now=now,
        stale_after_seconds=180,
        retention_seconds=86400,
    )

    assert decision.eligible is False
    assert decision.reason is GCDecisionReason.CLOCK_SKEW


def test_transient_gc_requires_retention_longer_than_stale_threshold():
    now = datetime(2026, 10, 2, tzinfo=timezone.utc)

    with pytest.raises(ValueError, match="longer than stale"):
        transient_gc_decision(
            record=RegistryRecord.model_validate(
                {
                    **_record("transient-session").model_dump(),
                    "lifecycle": "transient",
                }
            ),
            presence=None,
            now=now,
            stale_after_seconds=180,
            retention_seconds=180,
        )
