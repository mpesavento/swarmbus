import asyncio
import base64
import hashlib
import inspect
import json
import os
import signal
import stat
import struct
import sys
import threading
from unittest.mock import AsyncMock, patch

import pytest
from click.testing import CliRunner
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

import swarmbus
from swarmbus.cli import main
from swarmbus.local_rpc import (
    MAX_FRAME_BYTES,
    LocalRPCDaemonMismatch,
    LocalRPCOwnershipError,
    LocalRPCProtocolError,
    LocalRPCUnavailable,
    LocalRuntimeDaemon,
    LocalRuntimeProxy,
    _DARWIN_XUCRED_VERSION,
    _DISPATCH_PARAMS,
    _decode_darwin_xucred,
    _encode_frame,
    _read_frame,
    _request,
    ensure_daemon,
    run_mcp_daemon,
)
from swarmbus.mcp_server import create_mcp_app
from swarmbus.message import (
    DEFAULT_HOP_LIMIT,
    DEFAULT_RATE_COOLDOWN,
    DEFAULT_RATE_LIMIT,
)
from swarmbus.runtime import (
    ManagedMCPRuntime,
    RuntimeFatalError,
    TransportUnavailable,
)
from swarmbus.topics import TopicMap


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
            "daemon": {
                "pid": os.getpid(),
                "version": swarmbus.__version__,
                "settings": None,
            },
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
            "daemon": {
                "pid": os.getpid(),
                "version": swarmbus.__version__,
                "settings": None,
            },
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
                "daemon": {
                    "pid": os.getpid(),
                    "version": swarmbus.__version__,
                    "settings": None,
                },
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


@pytest.mark.asyncio
async def test_shutdown_is_acknowledged_before_daemon_stops(tmp_path):
    socket_path = tmp_path / "runtime.sock"
    runtime = _FakeRuntime()
    daemon = LocalRuntimeDaemon(runtime, socket_path)

    await daemon.start()
    shutdown_wait = asyncio.create_task(daemon.wait_for_shutdown())
    response = await _raw_call(socket_path, _request("shutdown", {}))

    assert response == {
        "schema_version": 1,
        "ok": True,
        "result": {"accepted": True},
    }
    await asyncio.wait_for(shutdown_wait, timeout=1)
    runtime.stop.assert_not_awaited()

    await daemon.stop()
    runtime.stop.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_shutdown_rejects_a_different_peer_uid(tmp_path):
    socket_path = tmp_path / "runtime.sock"
    runtime = _FakeRuntime()
    daemon = LocalRuntimeDaemon(runtime, socket_path)

    async with daemon:
        with patch.object(daemon, "_peer_uid", return_value=os.getuid() + 1):
            response = await _raw_call(
                socket_path,
                _request("shutdown", {}),
            )
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(daemon.wait_for_shutdown(), timeout=0.01)

    assert response["ok"] is False
    assert response["error"]["code"] == "permission_denied"


@pytest.mark.asyncio
async def test_peer_uid_reads_the_real_credentials_of_a_connection(tmp_path):
    """Unpatched: SO_PEERCRED on Linux, LOCAL_PEERCRED on Darwin.

    The sibling tests above patch _peer_uid because a foreign uid cannot be
    obtained in-process; this one exercises the real syscall and decode.
    """
    socket_path = tmp_path / "peercred.sock"
    daemon = LocalRuntimeDaemon(_FakeRuntime(), socket_path)
    seen: list[int] = []

    async def handle(reader, writer):
        seen.append(daemon._peer_uid(writer))
        writer.close()

    server = await asyncio.start_unix_server(handle, path=str(socket_path))
    try:
        _, writer = await asyncio.open_unix_connection(str(socket_path))
        writer.close()
        await writer.wait_closed()
        for _ in range(200):
            if seen:
                break
            await asyncio.sleep(0.01)
    finally:
        server.close()
        await server.wait_closed()

    assert seen == [os.getuid()]


def test_darwin_xucred_takes_the_uid_from_the_second_word():
    # struct xucred: cr_version then cr_uid, both u_int32, native order.
    raw = (_DARWIN_XUCRED_VERSION).to_bytes(4, sys.byteorder) + (
        4242
    ).to_bytes(4, sys.byteorder)

    assert _decode_darwin_xucred(raw) == 4242


def test_darwin_xucred_of_an_unknown_version_is_not_trusted():
    raw = (_DARWIN_XUCRED_VERSION + 1).to_bytes(4, sys.byteorder) + (
        4242
    ).to_bytes(4, sys.byteorder)

    with pytest.raises(LocalRPCOwnershipError):
        _decode_darwin_xucred(raw)


@pytest.mark.asyncio
async def test_start_refuses_a_platform_without_peer_credentials(tmp_path):
    daemon = LocalRuntimeDaemon(_FakeRuntime(), tmp_path / "runtime.sock")

    with patch("swarmbus.local_rpc._PEER_CRED_API", None):
        with pytest.raises(LocalRPCOwnershipError):
            await daemon.start()


@pytest.mark.asyncio
async def test_start_names_an_over_long_socket_path(tmp_path):
    """The refusal comes from a real bind, not a constructed exception.

    125 bytes of filename clears both limits -- 104 on Darwin, 108 on Linux --
    so this exercises the same path on either platform.
    """
    socket_path = tmp_path / ("s" * 120 + ".sock")
    daemon = LocalRuntimeDaemon(_FakeRuntime(), socket_path)

    with pytest.raises(LocalRPCOwnershipError) as caught:
        await daemon.start()

    assert "AF_UNIX limit" in str(caught.value)
    assert str(socket_path) in str(caught.value)


@pytest.mark.asyncio
async def test_proxy_blames_the_path_not_a_missing_daemon(tmp_path):
    proxy = LocalRuntimeProxy(tmp_path / ("s" * 120 + ".sock"))

    with pytest.raises(LocalRPCUnavailable) as caught:
        await proxy.ping()

    assert "AF_UNIX limit" in str(caught.value)
    assert "unavailable" not in str(caught.value)


@pytest.mark.asyncio
async def test_an_unreadable_peer_uid_denies_instead_of_serving(tmp_path):
    """The uid check fails closed.

    Pins the regression that matters if the platform predicate used by start()
    ever disagrees with the one used to decode: the daemon must refuse, not
    serve every caller unauthenticated.
    """
    socket_path = tmp_path / "runtime.sock"
    runtime = _FakeRuntime()
    daemon = LocalRuntimeDaemon(runtime, socket_path)

    async with daemon:
        with patch("swarmbus.local_rpc._PEER_CRED_API", None):
            response = await _raw_call(socket_path, _request("ping", {}))

    assert response["ok"] is False
    assert response["error"]["code"] == "permission_denied"
    runtime.runtime_status.assert_not_awaited()


def test_dispatch_allowlists_name_only_real_runtime_parameters():
    """A typo'd allowlist key would otherwise fail only at call time.

    Deliberately a subset check, not equality: a new runtime parameter must not
    reach the wire until someone adds it to _DISPATCH_PARAMS on purpose.
    """
    for operation, allowed in _DISPATCH_PARAMS.items():
        if operation in {"shutdown", "ping"}:
            continue
        signature = inspect.signature(getattr(ManagedMCPRuntime, operation))
        parameters = set(signature.parameters) - {"self"}

        assert allowed <= parameters, operation


@pytest.mark.asyncio
async def test_dispatch_refuses_a_parameter_outside_the_allowlist(tmp_path):
    socket_path = tmp_path / "runtime.sock"
    runtime = _FakeRuntime()

    async with LocalRuntimeDaemon(runtime, socket_path):
        response = await _raw_call(
            socket_path,
            _request(
                "send_message",
                {
                    "to": "peer",
                    "subject": "s",
                    "body": "b",
                    "hops": 7,
                    "origin": "spoofed",
                },
            ),
        )

    assert response["ok"] is False
    assert response["error"]["code"] == "ValueError"
    assert "hops" in response["error"]["message"]
    assert "origin" in response["error"]["message"]
    runtime.send_message.assert_not_awaited()


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
async def test_send_message_uses_the_existing_runtime_contract():
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
            )

    runtime.send_message.assert_awaited_once_with(
        to="wren",
        subject="hello",
        body="world",
        content_type="text/plain",
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
            "daemon": {
                "pid": os.getpid(),
                "version": swarmbus.__version__,
                "settings": None,
            },
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
                "--broker",
                "mqtt.example",
                "--presence",
                "--capability",
                "web.read",
                "--username",
                "alice",
                "--password",
                "secret",
                "--tls",
            ],
        )

    assert result.exit_code == 0, result.output
    assert mock_run.call_args.kwargs == {
        "socket_path": "/private/swarmbus.sock",
        "agent_id": "loom__session-abc",
        "broker": "mqtt.example",
        "port": 1883,
        "persistent": False,
        "presence": True,
        "lifecycle": "named",
        "capabilities": ("web.read",),
        "state_dir": "~/.local/state/swarmbus",
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
        "on_trip": None,
        "topics": TopicMap(),
    }


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
    password_file.chmod(0o600)

    with patch(patch_target) as mock_run:
        result = CliRunner().invoke(
            main,
            [*command, "--password-file", str(password_file)],
        )

    assert result.exit_code == 0, result.output
    assert mock_run.call_args.kwargs["password"] == "file-secret"


@pytest.mark.parametrize("command,patch_target", _MCP_RUNTIME_COMMANDS)
def test_mcp_runtime_commands_refuse_a_readable_password_file(
    tmp_path,
    command,
    patch_target,
):
    password_file = tmp_path / "mqtt.password"
    password_file.write_text("file-secret\n")
    password_file.chmod(0o644)

    with patch(patch_target) as mock_run:
        result = CliRunner().invoke(
            main,
            [*command, "--password-file", str(password_file)],
        )

    assert result.exit_code == 2
    assert "readable by group or others" in result.output
    mock_run.assert_not_called()


@pytest.mark.parametrize("command,patch_target", _MCP_RUNTIME_COMMANDS)
def test_mcp_runtime_commands_refuse_a_fifo_password_file(
    tmp_path,
    command,
    patch_target,
):
    """A fifo would block open() forever, so this must be refused on stat.

    If the regular-file check is ever removed this test HANGS rather than
    fails, which is the same symptom a user would get.
    """
    password_file = tmp_path / "mqtt.password"
    os.mkfifo(password_file, 0o600)

    with patch(patch_target) as mock_run:
        result = CliRunner().invoke(
            main,
            [*command, "--password-file", str(password_file)],
        )

    assert result.exit_code == 2
    assert "not a regular file" in result.output
    mock_run.assert_not_called()


@pytest.mark.parametrize("command,patch_target", _MCP_RUNTIME_COMMANDS)
def test_mcp_runtime_commands_refuse_a_foreign_owned_password_file(
    tmp_path,
    command,
    patch_target,
):
    # chown to another uid needs privileges, so the foreign owner is injected
    # for this one path and every other stat is delegated untouched.
    password_file = tmp_path / "mqtt.password"
    password_file.write_text("file-secret\n")
    password_file.chmod(0o600)
    real_stat = os.stat

    def foreign_owner(path, *args, **kwargs):
        info = real_stat(path, *args, **kwargs)
        if str(path) != str(password_file):
            return info
        fields = list(info)
        fields[4] = info.st_uid + 1
        return os.stat_result(fields)

    with (
        patch(patch_target) as mock_run,
        patch("os.stat", side_effect=foreign_owner),
    ):
        result = CliRunner().invoke(
            main,
            [*command, "--password-file", str(password_file)],
        )

    assert result.exit_code == 2
    assert "owned by uid" in result.output
    mock_run.assert_not_called()


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


# A throwaway daemon for the attach tests: it serves a stub runtime on the given
# socket, records its pid in the marker file once it owns the socket, and never
# outlives the test run (SIGTERM, plus a 30 second watchdog).
_STUB_DAEMON = """
import asyncio, json, os, signal, sys
from swarmbus.local_rpc import LocalRuntimeDaemon


class Runtime:
    broker = "stub"
    port = 0

    async def start(self):
        pass

    async def stop(self):
        pass

    async def runtime_status(self):
        return {"ready": True, "connected": True}

    async def send_message(self, **kwargs):
        pass

    async def read_inbox(self, **kwargs):
        return []

    async def list_states(self, **kwargs):
        return []

    async def get_state(self, agent_id):
        return {"agent_id": agent_id}

    async def update_state(self, **kwargs):
        return {}


async def main(socket, settings_json, marker):
    daemon = LocalRuntimeDaemon(
        Runtime(), socket, settings=json.loads(settings_json)
    )
    loop = asyncio.get_running_loop()
    loop.add_signal_handler(signal.SIGTERM, daemon.request_shutdown)
    loop.call_later(30, daemon.request_shutdown)
    async with daemon:
        with open(marker, "a") as handle:
            handle.write(f"{os.getpid()}\\n")
        await daemon.wait_for_shutdown()


asyncio.run(main(*sys.argv[1:4]))
"""

_PASSWORD = "pw-s3cret-value"


@pytest.fixture
def stub_pids():
    pids: list[int] = []
    yield pids
    for pid in pids:
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass


def _settings(**overrides):
    from swarmbus.local_rpc import daemon_settings

    values = dict(
        agent_id="loom",
        broker="mqtt.example",
        port=1883,
        username=None,
        password=None,
        tls=False,
        ca_cert=None,
        client_cert=None,
        client_key=None,
        persistent=True,
        presence=True,
        lifecycle="named",
    )
    values.update(overrides)
    return daemon_settings(**values)


def _stub_argv(socket_path, settings, marker):
    return [
        sys.executable,
        "-c",
        _STUB_DAEMON,
        str(socket_path),
        json.dumps(settings),
        str(marker),
    ]


def _marker_pids(marker):
    return marker.read_text().splitlines() if marker.exists() else []


@pytest.mark.asyncio
async def test_ping_reports_daemon_pid_version_and_settings(tmp_path):
    settings = _settings(username="alice")
    socket_path = tmp_path / "runtime.sock"

    async with LocalRuntimeDaemon(_FakeRuntime(), socket_path, settings=settings):
        reply = await LocalRuntimeProxy(socket_path).ping()

    assert reply == {
        "ready": True,
        "connected": True,
        "state": "connected",
        "daemon": {
            "pid": os.getpid(),
            "version": swarmbus.__version__,
            "settings": settings,
        },
    }


@pytest.mark.asyncio
async def test_ping_never_carries_the_password(tmp_path):
    settings = _settings(password=_PASSWORD)
    socket_path = tmp_path / "runtime.sock"

    async with LocalRuntimeDaemon(_FakeRuntime(), socket_path, settings=settings):
        raw = json.dumps(await _raw_call(socket_path, _request("ping", {})))

    secret = _PASSWORD.encode()
    forbidden = [
        _PASSWORD,
        hashlib.sha256(secret).hexdigest(),
        hashlib.sha1(secret).hexdigest(),
        hashlib.md5(secret).hexdigest(),
        base64.b64encode(secret).decode(),
        base64.urlsafe_b64encode(secret).decode(),
    ]
    for needle in forbidden:
        assert needle not in raw
    assert settings["password_set"] is True
    assert "password" not in settings
    assert _settings(password=None)["password_set"] is False


def test_run_mcp_daemon_reports_its_settings_without_the_password(tmp_path):
    from swarmbus.local_rpc import daemon_settings, run_mcp_daemon

    captured = {}

    class _Capture:
        def __init__(self, runtime, socket_path, *, settings=None):
            captured["settings"] = settings

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc_info):
            return None

        def request_shutdown(self):
            pass

        async def wait_for_shutdown(self):
            return None

    with (
        patch(
            "swarmbus.local_rpc.create_managed_runtime",
            return_value=_FakeRuntime(),
        ),
        patch("swarmbus.local_rpc.LocalRuntimeDaemon", _Capture),
    ):
        run_mcp_daemon(
            socket_path=str(tmp_path / "loom.sock"),
            agent_id="loom",
            broker="mqtt.example",
            username="alice",
            password=_PASSWORD,
            persistent=True,
            presence=True,
        )

    assert captured["settings"] == daemon_settings(
        agent_id="loom",
        broker="mqtt.example",
        port=1883,
        username="alice",
        password=_PASSWORD,
        tls=False,
        ca_cert=None,
        client_cert=None,
        client_key=None,
        persistent=True,
        presence=True,
        lifecycle="named",
    )
    assert "password" not in captured["settings"]


def test_run_mcp_daemon_runs_and_reports_on_the_root_it_is_given(tmp_path):
    fleet = TopicMap(root="fleet")
    captured = {}

    class _Capture:
        def __init__(self, runtime, socket_path, *, settings=None):
            captured["settings"] = settings

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc_info):
            return None

        def request_shutdown(self):
            pass

        async def wait_for_shutdown(self):
            return None

    with (
        patch(
            "swarmbus.local_rpc.create_managed_runtime",
            return_value=_FakeRuntime(),
        ) as build_runtime,
        patch("swarmbus.local_rpc.LocalRuntimeDaemon", _Capture),
    ):
        run_mcp_daemon(
            socket_path=str(tmp_path / "loom.sock"),
            agent_id="loom",
            presence=True,
            topics=fleet,
        )

    assert build_runtime.call_args.kwargs["topics"] is fleet
    # What an attaching session compares its own root against.
    assert captured["settings"]["topic_root"] == fleet.root


def test_ensure_starts_a_detached_daemon_in_a_private_dir(tmp_path, stub_pids):
    from swarmbus.local_rpc import ensure_daemon

    socket_path = tmp_path / "run" / "a.sock"
    log_path = tmp_path / "a.daemon.log"
    marker = tmp_path / "marker"
    settings = _settings()

    reply = ensure_daemon(
        socket_path,
        settings=settings,
        daemon_argv=_stub_argv(socket_path, settings, marker),
        env=dict(os.environ),
        log_path=log_path,
    )

    pid = reply["daemon"]["pid"]
    stub_pids.append(pid)
    assert pid != os.getpid()
    assert os.getsid(pid) == pid
    assert stat.S_IMODE(socket_path.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(log_path.stat().st_mode) == 0o600
    assert _marker_pids(marker) == [str(pid)]


def test_second_ensure_attaches_without_spawning(tmp_path, stub_pids):
    from swarmbus.local_rpc import ensure_daemon

    socket_path = tmp_path / "run" / "a.sock"
    log_path = tmp_path / "a.daemon.log"
    marker = tmp_path / "marker"
    settings = _settings()

    first = ensure_daemon(
        socket_path,
        settings=settings,
        daemon_argv=_stub_argv(socket_path, settings, marker),
        env=dict(os.environ),
        log_path=log_path,
    )
    stub_pids.append(first["daemon"]["pid"])
    second = ensure_daemon(
        socket_path,
        settings=settings,
        daemon_argv=[sys.executable, "-c", "raise SystemExit(9)"],
        env=dict(os.environ),
        log_path=log_path,
    )

    assert second["daemon"]["pid"] == first["daemon"]["pid"]
    assert len(_marker_pids(marker)) == 1


def test_concurrent_ensures_end_with_one_daemon(tmp_path, stub_pids):
    from swarmbus.local_rpc import ensure_daemon

    socket_path = tmp_path / "run" / "a.sock"
    log_path = tmp_path / "a.daemon.log"
    marker = tmp_path / "marker"
    settings = _settings()
    results: list = []
    errors: list = []
    barrier = threading.Barrier(2)

    def attach():
        try:
            barrier.wait()
            results.append(
                ensure_daemon(
                    socket_path,
                    settings=settings,
                    daemon_argv=_stub_argv(socket_path, settings, marker),
                    env=dict(os.environ),
                    log_path=log_path,
                )
            )
        except Exception as exc:  # noqa: BLE001 - reported below
            errors.append(exc)

    threads = [threading.Thread(target=attach) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)

    pids = [reply["daemon"]["pid"] for reply in results]
    stub_pids.extend(pids)
    assert errors == []
    assert len(pids) == 2
    assert pids[0] == pids[1]
    assert _marker_pids(marker) == [str(pids[0])]


@pytest.mark.asyncio
async def test_ensure_refuses_a_daemon_with_different_settings(tmp_path):
    from swarmbus.local_rpc import LocalRPCDaemonMismatch, ensure_daemon

    socket_path = tmp_path / "run" / "a.sock"
    socket_path.parent.mkdir(mode=0o700)
    marker = tmp_path / "marker"
    running = _settings(broker="old.example", username="bob", persistent=False)
    wanted = _settings(
        broker="mqtt.example",
        username="alice",
        password=_PASSWORD,
        persistent=True,
    )
    argv = [
        sys.executable,
        "-c",
        f"open({str(marker)!r}, 'w').write('ran')",
    ]

    async with LocalRuntimeDaemon(_FakeRuntime(), socket_path, settings=running):
        with pytest.raises(LocalRPCDaemonMismatch) as caught:
            await asyncio.to_thread(
                ensure_daemon,
                socket_path,
                settings=wanted,
                daemon_argv=argv,
                env=dict(os.environ),
                log_path=tmp_path / "a.daemon.log",
            )

    message = str(caught.value)
    assert "broker" in message
    assert "username" in message
    assert "persistent" in message
    assert f"kill {os.getpid()}" in message
    assert _PASSWORD not in message
    assert not marker.exists()


@pytest.mark.asyncio
async def test_ensure_refuses_a_daemon_on_another_topic_root(tmp_path):
    # Reusing it would put this session in a namespace it did not ask for,
    # and the broker would report nothing.
    socket_path = tmp_path / "run" / "a.sock"
    socket_path.parent.mkdir(mode=0o700)
    marker = tmp_path / "marker"
    argv = [
        sys.executable,
        "-c",
        f"open({str(marker)!r}, 'w').write('ran')",
    ]

    async with LocalRuntimeDaemon(
        _FakeRuntime(), socket_path, settings=_settings()
    ):
        with pytest.raises(LocalRPCDaemonMismatch) as caught:
            await asyncio.to_thread(
                ensure_daemon,
                socket_path,
                settings=_settings(topic_root="fleet"),
                daemon_argv=argv,
                env=dict(os.environ),
                log_path=tmp_path / "a.daemon.log",
            )

    assert "topic_root" in str(caught.value)
    assert not marker.exists()


def test_ensure_refuses_a_daemon_on_another_version(tmp_path):
    from swarmbus.local_rpc import LocalRPCDaemonMismatch, ensure_daemon

    settings = _settings()
    reply = {
        "ready": True,
        "daemon": {"pid": 4242, "version": "0.0.1", "settings": settings},
    }

    with patch("swarmbus.local_rpc.probe_daemon", return_value=reply):
        with pytest.raises(LocalRPCDaemonMismatch) as caught:
            ensure_daemon(
                tmp_path / "a.sock",
                settings=settings,
                daemon_argv=[sys.executable, "-c", "raise SystemExit(9)"],
                env=dict(os.environ),
                log_path=tmp_path / "a.daemon.log",
            )

    assert "version" in str(caught.value)
    assert "kill 4242" in str(caught.value)


def test_ensure_names_the_log_when_no_daemon_answers(tmp_path):
    from swarmbus.local_rpc import LocalRPCStartTimeout, ensure_daemon

    log_path = tmp_path / "a.daemon.log"

    with pytest.raises(LocalRPCStartTimeout) as caught:
        ensure_daemon(
            tmp_path / "a.sock",
            settings=_settings(),
            daemon_argv=[
                sys.executable,
                "-c",
                "print('boom'); raise SystemExit(3)",
            ],
            env=dict(os.environ),
            log_path=log_path,
            start_timeout=1.0,
        )

    assert str(log_path) in str(caught.value)
    assert "status 3" in str(caught.value)
    assert "boom" in log_path.read_text()


def test_private_dir_tolerates_a_concurrent_create(tmp_path):
    from swarmbus.local_rpc import _private_dir

    for attempt in range(20):
        target = tmp_path / f"state{attempt}"
        barrier = threading.Barrier(2)
        errors: list = []

        def create():
            try:
                barrier.wait()
                _private_dir(target)
            except Exception as exc:  # noqa: BLE001 - reported below
                errors.append(exc)

        threads = [threading.Thread(target=create) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        assert errors == []
        assert stat.S_IMODE(target.stat().st_mode) == 0o700
        assert target.stat().st_uid == os.getuid()

    shared = tmp_path / "shared"
    shared.mkdir()
    shared.chmod(0o755)
    with pytest.raises(LocalRPCOwnershipError, match="chmod 700"):
        _private_dir(shared)
    assert stat.S_IMODE(shared.stat().st_mode) == 0o755


def test_ensure_refuses_an_over_long_socket_path(tmp_path):
    from swarmbus.local_rpc import ensure_daemon

    socket_path = tmp_path / ("s" * 120 + ".sock")
    marker = tmp_path / "marker"
    settings = _settings()

    with pytest.raises(LocalRPCOwnershipError) as caught:
        ensure_daemon(
            socket_path,
            settings=settings,
            daemon_argv=_stub_argv(socket_path, settings, marker),
            env=dict(os.environ),
            log_path=tmp_path / "a.daemon.log",
        )

    assert "AF_UNIX limit" in str(caught.value)
    assert str(socket_path) in str(caught.value)
    assert not marker.exists()


def test_mcp_attach_cli_starts_the_daemon_with_its_own_settings(tmp_path):
    from swarmbus.local_rpc import daemon_settings

    password_file = tmp_path / "mqtt.password"
    password_file.write_text("file-secret\n")
    password_file.chmod(0o600)
    state = tmp_path / "swarmbus-state"

    with patch("swarmbus.cli.ensure_daemon") as mock_ensure, \
         patch("swarmbus.cli.run_mcp_proxy") as mock_proxy:
        result = CliRunner().invoke(
            main,
            [
                "mcp-attach",
                "--agent-id", "loom",
                "--broker", "mqtt.example",
                "--username", "alice",
                "--password-file", str(password_file),
                "--persistent",
                "--presence",
                "--tls",
            ],
        )

    assert result.exit_code == 0, result.output
    args, kwargs = mock_ensure.call_args
    assert args == (state / "loom.sock",)
    assert kwargs["log_path"] == state / "loom.daemon.log"
    assert kwargs["settings"] == daemon_settings(
        agent_id="loom",
        broker="mqtt.example",
        port=1883,
        username="alice",
        password="file-secret",
        tls=True,
        ca_cert=None,
        client_cert=None,
        client_key=None,
        persistent=True,
        presence=True,
        lifecycle="named",
    )
    argv = kwargs["daemon_argv"]
    assert argv[3] == "mcp-daemon"
    for flag in ("--persistent", "--presence", "--tls"):
        assert flag in argv
    assert argv[argv.index("--username") + 1] == "alice"
    assert argv[argv.index("--socket") + 1] == str(state / "loom.sock")
    assert not any("file-secret" in element for element in argv)
    assert kwargs["env"]["SWARMBUS_BROKER_PASSWORD"] == "file-secret"
    mock_proxy.assert_called_once_with(str(state / "loom.sock"))


def test_mcp_attach_cli_exits_non_zero_on_a_mismatch():
    from swarmbus.local_rpc import LocalRPCDaemonMismatch

    with patch(
        "swarmbus.cli.ensure_daemon",
        side_effect=LocalRPCDaemonMismatch(
            "the daemon differs in: broker. Stop it with `kill 4242`"
        ),
    ), patch("swarmbus.cli.run_mcp_proxy") as mock_proxy:
        result = CliRunner().invoke(
            main, ["mcp-attach", "--agent-id", "loom"]
        )

    assert result.exit_code == 1
    assert "kill 4242" in result.output
    mock_proxy.assert_not_called()


@pytest.mark.asyncio
async def test_mcp_attach_serves_the_three_tools_through_a_running_daemon(
    tmp_path,
):
    from swarmbus.local_rpc import daemon_settings

    runtime = _FakeRuntime()
    socket_path = tmp_path / "e2e.sock"
    settings = daemon_settings(
        agent_id="attach-e2e",
        broker="127.0.0.1",
        port=1883,
        username=None,
        password=None,
        tls=False,
        ca_cert=None,
        client_cert=None,
        client_key=None,
        persistent=True,
        presence=True,
        lifecycle="named",
    )
    parameters = StdioServerParameters(
        command=sys.executable,
        args=[
            "-c",
            "from swarmbus.cli import main; main()",
            "mcp-attach",
            "--socket", str(socket_path),
            "--state-dir", str(tmp_path),
            "--agent-id", "attach-e2e",
            "--broker", "127.0.0.1",
            "--port", "1883",
            "--persistent",
            "--presence",
        ],
        env={"HOME": str(tmp_path)},
    )

    async with LocalRuntimeDaemon(runtime, socket_path, settings=settings):
        async with stdio_client(parameters) as (read_stream, write_stream):
            async with ClientSession(read_stream, write_stream) as session:
                await session.initialize()
                tools = await session.list_tools()
                result = await session.call_tool(
                    "agent_state",
                    {"action": "get", "agent_id": "attach-e2e"},
                )

    assert {tool.name for tool in tools.tools} == {
        "send_message",
        "read_inbox",
        "agent_state",
    }
    assert not result.isError
    runtime.start.assert_awaited_once()


@pytest.mark.asyncio
async def test_doctor_lists_each_daemon_socket(tmp_path):
    import socket as socket_module
    from unittest.mock import MagicMock

    settings = _settings()
    stale = tmp_path / "old.sock"
    sock = socket_module.socket(socket_module.AF_UNIX)
    sock.bind(str(stale))
    sock.close()
    empty = tmp_path / "empty"
    empty.mkdir(mode=0o700)

    def run_doctor(state_dir):
        with patch(
            "subprocess.run",
            return_value=MagicMock(returncode=1, stdout="", stderr=""),
        ), patch("swarmbus.cli.AgentBus") as mock_bus, \
             patch("swarmbus.cli.aiomqtt"):
            mock_bus.probe.return_value._aiomqtt_kwargs = lambda: {}
            mock_bus.probe.return_value.list_agents = AsyncMock(return_value=[])
            return CliRunner().invoke(
                main,
                [
                    "doctor",
                    "--agent-id", "loom",
                    "--state-dir", str(state_dir),
                ],
            )

    async with LocalRuntimeDaemon(
        _FakeRuntime(), tmp_path / "loom.sock", settings=settings
    ):
        listed = await asyncio.to_thread(run_doctor, tmp_path)
        none = await asyncio.to_thread(run_doctor, empty)

    assert "local daemons" in listed.output
    assert (
        f"loom.sock: pid {os.getpid()}, swarmbus {swarmbus.__version__}, "
        "agent loom"
    ) in listed.output
    assert "old.sock: no daemon answers" in listed.output
    assert "(none in" in none.output
