from __future__ import annotations

import asyncio
import json
import logging
from collections import Counter
from datetime import datetime, timezone
from typing import Any, Callable, Literal

import aiomqtt
from pydantic import BaseModel, Field

from .bus import _build_tls_context
from .message import _validate_registered_agent_id
from .registry import (
    PresenceRecord,
    RegistryRecord,
    transient_gc_decision,
)
from .topics import DEFAULT_TOPICS, TopicMap

logger = logging.getLogger(__name__)


class OnlineIdentityError(RuntimeError):
    """An explicit retirement targeted an identity that is still online."""


class UnconfirmedOfflineError(OnlineIdentityError):
    """Raised when retirement cannot confirm the identity is offline."""


class RegistryGCReport(BaseModel):
    """Structured result of one bounded transient-registry collection."""

    dry_run: bool
    scanned: int
    # Presence without registry is reported, never collected.
    orphan_presence: int = 0
    eligible: int
    selected: int
    online: int = 0
    retained: int = 0
    # Selected records not completed during the act phase.
    skipped: int = 0
    raced: int = 0
    malformed: int = 0
    deleted: int = 0
    # Deleted identities whose durable MQTT session was destroyed.
    sessions_destroyed: int = 0
    would_delete: int = 0
    disappeared: int = 0
    failed: int = 0
    partial: int = 0
    preserved_by_reason: dict[str, int] = Field(default_factory=dict)
    deleted_agent_ids: list[str] = Field(default_factory=list)
    would_delete_agent_ids: list[str] = Field(default_factory=list)
    failed_agent_ids: list[str] = Field(default_factory=list)


class RegistryListEntry(RegistryRecord):
    """One registry record joined with its retained presence evidence."""

    presence_state: Literal["online", "offline", "unknown"] = "unknown"
    online: bool | None = None
    offline_reason: str | None = None


class RegistryListReport(BaseModel):
    """Structured snapshot of the retained identity directory."""

    scanned: int
    orphan_presence: int = 0
    malformed: int = 0
    identities: list[RegistryListEntry] = Field(default_factory=list)
    orphan_presence_agent_ids: list[str] = Field(default_factory=list)


class RegistryForgetReport(BaseModel):
    """Structured result of explicit retained-state retirement."""

    agent_id: str
    dry_run: bool
    lifecycle: str = "unknown"
    # True=online, False=offline, None=no presence evidence.
    online: bool | None = None
    rechecked: bool = False
    # A refusal leaves retained state unchanged.
    refused: bool = False
    refused_reason: Literal["online", "unconfirmed-offline"] | None = None
    already_absent: bool = False
    deleted: bool = False
    session_destroyed: bool = False
    failed: bool = False
    partial: bool = False


class _RegistrySnapshot:
    def __init__(self, topics: TopicMap) -> None:
        self.topics = topics
        self.records: dict[str, RegistryRecord] = {}
        self.presence: dict[str, PresenceRecord] = {}
        self.invalid_messages = 0

    def ingest(self, topic: str, payload: bytes) -> None:
        if self.topics.is_registry_topic(topic):
            if not payload:
                try:
                    agent_id = self.topics.registry_agent(topic)
                except ValueError:
                    self.invalid_messages += 1
                    return
                self.records.pop(agent_id, None)
                return
            try:
                record = RegistryRecord.from_mqtt(
                    topic,
                    payload,
                    topics=self.topics,
                )
            except (ValueError, TypeError):
                self.invalid_messages += 1
                return
            self.records[record.agent_id] = record
            return

        if self.topics.is_presence_topic(topic):
            if not payload:
                try:
                    agent_id = self.topics.presence_agent(topic)
                except ValueError:
                    self.invalid_messages += 1
                    return
                self.presence.pop(agent_id, None)
                return
            try:
                record = PresenceRecord.from_mqtt(
                    topic,
                    payload,
                    topics=self.topics,
                )
            except (ValueError, TypeError):
                self.invalid_messages += 1
                return
            self.presence[record.agent_id] = record
            return

        # Neither tree: this map's subscriptions and predicates disagree.
        self.invalid_messages += 1


def _default_audit_sink(event: dict[str, Any]) -> None:
    logger.warning(
        "registry maintenance audit: %s",
        json.dumps(event, sort_keys=True),
    )


class RegistryMaintenance:
    """One-shot retained-registry maintenance against an MQTT broker."""

    def __init__(
        self,
        *,
        broker: str = "localhost",
        port: int = 1883,
        username: str | None = None,
        password: str | None = None,
        tls: bool = False,
        ca_cert: str | None = None,
        client_cert: str | None = None,
        client_key: str | None = None,
        topics: TopicMap = DEFAULT_TOPICS,
        snapshot_seconds: float = 0.5,
        client_factory: Callable[..., Any] = aiomqtt.Client,
        audit_sink: Callable[[dict[str, Any]], None] = _default_audit_sink,
    ) -> None:
        if snapshot_seconds <= 0:
            raise ValueError("snapshot_seconds must be positive")
        self.broker = broker
        self.port = port
        self.username = username
        self.password = password
        self.topics = topics
        self.snapshot_seconds = snapshot_seconds
        self._client_factory = client_factory
        self._audit_sink = audit_sink
        self._tls_context = _build_tls_context(
            tls=tls,
            ca_cert=ca_cert,
            client_cert=client_cert,
            client_key=client_key,
        )

    def _new_client(
        self,
        *,
        identifier: str | None = None,
        clean_session: bool | None = None,
    ):
        kwargs: dict[str, Any] = {}
        if self.username is not None:
            kwargs["username"] = self.username
        if self.password is not None:
            kwargs["password"] = self.password
        if self._tls_context is not None:
            kwargs["tls_context"] = self._tls_context
        if identifier is not None:
            kwargs["identifier"] = identifier
        if clean_session is not None:
            kwargs["clean_session"] = clean_session
        return self._client_factory(
            self.broker,
            port=self.port,
            **kwargs,
        )

    async def _snapshot_with_client(
        self,
        client: Any,
        filters: tuple[str, ...],
    ) -> _RegistrySnapshot:
        snapshot = _RegistrySnapshot(self.topics)
        for topic_filter in filters:
            await client.subscribe(topic_filter, qos=1)

        async def consume() -> None:
            async for message in client.messages:
                snapshot.ingest(str(message.topic), bytes(message.payload))

        try:
            await asyncio.wait_for(
                consume(),
                timeout=self.snapshot_seconds,
            )
        except asyncio.TimeoutError:
            pass
        return snapshot

    async def _snapshot(
        self,
        filters: tuple[str, ...],
    ) -> _RegistrySnapshot:
        async with self._new_client() as client:
            return await self._snapshot_with_client(client, filters)

    @staticmethod
    def _observed_online(
        record: RegistryRecord | None,
        presence: PresenceRecord | None,
        *,
        now: datetime,
        stale_after_seconds: float,
    ) -> bool | None:
        """Return True, False, or None when presence was not observed."""

        if presence is None:
            return None
        if presence.state != "online":
            return False
        if record is None:
            return True
        return (now - record.last_seen).total_seconds() <= stale_after_seconds

    @staticmethod
    def _enforce_offline_interlock(
        agent_id: str,
        online: bool | None,
    ) -> None:
        """Raise unless the identity was positively observed offline."""

        if online is True:
            raise OnlineIdentityError(
                f"refusing to forget online identity {agent_id!r}; "
                "use force_online only for emergency recovery"
            )
        if online is None:
            raise UnconfirmedOfflineError(
                f"could not confirm identity {agent_id!r} is offline: no "
                "retained presence message was observed, which is also "
                "what a live agent looks like when its presence misses "
                "the snapshot window. Retry or widen snapshot_seconds; "
                "use force_online only for emergency recovery"
            )

    @staticmethod
    def _audit_event(
        *,
        operation: str,
        agent_id: str,
        record: RegistryRecord | None,
        presence: PresenceRecord | None,
        now: datetime,
        outcome: str,
        retention_seconds: float | None,
        online: bool | None = None,
        force_online: bool = False,
    ) -> dict[str, Any]:
        # Preserve unknown presence rather than coercing it to offline.
        return {
            "operation": operation,
            "agent_id": agent_id,
            "lifecycle": record.lifecycle if record else "unknown",
            "started_at": record.started_at.isoformat() if record else None,
            "last_seen": record.last_seen.isoformat() if record else None,
            "presence_state": presence.state if presence else "unknown",
            "online": online,
            "offline_reason": presence.reason if presence else None,
            "record_age_seconds": (
                (now - record.last_seen).total_seconds() if record else None
            ),
            "retention_seconds": retention_seconds,
            "force_online": force_online,
            "outcome": outcome,
            "observed_at": now.isoformat(),
        }

    async def list_registry(
        self,
        *,
        stale_after_seconds: float,
        now: datetime | None = None,
    ) -> RegistryListReport:
        """Return the retained directory joined with presence evidence."""

        if stale_after_seconds <= 0:
            raise ValueError("stale_after_seconds must be positive")
        current = now or datetime.now(timezone.utc)
        if current.tzinfo is None or current.utcoffset() is None:
            raise ValueError("now must be timezone-aware")

        snapshot = await self._snapshot(
            (
                self.topics.any_registry_filter(),
                self.topics.any_presence_filter(),
            )
        )
        identities = []
        for agent_id, record in snapshot.records.items():
            presence = snapshot.presence.get(agent_id)
            identities.append(
                RegistryListEntry(
                    **record.model_dump(),
                    presence_state=(presence.state if presence else "unknown"),
                    online=self._observed_online(
                        record,
                        presence,
                        now=current,
                        stale_after_seconds=stale_after_seconds,
                    ),
                    offline_reason=presence.reason if presence else None,
                )
            )
        identities.sort(key=lambda identity: identity.agent_id)
        orphan_ids = sorted(snapshot.presence.keys() - snapshot.records.keys())
        return RegistryListReport(
            scanned=len(snapshot.records),
            orphan_presence=len(orphan_ids),
            malformed=snapshot.invalid_messages,
            identities=identities,
            orphan_presence_agent_ids=orphan_ids,
        )

    async def gc_transient(
        self,
        *,
        stale_after_seconds: float,
        retention_seconds: float,
        batch_size: int,
        dry_run: bool = False,
        now: datetime | None = None,
    ) -> RegistryGCReport:
        """Delete a bounded batch of old transient retained records."""

        if stale_after_seconds <= 0:
            raise ValueError("stale_after_seconds must be positive")
        if retention_seconds <= stale_after_seconds:
            raise ValueError(
                "retention_seconds must be longer than stale_after_seconds"
            )
        if batch_size <= 0:
            raise ValueError("batch_size must be positive")
        current = now or datetime.now(timezone.utc)
        if current.tzinfo is None or current.utcoffset() is None:
            raise ValueError("now must be timezone-aware")

        initial = await self._snapshot(
            (
                self.topics.any_registry_filter(),
                self.topics.any_presence_filter(),
            )
        )
        preserved: Counter[str] = Counter()
        candidates: list[RegistryRecord] = []
        for agent_id, record in initial.records.items():
            decision = transient_gc_decision(
                record=record,
                presence=initial.presence.get(agent_id),
                now=current,
                stale_after_seconds=stale_after_seconds,
                retention_seconds=retention_seconds,
            )
            if decision.eligible:
                candidates.append(record)
            else:
                preserved[decision.reason.value] += 1

        # Presence has no age metadata; report orphans but never collect them.
        orphan_presence = sum(
            1
            for agent_id in initial.presence
            if agent_id not in initial.records
        )

        candidates.sort(key=lambda record: (record.last_seen, record.agent_id))
        selected = candidates[:batch_size]
        retained = sum(preserved.values())
        report = RegistryGCReport(
            dry_run=dry_run,
            scanned=len(initial.records),
            orphan_presence=orphan_presence,
            eligible=len(candidates),
            selected=len(selected),
            online=preserved["online"],
            retained=retained,
            malformed=initial.invalid_messages,
            preserved_by_reason=dict(sorted(preserved.items())),
        )

        for original in selected:
            resolved_before = (
                report.deleted + report.would_delete + report.skipped
            )
            try:
                async with self._new_client() as client:
                    fresh = await self._snapshot_with_client(
                        client,
                        (
                            self.topics.registry(original.agent_id),
                            self.topics.presence(original.agent_id),
                        ),
                    )
                    report.malformed += fresh.invalid_messages
                    record = fresh.records.get(original.agent_id)
                    presence = fresh.presence.get(original.agent_id)
                    if record is None:
                        report.disappeared += 1
                        report.skipped += 1
                        continue
                    decision = transient_gc_decision(
                        record=record,
                        presence=presence,
                        now=current,
                        stale_after_seconds=stale_after_seconds,
                        retention_seconds=retention_seconds,
                    )
                    if not decision.eligible:
                        report.raced += 1
                        report.skipped += 1
                        continue
                    if dry_run:
                        report.would_delete += 1
                        report.would_delete_agent_ids.append(
                            original.agent_id
                        )
                        continue
                    # Preserve three-way presence evidence in the audit record.
                    event = self._audit_event(
                        operation="transient-gc",
                        agent_id=original.agent_id,
                        record=record,
                        presence=presence,
                        now=current,
                        outcome="authorized",
                        retention_seconds=retention_seconds,
                        online=self._observed_online(
                            record,
                            presence,
                            now=current,
                            stale_after_seconds=stale_after_seconds,
                        ),
                    )
                    try:
                        self._audit_sink(event)
                    except Exception as exc:
                        logger.error(
                            "registry audit failed for %s: %s",
                            original.agent_id,
                            exc,
                        )
                        report.failed += 1
                        report.skipped += 1
                        report.failed_agent_ids.append(original.agent_id)
                        continue

                    presence_deleted = False
                    try:
                        if record.durability == "durable":
                            # Retiring a durable identity also destroys its session.
                            async with self._new_client(
                                identifier=f"swarmbus-{original.agent_id}",
                                clean_session=True,
                            ) as agent_client:
                                await agent_client.publish(
                                    self.topics.presence(original.agent_id),
                                    b"",
                                    qos=1,
                                    retain=True,
                                )
                                presence_deleted = True
                                await agent_client.publish(
                                    self.topics.registry(original.agent_id),
                                    b"",
                                    qos=1,
                                    retain=True,
                                )
                            report.sessions_destroyed += 1
                        else:
                            await client.publish(
                                self.topics.presence(original.agent_id),
                                b"",
                                qos=1,
                                retain=True,
                            )
                            presence_deleted = True
                            await client.publish(
                                self.topics.registry(original.agent_id),
                                b"",
                                qos=1,
                                retain=True,
                            )
                    except Exception as exc:
                        logger.error(
                            "registry cleanup failed for %s: %s",
                            original.agent_id,
                            exc,
                        )
                        report.failed += 1
                        report.skipped += 1
                        report.partial += int(presence_deleted)
                        report.failed_agent_ids.append(original.agent_id)
                        continue
                    report.deleted += 1
                    report.deleted_agent_ids.append(original.agent_id)
            except aiomqtt.MqttError as exc:
                logger.error(
                    "registry gc transport failed at %s: %s",
                    original.agent_id,
                    exc,
                )
                # __aexit__ may fail after an already-counted delete.
                resolved_now = (
                    report.deleted + report.would_delete + report.skipped
                )
                if resolved_now == resolved_before:
                    report.failed += 1
                    report.failed_agent_ids.append(original.agent_id)
                # A broker failure makes the remaining records unattemptable.
                accounted = (
                    report.deleted + report.would_delete + report.skipped
                )
                report.skipped += len(selected) - accounted
                break

        return report

    async def forget(
        self,
        agent_id: str,
        *,
        dry_run: bool = False,
        force_online: bool = False,
        stale_after_seconds: float = 180,
        now: datetime | None = None,
    ) -> RegistryForgetReport:
        """Retire one identity and destroy its stable MQTT session."""

        _validate_registered_agent_id(agent_id)
        if stale_after_seconds <= 0:
            raise ValueError("stale_after_seconds must be positive")
        current = now or datetime.now(timezone.utc)
        if current.tzinfo is None or current.utcoffset() is None:
            raise ValueError("now must be timezone-aware")

        filters = (
            self.topics.registry(agent_id),
            self.topics.presence(agent_id),
        )
        snapshot = await self._snapshot(filters)
        record = snapshot.records.get(agent_id)
        presence = snapshot.presence.get(agent_id)
        online = self._observed_online(
            record,
            presence,
            now=current,
            stale_after_seconds=stale_after_seconds,
        )
        if not force_online:
            self._enforce_offline_interlock(agent_id, online)

        # Recheck before deletion because the bounded snapshot may miss presence.
        # The reader must be anonymous: clean_session=True destroys the target
        # session at connect time. Dry runs use the same decision path.
        async with self._new_client() as reader:
            fresh = await self._snapshot_with_client(reader, filters)
        record = fresh.records.get(agent_id)
        presence = fresh.presence.get(agent_id)
        online = self._observed_online(
            record,
            presence,
            now=current,
            stale_after_seconds=stale_after_seconds,
        )
        report = RegistryForgetReport(
            agent_id=agent_id,
            dry_run=dry_run,
            lifecycle=record.lifecycle if record else "unknown",
            online=online,
            rechecked=True,
            already_absent=record is None and presence is None,
        )
        if not force_online and online is not False:
            report.refused = True
            report.refused_reason = (
                "online" if online else "unconfirmed-offline"
            )
            return report

        if dry_run:
            return report

        # Audit the decisive re-read before publishing; audit failure preserves
        # retained state.
        event = self._audit_event(
            operation="explicit-forget",
            agent_id=agent_id,
            record=record,
            presence=presence,
            now=current,
            outcome="authorized",
            retention_seconds=None,
            online=online,
            force_online=force_online,
        )
        try:
            self._audit_sink(event)
        except Exception as exc:
            logger.error("registry audit failed for %s: %s", agent_id, exc)
            report.failed = True
            return report

        presence_deleted = False
        try:
            async with self._new_client(
                identifier=f"swarmbus-{agent_id}",
                clean_session=True,
            ) as client:
                await client.publish(
                    self.topics.presence(agent_id),
                    b"",
                    qos=1,
                    retain=True,
                )
                presence_deleted = True
                await client.publish(
                    self.topics.registry(agent_id),
                    b"",
                    qos=1,
                    retain=True,
                )
            report.session_destroyed = True
        except Exception as exc:
            logger.error("registry forget failed for %s: %s", agent_id, exc)
            report.failed = True
            report.partial = presence_deleted
            return report

        report.deleted = True
        return report
