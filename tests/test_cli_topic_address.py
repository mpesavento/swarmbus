"""`--topic-address` reaches the runtime behind every MCP command.

An address that one command drops fails silently: its runtime announces and
listens at one level while its peers, and a broker ACL, expect two. So each
command runs for real up to the point where it would start a runtime or a
daemon, and the tests read what that point was handed. Expected topics are
built with ``TopicMap``; ``tests/test_topics.py`` pins the literals.
"""
from __future__ import annotations

from contextlib import contextmanager
from unittest.mock import AsyncMock, patch

import pytest
from click.testing import CliRunner

from swarmbus.cli import main
from swarmbus.topics import DEFAULT_TOPICS

AGENT = "flock__wren"
ADDRESS = "flock/wren"

COMMANDS = {
    "mcp-server": ["mcp-server"],
    "mcp-daemon": ["mcp-daemon", "--socket", "wren.sock"],
    "mcp-attach": ["mcp-attach"],
}
EACH_COMMAND = pytest.mark.parametrize("command", sorted(COMMANDS))


@contextmanager
def _nothing_started():
    """Stop each command at the point where it would start something."""
    with patch("swarmbus.mcp_server.run_mcp_server") as server, \
            patch("swarmbus.cli.run_mcp_daemon") as daemon, \
            patch("swarmbus.cli.ensure_daemon") as attach, \
            patch("swarmbus.cli.run_mcp_proxy"):
        yield {
            "mcp-server": server,
            "mcp-daemon": daemon,
            "mcp-attach": attach,
        }


def _run(command, *extra, agent_id=AGENT, env=None):
    with _nothing_started() as started:
        result = CliRunner().invoke(
            main,
            [*COMMANDS[command], "--agent-id", agent_id, *extra],
            env=env,
        )
    return result, started[command]


def _address_handed_over(command, start):
    kwargs = start.call_args.kwargs
    if command == "mcp-attach":
        # The fingerprint a running daemon is compared against.
        return kwargs["settings"]["topic_address"]
    return kwargs["topic_address"]


def _assert_lives_at_the_address(runtime):
    assert runtime._inbox_topic() == DEFAULT_TOPICS.inbox(
        AGENT, topic_address=ADDRESS
    )
    # The complete address a broker ACL matches with %c.
    assert runtime._client_kwargs()["identifier"] == ADDRESS


# ---------------------------------------------------------------------------
# where the address comes from
# ---------------------------------------------------------------------------


@EACH_COMMAND
def test_the_address_arrives_by_flag_or_environment(command):
    result, start = _run(command, "--topic-address", ADDRESS)
    assert result.exit_code == 0, result.output
    assert _address_handed_over(command, start) == ADDRESS

    result, start = _run(command, env={"SWARMBUS_TOPIC_ADDRESS": ADDRESS})
    assert result.exit_code == 0, result.output
    assert _address_handed_over(command, start) == ADDRESS


@EACH_COMMAND
@pytest.mark.parametrize(
    "extra, env",
    [
        ((), None),
        ((), {"SWARMBUS_TOPIC_ADDRESS": ""}),
        (("--topic-address", ""), {"SWARMBUS_TOPIC_ADDRESS": ADDRESS}),
        (("--topic-address", AGENT), None),
    ],
    ids=["unset", "empty-env", "empty-flag-over-env", "the-agent-id-itself"],
)
def test_anything_but_a_two_level_address_leaves_the_agent_at_one_level(
    command, extra, env
):
    """One level has one spelling by the time it is handed over: attach
    compares it with a running daemon's, and two spellings of the same
    address would read as a mismatch."""
    result, start = _run(command, *extra, env=env)

    assert result.exit_code == 0, result.output
    assert _address_handed_over(command, start) is None


@EACH_COMMAND
@pytest.mark.parametrize(
    "agent_id, address",
    [
        (AGENT, "rook/wren"),
        (AGENT, "flock/wren/nest"),
        (AGENT, "flock/"),
        ("a___b", "a_/b"),
    ],
    ids=["another-agents", "three-levels", "empty-level", "ambiguous"],
)
def test_an_address_the_agent_does_not_own_is_refused_before_anything_starts(
    command, agent_id, address
):
    result, start = _run(
        command, "--topic-address", address, agent_id=agent_id
    )

    assert result.exit_code == 2, result.output
    assert "--topic-address" in result.output
    start.assert_not_called()


# ---------------------------------------------------------------------------
# what is started with it
# ---------------------------------------------------------------------------


def test_mcp_server_runs_a_runtime_at_the_address():
    with patch("swarmbus.mcp_server.create_mcp_app") as create_app, \
            patch("mcp.server.fastmcp.FastMCP"):
        result = CliRunner().invoke(
            main,
            ["mcp-server", "--agent-id", AGENT, "--topic-address", ADDRESS],
        )

    assert result.exit_code == 0, result.output
    _assert_lives_at_the_address(create_app.call_args.args[0])


def test_mcp_daemon_runs_and_reports_a_runtime_at_the_address(tmp_path):
    with patch("swarmbus.local_rpc.LocalRuntimeDaemon") as daemon:
        daemon.return_value.wait_for_shutdown = AsyncMock()
        result = CliRunner().invoke(
            main,
            ["mcp-daemon", "--socket", str(tmp_path / "wren.sock"),
             "--agent-id", AGENT, "--topic-address", ADDRESS],
        )

    assert result.exit_code == 0, result.output
    _assert_lives_at_the_address(daemon.call_args.args[0])
    # What an attaching session compares its own address against.
    assert daemon.call_args.kwargs["settings"]["topic_address"] == ADDRESS


@pytest.mark.parametrize(
    "address, in_daemon_env",
    [(ADDRESS, AGENT), (None, ADDRESS)],
    ids=["two-level", "one-level"],
)
def test_mcp_attach_starts_the_daemon_at_its_own_address(
    address, in_daemon_env
):
    extra = [] if address is None else ["--topic-address", address]
    result, ensure = _run("mcp-attach", *extra)
    assert result.exit_code == 0, result.output

    # The argv attach builds is what mcp-daemon parses: run it through the
    # real command. The daemon's environment holds the other address this
    # agent could use, which must not win.
    daemon_args = ensure.call_args.kwargs["daemon_argv"][3:]
    with patch("swarmbus.cli.run_mcp_daemon") as run_daemon:
        result = CliRunner().invoke(
            main, daemon_args, env={"SWARMBUS_TOPIC_ADDRESS": in_daemon_env}
        )
    assert result.exit_code == 0, result.output
    assert run_daemon.call_args.kwargs["topic_address"] == address
