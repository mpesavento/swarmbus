import pytest
from pathlib import Path
from swarmbus.archive import SQLiteArchive
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

    import aiosqlite
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

    import aiosqlite
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

    import aiosqlite
    async with aiosqlite.connect(db_path) as db:
        async with db.execute("SELECT content_type FROM messages WHERE id = ?", (msg.id,)) as cur:
            row = await cur.fetchone()
    assert row[0] == "text/markdown"


@pytest.mark.asyncio
async def test_idempotent_on_duplicate_id(db_path, msg):
    archive = SQLiteArchive(db_path)
    await archive.handle(msg)
    await archive.handle(msg)  # same id — must not raise

    import aiosqlite
    async with aiosqlite.connect(db_path) as db:
        async with db.execute("SELECT COUNT(*) FROM messages WHERE id = ?", (msg.id,)) as cur:
            row = await cur.fetchone()
    assert row[0] == 1


@pytest.mark.asyncio
async def test_creates_parent_dirs(tmp_path, msg):
    db_path = str(tmp_path / "nested" / "dir" / "archive.db")
    archive = SQLiteArchive(db_path)
    await archive.handle(msg)
    assert Path(db_path).exists()
