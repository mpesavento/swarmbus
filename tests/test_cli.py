from unittest.mock import patch, AsyncMock
from click.testing import CliRunner
import aiomqtt
from agentbus.cli import main


def test_send_inline_body(tmp_path):
    runner = CliRunner()
    with patch("agentbus.cli.AgentBus") as MockBus:
        instance = MockBus.return_value
        instance.send = AsyncMock()
        result = runner.invoke(main, [
            "send",
            "--agent-id", "sparrow",
            "--to", "wren",
            "--subject", "hello",
            "--body", "world",
        ])
    assert result.exit_code == 0, result.output
    instance.send.assert_called_once()
    call_kwargs = instance.send.call_args.kwargs
    assert call_kwargs["to"] == "wren"
    assert call_kwargs["subject"] == "hello"
    assert call_kwargs["body"] == "world"


def test_send_body_file(tmp_path):
    report = tmp_path / "report.md"
    report.write_text("# Report\nsome content")
    runner = CliRunner()
    with patch("agentbus.cli.AgentBus") as MockBus:
        instance = MockBus.return_value
        instance.send = AsyncMock()
        result = runner.invoke(main, [
            "send",
            "--agent-id", "sparrow",
            "--to", "wren",
            "--subject", "report",
            "--body-file", str(report),
        ])
    assert result.exit_code == 0, result.output
    call_kwargs = instance.send.call_args.kwargs
    assert call_kwargs["body"] == "# Report\nsome content"


def test_send_body_file_stdin():
    runner = CliRunner()
    with patch("agentbus.cli.AgentBus") as MockBus:
        instance = MockBus.return_value
        instance.send = AsyncMock()
        result = runner.invoke(main, [
            "send",
            "--agent-id", "sparrow",
            "--to", "wren",
            "--subject", "piped",
            "--body-file", "-",
        ], input="piped content")
    assert result.exit_code == 0, result.output
    call_kwargs = instance.send.call_args.kwargs
    assert call_kwargs["body"] == "piped content"


def test_send_body_and_body_file_mutually_exclusive(tmp_path):
    report = tmp_path / "report.md"
    report.write_text("content")
    runner = CliRunner()
    result = runner.invoke(main, [
        "send",
        "--agent-id", "sparrow",
        "--to", "wren",
        "--subject", "hello",
        "--body", "inline",
        "--body-file", str(report),
    ])
    assert result.exit_code != 0
    assert "mutually exclusive" in result.output


def test_send_body_required():
    runner = CliRunner()
    result = runner.invoke(main, [
        "send",
        "--agent-id", "sparrow",
        "--to", "wren",
        "--subject", "hello",
    ])
    assert result.exit_code != 0
    assert "required" in result.output


def test_send_missing_required_options():
    runner = CliRunner()
    result = runner.invoke(main, ["send", "--agent-id", "sparrow"])
    assert result.exit_code != 0
    assert "Missing option" in result.output


def test_help():
    runner = CliRunner()
    result = runner.invoke(main, ["--help"])
    assert result.exit_code == 0
    assert "agentbus" in result.output


def test_send_help():
    runner = CliRunner()
    result = runner.invoke(main, ["send", "--help"])
    assert result.exit_code == 0
    assert "--to" in result.output
    assert "--subject" in result.output
    assert "--body" in result.output
    assert "--body-file" in result.output


def test_read_empty_inbox():
    runner = CliRunner()
    with patch("agentbus.cli.AgentBus") as MockBus:
        instance = MockBus.return_value
        instance.read_inbox = AsyncMock(return_value=[])
        result = runner.invoke(main, ["read", "--agent-id", "sparrow"])
    assert result.exit_code == 0, result.output
    assert "inbox empty" in result.output


def test_read_pretty_output():
    runner = CliRunner()
    msg = {
        "id": "abc", "from": "wren", "to": "sparrow",
        "ts": "2026-04-14T05:00:00Z", "subject": "hi", "body": "hello there",
        "content_type": "text/plain", "priority": "normal", "reply_to": None,
    }
    with patch("agentbus.cli.AgentBus") as MockBus:
        instance = MockBus.return_value
        instance.read_inbox = AsyncMock(return_value=[msg])
        result = runner.invoke(main, ["read", "--agent-id", "sparrow"])
    assert result.exit_code == 0, result.output
    assert "from wren" in result.output
    assert "subject: hi" in result.output
    assert "hello there" in result.output


def test_read_json_output():
    runner = CliRunner()
    msg = {"id": "abc", "from": "wren", "subject": "hi", "body": "x"}
    with patch("agentbus.cli.AgentBus") as MockBus:
        instance = MockBus.return_value
        instance.read_inbox = AsyncMock(return_value=[msg])
        result = runner.invoke(main, ["read", "--agent-id", "sparrow", "--json"])
    assert result.exit_code == 0, result.output
    import json as _json
    parsed = _json.loads(result.output)
    assert parsed == [msg]


def test_watch_timeout_exits_1():
    runner = CliRunner()
    with patch("agentbus.cli.AgentBus") as MockBus:
        instance = MockBus.return_value
        instance.watch_inbox = AsyncMock(return_value=None)
        result = runner.invoke(main, ["watch", "--agent-id", "sparrow", "--timeout", "1"])
    assert result.exit_code == 1
    assert "timeout" in result.output


def test_watch_returns_message():
    runner = CliRunner()
    msg = {
        "id": "x", "from": "wren", "to": "sparrow",
        "ts": "2026-04-14T05:00:00Z", "subject": "pong", "body": "got it",
        "content_type": "text/plain", "priority": "normal", "reply_to": "wren",
    }
    with patch("agentbus.cli.AgentBus") as MockBus:
        instance = MockBus.return_value
        instance.watch_inbox = AsyncMock(return_value=msg)
        result = runner.invoke(main, ["watch", "--agent-id", "sparrow", "--timeout", "1"])
    assert result.exit_code == 0, result.output
    assert "pong" in result.output
    assert "got it" in result.output


def test_list_empty():
    runner = CliRunner()
    with patch("agentbus.cli.AgentBus") as MockBus:
        instance = MockBus.return_value
        instance.list_agents = AsyncMock(return_value=[])
        result = runner.invoke(main, ["list"])
    assert result.exit_code == 0, result.output
    assert "no agents online" in result.output


def test_list_prints_agents():
    runner = CliRunner()
    with patch("agentbus.cli.AgentBus") as MockBus:
        instance = MockBus.return_value
        instance.list_agents = AsyncMock(return_value=["sparrow", "wren"])
        result = runner.invoke(main, ["list"])
    assert result.exit_code == 0, result.output
    assert "sparrow" in result.output
    assert "wren" in result.output


def test_send_broker_unreachable_clean_error():
    """MqttError → friendly stderr message, exit 2, no traceback."""
    runner = CliRunner()
    with patch("agentbus.cli.AgentBus") as MockBus:
        instance = MockBus.return_value
        instance.send = AsyncMock(side_effect=aiomqtt.MqttError("Connection refused"))
        result = runner.invoke(main, [
            "send", "--agent-id", "sparrow", "--to", "wren",
            "--subject", "x", "--body", "y",
        ])
    assert result.exit_code == 2
    assert "broker unreachable" in result.output
    assert "Connection refused" in result.output
    assert "Traceback" not in result.output


def test_start_invoke_uses_shlex_split():
    """--invoke must tokenize with shlex so quoted args survive."""
    runner = CliRunner()
    with patch("agentbus.cli.AgentBus") as MockBus, \
         patch("agentbus.cli.DirectInvocationHandler") as MockHandler:
        instance = MockBus.return_value
        instance.run = lambda: None  # no-op so start returns
        runner.invoke(main, [
            "start", "--agent-id", "t",
            "--invoke", "bash -c 'echo $AGENTBUS_FROM'",
        ])
        MockHandler.assert_called_once_with(
            command=["bash", "-c", "echo $AGENTBUS_FROM"]
        )


def test_list_json():
    runner = CliRunner()
    with patch("agentbus.cli.AgentBus") as MockBus:
        instance = MockBus.return_value
        instance.list_agents = AsyncMock(return_value=["sparrow", "wren"])
        result = runner.invoke(main, ["list", "--json"])
    assert result.exit_code == 0, result.output
    import json as _json
    assert _json.loads(result.output) == ["sparrow", "wren"]
