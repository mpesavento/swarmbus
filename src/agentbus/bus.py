# src/agentbus/bus.py
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, List

import aiomqtt

from .message import AgentMessage, _validate_registered_agent_id
from .handlers.base import BaseHandler

logger = logging.getLogger(__name__)


class AgentBus:
    def __init__(
        self,
        agent_id: str,
        broker: str = "localhost",
        port: int = 1883,
        retain: bool = False,
    ) -> None:
        """Construct an AgentBus.

        retain: whether published inbox/broadcast messages are retained by the
            broker. Default **False** — retained inbox messages are redelivered
            to subscribers on every reconnect, which is almost never the
            desired semantic for directed messages. Presence publishes use
            retain=True unconditionally so late subscribers can see who is
            currently online.
        """
        _validate_registered_agent_id(agent_id)
        self.agent_id = agent_id
        self.broker = broker
        self.port = port
        self.retain = retain
        self._handlers: List[BaseHandler] = []
        # Persistent-client state (used when AgentBus is entered as an async
        # context manager, or when connect()/close() are called explicitly).
        # When None, send() falls back to opening a per-call client.
        self._client: aiomqtt.Client | None = None
        self._client_cm: Any = None

    def register_handler(self, handler: BaseHandler) -> None:
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
    ) -> None:
        """Publish a message.

        If a persistent client is open (via connect() or `async with`), it is
        reused — no per-call connection churn. Otherwise a one-shot client is
        opened just for this publish (fine for CLI/single-shot use, but avoid
        for tight loops).
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
            return
        async with aiomqtt.Client(self.broker, port=self.port) as client:
            await client.publish(topic, payload, qos=1, retain=self.retain)

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
        backoff = reconnect_initial
        while True:
            try:
                async with aiomqtt.Client(
                    self.broker, port=self.port, will=will
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
        """Non-blocking drain of queued retained messages for this agent.

        Returns a list of message dicts (up to `max_messages`). Malformed
        envelopes are skipped. Broker errors are logged and return [].
        """
        messages: list[dict] = []
        try:
            async with aiomqtt.Client(self.broker, port=self.port) as client:
                await client.subscribe(f"agents/{self.agent_id}/inbox", qos=1)
                try:
                    async with asyncio.timeout(drain_timeout):
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
        except aiomqtt.MqttError as exc:
            logger.error("read_inbox: broker error (%s:%d): %s", self.broker, self.port, exc)
        return messages

    async def watch_inbox(self, timeout: float = 30.0) -> dict | None:
        """Long-poll — blocks until a message arrives, returns it, or times out.

        Returns None on timeout or broker unavailability.
        """
        try:
            async with aiomqtt.Client(self.broker, port=self.port) as client:
                await client.subscribe(f"agents/{self.agent_id}/inbox", qos=1)
                try:
                    async with asyncio.timeout(timeout):
                        async for mqtt_msg in client.messages:
                            try:
                                msg = AgentMessage.from_json(mqtt_msg.payload)
                                return json.loads(msg.to_json())
                            except Exception as exc:
                                logger.warning("watch_inbox: skipping bad envelope: %s", exc)
                                continue
                except asyncio.TimeoutError:
                    return None
        except aiomqtt.MqttError as exc:
            logger.error("watch_inbox: broker error (%s:%d): %s", self.broker, self.port, exc)
        return None

    async def list_agents(self, collect_window: float = 0.5) -> list[str]:
        """Return sorted IDs of agents whose latest retained presence is online."""
        online: set[str] = set()
        try:
            async with aiomqtt.Client(self.broker, port=self.port) as client:
                await client.subscribe("agents/+/presence", qos=0)
                try:
                    async with asyncio.timeout(collect_window):
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
        except aiomqtt.MqttError as exc:
            logger.warning("list_agents: broker error: %s", exc)
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
