import asyncio
import logging
from datetime import datetime, timezone
from typing import Awaitable, Callable, Optional

from .base import BaseHandler
from ..message import AgentMessage

logger = logging.getLogger(__name__)


class PersistentListenerHandler(BaseHandler):
    """Heartbeat and stats handler for long-running always-on agents.

    Register this handler, then optionally start the heartbeat:
      asyncio.create_task(handler.start_heartbeat(bus_publish_presence_fn))
    """

    def __init__(self, heartbeat_interval: int = 60) -> None:
        self.heartbeat_interval = heartbeat_interval
        self._stats: dict = {
            "messages_received": 0,
            "last_message_ts": None,
            "started_at": None,
        }

    async def handle(self, msg: AgentMessage) -> None:
        self._stats["messages_received"] += 1
        self._stats["last_message_ts"] = datetime.now(timezone.utc).isoformat()

    async def start_heartbeat(
        self,
        publish_fn: Callable[[], Awaitable[None]],
    ) -> None:
        """Publish presence on interval. Run as asyncio.create_task(). Runs until cancelled."""
        self._stats["started_at"] = datetime.now(timezone.utc).isoformat()
        while True:
            await asyncio.sleep(self.heartbeat_interval)
            try:
                await publish_fn()
            except Exception as exc:
                logger.warning("Heartbeat publish failed: %s", exc)

    def stats(self) -> dict:
        return self._stats.copy()
