import asyncio
import json
import os
import stat
from datetime import datetime, timedelta, timezone
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
async def test_record_conflict_keeps_original_and_lists_rejected(db_path, msg):
    store = SQLiteMessageStore(db_path)
    await store.open()
    try:
        topic = "agents/sparrow/inbox"
        await store.store(msg, source_topic=topic)
        rejected = msg.model_copy(update={"subject": "reused id"})
        with pytest.raises(MessageConflictError):
            await store.store(rejected, source_topic=topic)

        await store.record_conflict(rejected, source_topic=topic)
        await store.record_conflict(rejected, source_topic=topic)

        conflicts = await store.list_conflicts()
        assert [
            (c["id"], c["from"], c["source_topic"], c["subject"])
            for c in conflicts
        ] == [(msg.id, "wren", topic, "reused id")]
        assert all(c["recorded_at"] for c in conflicts)
        assert await store.list_conflicts(limit=1) == conflicts[:1]

        # The first envelope is untouched: still readable and acknowledgeable.
        assert [item["subject"] for item in await store.read()] == [msg.subject]
        assert await store.ack([msg.id]) == 1
        async with aiosqlite.connect(db_path) as db:
            async with db.execute(
                "SELECT COUNT(*) FROM inbox_message_quarantine"
            ) as cursor:
                assert (await cursor.fetchone())[0] == 0
            async with db.execute(
                "SELECT version FROM swarmbus_schema WHERE component = 'inbox'"
            ) as cursor:
                assert (await cursor.fetchone())[0] == 1
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
        assert inbox_version == 1
        assert user_version == 37
        assert await store.read() == []
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
               VALUES ('inbox', 2)"""
        )
        await db.commit()

    Path(db_path).chmod(0o600)
    store = SQLiteMessageStore(db_path)

    with pytest.raises(RuntimeError, match="inbox schema 2 is newer"):
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
                     'inbox_message_conflicts',
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


@pytest.mark.asyncio
async def test_message_store_rows_are_marked_untrusted(db_path, msg):
    store = SQLiteMessageStore(db_path)
    await store.open()
    try:
        await store.store(msg, source_topic="agents/sparrow/inbox")
        rows = await store.read()
        assert rows[0]["untrusted"] is True
        assert rows[0]["body"] == msg.body
        assert rows[0]["subject"] == msg.subject
    finally:
        await store.close()


def _chain_msg(sender, hops, subject="chain"):
    return AgentMessage.create(
        from_=sender, to="sparrow", subject=subject, body="b", hops=hops,
    )


@pytest.mark.asyncio
async def test_store_holds_past_hop_limit_and_trips_sender(db_path):
    topic = "agents/sparrow/inbox"
    store = SQLiteMessageStore(db_path, managed=False, hop_limit=4)
    at_limit = _chain_msg("wren", 4, "at-limit")
    past = _chain_msg("wren", 5, "past")
    fresh_wren = _chain_msg("wren", 0, "fresh-wren")
    fresh_robin = _chain_msg("robin", 0, "fresh-robin")

    assert await store.store(at_limit, source_topic=topic) is True
    assert await store.store(past, source_topic=topic) is False
    assert await store.store(past, source_topic=topic) is False
    assert await store.store(fresh_wren, source_topic=topic) is False
    assert await store.store(fresh_robin, source_topic=topic) is True

    rows = await store.read()
    assert [r["subject"] for r in rows] == ["at-limit", "fresh-robin"]
    assert set(rows[0]) == {
        "id", "from", "to", "ts", "subject", "body",
        "content_type", "priority", "reply_to", "untrusted",
    }

    holds = await store.list_holds()
    assert [(t["sender"], t["message_id"]) for t in holds["tripped"]] == [("wren", past.id)]
    assert holds["held_count"] == 2
    assert {(h["id"], h["reason"]) for h in holds["held"]} == {
        (past.id, "hop-limit"), (fresh_wren.id, "sender-tripped"),
    }

    reopened = SQLiteMessageStore(db_path, managed=False, hop_limit=4)
    assert await reopened.admit(_chain_msg("wren", 0), source_topic=topic) is False
    assert await reopened.reset_trips("wren") == 1
    assert await reopened.reset_trips("wren") == 0
    after = _chain_msg("wren", 0, "after-reset")
    assert await reopened.store(after, source_topic=topic) is True
    assert [r["subject"] for r in await reopened.read()] == [
        "at-limit", "fresh-robin", "after-reset",
    ]


@pytest.mark.asyncio
async def test_admit_records_messages_for_lookup(db_path):
    topic = "agents/sparrow/inbox"
    store = SQLiteMessageStore(db_path, managed=False, hop_limit=4)
    parent = AgentMessage.create(from_="wren", to="sparrow", subject="p", body="b", hops=3, origin="planner")
    held = AgentMessage.create(from_="robin", to="sparrow", subject="h", body="b", hops=5)

    assert await store.admit(parent, source_topic=topic) is True
    assert await store.admit(parent, source_topic=topic) is True
    assert await store.admit(held, source_topic=topic) is False

    assert await store.lookup(parent.id) == {"from": "wren", "hops": 3, "origin": "planner"}
    assert (await store.lookup(held.id))["hops"] == 5
    assert await store.lookup("unknown-id") is None


@pytest.mark.asyncio
async def test_admit_holds_hops_beyond_sqlite_integer_range(db_path):
    store = SQLiteMessageStore(db_path, managed=False, hop_limit=4)
    huge = AgentMessage.create(from_="wren", to="sparrow", subject="huge", body="b", hops=2**70)

    assert await store.admit(huge, source_topic="agents/sparrow/inbox") is False

    holds = await store.list_holds()
    assert [t["sender"] for t in holds["tripped"]] == ["wren"]
    assert holds["held_count"] == 1


@pytest.mark.asyncio
async def test_admit_treats_reused_id_with_a_different_envelope_as_conflict(db_path):
    topic = "agents/sparrow/inbox"
    store = SQLiteMessageStore(db_path, managed=False, hop_limit=4)
    first = AgentMessage.create(from_="wren", to="sparrow", subject="first", body="b")
    changed = first.model_copy(update={"subject": "changed"})
    other_sender = first.model_copy(update={"from_agent": "robin"})

    assert await store.admit(first, source_topic=topic) is True
    assert await store.admit(first, source_topic=topic) is True
    assert await store.admit(changed, source_topic=topic) is False
    assert await store.admit(changed, source_topic=topic) is False
    assert await store.admit(other_sender, source_topic=topic) is False

    conflicts = await store.list_conflicts(limit=100)
    assert sorted((c["from"], c["subject"]) for c in conflicts) == [
        ("robin", "first"), ("wren", "changed"),
    ]
    assert await store.lookup(first.id) == {"from": "wren", "hops": 0, "origin": "wren"}
    assert (await store.list_holds())["held_count"] == 0


@pytest.mark.asyncio
async def test_tripped_sender_cannot_reuse_an_admitted_id(db_path):
    topic = "agents/sparrow/inbox"
    store = SQLiteMessageStore(db_path, managed=False, hop_limit=4)
    old = _chain_msg("wren", 0, "old")
    assert await store.admit(old, source_topic=topic) is True
    assert await store.admit(_chain_msg("wren", 5, "past"), source_topic=topic) is False

    assert await store.admit(old.model_copy(update={"hops": 50}), source_topic=topic) is False
    assert await store.admit(old, source_topic=topic) is False

    holds = await store.list_holds()
    assert holds["held_count"] == 2
    assert {h["reason"] for h in holds["held"]} == {"hop-limit", "sender-tripped"}


@pytest.mark.asyncio
async def test_admit_reads_a_holds_v1_database_and_migrates_it(db_path):
    now = datetime.now(timezone.utc).isoformat()
    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            "CREATE TABLE swarmbus_schema (component TEXT PRIMARY KEY, version INTEGER NOT NULL)"
        )
        await db.execute("INSERT INTO swarmbus_schema VALUES ('holds', 1)")
        await db.execute(
            """CREATE TABLE admitted_messages (id TEXT PRIMARY KEY, from_agent TEXT NOT NULL,
               hops INTEGER NOT NULL, origin TEXT, seen_at TEXT NOT NULL)"""
        )
        await db.execute(
            """CREATE TABLE held_messages (id TEXT PRIMARY KEY, from_agent TEXT NOT NULL,
               hops INTEGER NOT NULL, reason TEXT NOT NULL, source_topic TEXT NOT NULL,
               held_at TEXT NOT NULL, envelope TEXT NOT NULL)"""
        )
        await db.execute(
            "INSERT INTO admitted_messages VALUES ('legacy-1', 'wren', 2, 'planner', ?)", (now,)
        )
        await db.commit()

    store = SQLiteMessageStore(db_path, managed=False, hop_limit=4)
    base = AgentMessage.create(from_="wren", to="sparrow", subject="s", body="b", hops=2)
    same = base.model_copy(update={"id": "legacy-1"})
    other = base.model_copy(update={"id": "legacy-1", "from_agent": "robin"})
    topic = "agents/sparrow/inbox"

    assert await store.admit(same, source_topic=topic) is True
    assert await store.admit(other, source_topic=topic) is False
    assert await store.admit(_chain_msg("kestrel", 0), source_topic=topic) is True
    async with aiosqlite.connect(db_path) as db:
        async with db.execute(
            "SELECT version FROM swarmbus_schema WHERE component = 'holds'"
        ) as cursor:
            assert (await cursor.fetchone())[0] == 2


@pytest.mark.asyncio
async def test_conflicts_are_recorded_once_per_envelope_and_capped(db_path):
    topic = "agents/sparrow/inbox"
    store = SQLiteMessageStore(db_path, managed=False)
    store.CONFLICTS_MAX_ROWS = 3
    original = AgentMessage.create(from_="wren", to="sparrow", subject="original", body="b")
    assert await store.store(original, source_topic=topic) is True
    reused = original.model_copy(update={"subject": "reused"})

    for _ in range(5):
        await store.record_conflict(reused, source_topic=topic)
    assert len(await store.list_conflicts(limit=100)) == 1

    for i in range(5):
        await store.record_conflict(
            original.model_copy(update={"subject": f"v{i}"}), source_topic=topic,
        )
    assert [c["subject"] for c in await store.list_conflicts(limit=100)] == ["v4", "v3", "v2"]
    assert (await store.list_holds())["conflict_count"] == 3


@pytest.mark.asyncio
async def test_open_dedupes_conflict_rows_written_before_the_key(db_path):
    store = SQLiteMessageStore(db_path, managed=False)
    await store.open()
    async with aiosqlite.connect(db_path) as db:
        await db.execute("DROP INDEX inbox_message_conflicts_once")
        for _ in range(3):
            await db.execute(
                "INSERT INTO inbox_message_conflicts VALUES (?, ?, ?, ?, ?)",
                ("dup", "wren", "agents/sparrow/inbox",
                 "2026-10-16T00:00:00+00:00", json.dumps({"subject": "x"})),
            )
        await db.commit()

    again = SQLiteMessageStore(db_path, managed=False)
    assert len(await again.list_conflicts(limit=100)) == 1


@pytest.mark.asyncio
async def test_admitted_messages_age_out_after_retention(db_path):
    topic = "agents/sparrow/inbox"
    store = SQLiteMessageStore(db_path, managed=False)
    old = _chain_msg("wren", 0, "old")
    recent = _chain_msg("wren", 0, "recent")
    assert await store.admit(old, source_topic=topic) is True
    assert await store.admit(recent, source_topic=topic) is True
    stale = (datetime.now(timezone.utc) - timedelta(days=31)).isoformat()

    async def make_stale(message_id):
        async with aiosqlite.connect(db_path) as db:
            await db.execute(
                "UPDATE admitted_messages SET seen_at = ? WHERE id = ?", (stale, message_id),
            )
            await db.commit()

    await make_stale(old.id)
    reopened = SQLiteMessageStore(db_path, managed=False)
    assert await reopened.lookup(recent.id) is not None
    assert await reopened.lookup(old.id) is None
    assert (await reopened.list_holds())["admitted_count"] == 1

    reopened.PRUNE_EVERY_ADMISSIONS = 2
    await make_stale(recent.id)
    assert await reopened.admit(_chain_msg("robin", 0, "a"), source_topic=topic) is True
    assert await reopened.lookup(recent.id) is not None
    assert await reopened.admit(_chain_msg("robin", 0, "b"), source_topic=topic) is True
    assert await reopened.lookup(recent.id) is None


@pytest.mark.asyncio
async def test_held_envelopes_are_capped_per_tripped_sender_and_in_total(db_path):
    topic = "agents/sparrow/inbox"
    store = SQLiteMessageStore(db_path, managed=False, hop_limit=0)
    store.HELD_FULL_ENVELOPES_PER_SENDER = 2
    store.HELD_MAX_ROWS = 3  # unprotected rows; the trip row is protected and not counted
    trip = _chain_msg("wren", 1, "trip")
    after = [_chain_msg("wren", 0, f"after-{i}") for i in range(4)]

    assert await store.admit(trip, source_topic=topic) is False
    for m in after:
        assert await store.admit(m, source_topic=topic) is False

    holds = await store.list_holds()
    assert holds["held_count"] == 4
    assert holds["held_by_sender"] == [{"sender": "wren", "count": 4}]
    assert {h["id"] for h in holds["held"]} == {trip.id, after[1].id, after[2].id, after[3].id}

    async with aiosqlite.connect(db_path) as db:
        async with db.execute(
            "SELECT id, stub, envelope FROM held_messages"
        ) as cursor:
            rows = {r[0]: (r[1], json.loads(r[2])) for r in await cursor.fetchall()}
    assert rows[trip.id][0] == 0 and rows[trip.id][1]["body"] == "b"
    assert rows[after[1].id][0] == 0 and rows[after[1].id][1]["body"] == "b"
    assert rows[after[3].id][0] == 1
    assert "body" not in rows[after[3].id][1]
    assert rows[after[3].id][1]["subject"] == "after-3"
    assert (await store.lookup(after[3].id))["hops"] == 0
    assert await store.admit(after[0], source_topic=topic) is False


@pytest.mark.asyncio
async def test_state_db_uses_wal_a_30s_busy_timeout_and_private_sidecar_files(tmp_path):
    from unittest.mock import patch

    real_connect = aiosqlite.connect
    timeouts = []

    def spy(*args, **kwargs):
        timeouts.append(kwargs.get("timeout"))
        return real_connect(*args, **kwargs)

    store = SQLiteMessageStore(tmp_path / "state" / "sparrow.sqlite3")
    topic = "agents/sparrow/inbox"
    old_umask = os.umask(0o022)
    try:
        with patch("swarmbus.archive.aiosqlite.connect", side_effect=spy):
            assert await store.admit(_chain_msg("wren", 0, "one"), source_topic=topic) is True
        assert timeouts and all(t == 30.0 for t in timeouts)

        async with real_connect(store.db_path) as reader:
            async with reader.execute("PRAGMA journal_mode") as cursor:
                assert (await cursor.fetchone())[0] == "wal"
            async with reader.execute("SELECT COUNT(*) FROM admitted_messages") as cursor:
                assert (await cursor.fetchone())[0] == 1
            assert await store.admit(_chain_msg("wren", 0, "two"), source_topic=topic) is True
            for suffix in ("-wal", "-shm"):
                side = Path(f"{store.db_path}{suffix}")
                assert side.exists(), suffix
                assert stat.S_IMODE(side.stat().st_mode) == 0o600, suffix
    finally:
        os.umask(old_umask)


INBOX = "agents/sparrow/inbox"
BROADCAST = "agents/broadcast"


def _rate_store(db_path, now, **kw):
    kw.setdefault("rate_limit", 2)
    kw.setdefault("rate_cooldown", 600)
    return SQLiteMessageStore(db_path, managed=False, clock=lambda: now[0], **kw)


def _from(sender, subject="m", hops=0):
    return AgentMessage.create(from_=sender, to="sparrow", subject=subject, body="b", hops=hops)


def _iso(ts):
    return datetime.fromtimestamp(ts, timezone.utc).isoformat()


@pytest.mark.asyncio
async def test_rate_limit_holds_sender_for_cooldown_then_admits_again(db_path):
    now = [1000.0]
    store = _rate_store(db_path, now)
    over = _from("wren", "3")

    assert await store.store(_from("wren", "1"), source_topic=INBOX) is True
    assert await store.store(_from("wren", "2"), source_topic=INBOX) is True
    assert await store.store(over, source_topic=INBOX) is False
    assert await store.store(_from("robin", "r"), source_topic=INBOX) is True

    holds = await store.list_holds()
    assert holds["tripped"] == []
    assert [(c["sender"], c["message_id"], c["until"]) for c in holds["cooling"]] == [
        ("wren", over.id, _iso(1600.0)),
    ]
    assert [(h["id"], h["reason"]) for h in holds["held"]] == [(over.id, "rate-limit")]

    now[0] = 1599.0
    assert await store.admit(_from("wren", "still"), source_topic=INBOX) is False
    now[0] = 1600.0
    assert await store.store(_from("wren", "after"), source_topic=INBOX) is True
    assert await store.store(over, source_topic=INBOX) is False
    assert (await store.list_holds())["cooling"] == []
    assert [r["subject"] for r in await store.read()] == ["1", "2", "r", "after"]


@pytest.mark.asyncio
async def test_rate_bucket_refills_caps_its_burst_and_survives_clock_jumps(db_path):
    now = [0.0]
    store = _rate_store(db_path, now, rate_cooldown=0)

    async def burst(n):
        return [await store.admit(_from("wren"), source_topic=INBOX) for _ in range(n)]

    assert await burst(3) == [True, True, False]
    assert (await store.list_holds())["cooling"] == []
    now[0] = 20.0      # 2 per 60 s: one token every 30 s
    assert await burst(1) == [False]
    now[0] = 45.0
    assert await burst(1) == [True]
    now[0] = 10_000.0  # a long silence refills to the burst size and no more
    assert await burst(3) == [True, True, False]
    now[0] = 5_000.0   # the clock jumps back: no refill, nothing negative
    assert await burst(1) == [False]
    now[0] = 5_031.0
    assert await burst(1) == [True]


@pytest.mark.asyncio
async def test_cooldown_never_outlasts_one_cooldown_after_the_clock_jumps_back(db_path):
    now = [10_000.0]
    store = _rate_store(db_path, now)

    async def burst(n):
        return [await store.admit(_from("wren"), source_topic=INBOX) for _ in range(n)]

    assert await burst(3) == [True, True, False]
    now[0] = 0.0
    assert await burst(1) == [False]
    assert [c["until"] for c in (await store.list_holds())["cooling"]] == [_iso(600.0)]
    now[0] = 600.0
    assert await burst(3) == [True, True, False]
    assert [c["until"] for c in (await store.list_holds())["cooling"]] == [_iso(1200.0)]


@pytest.mark.asyncio
async def test_broadcast_is_held_but_never_trips_its_sender(db_path):
    now = [0.0]
    store = _rate_store(db_path, now, hop_limit=4)
    deep = _from("wren", "deep", hops=9)
    flood = _from("wren", "b3")

    assert await store.admit(deep, source_topic=BROADCAST) is False
    assert await store.admit(_from("wren", "b1"), source_topic=BROADCAST) is True
    assert await store.admit(_from("wren", "b2"), source_topic=BROADCAST) is True
    assert await store.admit(flood, source_topic=BROADCAST) is False
    assert await store.admit(_from("wren", "d1"), source_topic=INBOX) is True
    holds = await store.list_holds()
    assert holds["tripped"] == [] and holds["cooling"] == []
    assert {(h["id"], h["reason"]) for h in holds["held"]} == {
        (deep.id, "hop-limit"), (flood.id, "rate-limit"),
    }

    assert await store.admit(_from("robin", hops=9), source_topic=INBOX) is False
    assert await store.admit(_from("robin"), source_topic=BROADCAST) is False
    assert await store.admit(_from("wren", "d2"), source_topic=INBOX) is True
    assert await store.admit(_from("wren", "d3"), source_topic=INBOX) is False
    now[0] = 60.0      # the broadcast bucket is full again, but wren is cooling
    assert await store.admit(_from("wren", "b4"), source_topic=BROADCAST) is False
    holds = await store.list_holds()
    assert [t["sender"] for t in holds["tripped"]] == ["robin"]
    assert [c["sender"] for c in holds["cooling"]] == ["wren"]


@pytest.mark.asyncio
async def test_two_listeners_on_one_state_db_share_one_bucket(db_path):
    now = [0.0]
    daemon, watch = _rate_store(db_path, now), _rate_store(db_path, now)
    one = _from("wren", "1")

    assert await daemon.admit(one, source_topic=INBOX) is True
    assert await watch.admit(one, source_topic=INBOX) is True   # counted once
    assert await watch.admit(_from("wren", "2"), source_topic=INBOX) is True
    assert await daemon.admit(_from("wren", "3"), source_topic=INBOX) is False
    assert [c["sender"] for c in (await watch.list_holds())["cooling"]] == ["wren"]

    assert await watch.reset_trips("wren") == 1
    assert (await daemon.list_holds())["cooling"] == []
    assert await daemon.admit(_from("wren", "4"), source_topic=INBOX) is True
