import asyncio
import json
import os
import stat
import struct
import sys
from unittest.mock import AsyncMock, patch

import pytest
from click.testing import CliRunner
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from swarmbus.cli import main
from swarmbus.local_rpc import (
    MAX_FRAME_BYTES,
    LocalRPCOwnershipError,
    LocalRPCProtocolError,
    LocalRPCUnavailable,
    LocalRuntimeDaemon,
    LocalRuntimeProxy,
    _encode_frame,
    _read_frame,
    _request,
    run_mcp_daemon,
)
from swarmbus.mcp_server import create_mcp_app
from swarmbus.message import (
    DEFAULT_HOP_LIMIT,
    DEFAULT_RATE_COOLDOWN,
    DEFAULT_RATE_LIMIT,
)
from swarmbus.runtime import RuntimeFatalError, TransportUnavailable


class _FakeRuntime:
    broker = "broker.example"
    port = 1883

    def __init__(self) -> None:
        self.start = AsyncMock()
        self.stop = AsyncMock()
        self.runtime_status = AsyncMock(
            return_value={
                "ready": True,
                "connected": True,
                "state": "connected",
            }
        )
        self.send_message = AsyncMock()
        self.read_inbox = AsyncMock(return_value=[])
        self.inbox_state = AsyncMock(
            return_value={
                "cursor": {"instance_id": "runtime-1", "generation": 0},
                "reset": False,
                "runtime_state": "connected",
                "connected": True,
                "pending_count": 0,
                "pending_messages": [],
                "truncated": False,
            }
        )
        self.list_states = AsyncMock(return_value=[])
        self.get_state = AsyncMock(return_value={})
        self.update_state = AsyncMock(return_value={})


async def _raw_call(socket_path, payload):
    reader, writer = await asyncio.open_unix_connection(str(socket_path))
    writer.write(_encode_frame(payload))
    await writer.drain()
    response = await _read_frame(reader)
    writer.close()
    await writer.wait_closed()
    return response


async def _stdio_agent_state(socket_path, agent_id):
    parameters = StdioServerParameters(
        command=sys.executable,
        args=[
            "-c",
            "from swarmbus.cli import main; main()",
            "mcp-proxy",
            "--socket",
            str(socket_path),
        ],
    )
    async with stdio_client(parameters) as (read_stream, write_stream):
        async with ClientSession(read_stream, write_stream) as session:
            await session.initialize()
            return await session.call_tool(
                "agent_state",
                {"action": "get", "agent_id": agent_id},
            )


def test_frame_is_big_endian_length_prefixed_json():
    payload = _request("ping", {})
    frame = _encode_frame(payload)
    (length,) = struct.unpack("!I", frame[:4])

    assert length == len(frame) - 4
    assert json.loads(frame[4:]) == payload


@pytest.mark.asyncio
async def test_reader_rejects_oversized_frame_before_reading_body():
    reader = asyncio.StreamReader()
    reader.feed_data(struct.pack("!I", MAX_FRAME_BYTES + 1))

    with pytest.raises(LocalRPCProtocolError, match="outside"):
        await _read_frame(reader)


@pytest.mark.asyncio
async def test_daemon_rejects_non_unix_platform(monkeypatch, tmp_path):
    monkeypatch.setattr("swarmbus.local_rpc.fcntl", None)

    with pytest.raises(LocalRPCOwnershipError, match="requires a Unix platform"):
        await LocalRuntimeDaemon(
            _FakeRuntime(),
            tmp_path / "runtime.sock",
        ).start()


@pytest.mark.asyncio
async def test_ping_and_tool_contract_use_one_managed_runtime(tmp_path):
    socket_path = tmp_path / "runtime.sock"
    runtime = _FakeRuntime()
    runtime.get_state.return_value = {"agent_id": "wren"}
    daemon = LocalRuntimeDaemon(runtime, socket_path)

    async with daemon:
        proxy = LocalRuntimeProxy(socket_path)
        assert await proxy.ping() == {
            "ready": True,
            "connected": True,
            "state": "connected",
        }

        app = create_mcp_app(proxy)
        result = await app._tool_fns["agent_state"](
            action="get",
            agent_id="wren",
        )

    assert result == {"agent_id": "wren"}
    runtime.start.assert_awaited_once_with()
    runtime.runtime_status.assert_awaited_once_with()
    runtime.get_state.assert_awaited_once_with(agent_id="wren")
    runtime.stop.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_ping_reports_broker_unavailable_while_socket_is_up(tmp_path):
    socket_path = tmp_path / "runtime.sock"
    runtime = _FakeRuntime()
    runtime.runtime_status.return_value = {
        "ready": False,
        "connected": False,
        "state": "connecting",
    }

    async with LocalRuntimeDaemon(runtime, socket_path):
        assert await LocalRuntimeProxy(socket_path).ping() == {
            "ready": False,
            "connected": False,
            "state": "connecting",
        }


@pytest.mark.asyncio
async def test_inbox_wait_extends_response_timeout_past_wait_boundary():
    proxy = LocalRuntimeProxy("/unused/runtime.sock")
    proxy._call = AsyncMock(return_value=[])

    result = await proxy.read_inbox(
        ack_ids=["message-1"],
        max_messages=10,
        wait_seconds=30.0,
    )

    assert result == []
    proxy._call.assert_awaited_once_with(
        "read_inbox",
        {
            "ack_ids": ["message-1"],
            "max_messages": 10,
            "wait_seconds": 30.0,
        },
        response_timeout=35.0,
    )


@pytest.mark.asyncio
async def test_inbox_state_round_trips_without_consuming_messages(tmp_path):
    socket_path = tmp_path / "runtime.sock"
    runtime = _FakeRuntime()
    expected = {
        "cursor": {"instance_id": "runtime-1", "generation": 3},
        "reset": False,
        "runtime_state": "connected",
        "connected": True,
        "pending_count": 1,
        "pending_messages": [
            {
                "id": "message-1",
                "from": "wren",
                "received_at": "2026-10-16T22:00:00+00:00",
                "priority": "high",
            }
        ],
        "truncated": False,
    }
    runtime.inbox_state.return_value = expected

    async with LocalRuntimeDaemon(runtime, socket_path):
        result = await LocalRuntimeProxy(socket_path).inbox_state(
            cursor={"instance_id": "runtime-1", "generation": 2},
            wait_seconds=10.0,
            max_messages=50,
        )

    assert result == expected
    runtime.inbox_state.assert_awaited_once_with(
        cursor={"instance_id": "runtime-1", "generation": 2},
        wait_seconds=10.0,
        max_messages=50,
    )
    runtime.read_inbox.assert_not_awaited()


@pytest.mark.asyncio
async def test_four_clients_can_reuse_the_same_request_shape(tmp_path):
    socket_path = tmp_path / "runtime.sock"
    runtime = _FakeRuntime()

    async with LocalRuntimeDaemon(runtime, socket_path):
        responses = await asyncio.gather(
            *[
                _raw_call(socket_path, _request("ping", {}))
                for _ in range(4)
            ]
        )

    assert responses == [
        {
            "schema_version": 1,
            "ok": True,
            "result": {
                "ready": True,
                "connected": True,
                "state": "connected",
            },
        }
    ] * 4
    runtime.start.assert_awaited_once_with()
    assert runtime.runtime_status.await_count == 4
    runtime.stop.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_daemon_rejects_a_different_peer_uid(tmp_path):
    socket_path = tmp_path / "runtime.sock"
    runtime = _FakeRuntime()
    daemon = LocalRuntimeDaemon(runtime, socket_path)

    async with daemon:
        with patch.object(daemon, "_peer_uid", return_value=os.getuid() + 1):
            response = await _raw_call(socket_path, _request("ping", {}))

    assert response == {
        "schema_version": 1,
        "ok": False,
        "error": {
            "code": "permission_denied",
            "message": "peer uid does not match",
        },
    }
    runtime.inbox_state.assert_not_awaited()


@pytest.mark.asyncio
async def test_four_stdio_frontends_share_one_runtime(tmp_path):
    socket_path = tmp_path / "runtime.sock"
    runtime = _FakeRuntime()
    runtime.get_state.side_effect = (
        lambda agent_id: {"agent_id": agent_id}
    )
    agent_ids = [f"frontend-{index}" for index in range(4)]

    async with LocalRuntimeDaemon(runtime, socket_path):
        results = await asyncio.wait_for(
            asyncio.gather(
                *[
                    _stdio_agent_state(socket_path, agent_id)
                    for agent_id in agent_ids
                ]
            ),
            timeout=20,
        )

    for agent_id, result in zip(agent_ids, results):
        assert result.isError is False
        assert any(
            agent_id in getattr(content, "text", "")
            for content in result.content
        )
    assert {
        awaited.kwargs["agent_id"]
        for awaited in runtime.get_state.await_args_list
    } == set(agent_ids)
    runtime.start.assert_awaited_once_with()
    runtime.stop.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_transport_error_preserves_read_inbox_contract(tmp_path):
    socket_path = tmp_path / "runtime.sock"
    runtime = _FakeRuntime()
    runtime.read_inbox.side_effect = TransportUnavailable("broker down")

    async with LocalRuntimeDaemon(runtime, socket_path):
        app = create_mcp_app(LocalRuntimeProxy(socket_path))
        result = await app._tool_fns["read_inbox"]()

    assert result == []
    runtime.read_inbox.assert_awaited_once_with(
        ack_ids=None,
        max_messages=10,
        wait_seconds=0.0,
    )


@pytest.mark.asyncio
async def test_default_priority_is_omitted_for_old_daemon_compatibility():
    proxy = LocalRuntimeProxy("/unused/runtime.sock")
    proxy._call = AsyncMock()

    await proxy.send_message(
        to="wren",
        subject="hello",
        body="world",
        content_type="text/plain",
    )

    proxy._call.assert_awaited_once_with(
        "send_message",
        {
            "to": "wren",
            "subject": "hello",
            "body": "world",
            "content_type": "text/plain",
            "in_reply_to": None,
        },
    )


@pytest.mark.asyncio
async def test_ambiguous_mutation_is_not_retried(tmp_path):
    socket_path = tmp_path / "runtime.sock"
    runtime = _FakeRuntime()
    runtime.send_message.side_effect = RuntimeFatalError("lost response")

    async with LocalRuntimeDaemon(runtime, socket_path):
        proxy = LocalRuntimeProxy(socket_path)
        with pytest.raises(RuntimeFatalError, match="lost response"):
            await proxy.send_message(
                to="wren",
                subject="hello",
                body="world",
                content_type="text/plain",
                priority="high",
            )

    runtime.send_message.assert_awaited_once_with(
        to="wren",
        subject="hello",
        body="world",
        content_type="text/plain",
        priority="high",
        in_reply_to=None,
    )


@pytest.mark.asyncio
async def test_backend_loss_fails_without_retry(tmp_path):
    socket_path = tmp_path / "runtime.sock"
    runtime = _FakeRuntime()
    proxy = LocalRuntimeProxy(socket_path)

    async with LocalRuntimeDaemon(runtime, socket_path):
        assert await proxy.ping() == {
            "ready": True,
            "connected": True,
            "state": "connected",
        }

    with pytest.raises(LocalRPCUnavailable):
        await proxy.ping()
    app = create_mcp_app(proxy)
    assert await app._tool_fns["read_inbox"]() == []
    runtime.start.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_nonblocking_lock_allows_only_one_owner(tmp_path):
    socket_path = tmp_path / "runtime.sock"
    first_runtime = _FakeRuntime()
    second_runtime = _FakeRuntime()

    async with LocalRuntimeDaemon(first_runtime, socket_path):
        second = LocalRuntimeDaemon(second_runtime, socket_path)
        with pytest.raises(LocalRPCOwnershipError, match="already has an owner"):
            await second.start()

    second_runtime.start.assert_not_awaited()


@pytest.mark.asyncio
async def test_daemon_refuses_to_unlink_non_socket(tmp_path):
    socket_path = tmp_path / "runtime.sock"
    socket_path.write_text("operator data")
    runtime = _FakeRuntime()

    with pytest.raises(LocalRPCOwnershipError, match="non-socket"):
        await LocalRuntimeDaemon(runtime, socket_path).start()

    assert socket_path.read_text() == "operator data"
    runtime.start.assert_not_awaited()


@pytest.mark.asyncio
async def test_clean_shutdown_stops_runtime_and_removes_private_socket(tmp_path):
    socket_path = tmp_path / "runtime.sock"
    runtime = _FakeRuntime()
    daemon = LocalRuntimeDaemon(runtime, socket_path)

    await daemon.start()
    socket_mode = stat.S_IMODE(os.lstat(socket_path).st_mode)
    await daemon.stop()

    assert socket_mode == 0o600
    assert not socket_path.exists()
    runtime.stop.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_stop_failure_still_removes_socket_and_releases_lock(tmp_path):
    socket_path = tmp_path / "runtime.sock"
    runtime = _FakeRuntime()
    runtime.stop.side_effect = RuntimeError("shutdown failed")
    daemon = LocalRuntimeDaemon(runtime, socket_path)

    await daemon.start()
    with pytest.raises(RuntimeError, match="shutdown failed"):
        await daemon.stop()

    assert not socket_path.exists()
    replacement = LocalRuntimeDaemon(_FakeRuntime(), socket_path)
    await replacement.start()
    await replacement.stop()


def test_mcp_proxy_cli_only_needs_socket():
    runner = CliRunner()

    with patch("swarmbus.cli.run_mcp_proxy") as mock_run:
        result = runner.invoke(
            main,
            ["mcp-proxy", "--socket", "/private/swarmbus.sock"],
        )

    assert result.exit_code == 0, result.output
    mock_run.assert_called_once_with("/private/swarmbus.sock")


def test_mcp_daemon_cli_threads_runtime_configuration(monkeypatch):
    monkeypatch.delenv("SWARMBUS_STATE_DIR")
    runner = CliRunner()

    with patch("swarmbus.cli.run_mcp_daemon") as mock_run:
        result = runner.invoke(
            main,
            [
                "mcp-daemon",
                "--socket",
                "/private/swarmbus.sock",
                "--agent-id",
                "loom__session-abc",
                "--topic-address",
                "loom/session-abc",
                "--broker",
                "mqtt.example",
                "--topic-root",
                "loom",
                "--presence",
                "--client-id",
                "loom-singleton",
                "--capability",
                "web.read",
                "--username",
                "alice",
                "--password",
                "secret",
                "--on-trip",
                "notify --urgent",
                "--tls",
            ],
        )

    assert result.exit_code == 0, result.output
    assert mock_run.call_args.kwargs == {
        "socket_path": "/private/swarmbus.sock",
        "agent_id": "loom__session-abc",
        "topic_address": "loom/session-abc",
        "broker": "mqtt.example",
        "port": 1883,
        "persistent": False,
        "presence": True,
        "lifecycle": "named",
        "client_id": "loom-singleton",
        "capabilities": ("web.read",),
        "state_dir": "~/.local/state/swarmbus",
        "topic_root": "loom",
        "registry_heartbeat_seconds": 60,
        "registry_stale_after_seconds": 180,
        "username": "alice",
        "password": "secret",
        "tls": True,
        "ca_cert": None,
        "client_cert": None,
        "client_key": None,
        "hop_limit": DEFAULT_HOP_LIMIT,
        "rate_limit": DEFAULT_RATE_LIMIT,
        "rate_cooldown": DEFAULT_RATE_COOLDOWN,
        "on_trip": ["notify", "--urgent"],
    }


def test_run_mcp_daemon_threads_trip_alert_to_runtime():
    with patch(
        "swarmbus.local_rpc.create_managed_runtime",
        side_effect=RuntimeError("stop after construction"),
    ) as create_runtime:
        with pytest.raises(RuntimeError, match="stop after construction"):
            run_mcp_daemon(
                "/private/swarmbus.sock",
                "loom__session-abc",
                on_trip=["notify", "--urgent"],
            )

    assert create_runtime.call_args.kwargs["on_trip"] == ["notify", "--urgent"]
    assert create_runtime.call_args.kwargs["trip_listener"] == "mcp-daemon"


@pytest.mark.parametrize(
    "args",
    [
        ["--persistent"],
        ["--lifecycle", "transient"],
    ],
)
def test_mcp_daemon_rejects_modes_without_presence(args):
    runner = CliRunner()

    with patch("swarmbus.cli.run_mcp_daemon") as mock_run:
        result = runner.invoke(
            main,
            [
                "mcp-daemon",
                "--socket",
                "/private/swarmbus.sock",
                "--agent-id",
                "loom",
                *args,
            ],
        )

    assert result.exit_code == 2
    assert "requires --presence" in result.output
    mock_run.assert_not_called()


_MCP_RUNTIME_COMMANDS = [
    (
        [
            "mcp-server",
            "--agent-id",
            "loom",
        ],
        "swarmbus.mcp_server.run_mcp_server",
    ),
    (
        [
            "mcp-daemon",
            "--socket",
            "/private/swarmbus.sock",
            "--agent-id",
            "loom",
        ],
        "swarmbus.cli.run_mcp_daemon",
    ),
]


@pytest.mark.parametrize("command,patch_target", _MCP_RUNTIME_COMMANDS)
def test_mcp_runtime_commands_read_password_file(
    tmp_path,
    command,
    patch_target,
):
    password_file = tmp_path / "mqtt.password"
    password_file.write_text("file-secret\n")

    with patch(patch_target) as mock_run:
        result = CliRunner().invoke(
            main,
            [*command, "--password-file", str(password_file)],
        )

    assert result.exit_code == 0, result.output
    assert mock_run.call_args.kwargs["password"] == "file-secret"


@pytest.mark.parametrize("command,patch_target", _MCP_RUNTIME_COMMANDS)
def test_mcp_runtime_commands_reject_password_env_with_file(
    tmp_path,
    command,
    patch_target,
):
    password_file = tmp_path / "mqtt.password"
    password_file.write_text("file-secret\n")

    with patch(patch_target) as mock_run:
        result = CliRunner().invoke(
            main,
            [*command, "--password-file", str(password_file)],
            env={"SWARMBUS_BROKER_PASSWORD": "env-secret"},
        )

    assert result.exit_code == 2
    assert "--password and --password-file are mutually exclusive" in result.output
    mock_run.assert_not_called()
