# src/swarmbus/bus.py
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, List

import aiomqtt

from .message import AgentMessage, _validate_registered_agent_id
from .handlers.base import BaseHandler
from ._compat import asyncio_timeout

logger = logging.getLogger(__name__)


def _append_outbox_entry(path: str, msg: AgentMessage) -> None:
    """Append a sent message to the sender's outbox log.

    Mirrors FileBridgeHandler's inbox format so an agent's outbound and
    inbound logs are structurally identical (same heading shape, swapped
    direction). Failures to write are logged but never raise — archive
    loss should not break message delivery.
    """
    from pathlib import Path
    p = Path(path).expanduser()
    entry = (
        f"\n## [{msg.ts.strftime('%Y-%m-%d %H:%M')}] "
        f"To: {msg.to} | {msg.subject}\n"
        f"{msg.body}\n"
    )
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("a", encoding="utf-8") as f:
            f.write(entry)
    except OSError as exc:
        logger.warning("outbox append to %s failed: %s", p, exc)


class AgentBus:
    def __init__(
        self,
        agent_id: str,
        broker: str = "localhost",
        port: int = 1883,
        retain: bool = False,
        persistent: bool = False,
    ) -> None:
        """Construct an AgentBus.

        retain: whether published inbox/broadcast messages are retained by the
            broker. Default **False** — retained inbox messages are redelivered
            to subscribers on every reconnect, which is almost never the
            desired semantic for directed messages. Presence publishes use
            retain=True unconditionally so late subscribers can see who is
            currently online.

        persistent: whether `listen()` uses an MQTT persistent session. When
            True, the broker queues QoS1 messages for this agent-id even when
            the listener is disconnected, and redelivers them on reconnect —
            a crashed or restarted daemon does not lose messages. Comes with
            two tradeoffs: only one client can be connected with this session's
            identifier at a time (a second connection kicks the first), and
            on reconnect the broker may redeliver in-flight messages whose
            PUBACK was lost — handlers must be idempotent (this is the standard
            QoS1 "at least once" contract). Default False for backward
            compatibility; daemons should set True.
        """
        _validate_registered_agent_id(agent_id)
        self.agent_id = agent_id
        self.broker = broker
        self.port = port
        self.retain = retain
        self.persistent = persistent
        self._handlers: List[BaseHandler] = []
        # Persistent-client state (used when AgentBus is entered as an async
        # context manager, or when connect()/close() are called explicitly).
        # When None, send() falls back to opening a per-call client.
        self._client: aiomqtt.Client | None = None
        self._client_cm: Any = None

    @classmethod
    def probe(cls, broker: str = "localhost", port: int = 1883) -> "AgentBus":
        """Construct a broker-only instance for operations that don't need a
        registered agent identity (e.g. `list_agents`).

        The returned bus bypasses agent-id validation; it never publishes
        presence, never subscribes to an inbox topic, and must not be used
        to send. Use only for presence/discovery queries.
        """
        self = cls.__new__(cls)
        self.agent_id = "_probe"
        self.broker = broker
        self.port = port
        self.retain = False
        self.persistent = False
        self._handlers = []
        self._client = None
        self._client_cm = None
        return self

    def register_handler(self, handler: BaseHandler) -> None:
        """Register a handler to run on every incoming message.

        Dispatch semantics (important — the spec did not nail these down,
        so they are documented here as the canonical contract):

        - **Order:** handlers run in the order they were registered.
        - **Serialisation:** handlers run sequentially, not in parallel.
          A slow handler blocks the next. If you need concurrency,
          handle it inside the handler's own `async def handle`.
        - **Exception isolation:** if a handler raises, the exception is
          caught and logged, and subsequent handlers still run. The
          listen loop is never taken down by a bad handler.

        These guarantees are tested in `tests/test_bus.py`.
        """
        self._handlers.append(handler)

    async def connect(self) -> None:
        """Open a persistent MQTT connection for send() calls.

        Idempotent. Pair with close() or use via `async with`. The listen()
        path always manages its own connection (so it can reconnect) and does
        not use this persistent client.
        """
        if self._client is not None:
            return
        self._client_cm = aiomqtt.Client(self.broker, port=self.port)
        self._client = await self._client_cm.__aenter__()

    async def close(self) -> None:
        """Close the persistent MQTT connection if open. Idempotent."""
        if self._client_cm is None:
            return
        cm = self._client_cm
        self._client = None
        self._client_cm = None
        await cm.__aexit__(None, None, None)

    async def __aenter__(self) -> "AgentBus":
        await self.connect()
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self.close()

    async def send(
        self,
        to: str,
        subject: str,
        body: str,
        content_type: str = "text/plain",
        priority: str = "normal",
        reply_to: str | None = None,
        outbox_path: str | None = None,
    ) -> None:
        """Publish a message.

        If a persistent client is open (via connect() or `async with`), it is
        reused — no per-call connection churn. Otherwise a one-shot client is
        opened just for this publish (fine for CLI/single-shot use, but avoid
        for tight loops).

        If `outbox_path` is set, the message is appended to that file after a
        successful publish. Format mirrors `FileBridgeHandler` but with `To:`
        so an agent's send-log and receive-log stay structurally identical.
        The path may contain `{agent_id}` as a placeholder — it expands to
        `self.agent_id` at call time. This lets multiple agents in the same
        process space safely share one outbox-path template:
        `outbox_path="~/sync/{agent_id}-outbox.md"`.
        """
        msg = AgentMessage.create(
            from_=self.agent_id,
            to=to,
            subject=subject,
            body=body,
            content_type=content_type,
            priority=priority,  # type: ignore[arg-type]
            reply_to=reply_to,
        )
        # Broadcast uses agents/broadcast; directed messages use agents/{to}/inbox
        topic = "agents/broadcast" if to == "broadcast" else f"agents/{to}/inbox"
        payload = msg.to_json()
        if self._client is not None:
            await self._client.publish(topic, payload, qos=1, retain=self.retain)
        else:
            async with aiomqtt.Client(self.broker, port=self.port) as client:
                await client.publish(topic, payload, qos=1, retain=self.retain)

        if outbox_path:
            resolved = outbox_path.replace("{agent_id}", self.agent_id)
            await asyncio.get_running_loop().run_in_executor(
                None, _append_outbox_entry, resolved, msg
            )

    async def listen(
        self,
        reconnect_initial: float = 1.0,
        reconnect_max: float = 60.0,
    ) -> None:
        """Subscribe to inbox/broadcast and dispatch messages to handlers.

        On broker disconnect (MqttError) the loop reconnects with exponential
        backoff (reconnect_initial → reconnect_max seconds). Normal exit — e.g.
        the message iterator exhausting in tests, or asyncio.CancelledError
        from the caller — returns cleanly without retry.

        Retained presence: both the online announce and the LWT offline use
        qos=1, retain=True so late subscribers see current state and stale
        "online" is overwritten when an agent crashes unexpectedly.
        """
        will = aiomqtt.Will(
            topic=f"agents/{self.agent_id}/presence",
            payload=json.dumps({"agent": self.agent_id, "status": "offline"}),
            qos=1,
            retain=True,
        )
        # Persistent session: stable client identifier + clean_session=False
        # so the broker queues QoS1 messages for this agent when the listener
        # is offline, and redelivers them on reconnect.
        client_kwargs: dict[str, Any] = {"will": will}
        if self.persistent:
            client_kwargs["identifier"] = f"swarmbus-{self.agent_id}"
            client_kwargs["clean_session"] = False
        backoff = reconnect_initial
        while True:
            try:
                async with aiomqtt.Client(
                    self.broker, port=self.port, **client_kwargs
                ) as client:
                    await client.publish(
                        f"agents/{self.agent_id}/presence",
                        json.dumps({"agent": self.agent_id, "status": "online"}),
                        qos=1,
                        retain=True,
                    )
                    await client.subscribe(f"agents/{self.agent_id}/inbox", qos=1)
                    await client.subscribe("agents/broadcast", qos=1)
                    backoff = reconnect_initial  # reset after successful (re)connect

                    async for mqtt_msg in client.messages:
                        try:
                            msg = AgentMessage.from_json(mqtt_msg.payload)
                        except Exception as exc:
                            logger.warning(
                                "Discarding invalid message envelope: %s", exc
                            )
                            continue

                        for handler in self._handlers:
                            try:
                                await handler.handle(msg)
                            except Exception as exc:
                                logger.error(
                                    "Handler %s raised: %s",
                                    handler.__class__.__name__, exc,
                                )
                return  # iterator exhausted / clean shutdown
            except aiomqtt.MqttError as exc:
                logger.warning(
                    "MQTT broker disconnected (%s); reconnecting in %.1fs",
                    exc, backoff,
                )
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, reconnect_max)

    async def read_inbox(self, max_messages: int = 10, drain_timeout: float = 1.0) -> list[dict]:
        """Non-blocking drain of **retained** messages for this agent.

        Opens a fresh non-persistent MQTT session per call. That means only
        messages sent with `retain=True` are visible — ordinary directed
        sends (our default, `retain=False`) that arrived while no subscriber
        was connected are already gone. For durable delivery of non-retained
        sends, keep a listener daemon up: `swarmbus start --agent-id <me>`.

        Returns a list of message dicts (up to `max_messages`). Malformed
        envelopes are skipped. Raises `aiomqtt.MqttError` if the broker is
        unreachable — callers that want graceful empty-on-error behaviour
        (e.g. the MCP tool surface) must catch it themselves.
        """
        messages: list[dict] = []
        async with aiomqtt.Client(self.broker, port=self.port) as client:
            await client.subscribe(f"agents/{self.agent_id}/inbox", qos=1)
            try:
                async with asyncio_timeout(drain_timeout):
                    async for mqtt_msg in client.messages:
                        try:
                            msg = AgentMessage.from_json(mqtt_msg.payload)
                            messages.append(json.loads(msg.to_json()))
                        except Exception as exc:
                            logger.warning("read_inbox: skipping bad envelope: %s", exc)
                        if len(messages) >= max_messages:
                            break
            except asyncio.TimeoutError:
                pass
        return messages

    async def watch_inbox(self, timeout: float = 30.0) -> dict | None:
        """Long-poll — blocks until a message arrives, returns it, or times out.

        Opens a fresh non-persistent MQTT session. Only catches messages
        **published while this call is active**, plus any with `retain=True`
        on subscribe. If a durable listener daemon is already running for
        this agent-id, it will race with you for the same message — use one
        or the other, not both, for the same id.

        Returns None on timeout. Raises `aiomqtt.MqttError` if the broker is
        unreachable; callers that want graceful None-on-error (e.g. the MCP
        tool surface) must catch it themselves.
        """
        async with aiomqtt.Client(self.broker, port=self.port) as client:
            await client.subscribe(f"agents/{self.agent_id}/inbox", qos=1)
            try:
                async with asyncio_timeout(timeout):
                    async for mqtt_msg in client.messages:
                        try:
                            msg = AgentMessage.from_json(mqtt_msg.payload)
                            return json.loads(msg.to_json())
                        except Exception as exc:
                            logger.warning("watch_inbox: skipping bad envelope: %s", exc)
                            continue
            except asyncio.TimeoutError:
                return None
        return None

    async def list_agents(self, collect_window: float = 0.5) -> list[str]:
        """Return sorted IDs of agents whose latest retained presence is online.

        Raises `aiomqtt.MqttError` on broker failure; callers that want the
        graceful empty-list fallback must catch it.
        """
        online: set[str] = set()
        async with aiomqtt.Client(self.broker, port=self.port) as client:
            await client.subscribe("agents/+/presence", qos=0)
            try:
                async with asyncio_timeout(collect_window):
                    async for mqtt_msg in client.messages:
                        try:
                            payload = json.loads(mqtt_msg.payload)
                        except (json.JSONDecodeError, TypeError, ValueError):
                            continue
                        name = payload.get("agent")
                        status = payload.get("status")
                        if not name:
                            continue
                        if status == "online":
                            online.add(name)
                        else:
                            online.discard(name)
            except asyncio.TimeoutError:
                pass
        return sorted(online)

    async def disconnect(self) -> None:
        """Publish offline presence (retained). Call before process exit if
        not using listen()."""
        async with aiomqtt.Client(self.broker, port=self.port) as client:
            await client.publish(
                f"agents/{self.agent_id}/presence",
                json.dumps({"agent": self.agent_id, "status": "offline"}),
                qos=1,
                retain=True,
            )

    def run(self) -> None:
        """Sync entry point — blocks until listen() returns or KeyboardInterrupt."""
        asyncio.run(self.listen())
