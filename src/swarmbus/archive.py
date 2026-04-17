from __future__ import annotations

from pathlib import Path
from typing import Optional

import aiosqlite

from .handlers.base import BaseHandler
from .message import AgentMessage

_CREATE_TABLE = """
CREATE TABLE IF NOT EXISTS messages (
    id          TEXT PRIMARY KEY,
    from_agent  TEXT NOT NULL,
    to_agent    TEXT NOT NULL,
    ts          TEXT NOT NULL,
    subject     TEXT NOT NULL,
    body        TEXT NOT NULL,
    content_type TEXT NOT NULL DEFAULT 'text/plain',
    priority    TEXT NOT NULL DEFAULT 'normal',
    reply_to    TEXT,
    direction   TEXT NOT NULL DEFAULT 'received',
    error       TEXT
)
"""


class SQLiteArchive(BaseHandler):
    """Logs all handled messages to a SQLite database."""

    def __init__(self, db_path: str) -> None:
        self.db_path = Path(db_path).expanduser()

    async def handle(self, msg: AgentMessage) -> None:
        """BaseHandler interface — archives with direction='received'."""
        await self.archive(msg, direction="received")

    async def archive(
        self,
        msg: AgentMessage,
        direction: str = "received",
        error: Optional[str] = None,
    ) -> None:
        """Store a message with explicit direction and optional error note."""
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(_CREATE_TABLE)
            await db.execute(
                """INSERT OR REPLACE INTO messages
                   (id, from_agent, to_agent, ts, subject, body,
                    content_type, priority, reply_to, direction, error)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    msg.id, msg.from_agent, msg.to,
                    msg.ts.isoformat(), msg.subject, msg.body,
                    msg.content_type, msg.priority, msg.reply_to,
                    direction, error,
                ),
            )
            await db.commit()
