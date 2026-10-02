from __future__ import annotations

import asyncio
import os
import stat
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import aiosqlite
from pydantic import ValidationError

from .handlers.base import BaseHandler
from .message import AgentMessage

_CREATE_ARCHIVE_TABLE = """
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

_CREATE_INBOX_TABLE = """
CREATE TABLE IF NOT EXISTS inbox_messages (
    id              TEXT PRIMARY KEY,
    from_agent      TEXT NOT NULL,
    to_agent        TEXT NOT NULL,
    ts              TEXT NOT NULL,
    subject         TEXT NOT NULL,
    body            TEXT NOT NULL,
    content_type    TEXT NOT NULL DEFAULT 'text/plain',
    priority        TEXT NOT NULL DEFAULT 'normal',
    reply_to        TEXT,
    received_at     TEXT NOT NULL,
    source_topic    TEXT NOT NULL,
    acknowledged_at TEXT
)
"""

_CREATE_SCHEMA_TABLE = """
CREATE TABLE IF NOT EXISTS swarmbus_schema (
    component TEXT PRIMARY KEY,
    version   INTEGER NOT NULL
)
"""

_CREATE_QUARANTINE_TABLE = """
CREATE TABLE IF NOT EXISTS inbox_message_quarantine (
    message_id     TEXT PRIMARY KEY REFERENCES inbox_messages(id),
    quarantined_at TEXT NOT NULL,
    error          TEXT NOT NULL
)
"""

_INBOX_MESSAGE_COLUMNS = """
id, from_agent, to_agent, ts, subject, body, content_type, priority, reply_to
"""


class MessageConflictError(RuntimeError):
    """A stable message ID was reused for a different envelope."""


class SQLiteMessageStore:
    """Durable inbound message store for the managed MCP runtime."""

    SCHEMA_COMPONENT = "inbox"
    SCHEMA_VERSION = 1
    _MANAGED_DIRECTORY_MODE = 0o700
    _MANAGED_DATABASE_MODE = 0o600

    def __init__(
        self,
        db_path: str | Path,
        *,
        managed: bool = True,
    ) -> None:
        self.db_path = Path(db_path).expanduser()
        self._managed = managed
        self._open_lock = asyncio.Lock()
        self._inbox_lock = asyncio.Lock()
        self._opened = False

    @staticmethod
    def _require_mode(path: Path, expected: int, label: str) -> None:
        if path.is_symlink():
            raise PermissionError(f"managed {label} must not be a symlink: {path}")
        path_stat = path.stat()
        if label == "directory" and not stat.S_ISDIR(path_stat.st_mode):
            raise PermissionError(f"managed state directory is not a directory: {path}")
        if label == "database" and not stat.S_ISREG(path_stat.st_mode):
            raise PermissionError(f"managed database is not a regular file: {path}")
        actual = stat.S_IMODE(path_stat.st_mode)
        if actual != expected:
            raise PermissionError(
                f"managed {label} {path} has mode {oct(actual)}; "
                f"required {oct(expected)}"
            )

    def _prepare_path(self) -> None:
        parent = self.db_path.parent
        if not self._managed:
            parent.mkdir(parents=True, exist_ok=True)
            return

        if parent.exists() or parent.is_symlink():
            self._require_mode(
                parent,
                self._MANAGED_DIRECTORY_MODE,
                "directory",
            )
        else:
            parent.mkdir(
                mode=self._MANAGED_DIRECTORY_MODE,
                parents=True,
            )
            os.chmod(parent, self._MANAGED_DIRECTORY_MODE)
            self._require_mode(
                parent,
                self._MANAGED_DIRECTORY_MODE,
                "directory",
            )

        if self.db_path.exists() or self.db_path.is_symlink():
            self._require_mode(
                self.db_path,
                self._MANAGED_DATABASE_MODE,
                "database",
            )
            return

        flags = os.O_CREAT | os.O_EXCL | os.O_RDWR
        if hasattr(os, "O_CLOEXEC"):
            flags |= os.O_CLOEXEC
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            fd = os.open(self.db_path, flags, self._MANAGED_DATABASE_MODE)
        except FileExistsError:
            self._require_mode(
                self.db_path,
                self._MANAGED_DATABASE_MODE,
                "database",
            )
        else:
            try:
                os.fchmod(fd, self._MANAGED_DATABASE_MODE)
            finally:
                os.close(fd)

    async def open(self) -> None:
        async with self._open_lock:
            if self._opened:
                return
            self._prepare_path()
            async with aiosqlite.connect(self.db_path) as db:
                await db.execute("BEGIN IMMEDIATE")
                committed = False
                try:
                    await db.execute(_CREATE_SCHEMA_TABLE)
                    async with db.execute(
                        """SELECT version FROM swarmbus_schema
                           WHERE component = ?""",
                        (self.SCHEMA_COMPONENT,),
                    ) as cursor:
                        row = await cursor.fetchone()
                    schema_version = row[0] if row is not None else None
                    if (
                        schema_version is not None
                        and schema_version > self.SCHEMA_VERSION
                    ):
                        raise RuntimeError(
                            f"{self.SCHEMA_COMPONENT} schema {schema_version} "
                            f"is newer than supported version "
                            f"{self.SCHEMA_VERSION}"
                        )

                    await db.execute(_CREATE_INBOX_TABLE)
                    await db.execute(_CREATE_QUARANTINE_TABLE)
                    if schema_version is None:
                        await db.execute(
                            """INSERT INTO swarmbus_schema
                               (component, version) VALUES (?, ?)""",
                            (
                                self.SCHEMA_COMPONENT,
                                self.SCHEMA_VERSION,
                            ),
                        )
                    await db.commit()
                    committed = True
                finally:
                    if not committed:
                        await db.rollback()
            self._opened = True

    async def close(self) -> None:
        self._opened = False

    async def _ensure_open(self) -> None:
        if not self._opened:
            await self.open()

    async def store(
        self,
        msg: AgentMessage,
        *,
        source_topic: str,
        received_at: datetime | None = None,
    ) -> bool:
        """Commit one inbound message, returning whether it was newly inserted."""
        await self._ensure_open()
        received = received_at or datetime.now(timezone.utc)
        envelope = (
            msg.id,
            msg.from_agent,
            msg.to,
            msg.ts.isoformat(),
            msg.subject,
            msg.body,
            msg.content_type,
            msg.priority,
            msg.reply_to,
        )
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(
                """INSERT INTO inbox_messages
                   (id, from_agent, to_agent, ts, subject, body,
                    content_type, priority, reply_to, received_at,
                    source_topic)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(id) DO NOTHING""",
                (*envelope, received.isoformat(), source_topic),
            )
            if cursor.rowcount == 1:
                await db.commit()
                return True

            async with db.execute(
                f"""SELECT {_INBOX_MESSAGE_COLUMNS}
                    FROM inbox_messages
                    WHERE id = ?""",
                (msg.id,),
            ) as existing_cursor:
                existing = await existing_cursor.fetchone()
            if existing is None or tuple(existing) != envelope:
                await db.rollback()
                raise MessageConflictError(
                    f"message ID {msg.id!r} is already bound to "
                    "a different envelope"
                )

            await db.commit()
            return False

    @staticmethod
    def _deserialize_row(row: aiosqlite.Row) -> dict:
        message = AgentMessage(
            id=row["id"],
            **{
                "from": row["from_agent"],
                "to": row["to_agent"],
                "ts": row["ts"],
                "subject": row["subject"],
                "body": row["body"],
                "content_type": row["content_type"],
                "priority": row["priority"],
                "reply_to": row["reply_to"],
            },
        )
        return message.model_dump(by_alias=True, mode="json")

    @staticmethod
    def _quarantine_error(exc: ValidationError) -> str:
        details = []
        for error in exc.errors(
            include_url=False,
            include_context=False,
            include_input=False,
        ):
            location = ".".join(str(part) for part in error["loc"])
            details.append(f"{location}: {error['msg']}")
        return "; ".join(details)

    async def read(self, max_messages: int = 10) -> list[dict]:
        """Read valid unacknowledged rows and quarantine malformed rows."""
        if max_messages < 1:
            raise ValueError("max_messages must be at least 1")
        await self._ensure_open()
        messages: list[dict] = []
        async with self._inbox_lock:
            async with aiosqlite.connect(self.db_path) as db:
                db.row_factory = aiosqlite.Row
                await db.execute("BEGIN IMMEDIATE")
                committed = False
                try:
                    last_rowid = 0
                    while len(messages) < max_messages:
                        remaining = max_messages - len(messages)
                        async with db.execute(
                            f"""SELECT rowid AS inbox_rowid,
                                       {_INBOX_MESSAGE_COLUMNS}
                                FROM inbox_messages
                                WHERE rowid > ?
                                  AND acknowledged_at IS NULL
                                  AND NOT EXISTS (
                                      SELECT 1
                                      FROM inbox_message_quarantine
                                      WHERE message_id = inbox_messages.id
                                  )
                                ORDER BY rowid
                                LIMIT ?""",
                            (last_rowid, remaining),
                        ) as cursor:
                            rows = await cursor.fetchall()
                        if not rows:
                            break
                        last_rowid = rows[-1]["inbox_rowid"]

                        quarantined = []
                        quarantine_time = datetime.now(timezone.utc).isoformat()
                        for row in rows:
                            try:
                                message = self._deserialize_row(row)
                            except ValidationError as exc:
                                quarantined.append(
                                    (
                                        quarantine_time,
                                        self._quarantine_error(exc),
                                        row["id"],
                                    )
                                )
                            else:
                                messages.append(message)

                        if quarantined:
                            await db.executemany(
                                """INSERT INTO inbox_message_quarantine
                                   (quarantined_at, error, message_id)
                                   VALUES (?, ?, ?)
                                   ON CONFLICT(message_id) DO NOTHING""",
                                quarantined,
                            )
                    await db.commit()
                    committed = True
                finally:
                    if not committed:
                        await db.rollback()
        return messages

    async def ack(self, message_ids: list[str]) -> int:
        """Acknowledge messages after the caller has durably handled them."""
        unique_ids: list[str] = []
        seen: set[str] = set()
        for message_id in message_ids:
            if not isinstance(message_id, str) or not message_id:
                raise ValueError("message_ids must contain non-empty strings")
            if message_id not in seen:
                seen.add(message_id)
                unique_ids.append(message_id)
        if not unique_ids:
            return 0

        await self._ensure_open()
        acknowledged_at = datetime.now(timezone.utc).isoformat()
        acknowledged = 0
        async with self._inbox_lock:
            async with aiosqlite.connect(self.db_path) as db:
                await db.execute("BEGIN IMMEDIATE")
                committed = False
                try:
                    for message_id in unique_ids:
                        cursor = await db.execute(
                            """UPDATE inbox_messages
                               SET acknowledged_at = ?
                               WHERE id = ?
                                 AND acknowledged_at IS NULL
                                 AND NOT EXISTS (
                                     SELECT 1
                                     FROM inbox_message_quarantine
                                     WHERE message_id = inbox_messages.id
                                 )""",
                            (acknowledged_at, message_id),
                        )
                        acknowledged += cursor.rowcount
                    await db.commit()
                    committed = True
                finally:
                    if not committed:
                        await db.rollback()
        return acknowledged


class SQLiteArchive(BaseHandler):
    """Compatibility archive with the released table and replacement behavior."""

    def __init__(self, db_path: str) -> None:
        self.db_path = Path(db_path).expanduser()

    async def handle(self, msg: AgentMessage) -> None:
        await self.archive(msg, direction="received")

    async def archive(
        self,
        msg: AgentMessage,
        direction: str = "received",
        error: Optional[str] = None,
    ) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(_CREATE_ARCHIVE_TABLE)
            await db.execute(
                """INSERT OR REPLACE INTO messages
                   (id, from_agent, to_agent, ts, subject, body,
                    content_type, priority, reply_to, direction, error)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    msg.id,
                    msg.from_agent,
                    msg.to,
                    msg.ts.isoformat(),
                    msg.subject,
                    msg.body,
                    msg.content_type,
                    msg.priority,
                    msg.reply_to,
                    direction,
                    error,
                ),
            )
            await db.commit()
