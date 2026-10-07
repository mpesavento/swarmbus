from __future__ import annotations

import asyncio
import json
import logging
import random
import sqlite3
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Literal

import aiomqtt

from .archive import SQLiteMessageStore
from .bus import _build_tls_context
from .message import AgentMessage, _validate_registered_agent_id
from .registry import (
    AgentLifecycle,
    RegistryCache,
    RegistryRecord,
    presence_payload,
)
from .topics import DEFAULT_TOPICS

logger = logging.getLogger(__name__)

_RECONNECT_BACKOFF_MAX = 60.0
_STORE_RETRY_BACKOFF_MAX = 60.0
_CONNECTION_STABILITY_SECONDS = 30.0
_REPLAY_SETTLE_SECONDS = 0.1


class TransportUnavailable(RuntimeError):
    """The managed runtime has no live MQTT transport."""


class PresenceRequiredError(RuntimeError):
    """Raised when a directory operation requires disabled presence."""


class RuntimeFatalError(RuntimeError):
    """The managed runtime cannot recover without operator intervention."""


class StoreUnavailable(RuntimeError):
    """The managed runtime cannot currently commit inbound messages."""


class AcknowledgementError(RuntimeError):
    """Acknowledging an MQTT delivery failed and requires reconnection."""


class _ManualAckUnavailable(RuntimeError):
    """The installed MQTT stack cannot provide manual acknowledgements."""


class RuntimeState(str, Enum):
    STOPPED = "stopped"
    CONNECTING = "connecting"
    CONNECTED = "connected"
    STORE_DEGRADED = "store-degraded"
    FATAL = "fatal"


class _ManualAckAdapter:
    """Quarantine aiomqtt's private Paho handle until it grows a public API."""

    def __init__(self, client: aiomqtt.Client) -> None:
        try:
            self._paho = client._client
        except AttributeError as exc:
            raise _ManualAckUnavailable(
                "aiomqtt does not expose the Paho client required for manual ACK"
            ) from exc
        if not callable(getattr(self._paho, "manual_ack_set", None)) or not callable(
            getattr(self._paho, "ack", None)
        ):
            raise _ManualAckUnavailable(
                "installed Paho client does not support manual ACK"
            )

    def enable(self) -> None:
        self._paho.manual_ack_set(True)

    def ack(self, message: Any) -> None:
        qos = int(message.qos)
        if qos == 0:
            return
        result = self._paho.ack(message.mid, qos)
        if result not in (0, None):
            raise AcknowledgementError(
                f"Paho ACK failed with result {result}"
            )


class ManagedMCPRuntime:
    """One MQTT connection and durable SQLite inbox for an MCP server."""

    def __init__(
        self,
        *,
        agent_id: str,
        broker: str = "localhost",
        port: int = 1883,
        durable: bool = False,
        presence: bool = False,
        lifecycle: AgentLifecycle = "persistent",
        client_id: str | None = None,
        heartbeat_seconds: float = 60,
        stale_after_seconds: float = 180,
        capabilities: list[str] | tuple[str, ...] = (),
        state_path: str | Path,
        username: str | None = None,
        password: str | None = None,
        tls: bool = False,
        ca_cert: str | None = None,
        client_cert: str | None = None,
        client_key: str | None = None,
        store: SQLiteMessageStore | Any | None = None,
    ) -> None:
        _validate_registered_agent_id(agent_id)
        if lifecycle not in {"persistent", "transient"}:
            raise ValueError("lifecycle must be persistent or transient")
        if lifecycle == "transient" and not presence:
            raise ValueError(
                "lifecycle='transient' requires presence=True "
                "(--lifecycle transient requires --presence): lifecycle "
                "describes how this identity's directory record is "
                "retired, and a presence-free runtime has no directory "
                "record, so the declaration applies to nothing. Enable "
                "presence, or use lifecycle='persistent' "
                "(--lifecycle persistent)."
            )
        if durable and not presence:
            raise ValueError(
                "durable=True requires presence=True (--durable "
                "requires --presence): a durable MQTT session makes the "
                "broker queue messages for this identity while it is "
                "away, but a presence-free identity never appears in the "
                "directory, so no operator can discover it with "
                "`swarmbus list` or retire it with `swarmbus "
                "registry-forget` and its queued backlog grows forever. "
                "Enable presence, or use durable=False "
                "(--no-durable) for a live-session runtime."
            )
        if client_id is not None:
            _validate_registered_agent_id(client_id)
        if heartbeat_seconds <= 0:
            raise ValueError("heartbeat_seconds must be positive")
        if stale_after_seconds < heartbeat_seconds * 2:
            raise ValueError(
                "stale_after_seconds must be at least twice heartbeat_seconds"
            )
        self.agent_id = agent_id
        self.broker = broker
        self.port = port
        self.durable = durable
        self.presence = presence
        self.lifecycle = lifecycle
        self.client_id = client_id
        self.heartbeat_seconds = heartbeat_seconds
        self.username = username
        self.password = password
        self._tls_context = _build_tls_context(
            tls=tls,
            ca_cert=ca_cert,
            client_cert=client_cert,
            client_key=client_key,
        )
        self.store = store or SQLiteMessageStore(state_path)
        self.topics = DEFAULT_TOPICS
        self.registry = RegistryCache(
            stale_after_seconds=stale_after_seconds,
            topics=self.topics,
        )
        self.started_at = datetime.now(timezone.utc)
        self.status = ""
        self.working_set: list[str] = []
        self.runtime_capabilities = [
            "messaging",
            "durable-inbox",
            "agent-state",
        ]
        normalized_capabilities = RegistryRecord(
            agent_id=self.agent_id,
            capabilities=list(capabilities),
            lifecycle=self.lifecycle,
            started_at=self.started_at,
            last_seen=self.started_at,
        ).capabilities
        runtime_capabilities = set(self.runtime_capabilities)
        self.declared_capabilities = [
            capability
            for capability in normalized_capabilities
            if capability not in runtime_capabilities
        ]
        self._client: aiomqtt.Client | None = None
        self._connection_task: asyncio.Task | None = None
        self._heartbeat_task: asyncio.Task | None = None
        self._pending_client: aiomqtt.Client | None = None
        self._pending_ack_adapter: _ManualAckAdapter | None = None
        self._ready = asyncio.Event()
        self._stopping = asyncio.Event()
        self._inbox_condition = asyncio.Condition()
        self._state = RuntimeState.STOPPED
        self._last_fatal_error: Exception | None = None
        self._last_store_error: Exception | None = None
        self._replay_activity_at: float | None = None
        self._replay_settled = True

    def _client_kwargs(self) -> dict[str, Any]:
        kwargs: dict[str, Any] = {}
        if self.presence:
            kwargs["will"] = aiomqtt.Will(
                topic=self.topics.presence(self.agent_id),
                payload=presence_payload(
                    self.agent_id,
                    "offline",
                    reason="connection-lost",
                ),
                qos=1,
                retain=True,
            )
        if self.username is not None:
            kwargs["username"] = self.username
        if self.password is not None:
            kwargs["password"] = self.password
        if self._tls_context is not None:
            kwargs["tls_context"] = self._tls_context
        identifier = self.client_id
        if self.durable:
            identifier = identifier or f"swarmbus-{self.agent_id}"
            kwargs["clean_session"] = False
        if identifier is not None:
            kwargs["identifier"] = identifier
        return kwargs

    def _new_client(self) -> aiomqtt.Client:
        return aiomqtt.Client(
            self.broker,
            port=self.port,
            **self._client_kwargs(),
        )

    @property
    def state(self) -> RuntimeState:
        return self._state

    @property
    def last_fatal_error(self) -> Exception | None:
        return self._last_fatal_error

    @property
    def last_store_error(self) -> Exception | None:
        return self._last_store_error

    @property
    def connected(self) -> bool:
        return self._client is not None

    def _record_fatal(self, exc: Exception) -> None:
        self._last_fatal_error = exc
        self._state = RuntimeState.FATAL

    def _fatal_error(self) -> RuntimeFatalError:
        assert self._last_fatal_error is not None
        return RuntimeFatalError(
            f"swarmbus managed MQTT runtime failed: {self._last_fatal_error}"
        )

    async def _notify_inbox_waiters(self) -> None:
        async with self._inbox_condition:
            self._inbox_condition.notify_all()

    async def start(self) -> None:
        """Validate capabilities, open local state, and start connecting."""
        if self._last_fatal_error is not None:
            raise self._fatal_error()
        if self._connection_task is not None:
            return

        self._state = RuntimeState.CONNECTING
        try:
            client = self._new_client()
            ack_adapter = _ManualAckAdapter(client)
            ack_adapter.enable()
        except _ManualAckUnavailable as exc:
            fatal = RuntimeFatalError(str(exc))
            self._record_fatal(fatal)
            raise fatal from exc

        try:
            await self.store.open()
        except (OSError, sqlite3.Error) as exc:
            self._last_store_error = exc
            self._state = RuntimeState.STORE_DEGRADED
            raise

        self._pending_client = client
        self._pending_ack_adapter = ack_adapter
        self._connection_task = asyncio.create_task(
            self._run_connection_loop()
        )
        self._connection_task.add_done_callback(self._connection_task_done)

    async def _run_connection_loop(self) -> None:
        try:
            await self._connection_loop()
        except Exception as exc:
            self._record_fatal(exc)
            await self._notify_inbox_waiters()
            raise

    def _connection_task_done(self, task: asyncio.Task[None]) -> None:
        if task.cancelled() or self._stopping.is_set():
            return
        exc = task.exception()
        if exc is not None:
            if self._last_fatal_error is None:
                self._record_fatal(exc)
            logger.error(
                "managed MQTT runtime stopped: %s",
                exc,
                exc_info=(type(exc), exc, exc.__traceback__),
            )
            asyncio.create_task(self._notify_inbox_waiters())

    async def wait_until_ready(self, timeout: float = 10.0) -> None:
        """Wait for the first broker connection, surfacing early task failure."""
        if self._connection_task is None:
            raise RuntimeError("swarmbus MQTT runtime has not been started")
        ready_wait = asyncio.create_task(self._ready.wait())
        try:
            done, _ = await asyncio.wait(
                {ready_wait, self._connection_task},
                timeout=timeout,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if not done:
                raise TimeoutError(
                    f"swarmbus MQTT runtime did not connect within {timeout:g}s"
                )
            if self._connection_task in done:
                exc = self._connection_task.exception()
                if exc is not None:
                    if self._last_fatal_error is None:
                        self._record_fatal(exc)
                    raise self._fatal_error() from exc
        finally:
            if not ready_wait.done():
                ready_wait.cancel()

    async def stop(self) -> None:
        if self._stopping.is_set():
            return
        self._stopping.set()
        if self._heartbeat_task is not None:
            self._heartbeat_task.cancel()
            await asyncio.gather(
                self._heartbeat_task,
                return_exceptions=True,
            )
            self._heartbeat_task = None
        try:
            if self._client is not None and self.presence:
                try:
                    if self.lifecycle == "transient":
                        await self._clear_transient_retained_state()
                    else:
                        await self._publish_presence(
                            "offline",
                            reason="clean-shutdown",
                        )
                except (aiomqtt.MqttError, RuntimeError) as exc:
                    logger.warning("failed to publish shutdown state: %s", exc)
        finally:
            try:
                if self._connection_task is not None:
                    if not self._connection_task.done():
                        self._connection_task.cancel()
                    result = (
                        await asyncio.gather(
                            self._connection_task,
                            return_exceptions=True,
                        )
                    )[0]
                    if (
                        isinstance(result, Exception)
                        and self._last_fatal_error is None
                    ):
                        self._record_fatal(result)
            finally:
                self._client = None
                self._pending_client = None
                self._pending_ack_adapter = None
                await self.store.close()
                if self._state is not RuntimeState.FATAL:
                    self._state = RuntimeState.STOPPED

    async def _connection_loop(self) -> None:
        backoff = 1.0
        client = self._pending_client
        ack_adapter = self._pending_ack_adapter
        self._pending_client = None
        self._pending_ack_adapter = None

        while not self._stopping.is_set():
            self._state = RuntimeState.CONNECTING
            if client is None:
                client = self._new_client()
                ack_adapter = _ManualAckAdapter(client)
                ack_adapter.enable()
            assert ack_adapter is not None

            connected_at: float | None = None
            try:
                async with client:
                    connected_at = asyncio.get_running_loop().time()
                    await client.subscribe(
                        self.topics.inbox(self.agent_id), qos=1
                    )
                    await client.subscribe(self.topics.broadcast, qos=1)
                    if self.presence:
                        # Presence-free runtimes do not subscribe to directory data.
                        await client.subscribe(
                            self.topics.any_presence_filter(), qos=1
                        )
                        await client.subscribe(
                            self.topics.any_registry_filter(), qos=1
                        )
                    self._client = client
                    if self.durable:
                        self._replay_activity_at = (
                            asyncio.get_running_loop().time()
                        )
                        self._replay_settled = False
                    else:
                        self._replay_activity_at = None
                        self._replay_settled = True
                    if self.presence:
                        # Publish registry first: GC can age out a registry orphan,
                        # while a presence orphan has no timestamp to collect safely.
                        await self._publish_registry()
                        await self._publish_presence("online")
                        self._heartbeat_task = asyncio.create_task(
                            self._heartbeat_loop()
                        )
                    self._state = RuntimeState.CONNECTED
                    await self._notify_inbox_waiters()
                    self._ready.set()
                    async for message in client.messages:
                        if self.durable and not self._replay_settled:
                            self._replay_activity_at = (
                                asyncio.get_running_loop().time()
                            )
                        try:
                            await self._handle_message(message, ack_adapter)
                            backoff = 1.0
                        except AcknowledgementError as exc:
                            self._state = RuntimeState.CONNECTING
                            logger.warning(
                                "MQTT acknowledgement failed; reconnecting: %s",
                                exc,
                            )
                            break
                        except (OSError, sqlite3.Error) as exc:
                            self._state = RuntimeState.STORE_DEGRADED
                            logger.error(
                                "inbox commit failed; reconnecting for redelivery: %s",
                                exc,
                            )
                            break
            except aiomqtt.MqttError as exc:
                if self._stopping.is_set():
                    break
                self._state = RuntimeState.CONNECTING
                logger.warning(
                    "MQTT broker disconnected (%s); reconnecting in %.1fs",
                    exc,
                    backoff,
                )
            finally:
                if (
                    connected_at is not None
                    and asyncio.get_running_loop().time() - connected_at
                    >= _CONNECTION_STABILITY_SECONDS
                ):
                    backoff = 1.0
                self._client = None
                if self._heartbeat_task is not None:
                    self._heartbeat_task.cancel()
                    await asyncio.gather(
                        self._heartbeat_task,
                        return_exceptions=True,
                    )
                    self._heartbeat_task = None
                if self._state is RuntimeState.CONNECTED:
                    self._state = RuntimeState.CONNECTING
                await self._notify_inbox_waiters()

            client = None
            ack_adapter = None
            if not self._stopping.is_set():
                await asyncio.sleep(backoff + random.uniform(0, backoff * 0.1))
                backoff = min(backoff * 2, _RECONNECT_BACKOFF_MAX)

    async def _heartbeat_loop(self) -> None:
        while not self._stopping.is_set():
            await asyncio.sleep(self.heartbeat_seconds)
            if self._client is not None and self.presence:
                try:
                    await self._publish_registry()
                except (aiomqtt.MqttError, RuntimeError) as exc:
                    logger.warning("heartbeat registry publish failed: %s", exc)

    def _observe_sender_state(self, agent_id: str) -> dict:
        observed_at = datetime.now(timezone.utc).isoformat()
        try:
            state = self.registry.get_state(agent_id)
        except KeyError:
            return {
                "lifecycle": "unknown",
                "online": None,
                "observed_at": observed_at,
                "started_at": None,
                "capabilities": [],
            }
        lifecycle = state["lifecycle"]
        if lifecycle not in {"persistent", "transient"}:
            lifecycle = "unknown"
        return {
            "lifecycle": lifecycle,
            "online": state["online"],
            "observed_at": observed_at,
            "started_at": state["started_at"],
            "capabilities": state["capabilities"],
        }

    async def _store_inbox_message(
        self,
        message: AgentMessage,
        *,
        source_topic: str,
    ) -> bool:
        """Store once in durable mode; hold and retry in live-session mode."""
        sender_provenance = self._observe_sender_state(message.from_agent)
        backoff = 1.0
        while not self._stopping.is_set():
            try:
                inserted = await self.store.store(
                    message,
                    source_topic=source_topic,
                    sender_provenance=sender_provenance,
                )
            except (OSError, sqlite3.Error) as exc:
                self._last_store_error = exc
                self._state = RuntimeState.STORE_DEGRADED
                await self._notify_inbox_waiters()
                if self.durable:
                    raise
                logger.error(
                    "inbox commit failed; retaining delivery and retrying "
                    "in %.1fs: %s",
                    backoff,
                    exc,
                )
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, _STORE_RETRY_BACKOFF_MAX)
            else:
                self._last_store_error = None
                self._state = RuntimeState.CONNECTED
                return inserted
        raise asyncio.CancelledError

    async def _handle_message(
        self,
        mqtt_message: Any,
        ack_adapter: _ManualAckAdapter | Any,
    ) -> None:
        topic = str(mqtt_message.topic)
        if self.topics.is_message_topic(topic, agent_id=self.agent_id):
            try:
                message = AgentMessage.from_json(mqtt_message.payload)
            except (TypeError, ValueError, UnicodeDecodeError) as exc:
                logger.warning("discarding invalid message envelope: %s", exc)
                ack_adapter.ack(mqtt_message)
                return
            inserted = await self._store_inbox_message(
                message,
                source_topic=topic,
            )
            if inserted:
                async with self._inbox_condition:
                    self._inbox_condition.notify_all()
            ack_adapter.ack(mqtt_message)
            return

        if self.topics.is_registry_topic(topic):
            try:
                if mqtt_message.payload:
                    self.registry.update_registry(topic, mqtt_message.payload)
                else:
                    self.registry.remove_registry(topic)
            except (
                AttributeError,
                json.JSONDecodeError,
                TypeError,
                ValueError,
                UnicodeDecodeError,
            ) as exc:
                logger.warning("discarding invalid registry record: %s", exc)
            ack_adapter.ack(mqtt_message)
            return

        if self.topics.is_presence_topic(topic):
            try:
                if mqtt_message.payload:
                    self.registry.update_presence(topic, mqtt_message.payload)
                else:
                    self.registry.remove_presence(topic)
            except (
                AttributeError,
                json.JSONDecodeError,
                TypeError,
                ValueError,
                UnicodeDecodeError,
            ) as exc:
                logger.warning("discarding invalid presence record: %s", exc)
            ack_adapter.ack(mqtt_message)
            return

        ack_adapter.ack(mqtt_message)

    async def send_message(
        self,
        *,
        to: str,
        subject: str,
        body: str,
        content_type: str = "text/plain",
    ) -> None:
        if self._client is None:
            raise self._transport_unavailable()
        message = AgentMessage.create(
            from_=self.agent_id,
            to=to,
            subject=subject,
            body=body,
            content_type=content_type,
        )
        await self._client.publish(
            self.topics.route(to),
            message.to_json(),
            qos=1,
            retain=False,
        )

    def _transport_unavailable(
        self,
    ) -> TransportUnavailable | RuntimeFatalError | StoreUnavailable:
        if self._last_fatal_error is not None:
            return self._fatal_error()
        if (
            self._state is RuntimeState.STORE_DEGRADED
            and self._last_store_error is not None
        ):
            return StoreUnavailable(
                f"swarmbus durable inbox is degraded: "
                f"{self._last_store_error}"
            )
        return TransportUnavailable(
            f"swarmbus MQTT runtime is disconnected from "
            f"{self.broker}:{self.port}"
        )

    def _presence_required(self, operation: str) -> PresenceRequiredError:
        """Build the presence-required error for a directory operation."""
        return PresenceRequiredError(
            f"swarmbus runtime {self.agent_id!r} cannot {operation}: it "
            "runs without presence (--no-presence), which makes it "
            "messaging-only -- it is not subscribed to the registry or "
            "presence topics and holds no directory data to answer "
            "with. Restart it with presence enabled (--presence) to use "
            "the directory."
        )

    async def _await_startup_replay(self) -> None:
        """Wait for a quiet window while a durable session replays."""
        while self.connected and not self._replay_settled:
            assert self._replay_activity_at is not None
            remaining = (
                self._replay_activity_at
                + _REPLAY_SETTLE_SECONDS
                - asyncio.get_running_loop().time()
            )
            if remaining > 0:
                await asyncio.sleep(remaining)
                continue
            self._replay_settled = True

    async def read_inbox(
        self,
        *,
        ack_ids: list[str] | None = None,
        max_messages: int = 10,
        wait_seconds: float = 0.0,
    ) -> list[dict]:
        """Acknowledge a prior batch, then read or wait for pending messages."""
        if max_messages < 0:
            raise ValueError("max_messages must be non-negative")
        if wait_seconds < 0:
            raise ValueError("wait_seconds must be non-negative")

        if ack_ids is not None:
            await self.ack_inbox(message_ids=ack_ids)
        if max_messages == 0:
            return []
        if wait_seconds > 0:
            return await self._wait_for_inbox(
                max_messages=max_messages,
                timeout=wait_seconds,
            )

        messages = await self.store.read(max_messages=max_messages)
        if messages:
            return messages
        if self.connected:
            await self._await_startup_replay()
            messages = await self.store.read(max_messages=max_messages)
            if messages:
                return messages
            if self._state is RuntimeState.STORE_DEGRADED:
                raise self._transport_unavailable()
            return []
        raise self._transport_unavailable()

    async def ack_inbox(self, *, message_ids: list[str]) -> int:
        return await self.store.ack(message_ids)

    async def _wait_for_inbox(
        self,
        *,
        max_messages: int,
        timeout: float,
    ) -> list[dict]:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        await self._await_startup_replay()
        while True:
            messages = await self.store.read(max_messages=max_messages)
            if messages:
                return messages
            if (
                not self.connected
                or self._state is RuntimeState.STORE_DEGRADED
            ):
                raise self._transport_unavailable()
            remaining = deadline - loop.time()
            if remaining <= 0:
                return []
            async with self._inbox_condition:
                messages = await self.store.read(max_messages=max_messages)
                if messages:
                    return messages
                if (
                    not self.connected
                    or self._state is RuntimeState.STORE_DEGRADED
                ):
                    raise self._transport_unavailable()
                try:
                    await asyncio.wait_for(
                        self._inbox_condition.wait(),
                        timeout=remaining,
                    )
                except asyncio.TimeoutError:
                    if (
                        not self.connected
                        or self._state is RuntimeState.STORE_DEGRADED
                    ):
                        raise self._transport_unavailable()
                    return []

    async def watch_inbox(self, timeout: float = 30.0) -> dict | None:
        """Compatibility helper over the consolidated inbox wait path."""
        messages = await self._wait_for_inbox(
            max_messages=1,
            timeout=timeout,
        )
        return messages[0] if messages else None

    async def list_agents(self) -> list[str]:
        if not self.presence:
            raise self._presence_required("list online agents")
        if not self.connected:
            raise self._transport_unavailable()
        return self.registry.online_agent_ids()

    async def list_states(
        self,
        *,
        include_offline: bool = False,
        lifecycle: AgentLifecycle | None = None,
    ) -> list[dict]:
        if not self.presence:
            raise self._presence_required("list agent registry state")
        if not self.connected:
            raise self._transport_unavailable()
        return self.registry.list_states(
            include_offline=include_offline,
            lifecycle=lifecycle,
        )

    async def get_state(self, agent_id: str) -> dict:
        if not self.presence:
            raise self._presence_required(
                f"read registry state for agent {agent_id!r}"
            )
        if not self.connected:
            raise self._transport_unavailable()
        try:
            return self.registry.get_state(agent_id)
        except KeyError as exc:
            raise ValueError(
                f"no registry record for agent {agent_id!r}"
            ) from exc

    async def update_state(
        self,
        *,
        status: str | None,
        working_set: list[str] | None,
        capabilities: list[str] | None = None,
    ) -> dict:
        if not self.presence:
            raise self._presence_required("publish its own registry state")
        if status is None and working_set is None and capabilities is None:
            raise ValueError(
                "update requires status, working_set, or capabilities"
            )
        declared = (
            self.declared_capabilities
            if capabilities is None
            else capabilities
        )
        candidate = RegistryRecord(
            agent_id=self.agent_id,
            status=self.status if status is None else status,
            working_set=self.working_set if working_set is None else working_set,
            capabilities=[*self.runtime_capabilities, *declared],
            lifecycle=self.lifecycle,
            durability="durable" if self.durable else "ephemeral",
            started_at=self.started_at,
            last_seen=datetime.now(timezone.utc),
        )
        self.status = candidate.status
        self.working_set = candidate.working_set
        runtime_capabilities = set(self.runtime_capabilities)
        self.declared_capabilities = [
            capability
            for capability in candidate.capabilities
            if capability not in runtime_capabilities
        ]
        await self._publish_registry()
        return self.registry.get_state(self.agent_id)

    async def _publish_presence(
        self,
        state: Literal["online", "offline"],
        *,
        reason: str | None = None,
    ) -> None:
        if self._client is None:
            raise self._transport_unavailable()
        topic = self.topics.presence(self.agent_id)
        payload = presence_payload(self.agent_id, state, reason=reason)
        await self._client.publish(
            topic,
            payload,
            qos=1,
            retain=True,
        )
        self.registry.update_presence(topic, payload)

    async def _publish_registry(self) -> None:
        if self._client is None:
            raise self._transport_unavailable()
        record = RegistryRecord(
            agent_id=self.agent_id,
            status=self.status,
            working_set=self.working_set,
            capabilities=[
                *self.runtime_capabilities,
                *self.declared_capabilities,
            ],
            lifecycle=self.lifecycle,
            durability="durable" if self.durable else "ephemeral",
            started_at=self.started_at,
            last_seen=datetime.now(timezone.utc),
        )
        topic = self.topics.registry(self.agent_id)
        payload = record.to_json()
        await self._client.publish(
            topic,
            payload,
            qos=1,
            retain=True,
        )
        self.registry.update_registry(topic, payload)

    async def _clear_transient_retained_state(self) -> None:
        if self._client is None:
            raise self._transport_unavailable()
        registry_topic = self.topics.registry(self.agent_id)
        presence_topic = self.topics.presence(self.agent_id)
        # Remove presence first so interruption leaves a GC-eligible registry
        # orphan rather than an uncollectable presence orphan.
        await self._client.publish(
            presence_topic,
            b"",
            qos=1,
            retain=True,
        )
        await self._client.publish(
            registry_topic,
            b"",
            qos=1,
            retain=True,
        )
        self.registry.remove_presence(presence_topic)
        self.registry.remove_registry(registry_topic)
        if self.durable:
            # Reclaiming the client id disconnects the tombstone publisher.
            await self._destroy_durable_session()

    async def _destroy_durable_session(self) -> None:
        """Destroy the broker session for a retired transient identity."""
        # A will could recreate the presence orphan just removed.
        kwargs = {
            key: value
            for key, value in self._client_kwargs().items()
            if key != "will"
        }
        kwargs["clean_session"] = True
        try:
            async with aiomqtt.Client(self.broker, port=self.port, **kwargs):
                pass
        except aiomqtt.MqttError as exc:
            logger.warning("failed to destroy durable session: %s", exc)
