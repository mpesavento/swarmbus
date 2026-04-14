import pytest
from unittest.mock import patch, MagicMock
from agentbus.handlers.direct_invoke import DirectInvocationHandler
from agentbus.message import AgentMessage


@pytest.fixture
def msg():
    return AgentMessage.create(
        from_="wren", to="sparrow",
        subject="task", body="Do the thing.",
        content_type="text/plain",
    )


@pytest.mark.asyncio
async def test_calls_command_with_body_as_stdin(msg):
    captured = {}

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        captured["input"] = kwargs.get("input")
        return MagicMock(returncode=0)

    handler = DirectInvocationHandler(command=["echo"])
    with patch("agentbus.handlers.direct_invoke.subprocess.run", side_effect=fake_run):
        await handler.handle(msg)

    assert captured["cmd"] == ["echo"]
    assert captured["input"] == b"Do the thing."


@pytest.mark.asyncio
async def test_env_vars_set(msg):
    captured = {}

    def fake_run(cmd, **kwargs):
        captured["env"] = kwargs.get("env", {})
        return MagicMock(returncode=0)

    handler = DirectInvocationHandler(command=["true"])
    with patch("agentbus.handlers.direct_invoke.subprocess.run", side_effect=fake_run):
        await handler.handle(msg)

    assert captured["env"]["AGENTBUS_FROM"] == "wren"
    assert captured["env"]["AGENTBUS_TO"] == "sparrow"
    assert captured["env"]["AGENTBUS_SUBJECT"] == "task"
    assert captured["env"]["AGENTBUS_CONTENT_TYPE"] == "text/plain"
    assert captured["env"]["AGENTBUS_PRIORITY"] == "normal"


@pytest.mark.asyncio
async def test_no_shell_equals_true(msg):
    """Body must never be shell-interpolated. subprocess.run must not use shell=True."""
    called_with = {}

    def fake_run(cmd, **kwargs):
        called_with["shell"] = kwargs.get("shell", False)
        return MagicMock(returncode=0)

    handler = DirectInvocationHandler(command=["echo"])
    with patch("agentbus.handlers.direct_invoke.subprocess.run", side_effect=fake_run):
        await handler.handle(msg)

    assert called_with["shell"] is False


@pytest.mark.asyncio
async def test_nonzero_exit_does_not_raise(msg):
    """A failing command should log but not propagate exception."""
    def fake_run(cmd, **kwargs):
        return MagicMock(returncode=1)

    handler = DirectInvocationHandler(command=["false"])
    with patch("agentbus.handlers.direct_invoke.subprocess.run", side_effect=fake_run):
        await handler.handle(msg)  # must not raise


@pytest.mark.asyncio
async def test_markdown_body_passed_verbatim(msg):
    """Markdown with code blocks must survive the transport unchanged."""
    md_msg = AgentMessage.create(
        from_="wren", to="sparrow",
        subject="code review",
        body="Here's the fix:\n```python\nprint('hello')\n```\n> Note: tested.",
        content_type="text/markdown",
    )
    captured = {}

    def fake_run(cmd, **kwargs):
        captured["input"] = kwargs.get("input", b"")
        return MagicMock(returncode=0)

    handler = DirectInvocationHandler(command=["cat"])
    with patch("agentbus.handlers.direct_invoke.subprocess.run", side_effect=fake_run):
        await handler.handle(md_msg)

    assert b"```python" in captured["input"]
    assert b"print('hello')" in captured["input"]
