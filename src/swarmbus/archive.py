from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import os
import stat
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import AsyncIterator, Optional

import aiosqlite
from pydantic import ValidationError

from .handlers.base import BaseHandler
from .message import DEFAULT_HOP_LIMIT, AgentMessage, sanitize_field

logger = logging.getLogger(__name__)

_SQLITE_MAX_INTEGER = 2**63 - 1

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

_CREATE_CONFLICTS_TABLE = """
CREATE TABLE IF NOT EXISTS inbox_message_conflicts (
    message_id   TEXT NOT NULL,       -- the id already bound to another envelope
    from_agent   TEXT NOT NULL,
    source_topic TEXT NOT NULL,
    recorded_at  TEXT NOT NULL,
    envelope     TEXT NOT NULL        -- msg.to_json() of the rejected message
)
"""

_CREATE_HELD_TABLE = """
CREATE TABLE IF NOT EXISTS held_messages (
    id           TEXT PRIMARY KEY,
    from_agent   TEXT NOT NULL,
    hops         INTEGER NOT NULL,
    reason       TEXT NOT NULL,      -- 'hop-limit' | 'sender-tripped'
    source_topic TEXT NOT NULL,
    held_at      TEXT NOT NULL,
    envelope     TEXT NOT NULL,      -- msg.to_json(), or a stub without the body
    stub         INTEGER NOT NULL DEFAULT 0
)
"""

_CREATE_TRIPS_TABLE = """
CREATE TABLE IF NOT EXISTS hop_trips (
    sender     TEXT PRIMARY KEY,
    tripped_at TEXT NOT NULL,
    message_id TEXT NOT NULL         -- the message that tripped it
)
"""

_CREATE_ADMITTED_TABLE = """
CREATE TABLE IF NOT EXISTS admitted_messages (
    id         TEXT PRIMARY KEY,
    from_agent TEXT NOT NULL,
    hops       INTEGER NOT NULL,
    origin     TEXT,
    seen_at    TEXT NOT NULL,
    envelope_sha256 TEXT             -- NULL only on rows migrated from v1
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
    HOLDS_SCHEMA_COMPONENT = "holds"
    HOLDS_SCHEMA_VERSION = 2
    _MANAGED_DIRECTORY_MODE = 0o700
    _MANAGED_DATABASE_MODE = 0o600
    _BUSY_TIMEOUT_SECONDS = 30.0

    ADMITTED_RETENTION_DAYS = 30
    PRUNE_EVERY_ADMISSIONS = 1000
    HELD_FULL_ENVELOPES_PER_SENDER = 50
    HELD_MAX_ROWS = 10_000
    CONFLICTS_MAX_ROWS = 200
    STUB_SUBJECT_CHARS = 200
    _STUB_KEYS = (
        "id", "from", "to", "ts", "subject", "hops", "origin", "in_reply_to",
    )

    def __init__(
        self,
        db_path: str | Path,
        *,
        managed: bool = True,
        hop_limit: int = DEFAULT_HOP_LIMIT,
    ) -> None:
        if hop_limit < 0:
            raise ValueError("hop_limit must not be negative")
        self.hop_limit = hop_limit
        self.db_path = Path(db_path).expanduser()
        self._managed = managed
        self._open_lock = asyncio.Lock()
        self._inbox_lock = asyncio.Lock()
        self._opened = False
        self._admissions_since_prune = 0

    @contextlib.asynccontextmanager
    async def _connect(self) -> AsyncIterator[aiosqlite.Connection]:
        async with aiosqlite.connect(
            self.db_path, timeout=self._BUSY_TIMEOUT_SECONDS
        ) as db:
            await db.execute("PRAGMA synchronous=FULL")
            yield db

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
            self._admissions_since_prune = 0
            async with self._connect() as db:
                # The journal mode cannot change inside a transaction.
                async with db.execute("PRAGMA journal_mode=WAL") as cursor:
                    mode_row = await cursor.fetchone()
                mode = str(mode_row[0]).lower() if mode_row else ""
                if mode != "wal":
                    logger.warning(
                        "state db %s did not switch to WAL mode (journal mode "
                        "is %r); readers may block the listener",
                        self.db_path,
                        mode,
                    )
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
                    await db.execute(_CREATE_CONFLICTS_TABLE)
                    async with db.execute(
                        """SELECT 1 FROM sqlite_master
                           WHERE type = 'index'
                             AND name = 'inbox_message_conflicts_once'"""
                    ) as cursor:
                        has_conflict_key = await cursor.fetchone() is not None
                    if not has_conflict_key:
                        await db.execute(
                            """DELETE FROM inbox_message_conflicts
                               WHERE rowid NOT IN (
                                   SELECT MIN(rowid)
                                   FROM inbox_message_conflicts
                                   GROUP BY message_id, envelope
                               )"""
                        )
                        await db.execute(
                            """CREATE UNIQUE INDEX inbox_message_conflicts_once
                               ON inbox_message_conflicts(message_id, envelope)"""
                        )
                    await self._cap_conflicts(db)

                    async with db.execute(
                        """SELECT version FROM swarmbus_schema
                           WHERE component = ?""",
                        (self.HOLDS_SCHEMA_COMPONENT,),
                    ) as cursor:
                        holds_row = await cursor.fetchone()
                    holds_version = holds_row[0] if holds_row is not None else None
                    if (
                        holds_version is not None
                        and holds_version > self.HOLDS_SCHEMA_VERSION
                    ):
                        raise RuntimeError(
                            f"holds schema {holds_version} is newer than "
                            f"supported version {self.HOLDS_SCHEMA_VERSION}"
                        )
                    await db.execute(_CREATE_HELD_TABLE)
                    await db.execute(_CREATE_TRIPS_TABLE)
                    await db.execute(_CREATE_ADMITTED_TABLE)
                    if holds_version == 1:
                        await self._migrate_holds_v1(db)
                    await db.execute(
                        """CREATE INDEX IF NOT EXISTS admitted_messages_seen_at
                           ON admitted_messages(seen_at)"""
                    )
                    await db.execute(
                        """CREATE INDEX IF NOT EXISTS held_messages_sender
                           ON held_messages(from_agent, stub)"""
                    )
                    await self._prune_admitted(db)
                    if holds_version is None:
                        await db.execute(
                            """INSERT INTO swarmbus_schema
                               (component, version) VALUES (?, ?)""",
                            (
                                self.HOLDS_SCHEMA_COMPONENT,
                                self.HOLDS_SCHEMA_VERSION,
                            ),
                        )
                    elif holds_version == 1:
                        await db.execute(
                            """UPDATE swarmbus_schema SET version = ?
                               WHERE component = ?""",
                            (
                                self.HOLDS_SCHEMA_VERSION,
                                self.HOLDS_SCHEMA_COMPONENT,
                            ),
                        )
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

    @staticmethod
    async def _columns(db: aiosqlite.Connection, table: str) -> set[str]:
        async with db.execute(f"PRAGMA table_info({table})") as cursor:
            return {r[1] for r in await cursor.fetchall()}

    async def _migrate_holds_v1(self, db: aiosqlite.Connection) -> None:
        if "envelope_sha256" not in await self._columns(db, "admitted_messages"):
            await db.execute(
                "ALTER TABLE admitted_messages ADD COLUMN envelope_sha256 TEXT"
            )
        if "stub" not in await self._columns(db, "held_messages"):
            await db.execute(
                "ALTER TABLE held_messages "
                "ADD COLUMN stub INTEGER NOT NULL DEFAULT 0"
            )

    async def _prune_admitted(self, db: aiosqlite.Connection) -> None:
        cutoff = (
            datetime.now(timezone.utc)
            - timedelta(days=self.ADMITTED_RETENTION_DAYS)
        ).isoformat()
        await db.execute(
            "DELETE FROM admitted_messages WHERE seen_at < ?", (cutoff,)
        )

    async def _cap_conflicts(self, db: aiosqlite.Connection) -> None:
        await db.execute(
            """DELETE FROM inbox_message_conflicts
               WHERE rowid NOT IN (
                   SELECT rowid FROM inbox_message_conflicts
                   ORDER BY recorded_at DESC, rowid DESC
                   LIMIT ?
               )""",
            (self.CONFLICTS_MAX_ROWS,),
        )

    async def _cap_held(self, db: aiosqlite.Connection) -> None:
        """Drop the oldest unprotected held rows past HELD_MAX_ROWS.

        A row is protected when it is the message that tripped a sender.
        """
        async with db.execute(
            """SELECT COUNT(*) FROM held_messages
               WHERE id NOT IN (SELECT message_id FROM hop_trips)"""
        ) as cursor:
            unprotected = (await cursor.fetchone())[0]
        excess = unprotected - self.HELD_MAX_ROWS
        if excess > 0:
            await db.execute(
                """DELETE FROM held_messages WHERE rowid IN (
                       SELECT rowid FROM held_messages
                       WHERE id NOT IN (SELECT message_id FROM hop_trips)
                       ORDER BY held_at, rowid
                       LIMIT ?
                   )""",
                (excess,),
            )

    async def close(self) -> None:
        self._opened = False

    async def _ensure_open(self) -> None:
        if not self._opened:
            await self.open()

    async def _admit_in(
        self,
        db: aiosqlite.Connection,
        msg: AgentMessage,
        *,
        source_topic: str,
    ) -> bool:
        """Decide admission on a connection whose transaction is already open."""
        now = datetime.now(timezone.utc).isoformat()
        hops = min(msg.hops, _SQLITE_MAX_INTEGER)
        sha = hashlib.sha256(msg.to_json().encode("utf-8")).hexdigest()

        self._admissions_since_prune += 1
        if self._admissions_since_prune >= self.PRUNE_EVERY_ADMISSIONS:
            await self._prune_admitted(db)
            self._admissions_since_prune = 0

        async with db.execute(
            "SELECT 1 FROM held_messages WHERE id = ?", (msg.id,)
        ) as cursor:
            if await cursor.fetchone() is not None:
                return False

        async with db.execute(
            "SELECT 1 FROM hop_trips WHERE sender = ?", (msg.from_agent,)
        ) as cursor:
            tripped = await cursor.fetchone() is not None
        if tripped:
            await self._hold_tripped_sender_message(
                db, msg, hops=hops, source_topic=source_topic, now=now
            )
            await self._cap_held(db)
            logger.debug(
                "holding message %s from tripped sender %s",
                sanitize_field(msg.id),
                sanitize_field(msg.from_agent),
            )
            return False

        async with db.execute(
            """SELECT from_agent, hops, envelope_sha256
               FROM admitted_messages WHERE id = ?""",
            (msg.id,),
        ) as cursor:
            admitted_row = await cursor.fetchone()
        if admitted_row is not None:
            stored_from, stored_hops, stored_sha = admitted_row
            if stored_sha is None:
                # Migrated from holds v1: no hash, so compare what was kept.
                redelivery = stored_from == msg.from_agent and stored_hops == hops
            else:
                redelivery = stored_sha == sha
            if redelivery:
                return True
            if await self._record_conflict_in(
                db, msg, source_topic=source_topic, now=now
            ):
                logger.warning(
                    "discarding message %s from %s: its id is already bound "
                    "to a different envelope; recorded in "
                    "inbox_message_conflicts",
                    sanitize_field(msg.id),
                    sanitize_field(msg.from_agent),
                )
            return False

        if msg.hops > self.hop_limit:
            await db.execute(
                """INSERT INTO held_messages
                   (id, from_agent, hops, reason, source_topic, held_at,
                    envelope, stub)
                   VALUES (?, ?, ?, ?, ?, ?, ?, 0)""",
                (
                    msg.id,
                    msg.from_agent,
                    hops,
                    "hop-limit",
                    source_topic,
                    now,
                    msg.to_json(),
                ),
            )
            await db.execute(
                """INSERT INTO hop_trips (sender, tripped_at, message_id)
                   VALUES (?, ?, ?)""",
                (msg.from_agent, now, msg.id),
            )
            await self._cap_held(db)
            sender = sanitize_field(msg.from_agent)
            logger.error(
                "hop limit %s exceeded: holding message %s from %s "
                "(hops=%s, origin=%s); every message from %s is held until "
                "a person runs: swarmbus held --agent-id <this agent> "
                "--reset %s",
                self.hop_limit,
                sanitize_field(msg.id),
                sender,
                msg.hops,
                sanitize_field(msg.origin),
                sender,
                sender,
            )
            return False

        await db.execute(
            """INSERT INTO admitted_messages
               (id, from_agent, hops, origin, seen_at, envelope_sha256)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (msg.id, msg.from_agent, hops, msg.origin, now, sha),
        )
        return True

    async def _hold_tripped_sender_message(
        self,
        db: aiosqlite.Connection,
        msg: AgentMessage,
        *,
        hops: int,
        source_topic: str,
        now: str,
    ) -> None:
        async with db.execute(
            """SELECT COUNT(*) FROM held_messages
               WHERE from_agent = ? AND reason = 'sender-tripped'
                 AND stub = 0""",
            (msg.from_agent,),
        ) as cursor:
            full = (await cursor.fetchone())[0]
        if full < self.HELD_FULL_ENVELOPES_PER_SENDER:
            stub = 0
            envelope = msg.to_json()
        else:
            stub = 1
            data = json.loads(msg.to_json())
            kept = {k: data[k] for k in self._STUB_KEYS if k in data}
            kept["subject"] = str(kept.get("subject", ""))[
                : self.STUB_SUBJECT_CHARS
            ]
            kept["stub"] = True
            envelope = json.dumps(kept)
        await db.execute(
            """INSERT INTO held_messages
               (id, from_agent, hops, reason, source_topic, held_at,
                envelope, stub)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                msg.id,
                msg.from_agent,
                hops,
                "sender-tripped",
                source_topic,
                now,
                envelope,
                stub,
            ),
        )

    async def _record_conflict_in(
        self,
        db: aiosqlite.Connection,
        msg: AgentMessage,
        *,
        source_topic: str,
        now: str,
    ) -> bool:
        """Record a rejected envelope once; return whether a row was added."""
        cursor = await db.execute(
            """INSERT OR IGNORE INTO inbox_message_conflicts
               (message_id, from_agent, source_topic, recorded_at, envelope)
               VALUES (?, ?, ?, ?, ?)""",
            (msg.id, msg.from_agent, source_topic, now, msg.to_json()),
        )
        inserted = cursor.rowcount == 1
        if inserted:
            await self._cap_conflicts(db)
        return inserted

    async def admit(self, msg: AgentMessage, *, source_topic: str) -> bool:
        """Record a message and return whether it may be delivered."""
        await self._ensure_open()
        async with self._connect() as db:
            await db.execute("BEGIN IMMEDIATE")
            committed = False
            try:
                admitted = await self._admit_in(
                    db, msg, source_topic=source_topic
                )
                await db.commit()
                committed = True
            finally:
                if not committed:
                    await db.rollback()
        return admitted

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
        async with self._connect() as db:
            await db.execute("BEGIN IMMEDIATE")
            committed = False
            try:
                async with db.execute(
                    f"""SELECT {_INBOX_MESSAGE_COLUMNS}
                        FROM inbox_messages
                        WHERE id = ?""",
                    (msg.id,),
                ) as existing_cursor:
                    existing = await existing_cursor.fetchone()
                if existing is not None:
                    if tuple(existing) != envelope:
                        raise MessageConflictError(
                            f"message ID {msg.id!r} is already bound to "
                            "a different envelope"
                        )
                    await db.commit()
                    committed = True
                    return False

                if not await self._admit_in(
                    db, msg, source_topic=source_topic
                ):
                    await db.commit()
                    committed = True
                    return False

                await db.execute(
                    """INSERT INTO inbox_messages
                       (id, from_agent, to_agent, ts, subject, body,
                        content_type, priority, reply_to, received_at,
                        source_topic)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (*envelope, received.isoformat(), source_topic),
                )
                await db.commit()
                committed = True
                return True
            finally:
                if not committed:
                    await db.rollback()

    async def record_conflict(
        self,
        msg: AgentMessage,
        *,
        source_topic: str,
        recorded_at: datetime | None = None,
    ) -> bool:
        """Durably note a message whose id is already bound to another envelope.

        The stored envelope is untouched; the rejected one is kept in
        ``inbox_message_conflicts`` so a person can see who reused the id.
        Each id and envelope is kept once; returns whether a row was added.
        """
        await self._ensure_open()
        recorded = recorded_at or datetime.now(timezone.utc)
        async with self._connect() as db:
            await db.execute("BEGIN IMMEDIATE")
            committed = False
            try:
                inserted = await self._record_conflict_in(
                    db,
                    msg,
                    source_topic=source_topic,
                    now=recorded.isoformat(),
                )
                await db.commit()
                committed = True
            finally:
                if not committed:
                    await db.rollback()
        return inserted

    async def list_conflicts(self, limit: int = 20) -> list[dict]:
        """Return the newest recorded id conflicts, newest first."""
        await self._ensure_open()
        async with self._connect() as db:
            async with db.execute(
                """SELECT message_id, from_agent, source_topic, recorded_at,
                          envelope
                   FROM inbox_message_conflicts
                   ORDER BY recorded_at DESC, rowid DESC
                   LIMIT ?""",
                (limit,),
            ) as cursor:
                return [
                    {
                        "id": r[0],
                        "from": r[1],
                        "source_topic": r[2],
                        "recorded_at": r[3],
                        "subject": json.loads(r[4]).get("subject"),
                    }
                    for r in await cursor.fetchall()
                ]

    async def list_holds(self, limit: int = 20) -> dict:
        """Report tripped senders and the newest held messages."""
        await self._ensure_open()
        async with self._connect() as db:
            async with db.execute(
                """SELECT sender, tripped_at, message_id
                   FROM hop_trips ORDER BY tripped_at"""
            ) as cursor:
                tripped = [
                    {"sender": r[0], "tripped_at": r[1], "message_id": r[2]}
                    for r in await cursor.fetchall()
                ]
            async with db.execute("SELECT COUNT(*) FROM held_messages") as cursor:
                held_count = (await cursor.fetchone())[0]
            async with db.execute(
                """SELECT id, from_agent, hops, reason, held_at, envelope
                   FROM held_messages
                   ORDER BY held_at DESC, rowid DESC
                   LIMIT ?""",
                (limit,),
            ) as cursor:
                held = [
                    {
                        "id": r[0],
                        "from": r[1],
                        "hops": r[2],
                        "reason": r[3],
                        "held_at": r[4],
                        "subject": json.loads(r[5]).get("subject"),
                    }
                    for r in await cursor.fetchall()
                ]
            async with db.execute(
                """SELECT from_agent, COUNT(*) FROM held_messages
                   GROUP BY from_agent
                   ORDER BY COUNT(*) DESC, from_agent ASC"""
            ) as cursor:
                held_by_sender = [
                    {"sender": r[0], "count": r[1]}
                    for r in await cursor.fetchall()
                ]
            async with db.execute(
                "SELECT COUNT(*) FROM admitted_messages"
            ) as cursor:
                admitted_count = (await cursor.fetchone())[0]
            async with db.execute(
                "SELECT COUNT(*) FROM inbox_message_conflicts"
            ) as cursor:
                conflict_count = (await cursor.fetchone())[0]
        return {
            "tripped": tripped,
            "held_count": held_count,
            "held": held,
            "held_by_sender": held_by_sender,
            "admitted_count": admitted_count,
            "conflict_count": conflict_count,
        }

    async def lookup(self, message_id: str) -> dict | None:
        """Return from/hops/origin of an admitted or held message, if known."""
        await self._ensure_open()
        async with self._connect() as db:
            async with db.execute(
                """SELECT from_agent, hops, origin
                   FROM admitted_messages WHERE id = ?""",
                (message_id,),
            ) as cursor:
                row = await cursor.fetchone()
            if row is not None:
                return {"from": row[0], "hops": row[1], "origin": row[2]}
            async with db.execute(
                """SELECT from_agent, hops, envelope
                   FROM held_messages WHERE id = ?""",
                (message_id,),
            ) as cursor:
                row = await cursor.fetchone()
        if row is None:
            return None
        return {
            "from": row[0],
            "hops": row[1],
            "origin": json.loads(row[2]).get("origin"),
        }

    async def reset_trips(self, sender: str | None = None) -> int:
        """Clear one sender's trip, or every trip; held messages stay held."""
        await self._ensure_open()
        async with self._connect() as db:
            await db.execute("BEGIN IMMEDIATE")
            committed = False
            try:
                if sender is None:
                    cursor = await db.execute("DELETE FROM hop_trips")
                else:
                    cursor = await db.execute(
                        "DELETE FROM hop_trips WHERE sender = ?", (sender,)
                    )
                removed = cursor.rowcount
                await db.commit()
                committed = True
            finally:
                if not committed:
                    await db.rollback()
        return removed

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
        row = message.model_dump(
            by_alias=True,
            mode="json",
            exclude={"in_reply_to", "hops", "origin", "envelope_version"},
        )
        row["untrusted"] = True
        return row

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
            async with self._connect() as db:
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
            async with self._connect() as db:
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
