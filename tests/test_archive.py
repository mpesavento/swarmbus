import asyncio
import stat
from pathlib import Path

import aiosqlite
import pytest

from swarmbus.archive import (
    MessageConflictError,
    SQLiteArchive,
    SQLiteMessageStore,
)
from swarmbus.message import AgentMessage


@pytest.fixture
def db_path(tmp_path):
    return str(tmp_path / "test.db")


@pytest.fixture
def msg():
    return AgentMessage.create(
        from_="wren", to="sparrow",
        subject="archive test", body="stored forever",
    )


@pytest.mark.asyncio
async def test_creates_table_and_stores_message(db_path, msg):
    archive = SQLiteArchive(db_path)
    await archive.handle(msg)

    async with aiosqlite.connect(db_path) as db:
        async with db.execute("SELECT * FROM messages WHERE id = ?", (msg.id,)) as cur:
            row = await cur.fetchone()
    assert row is not None
    assert row[1] == "wren"   # from_agent
    assert row[5] == "stored forever"  # body


@pytest.mark.asyncio
async def test_direction_defaults_to_received(db_path, msg):
    archive = SQLiteArchive(db_path)
    await archive.handle(msg)

    async with aiosqlite.connect(db_path) as db:
        async with db.execute("SELECT direction FROM messages WHERE id = ?", (msg.id,)) as cur:
            row = await cur.fetchone()
    assert row[0] == "received"


@pytest.mark.asyncio
async def test_stores_content_type(db_path):
    archive = SQLiteArchive(db_path)
    msg = AgentMessage.create(
        from_="wren", to="sparrow", subject="md", body="# Hello",
        content_type="text/markdown",
    )
    await archive.handle(msg)

    async with aiosqlite.connect(db_path) as db:
        async with db.execute("SELECT content_type FROM messages WHERE id = ?", (msg.id,)) as cur:
            row = await cur.fetchone()
    assert row[0] == "text/markdown"


@pytest.mark.asyncio
async def test_archive_replaces_duplicate_id(db_path, msg):
    archive = SQLiteArchive(db_path)
    replacement = msg.model_copy(update={"body": "replacement"})
    await archive.handle(msg)
    await archive.handle(replacement)

    async with aiosqlite.connect(db_path) as db:
        async with db.execute(
            "SELECT COUNT(*), body FROM messages WHERE id = ?",
            (msg.id,),
        ) as cur:
            row = await cur.fetchone()
    assert row == (1, "replacement")


@pytest.mark.asyncio
async def test_creates_parent_dirs(tmp_path, msg):
    db_path = str(tmp_path / "nested" / "dir" / "archive.db")
    archive = SQLiteArchive(db_path)
    await archive.handle(msg)
    assert Path(db_path).exists()


@pytest.mark.asyncio
async def test_message_store_reads_until_explicit_ack(db_path, msg):
    store = SQLiteMessageStore(db_path)
    await store.open()
    try:
        inserted = await store.store(msg, source_topic="agents/sparrow/inbox")
        assert inserted is True

        first = await store.read()
        assert [item["id"] for item in first] == [msg.id]
        assert await store.ack([msg.id]) == 1
        assert await store.read() == []
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_message_store_preserves_receiver_observed_sender_state(db_path, msg):
    store = SQLiteMessageStore(db_path)
    await store.open()
    try:
        provenance = {
            "lifecycle": "transient",
            "online": True,
            "observed_at": "2026-10-01T12:00:00+00:00",
            "started_at": "2026-10-01T11:00:00+00:00",
            "capabilities": ["messaging", "development.files.write"],
        }
        await store.store(
            msg,
            source_topic="agents/sparrow/inbox",
            sender_provenance=provenance,
        )

        stored = await store.read()

        assert stored[0]["sender_state_observed"] == provenance
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_unacknowledged_message_survives_reads_and_restart(db_path, msg):
    store = SQLiteMessageStore(db_path)
    await store.open()
    try:
        await store.store(msg, source_topic="agents/sparrow/inbox")

        first = await store.read()
        second = await store.read()

        assert [item["id"] for item in first] == [msg.id]
        assert [item["id"] for item in second] == [msg.id]
    finally:
        await store.close()

    restarted = SQLiteMessageStore(db_path)
    await restarted.open()
    try:
        assert [item["id"] for item in await restarted.read()] == [msg.id]
        assert await restarted.ack([msg.id]) == 1
        assert await restarted.ack([msg.id]) == 0
        assert await restarted.read() == []
    finally:
        await restarted.close()


@pytest.mark.asyncio
async def test_duplicate_delivery_does_not_reset_consumed_state(db_path, msg):
    store = SQLiteMessageStore(db_path)
    await store.open()
    try:
        assert await store.store(msg, source_topic="agents/sparrow/inbox") is True
        assert len(await store.read()) == 1
        assert await store.ack([msg.id]) == 1

        assert await store.store(msg, source_topic="agents/sparrow/inbox") is False
        assert await store.read() == []
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_conflicting_duplicate_message_id_is_rejected(db_path, msg):
    store = SQLiteMessageStore(db_path)
    await store.open()
    try:
        await store.store(msg, source_topic="agents/sparrow/inbox")
        conflicting = msg.model_copy(update={"body": "different envelope"})

        with pytest.raises(MessageConflictError, match="different envelope"):
            await store.store(
                conflicting,
                source_topic="agents/sparrow/inbox",
            )

        stored = await store.read()
        assert [item["body"] for item in stored] == [msg.body]
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_concurrent_acknowledgements_are_idempotent(db_path, msg):
    store = SQLiteMessageStore(db_path)
    await store.open()
    try:
        await store.store(msg, source_topic="agents/sparrow/inbox")
        left, right = await asyncio.gather(
            store.ack([msg.id]),
            store.ack([msg.id]),
        )
        assert left + right == 1
        assert await store.read() == []
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_message_store_preserves_archive_without_importing(db_path, msg):
    archive = SQLiteArchive(db_path)
    await archive.handle(msg)
    async with aiosqlite.connect(db_path) as db:
        await db.execute("PRAGMA user_version = 37")
        await db.commit()

    Path(db_path).chmod(0o600)
    store = SQLiteMessageStore(db_path)
    await store.open()
    try:
        async with aiosqlite.connect(db_path) as db:
            async with db.execute("PRAGMA table_info(messages)") as cursor:
                columns = {row[1] for row in await cursor.fetchall()}
            async with db.execute(
                "SELECT body FROM messages WHERE id = ?",
                (msg.id,),
            ) as cursor:
                archive_row = await cursor.fetchone()
            async with db.execute(
                "SELECT COUNT(*) FROM inbox_messages"
            ) as cursor:
                inbox_count = (await cursor.fetchone())[0]
            async with db.execute(
                """SELECT version FROM swarmbus_schema
                   WHERE component = 'inbox'"""
            ) as cursor:
                inbox_version = (await cursor.fetchone())[0]
            async with db.execute("PRAGMA user_version") as cursor:
                user_version = (await cursor.fetchone())[0]

        assert columns == {
            "id",
            "from_agent",
            "to_agent",
            "ts",
            "subject",
            "body",
            "content_type",
            "priority",
            "reply_to",
            "direction",
            "error",
        }
        assert archive_row == (msg.body,)
        assert inbox_count == 0
        assert inbox_version == 2
        assert user_version == 37
        assert await store.read() == []
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_message_store_migrates_v1_inbox_to_sender_provenance(db_path):
    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            """CREATE TABLE swarmbus_schema (
                component TEXT PRIMARY KEY,
                version INTEGER NOT NULL
            )"""
        )
        await db.execute(
            """INSERT INTO swarmbus_schema (component, version)
               VALUES ('inbox', 1)"""
        )
        await db.execute(
            """CREATE TABLE inbox_messages (
                id TEXT PRIMARY KEY,
                from_agent TEXT NOT NULL,
                to_agent TEXT NOT NULL,
                ts TEXT NOT NULL,
                subject TEXT NOT NULL,
                body TEXT NOT NULL,
                content_type TEXT NOT NULL DEFAULT 'text/plain',
                priority TEXT NOT NULL DEFAULT 'normal',
                reply_to TEXT,
                received_at TEXT NOT NULL,
                source_topic TEXT NOT NULL,
                acknowledged_at TEXT
            )"""
        )
        await db.commit()

    Path(db_path).chmod(0o600)
    store = SQLiteMessageStore(db_path)
    await store.open()
    try:
        async with aiosqlite.connect(db_path) as db:
            async with db.execute(
                "PRAGMA table_info(inbox_messages)"
            ) as cursor:
                columns = {row[1] for row in await cursor.fetchall()}
            async with db.execute(
                """SELECT version FROM swarmbus_schema
                   WHERE component = 'inbox'"""
            ) as cursor:
                version = (await cursor.fetchone())[0]

        assert {
            "sender_lifecycle_observed",
            "sender_online_observed",
            "sender_registry_observed_at",
            "sender_started_at_observed",
            "sender_capabilities_observed",
        } <= columns
        assert version == 2
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_message_store_rejects_newer_schema(db_path):
    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            """CREATE TABLE swarmbus_schema (
                component TEXT PRIMARY KEY,
                version INTEGER NOT NULL
            )"""
        )
        await db.execute(
            """INSERT INTO swarmbus_schema (component, version)
               VALUES ('inbox', 3)"""
        )
        await db.commit()

    Path(db_path).chmod(0o600)
    store = SQLiteMessageStore(db_path)

    with pytest.raises(RuntimeError, match="inbox schema 3 is newer"):
        await store.open()


@pytest.mark.asyncio
async def test_failed_inbox_migration_rolls_back(monkeypatch, db_path, msg):
    archive = SQLiteArchive(db_path)
    await archive.handle(msg)
    monkeypatch.setattr(
        "swarmbus.archive._CREATE_QUARANTINE_TABLE",
        "CREATE TABLE invalid SQL",
    )
    store = SQLiteMessageStore(db_path, managed=False)

    with pytest.raises(aiosqlite.Error):
        await store.open()

    async with aiosqlite.connect(db_path) as db:
        async with db.execute(
            "SELECT body FROM messages WHERE id = ?",
            (msg.id,),
        ) as cursor:
            archive_row = await cursor.fetchone()
        async with db.execute(
            """SELECT name FROM sqlite_master
               WHERE type = 'table'
                 AND name IN (
                     'inbox_messages',
                     'inbox_message_quarantine',
                     'swarmbus_schema'
                 )
               ORDER BY name"""
        ) as cursor:
            migration_tables = await cursor.fetchall()

    assert archive_row == (msg.body,)
    assert migration_tables == []


@pytest.mark.asyncio
async def test_managed_store_creates_private_state(tmp_path):
    state_dir = tmp_path / "managed"
    db_path = state_dir / "inbox.sqlite3"

    store = SQLiteMessageStore(db_path)
    await store.open()
    try:
        assert stat.S_IMODE(state_dir.stat().st_mode) == 0o700
        assert stat.S_IMODE(db_path.stat().st_mode) == 0o600
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_managed_store_rejects_unsafe_existing_directory(tmp_path):
    state_dir = tmp_path / "managed"
    state_dir.mkdir(mode=0o755)
    state_dir.chmod(0o755)
    db_path = state_dir / "inbox.sqlite3"

    store = SQLiteMessageStore(db_path)

    with pytest.raises(PermissionError, match="directory.*0o700"):
        await store.open()
    assert not db_path.exists()


@pytest.mark.asyncio
async def test_managed_store_rejects_unsafe_existing_database(tmp_path):
    state_dir = tmp_path / "managed"
    state_dir.mkdir(mode=0o700)
    state_dir.chmod(0o700)
    db_path = state_dir / "inbox.sqlite3"
    db_path.touch(mode=0o644)
    db_path.chmod(0o644)

    store = SQLiteMessageStore(db_path)

    with pytest.raises(PermissionError, match="database.*0o600"):
        await store.open()


@pytest.mark.asyncio
async def test_archive_preserves_caller_managed_permissions(tmp_path, msg):
    archive_dir = tmp_path / "shared"
    archive_dir.mkdir(mode=0o755)
    archive_dir.chmod(0o755)
    db_path = archive_dir / "archive.sqlite3"
    db_path.touch(mode=0o644)
    db_path.chmod(0o644)

    archive = SQLiteArchive(str(db_path))
    await archive.handle(msg)

    assert stat.S_IMODE(archive_dir.stat().st_mode) == 0o755
    assert stat.S_IMODE(db_path.stat().st_mode) == 0o644


@pytest.mark.asyncio
async def test_read_quarantines_invalid_row_and_continues(db_path, msg):
    second = AgentMessage.create(
        from_="finch",
        to="sparrow",
        subject="valid after poison",
        body="deliver me",
    )
    store = SQLiteMessageStore(db_path)
    await store.open()
    try:
        await store.store(msg, source_topic="agents/sparrow/inbox")
        await store.store(second, source_topic="agents/sparrow/inbox")
        async with aiosqlite.connect(db_path) as db:
            await db.execute(
                "UPDATE inbox_messages SET from_agent = ? WHERE id = ?",
                ("INVALID!", msg.id),
            )
            await db.commit()

        drained = await store.read(max_messages=1)

        assert [item["id"] for item in drained] == [second.id]
        assert await store.ack([second.id]) == 1
        assert await store.read(max_messages=1) == []
        async with aiosqlite.connect(db_path) as db:
            async with db.execute(
                """SELECT inbox_messages.id, inbox_messages.acknowledged_at,
                          inbox_message_quarantine.quarantined_at,
                          inbox_message_quarantine.error
                   FROM inbox_messages
                   JOIN inbox_message_quarantine
                     ON inbox_message_quarantine.message_id = inbox_messages.id
                   WHERE inbox_messages.id = ?""",
                (msg.id,),
            ) as cursor:
                row = await cursor.fetchone()
        assert row[0] == msg.id
        assert row[1] is None
        assert row[2] is not None
        assert "Agent ID must match" in row[3]
        assert msg.body not in row[3]
    finally:
        await store.close()


@pytest.mark.parametrize(
    ("column", "value", "error_fragments"),
    [
        (
            "sender_capabilities_observed",
            "{not-json",
            ("JSON", "Expecting"),
        ),
        ("sender_lifecycle_observed", "ephemeral", ("lifecycle",)),
    ],
)
@pytest.mark.asyncio
async def test_read_quarantines_invalid_sender_provenance(
    db_path,
    msg,
    column,
    value,
    error_fragments,
):
    store = SQLiteMessageStore(db_path)
    await store.open()
    try:
        await store.store(msg, source_topic="agents/sparrow/inbox")
        async with aiosqlite.connect(db_path) as db:
            await db.execute(
                f"""UPDATE inbox_messages
                    SET {column} = ?
                    WHERE id = ?""",
                (value, msg.id),
            )
            await db.commit()

        assert await store.read() == []

        async with aiosqlite.connect(db_path) as db:
            async with db.execute(
                """SELECT error FROM inbox_message_quarantine
                   WHERE message_id = ?""",
                (msg.id,),
            ) as cursor:
                row = await cursor.fetchone()
        assert row is not None
        assert any(fragment in row[0] for fragment in error_fragments)
    finally:
        await store.close()
