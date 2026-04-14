# src/agentbus/bus.py
from __future__ import annotations

import asyncio
import json
import logging
from typing import List

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

    def register_handler(self, handler: BaseHandler) -> None:
        self._handlers.append(handler)

    async def send(
        self,
        to: str,
        subject: str,
        body: str,
        content_type: str = "text/plain",
        priority: str = "normal",
        reply_to: str | None = None,
    ) -> None:
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
        async with aiomqtt.Client(self.broker, port=self.port) as client:
            await client.publish(
                topic,
                msg.to_json(),
                qos=1,
                retain=self.retain,
            )

    async def listen(self) -> None:
        # Retained presence so late subscribers see current state.
        # LWT retained too so an unexpected disconnect overwrites "online"
        # with "offline" — without retain, a crashed agent would leave
        # stale "online" state that never clears.
        will = aiomqtt.Will(
            topic=f"agents/{self.agent_id}/presence",
            payload=json.dumps({"agent": self.agent_id, "status": "offline"}),
            qos=1,
            retain=True,
        )
        async with aiomqtt.Client(self.broker, port=self.port, will=will) as client:
            await client.publish(
                f"agents/{self.agent_id}/presence",
                json.dumps({"agent": self.agent_id, "status": "online"}),
                qos=1,
                retain=True,
            )
            await client.subscribe(f"agents/{self.agent_id}/inbox", qos=1)
            await client.subscribe("agents/broadcast", qos=1)

            async for mqtt_msg in client.messages:
                try:
                    msg = AgentMessage.from_json(mqtt_msg.payload)
                except Exception as exc:
                    logger.warning("Discarding invalid message envelope: %s", exc)
                    continue

                for handler in self._handlers:
                    try:
                        await handler.handle(msg)
                    except Exception as exc:
                        logger.error(
                            "Handler %s raised: %s",
                            handler.__class__.__name__, exc,
                        )

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
