import json
import logging
import os
import subprocess
import time
import pytest
from unittest.mock import patch, MagicMock
from swarmbus.handlers.direct_invoke import DEFAULT_INVOKE_TIMEOUT, DirectInvocationHandler
from swarmbus.message import AgentMessage

POPEN = "swarmbus.handlers.direct_invoke.subprocess.Popen"


@pytest.fixture
def msg():
    return AgentMessage.create(
        from_="wren", to="sparrow",
        subject="task", body="Do the thing.",
        content_type="text/plain",
    )


def _fake_popen(captured, returncode=0):
    """Stand in for subprocess.Popen: records argv, kwargs and the stdin bytes."""
    def fake(cmd, **kwargs):
        captured["cmd"] = cmd
        captured["kwargs"] = kwargs
        proc = MagicMock()
        proc.pid = 4242
        proc.returncode = returncode

        def communicate(input=None, timeout=None):
            if input is not None:
                captured["input"] = input
            captured["timeout"] = timeout
            return (None, None)

        proc.communicate.side_effect = communicate
        return proc
    return fake


@pytest.mark.asyncio
async def test_calls_command_with_body_as_stdin(msg):
    captured = {}
    handler = DirectInvocationHandler(command=["echo"])
    with patch(POPEN, side_effect=_fake_popen(captured)):
        await handler.handle(msg)

    assert captured["cmd"] == ["echo"]
    assert captured["input"] == b"Do the thing."
    assert captured["kwargs"]["stdin"] is subprocess.PIPE


@pytest.mark.asyncio
async def test_env_vars_set(msg):
    captured = {}
    handler = DirectInvocationHandler(command=["true"])
    with patch(POPEN, side_effect=_fake_popen(captured)):
        await handler.handle(msg)

    env = captured["kwargs"]["env"]
    assert env["SWARMBUS_FROM"] == "wren"
    assert env["SWARMBUS_TO"] == "sparrow"
    assert env["SWARMBUS_SUBJECT"] == "task"
    assert env["SWARMBUS_CONTENT_TYPE"] == "text/plain"
    assert env["SWARMBUS_PRIORITY"] == "normal"


@pytest.mark.asyncio
async def test_no_shell_equals_true(msg):
    """Body must never be shell-interpolated. Popen must not use shell=True."""
    captured = {}
    handler = DirectInvocationHandler(command=["echo"])
    with patch(POPEN, side_effect=_fake_popen(captured)):
        await handler.handle(msg)

    assert captured["kwargs"]["shell"] is False
    assert captured["kwargs"]["start_new_session"] is True


@pytest.mark.asyncio
async def test_nonzero_exit_does_not_raise(msg, caplog):
    """A failing command should log but not propagate exception."""
    handler = DirectInvocationHandler(command=["false"])
    with patch(POPEN, side_effect=_fake_popen({}, returncode=1)):
        with caplog.at_level(logging.WARNING, logger="swarmbus.handlers.direct_invoke"):
            await handler.handle(msg)  # must not raise
    assert "exited 1" in caplog.text


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
    handler = DirectInvocationHandler(command=["cat"])
    with patch(POPEN, side_effect=_fake_popen(captured)):
        await handler.handle(md_msg)

    assert b"```python" in captured["input"]
    assert b"print('hello')" in captured["input"]


@pytest.mark.asyncio
async def test_env_carries_chain_fields_to_the_wake():
    captured = {}
    handler = DirectInvocationHandler(command=["true"])
    chained = AgentMessage.create(
        from_="wren", to="sparrow", subject="s", body="b", hops=3, origin="planner",
    )
    old_peer = AgentMessage.from_json(json.dumps({
        "id": "old-1", "from": "wren", "to": "sparrow",
        "ts": "2026-10-01T00:00:00+00:00", "subject": "s", "body": "b",
    }))
    with patch(POPEN, side_effect=_fake_popen(captured)):
        await handler.handle(chained)
    env = captured["kwargs"]["env"]
    assert (env["SWARMBUS_HOPS"], env["SWARMBUS_ORIGIN"]) == ("3", "planner")
    with patch(POPEN, side_effect=_fake_popen(captured)):
        await handler.handle(old_peer)
    env = captured["kwargs"]["env"]
    assert (env["SWARMBUS_HOPS"], env["SWARMBUS_ORIGIN"]) == ("0", "wren")


# --- timeout -----------------------------------------------------------------

def test_timeout_defaults_and_disabling():
    assert DirectInvocationHandler().timeout == DEFAULT_INVOKE_TIMEOUT == 900.0
    assert DirectInvocationHandler(timeout=30).timeout == 30.0
    assert DirectInvocationHandler(timeout=0).timeout is None
    assert DirectInvocationHandler(timeout=None).timeout is None


@pytest.mark.asyncio
async def test_timeout_is_passed_to_the_child_wait(msg):
    captured = {}
    with patch(POPEN, side_effect=_fake_popen(captured)):
        await DirectInvocationHandler(command=["true"], timeout=12.5).handle(msg)
    assert captured["timeout"] == 12.5
    with patch(POPEN, side_effect=_fake_popen(captured)):
        await DirectInvocationHandler(command=["true"], timeout=None).handle(msg)
    assert captured["timeout"] is None


def _alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    stat = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)],
                          capture_output=True, text=True).stdout.strip()
    return bool(stat) and not stat.startswith("Z")


@pytest.mark.asyncio
async def test_timeout_kills_the_wake_and_its_children_and_logs(msg, tmp_path, caplog):
    """A hung wake is killed on expiry, children included, and the daemon moves on."""
    pidfile = tmp_path / "child.pid"
    hang = tmp_path / "hang.sh"
    hang.write_text(
        "#!/bin/sh\n"
        "sleep 60 &\n"            # the agent process a real wake would pipe into
        'echo $! > "$1"\n'
        "cat > /dev/null\n"       # drain stdin like the wake scripts do
        "wait\n"
    )
    handler = DirectInvocationHandler(command=["sh", str(hang), str(pidfile)], timeout=0.5)

    started = time.monotonic()
    with caplog.at_level(logging.WARNING, logger="swarmbus.handlers.direct_invoke"):
        await handler.handle(msg)  # must return, must not raise
    assert time.monotonic() - started < 15

    assert "timed out after 0.5s" in caplog.text
    assert msg.id in caplog.text and "killed" in caplog.text

    child = int(pidfile.read_text())
    deadline = time.monotonic() + 5
    while _alive(child) and time.monotonic() < deadline:
        time.sleep(0.05)
    assert not _alive(child), "grandchild survived the timeout kill"


@pytest.mark.asyncio
async def test_fast_command_under_timeout_logs_nothing(msg, caplog):
    handler = DirectInvocationHandler(command=["true"], timeout=30)
    with caplog.at_level(logging.WARNING, logger="swarmbus.handlers.direct_invoke"):
        await handler.handle(msg)
    assert caplog.text == ""


@pytest.mark.asyncio
async def test_wake_failure_log_line_sanitizes_the_message_id(caplog):
    msg = AgentMessage.create(from_="wren", to="sparrow", subject="s", body="b")
    msg = msg.model_copy(update={"id": "m-1\nFORGED log line"})
    proc = MagicMock(returncode=3)
    proc.communicate.return_value = (None, None)
    with caplog.at_level(logging.WARNING, logger="swarmbus.handlers.direct_invoke"), \
         patch(POPEN, return_value=proc):
        await DirectInvocationHandler(command=["false"]).handle(msg)
    [record] = [r for r in caplog.records if r.name == "swarmbus.handlers.direct_invoke"]
    assert "\n" not in record.getMessage()
    assert "m-1 FORGED log line" in record.getMessage()
