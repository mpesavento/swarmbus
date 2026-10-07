from __future__ import annotations

import json
from datetime import datetime, timezone
from enum import Enum
from typing import Literal

from pydantic import BaseModel, Field, field_validator

from .message import _validate_agent_id
from .topics import DEFAULT_TOPICS, TopicMap

MAX_STATUS_LENGTH = 280
MAX_CAPABILITY_LENGTH = 128
AgentLifecycle = Literal["persistent", "transient"]


def _normalize_string_list(
    value: object,
    *,
    field_name: str,
    max_item_length: int | None = None,
) -> list[str]:
    if not isinstance(value, list):
        raise ValueError(f"{field_name} must be a list of strings")
    normalized: list[str] = []
    seen: set[str] = set()
    for item in value:
        if not isinstance(item, str):
            raise ValueError(f"{field_name} must contain only strings")
        item = item.strip()
        if not item:
            raise ValueError(f"{field_name} entries cannot be empty")
        if max_item_length is not None and len(item) > max_item_length:
            raise ValueError(
                f"{field_name} entries cannot exceed "
                f"{max_item_length} characters"
            )
        if item not in seen:
            seen.add(item)
            normalized.append(item)
    return normalized


class RegistryRecord(BaseModel):
    schema_version: Literal[1] = 1
    agent_id: str
    status: str = ""
    working_set: list[str] = Field(default_factory=list)
    capabilities: list[str] = Field(default_factory=list)
    lifecycle: AgentLifecycle = "persistent"
    durability: str = "unknown"
    started_at: datetime
    last_seen: datetime

    @field_validator("agent_id")
    @classmethod
    def validate_agent_id(cls, value: str) -> str:
        return _validate_agent_id(value)

    @field_validator("started_at", "last_seen")
    @classmethod
    def validate_aware_timestamp(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("registry timestamps must be timezone-aware")
        return value

    @field_validator("status")
    @classmethod
    def validate_status(cls, value: str) -> str:
        if len(value) > MAX_STATUS_LENGTH:
            raise ValueError(f"status exceeds {MAX_STATUS_LENGTH} characters")
        return value

    @field_validator("working_set", mode="before")
    @classmethod
    def normalize_working_set(cls, value: object) -> list[str]:
        return _normalize_string_list(value, field_name="working_set")

    @field_validator("capabilities", mode="before")
    @classmethod
    def normalize_capabilities(cls, value: object) -> list[str]:
        return _normalize_string_list(
            value,
            field_name="capabilities",
            max_item_length=MAX_CAPABILITY_LENGTH,
        )

    def to_json(self) -> str:
        return self.model_dump_json()

    @classmethod
    def from_mqtt(
        cls,
        topic: str,
        payload: str | bytes,
        *,
        topics: TopicMap = DEFAULT_TOPICS,
    ) -> "RegistryRecord":
        # Topic ownership is authoritative; reject conflicting payload identity.
        topic_agent = topics.registry_agent(topic)
        record = cls.model_validate_json(payload)
        if record.agent_id != topic_agent:
            raise ValueError(
                f"registry agent_id {record.agent_id!r} does not match "
                f"topic agent {topic_agent!r}"
            )
        return record


class PresenceRecord(BaseModel):
    schema_version: Literal[1] = 1
    agent_id: str
    state: Literal["online", "offline"]
    connected_at: datetime | None = None
    reason: str | None = None

    @field_validator("agent_id")
    @classmethod
    def validate_agent_id(cls, value: str) -> str:
        return _validate_agent_id(value)

    def to_json(self) -> str:
        return self.model_dump_json(exclude_none=True)

    @classmethod
    def from_mqtt(
        cls,
        topic: str,
        payload: str | bytes,
        *,
        topics: TopicMap = DEFAULT_TOPICS,
    ) -> "PresenceRecord":
        # Apply the same topic-identity check as RegistryRecord.
        topic_agent = topics.presence_agent(topic)

        data = json.loads(payload)
        if "agent_id" not in data and "agent" in data:
            data = {
                "schema_version": 1,
                "agent_id": data["agent"],
                "state": data.get("status"),
                "connected_at": data.get("connected_at"),
                "reason": data.get("reason"),
            }
        record = cls.model_validate(data)
        if record.agent_id != topic_agent:
            raise ValueError(
                f"presence agent_id {record.agent_id!r} does not match "
                f"topic agent {topic_agent!r}"
            )
        return record


def presence_payload(
    agent_id: str,
    state: Literal["online", "offline"],
    *,
    reason: str | None = None,
) -> str:
    """Serialize the current presence payload."""
    return PresenceRecord(
        agent_id=agent_id,
        state=state,
        connected_at=(
            datetime.now(timezone.utc) if state == "online" else None
        ),
        reason=reason,
    ).to_json()


class GCDecisionReason(str, Enum):
    """Why a registry record was preserved or selected for transient GC."""

    ELIGIBLE = "eligible"
    PERSISTENT = "persistent"
    ONLINE = "online"
    WITHIN_RETENTION = "within-retention"
    CLOCK_SKEW = "clock-skew"


class TransientGCDecision(BaseModel):
    agent_id: str
    eligible: bool
    reason: GCDecisionReason
    age_seconds: float


def transient_gc_decision(
    *,
    record: RegistryRecord,
    presence: PresenceRecord | None,
    now: datetime,
    stale_after_seconds: float,
    retention_seconds: float,
) -> TransientGCDecision:
    """Return the deletion decision for one validated registry snapshot."""

    if stale_after_seconds <= 0:
        raise ValueError("stale_after_seconds must be positive")
    if retention_seconds <= stale_after_seconds:
        raise ValueError(
            "retention_seconds must be longer than stale_after_seconds"
        )
    if presence is not None and presence.agent_id != record.agent_id:
        raise ValueError(
            f"presence agent_id {presence.agent_id!r} does not match "
            f"registry agent_id {record.agent_id!r}"
        )
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    if record.last_seen.tzinfo is None or record.last_seen.utcoffset() is None:
        raise ValueError("registry last_seen must be timezone-aware")

    age_seconds = (now - record.last_seen).total_seconds()
    decision = {
        "agent_id": record.agent_id,
        "age_seconds": age_seconds,
    }

    if record.lifecycle == "persistent":
        return TransientGCDecision(
            **decision,
            eligible=False,
            reason=GCDecisionReason.PERSISTENT,
        )
    if age_seconds < 0:
        return TransientGCDecision(
            **decision,
            eligible=False,
            reason=GCDecisionReason.CLOCK_SKEW,
        )
    if (
        presence is not None
        and presence.state == "online"
        and age_seconds <= stale_after_seconds
    ):
        return TransientGCDecision(
            **decision,
            eligible=False,
            reason=GCDecisionReason.ONLINE,
        )
    if age_seconds <= retention_seconds:
        return TransientGCDecision(
            **decision,
            eligible=False,
            reason=GCDecisionReason.WITHIN_RETENTION,
        )
    return TransientGCDecision(
        **decision,
        eligible=True,
        reason=GCDecisionReason.ELIGIBLE,
    )


class RegistryCache:
    """In-memory view of retained registry and presence topics."""

    def __init__(
        self,
        *,
        stale_after_seconds: float,
        topics: TopicMap = DEFAULT_TOPICS,
    ) -> None:
        if stale_after_seconds <= 0:
            raise ValueError("stale_after_seconds must be positive")
        self.stale_after_seconds = stale_after_seconds
        self.topics = topics
        self._registry: dict[str, RegistryRecord] = {}
        self._presence: dict[str, PresenceRecord] = {}

    def update_registry(self, topic: str, payload: str | bytes) -> RegistryRecord:
        record = RegistryRecord.from_mqtt(topic, payload, topics=self.topics)
        self._registry[record.agent_id] = record
        return record

    def update_presence(self, topic: str, payload: str | bytes) -> PresenceRecord:
        record = PresenceRecord.from_mqtt(topic, payload, topics=self.topics)
        self._presence[record.agent_id] = record
        return record

    def remove_registry(self, topic: str) -> None:
        self._registry.pop(self.topics.registry_agent(topic), None)

    def remove_presence(self, topic: str) -> None:
        self._presence.pop(self.topics.presence_agent(topic), None)

    def _is_online(self, agent_id: str, *, now: datetime | None = None) -> bool:
        presence = self._presence.get(agent_id)
        if presence is None or presence.state != "online":
            return False
        record = self._registry.get(agent_id)
        if record is None:
            return True
        current = now or datetime.now(timezone.utc)
        age = (current - record.last_seen).total_seconds()
        return age <= self.stale_after_seconds

    def online_agent_ids(self) -> list[str]:
        agent_ids = set(self._presence) | set(self._registry)
        return sorted(agent_id for agent_id in agent_ids if self._is_online(agent_id))

    def get_state(self, agent_id: str) -> dict:
        if agent_id not in self._registry and agent_id not in self._presence:
            raise KeyError(agent_id)
        record = self._registry.get(agent_id)
        presence = self._presence.get(agent_id)
        state = {
            "agent_id": agent_id,
            "online": self._is_online(agent_id),
            "status": record.status if record else "",
            "working_set": list(record.working_set) if record else [],
            "capabilities": list(record.capabilities) if record else [],
            "lifecycle": record.lifecycle if record else "unknown",
            "durability": record.durability if record else "unknown",
            "started_at": record.started_at.isoformat() if record else None,
            "last_seen": record.last_seen.isoformat() if record else None,
        }
        if presence and presence.state == "offline" and presence.reason:
            state["offline_reason"] = presence.reason
        return state

    def list_states(
        self,
        *,
        include_offline: bool = False,
        lifecycle: AgentLifecycle | None = None,
    ) -> list[dict]:
        agent_ids = sorted(set(self._registry) | set(self._presence))
        states = [self.get_state(agent_id) for agent_id in agent_ids]
        if lifecycle is not None:
            states = [
                state for state in states
                if state["lifecycle"] == lifecycle
            ]
        if include_offline:
            return states
        return [state for state in states if state["online"]]
