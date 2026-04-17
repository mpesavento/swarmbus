from unittest.mock import patch, AsyncMock
from click.testing import CliRunner
import aiomqtt
from swarmbus.cli import main


def test_send_inline_body(tmp_path):
    runner = CliRunner()
    with patch("swarmbus.cli.AgentBus") as MockBus:
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
    assert call_kwargs["reply_to"] is None


def test_send_outbox_flag_passed_through():
    runner = CliRunner()
    with patch("swarmbus.cli.AgentBus") as MockBus:
        instance = MockBus.return_value
        instance.send = AsyncMock()
        result = runner.invoke(main, [
            "send", "--agent-id", "sparrow", "--to", "wren",
            "--subject", "x", "--body", "y",
            "--outbox", "/tmp/sparrow-outbox.md",
        ])
    assert result.exit_code == 0, result.output
    assert instance.send.call_args.kwargs["outbox_path"] == "/tmp/sparrow-outbox.md"


def test_send_outbox_from_shared_env_var():
    runner = CliRunner()
    with patch("swarmbus.cli.AgentBus") as MockBus:
        instance = MockBus.return_value
        instance.send = AsyncMock()
        result = runner.invoke(
            main,
            ["send", "--agent-id", "sparrow", "--to", "wren",
             "--subject", "x", "--body", "y"],
            env={"SWARMBUS_OUTBOX": "/tmp/env-outbox.md"},
        )
    assert result.exit_code == 0, result.output
    assert instance.send.call_args.kwargs["outbox_path"] == "/tmp/env-outbox.md"


def test_send_outbox_agent_scoped_env_wins_over_shared():
    """SWARMBUS_OUTBOX_<ID> must take precedence over SWARMBUS_OUTBOX."""
    runner = CliRunner()
    with patch("swarmbus.cli.AgentBus") as MockBus:
        instance = MockBus.return_value
        instance.send = AsyncMock()
        result = runner.invoke(
            main,
            ["send", "--agent-id", "sparrow", "--to", "wren",
             "--subject", "x", "--body", "y"],
            env={
                "SWARMBUS_OUTBOX": "/tmp/shared.md",
                "SWARMBUS_OUTBOX_SPARROW": "/tmp/sparrow-specific.md",
            },
        )
    assert result.exit_code == 0, result.output
    assert instance.send.call_args.kwargs["outbox_path"] == "/tmp/sparrow-specific.md"


def test_send_outbox_explicit_flag_wins_over_env():
    """--outbox must take precedence over any env var."""
    runner = CliRunner()
    with patch("swarmbus.cli.AgentBus") as MockBus:
        instance = MockBus.return_value
        instance.send = AsyncMock()
        result = runner.invoke(
            main,
            ["send", "--agent-id", "sparrow", "--to", "wren",
             "--subject", "x", "--body", "y",
             "--outbox", "/tmp/flag.md"],
            env={
                "SWARMBUS_OUTBOX": "/tmp/shared.md",
                "SWARMBUS_OUTBOX_SPARROW": "/tmp/scoped.md",
            },
        )
    assert result.exit_code == 0, result.output
    assert instance.send.call_args.kwargs["outbox_path"] == "/tmp/flag.md"


def test_send_outbox_agent_id_with_hyphens_in_env_var():
    """wren-beta → SWARMBUS_OUTBOX_WREN_BETA (hyphen -> underscore, upper)."""
    runner = CliRunner()
    with patch("swarmbus.cli.AgentBus") as MockBus:
        instance = MockBus.return_value
        instance.send = AsyncMock()
        result = runner.invoke(
            main,
            ["send", "--agent-id", "wren-beta", "--to", "sparrow",
             "--subject", "x", "--body", "y"],
            env={"SWARMBUS_OUTBOX_WREN_BETA": "/tmp/wren-beta.md"},
        )
    assert result.exit_code == 0, result.output
    assert instance.send.call_args.kwargs["outbox_path"] == "/tmp/wren-beta.md"


def test_send_outbox_template_substitution_via_env():
    """`{agent_id}` in shared SWARMBUS_OUTBOX survives to the bus call (substitution happens in AgentBus.send)."""
    runner = CliRunner()
    with patch("swarmbus.cli.AgentBus") as MockBus:
        instance = MockBus.return_value
        instance.send = AsyncMock()
        result = runner.invoke(
            main,
            ["send", "--agent-id", "sparrow", "--to", "wren",
             "--subject", "x", "--body", "y"],
            env={"SWARMBUS_OUTBOX": "/tmp/{agent_id}-outbox.md"},
        )
    assert result.exit_code == 0, result.output
    assert instance.send.call_args.kwargs["outbox_path"] == "/tmp/{agent_id}-outbox.md"


def test_send_priority_roundtrips_cli_to_envelope():
    """Regression: the --priority CLI flag must actually thread through
    to AgentBus.send's priority kwarg. An earlier version had no --priority
    flag at all — priority=high claims in docs and wake wrappers were
    untestable from the shell. Never again."""
    runner = CliRunner()
    with patch("swarmbus.cli.AgentBus") as MockBus:
        instance = MockBus.return_value
        instance.send = AsyncMock()
        result = runner.invoke(main, [
            "send", "--agent-id", "sparrow", "--to", "wren",
            "--subject", "x", "--body", "y",
            "--priority", "high",
        ])
    assert result.exit_code == 0, result.output
    assert instance.send.call_args.kwargs["priority"] == "high"


def test_send_priority_default_is_normal():
    runner = CliRunner()
    with patch("swarmbus.cli.AgentBus") as MockBus:
        instance = MockBus.return_value
        instance.send = AsyncMock()
        result = runner.invoke(main, [
            "send", "--agent-id", "sparrow", "--to", "wren",
            "--subject", "x", "--body", "y",
        ])
    assert result.exit_code == 0, result.output
    assert instance.send.call_args.kwargs["priority"] == "normal"


def test_send_priority_rejects_unknown_at_cli():
    """CLI level accepts only the canonical set — prevents typos. The
    wire envelope accepts any string for forward-compat, but operators
    typing swarmbus send on the command line shouldn't silently send
    'hight' and have the wake gate miss it."""
    runner = CliRunner()
    result = runner.invoke(main, [
        "send", "--agent-id", "sparrow", "--to", "wren",
        "--subject", "x", "--body", "y",
        "--priority", "hight",
    ])
    assert result.exit_code != 0
    assert "hight" in result.output or "Invalid value" in result.output


def test_send_content_type_roundtrips():
    runner = CliRunner()
    with patch("swarmbus.cli.AgentBus") as MockBus:
        instance = MockBus.return_value
        instance.send = AsyncMock()
        runner.invoke(main, [
            "send", "--agent-id", "sparrow", "--to", "wren",
            "--subject", "x", "--body", "y",
            "--content-type", "text/markdown",
        ])
    assert instance.send.call_args.kwargs["content_type"] == "text/markdown"


def test_send_reply_to_roundtrips():
    runner = CliRunner()
    with patch("swarmbus.cli.AgentBus") as MockBus:
        instance = MockBus.return_value
        instance.send = AsyncMock()
        result = runner.invoke(main, [
            "send",
            "--agent-id", "sparrow",
            "--to", "wren",
            "--subject", "q",
            "--body", "?",
            "--reply-to", "sparrow",
        ])
    assert result.exit_code == 0, result.output
    assert instance.send.call_args.kwargs["reply_to"] == "sparrow"


def test_send_body_file(tmp_path):
    report = tmp_path / "report.md"
    report.write_text("# Report\nsome content")
    runner = CliRunner()
    with patch("swarmbus.cli.AgentBus") as MockBus:
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
    with patch("swarmbus.cli.AgentBus") as MockBus:
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
    assert "swarmbus" in result.output


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
    with patch("swarmbus.cli.AgentBus") as MockBus:
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
    with patch("swarmbus.cli.AgentBus") as MockBus:
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
    with patch("swarmbus.cli.AgentBus") as MockBus:
        instance = MockBus.return_value
        instance.read_inbox = AsyncMock(return_value=[msg])
        result = runner.invoke(main, ["read", "--agent-id", "sparrow", "--json"])
    assert result.exit_code == 0, result.output
    import json as _json
    parsed = _json.loads(result.output)
    assert parsed == [msg]


def test_watch_timeout_exits_1():
    runner = CliRunner()
    with patch("swarmbus.cli.AgentBus") as MockBus:
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
    with patch("swarmbus.cli.AgentBus") as MockBus:
        instance = MockBus.return_value
        instance.watch_inbox = AsyncMock(return_value=msg)
        result = runner.invoke(main, ["watch", "--agent-id", "sparrow", "--timeout", "1"])
    assert result.exit_code == 0, result.output
    assert "pong" in result.output
    assert "got it" in result.output


def test_list_empty():
    runner = CliRunner()
    with patch("swarmbus.cli.AgentBus.probe") as MockProbe:
        instance = MockProbe.return_value
        instance.list_agents = AsyncMock(return_value=[])
        result = runner.invoke(main, ["list"])
    assert result.exit_code == 0, result.output
    assert "no agents online" in result.output


def test_list_prints_agents():
    runner = CliRunner()
    with patch("swarmbus.cli.AgentBus.probe") as MockProbe:
        instance = MockProbe.return_value
        instance.list_agents = AsyncMock(return_value=["sparrow", "wren"])
        result = runner.invoke(main, ["list"])
    assert result.exit_code == 0, result.output
    assert "sparrow" in result.output
    assert "wren" in result.output


def test_tail_reads_full_file_on_first_call(tmp_path):
    inbox = tmp_path / "inbox.md"
    inbox.write_text("\n## [2026-04-14 10:00] From: wren | hi\nhello sparrow\n")
    runner = CliRunner()
    result = runner.invoke(main, [
        "tail",
        "--agent-id", "sparrow",
        "--inbox", str(inbox),
        "--cursor-dir", str(tmp_path / "cursors"),
    ])
    assert result.exit_code == 0, result.output
    assert "hello sparrow" in result.output
    # Second call should print nothing new.
    result2 = runner.invoke(main, [
        "tail",
        "--agent-id", "sparrow",
        "--inbox", str(inbox),
        "--cursor-dir", str(tmp_path / "cursors"),
    ])
    assert result2.exit_code == 0
    assert result2.output == ""


def test_tail_only_shows_new_content_since_cursor(tmp_path):
    inbox = tmp_path / "inbox.md"
    cursors = tmp_path / "cursors"
    inbox.write_text("\n## [10:00] From: wren | first\nearly message\n")
    runner = CliRunner()
    runner.invoke(main, [
        "tail", "--agent-id", "sparrow",
        "--inbox", str(inbox), "--cursor-dir", str(cursors),
    ])
    # Append a new entry.
    with inbox.open("a") as f:
        f.write("\n## [10:01] From: wren | second\nfollowup\n")
    result = runner.invoke(main, [
        "tail", "--agent-id", "sparrow",
        "--inbox", str(inbox), "--cursor-dir", str(cursors),
    ])
    assert "followup" in result.output
    assert "early message" not in result.output


def test_tail_reset_flag_rereads_from_start(tmp_path):
    inbox = tmp_path / "inbox.md"
    cursors = tmp_path / "cursors"
    inbox.write_text("whole body\n")
    runner = CliRunner()
    # First call — consumes.
    runner.invoke(main, [
        "tail", "--agent-id", "sparrow",
        "--inbox", str(inbox), "--cursor-dir", str(cursors),
    ])
    # Second call with --reset — re-reads everything.
    result = runner.invoke(main, [
        "tail", "--agent-id", "sparrow",
        "--inbox", str(inbox), "--cursor-dir", str(cursors),
        "--reset",
    ])
    assert "whole body" in result.output


def test_tail_separate_consumers_have_independent_cursors(tmp_path):
    inbox = tmp_path / "inbox.md"
    cursors = tmp_path / "cursors"
    inbox.write_text("message A\n")
    runner = CliRunner()
    # Consumer "bot" reads.
    result1 = runner.invoke(main, [
        "tail", "--agent-id", "sparrow",
        "--inbox", str(inbox), "--cursor-dir", str(cursors),
        "--consumer", "bot",
    ])
    assert "message A" in result1.output
    # Consumer "human" reads the same file — should still see everything.
    result2 = runner.invoke(main, [
        "tail", "--agent-id", "sparrow",
        "--inbox", str(inbox), "--cursor-dir", str(cursors),
        "--consumer", "human",
    ])
    assert "message A" in result2.output


def test_tail_missing_inbox_exits_2(tmp_path):
    runner = CliRunner()
    result = runner.invoke(main, [
        "tail", "--agent-id", "sparrow",
        "--inbox", str(tmp_path / "does-not-exist.md"),
        "--cursor-dir", str(tmp_path / "cursors"),
    ])
    assert result.exit_code == 2
    assert "inbox does not exist" in result.output


def test_tail_detects_inode_change_and_rereads(tmp_path):
    """If the inbox file is replaced (different inode) with content ≥ old
    cursor, tail must detect the inode change and re-read from 0 rather than
    silently seeking mid-file into the replacement.
    """
    import os
    inbox = tmp_path / "inbox.md"
    cursors = tmp_path / "cursors"
    inbox.write_text("original content one\noriginal content two\n")
    runner = CliRunner()
    # First call consumes the whole original file.
    runner.invoke(main, [
        "tail", "--agent-id", "sparrow",
        "--inbox", str(inbox), "--cursor-dir", str(cursors),
    ])
    # Replace the file at the same path — new inode, enough content to
    # exceed the old cursor size.
    os.remove(inbox)
    inbox.write_text("replacement A\nreplacement B\nreplacement C\n")
    result = runner.invoke(main, [
        "tail", "--agent-id", "sparrow",
        "--inbox", str(inbox), "--cursor-dir", str(cursors),
    ])
    assert "inode changed" in result.output
    assert "replacement A" in result.output
    # Original content should NOT reappear in the replacement read.
    assert "original content one" not in result.output


def test_tail_cursor_legacy_format_reads_fresh(tmp_path):
    """A legacy cursor file with just '<offset>' (no inode) should still be
    honoured on next call — the first read writes the inode back, and no
    spurious re-emit happens just because the stored inode was missing."""
    inbox = tmp_path / "inbox.md"
    cursors = tmp_path / "cursors"
    cursors.mkdir()
    inbox.write_text("line one\nline two\n")
    cursor_file = cursors / "sparrow--default.cursor"
    # Simulate a legacy cursor: offset-only, no inode.
    cursor_file.write_text(str(len(inbox.read_bytes())))
    runner = CliRunner()
    result = runner.invoke(main, [
        "tail", "--agent-id", "sparrow",
        "--inbox", str(inbox), "--cursor-dir", str(cursors),
    ])
    assert result.exit_code == 0, result.output
    # No new content to emit (offset == size), and no "inode changed" noise.
    assert "inode changed" not in result.output
    assert "line one" not in result.output
    # Cursor should now have the inode recorded.
    assert len(cursor_file.read_text().split()) == 2


def test_tail_rejects_path_traversal_consumer(tmp_path):
    inbox = tmp_path / "inbox.md"
    inbox.write_text("anything\n")
    runner = CliRunner()
    for bad in ["../escape", "foo/bar", "with space", "x" * 200]:
        result = runner.invoke(main, [
            "tail", "--agent-id", "sparrow",
            "--inbox", str(inbox),
            "--cursor-dir", str(tmp_path / "cursors"),
            "--consumer", bad,
        ])
        assert result.exit_code == 2, f"expected reject for {bad!r}, got {result.output}"
        assert "invalid --consumer" in result.output


def test_tail_default_inbox_path_uses_home(tmp_path, monkeypatch):
    """Without --inbox, defaults to ~/sync/<agent-id>-inbox.md."""
    monkeypatch.setenv("HOME", str(tmp_path))
    fake_home_inbox = tmp_path / "sync" / "sparrow-inbox.md"
    fake_home_inbox.parent.mkdir(parents=True)
    fake_home_inbox.write_text("default-path content\n")
    runner = CliRunner()
    result = runner.invoke(main, [
        "tail", "--agent-id", "sparrow",
        "--cursor-dir", str(tmp_path / "cursors"),
    ])
    assert result.exit_code == 0, result.output
    assert "default-path content" in result.output


def test_tail_cursor_write_is_atomic(tmp_path):
    """Tmp file used for write; final cursor never empty after partial write."""
    inbox = tmp_path / "inbox.md"
    cursors = tmp_path / "cursors"
    inbox.write_text("payload\n")
    runner = CliRunner()
    runner.invoke(main, [
        "tail", "--agent-id", "sparrow",
        "--inbox", str(inbox), "--cursor-dir", str(cursors),
    ])
    cursor_file = cursors / "sparrow--default.cursor"
    # The atomic write means no .tmp leftover.
    assert cursor_file.exists()
    assert not (cursors / "sparrow--default.tmp").exists()
    # Cursor content must parse: "<offset> <inode>" (post-inode-tracking).
    parts = cursor_file.read_text().strip().split()
    assert len(parts) == 2
    int(parts[0]); int(parts[1])


def test_tail_handles_file_truncation(tmp_path):
    inbox = tmp_path / "inbox.md"
    cursors = tmp_path / "cursors"
    inbox.write_text("long original content\n")
    runner = CliRunner()
    runner.invoke(main, [
        "tail", "--agent-id", "sparrow",
        "--inbox", str(inbox), "--cursor-dir", str(cursors),
    ])
    # Truncate the file (rotation / manual edit / shell > redirection).
    inbox.write_text("short\n")
    result = runner.invoke(main, [
        "tail", "--agent-id", "sparrow",
        "--inbox", str(inbox), "--cursor-dir", str(cursors),
    ])
    assert "short" in result.output
    assert "inbox shrank" in result.output


def test_read_broker_unreachable_exits_2():
    runner = CliRunner()
    with patch("swarmbus.cli.AgentBus") as MockBus:
        instance = MockBus.return_value
        instance.read_inbox = AsyncMock(side_effect=aiomqtt.MqttError("Connection refused"))
        result = runner.invoke(main, ["read", "--agent-id", "sparrow"])
    assert result.exit_code == 2
    assert "broker unreachable" in result.output
    assert "Traceback" not in result.output


def test_watch_broker_unreachable_exits_2():
    runner = CliRunner()
    with patch("swarmbus.cli.AgentBus") as MockBus:
        instance = MockBus.return_value
        instance.watch_inbox = AsyncMock(side_effect=aiomqtt.MqttError("Connection refused"))
        result = runner.invoke(main, ["watch", "--agent-id", "sparrow", "--timeout", "1"])
    assert result.exit_code == 2
    assert "broker unreachable" in result.output


def test_list_broker_unreachable_exits_2():
    runner = CliRunner()
    with patch("swarmbus.cli.AgentBus.probe") as MockProbe:
        instance = MockProbe.return_value
        instance.list_agents = AsyncMock(side_effect=aiomqtt.MqttError("Connection refused"))
        result = runner.invoke(main, ["list"])
    assert result.exit_code == 2
    assert "broker unreachable" in result.output


def test_send_broker_unreachable_clean_error():
    """MqttError → friendly stderr message, exit 2, no traceback."""
    runner = CliRunner()
    with patch("swarmbus.cli.AgentBus") as MockBus:
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
    with patch("swarmbus.cli.AgentBus") as MockBus, \
         patch("swarmbus.cli.DirectInvocationHandler") as MockHandler:
        instance = MockBus.return_value
        instance.run = lambda: None  # no-op so start returns
        runner.invoke(main, [
            "start", "--agent-id", "t",
            "--invoke", "bash -c 'echo $SWARMBUS_FROM'",
        ])
        MockHandler.assert_called_once_with(
            command=["bash", "-c", "echo $SWARMBUS_FROM"]
        )


def test_list_json():
    runner = CliRunner()
    with patch("swarmbus.cli.AgentBus.probe") as MockProbe:
        instance = MockProbe.return_value
        instance.list_agents = AsyncMock(return_value=["sparrow", "wren"])
        result = runner.invoke(main, ["list", "--json"])
    assert result.exit_code == 0, result.output
    import json as _json
    assert _json.loads(result.output) == ["sparrow", "wren"]
