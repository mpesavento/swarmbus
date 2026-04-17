import pytest
from pathlib import Path
from swarmbus.handlers.file_bridge import FileBridgeHandler
from swarmbus.message import AgentMessage


@pytest.fixture
def inbox(tmp_path):
    return tmp_path / "inbox.md"


@pytest.fixture
def msg():
    return AgentMessage.create(
        from_="wren", to="sparrow",
        subject="daily report", body="All systems nominal.",
    )


@pytest.mark.asyncio
async def test_creates_file_if_missing(inbox, msg):
    handler = FileBridgeHandler(str(inbox))
    await handler.handle(msg)
    assert inbox.exists()


@pytest.mark.asyncio
async def test_appends_message_content(inbox, msg):
    handler = FileBridgeHandler(str(inbox))
    await handler.handle(msg)
    content = inbox.read_text()
    assert "wren" in content
    assert "daily report" in content
    assert "All systems nominal." in content


@pytest.mark.asyncio
async def test_appends_multiple_messages(inbox, msg):
    handler = FileBridgeHandler(str(inbox))
    await handler.handle(msg)
    msg2 = AgentMessage.create(from_="wren", to="sparrow", subject="update", body="Still good.")
    await handler.handle(msg2)
    content = inbox.read_text()
    assert "All systems nominal." in content
    assert "Still good." in content


@pytest.mark.asyncio
async def test_creates_parent_dirs(tmp_path, msg):
    inbox = tmp_path / "deep" / "nested" / "inbox.md"
    handler = FileBridgeHandler(str(inbox))
    await handler.handle(msg)
    assert inbox.exists()
