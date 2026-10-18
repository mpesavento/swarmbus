"""Private local RPC joining stdio MCP frontends to one managed runtime."""
from __future__ import annotations

import asyncio
import errno
import json
import os
import signal
import socket
import stat
import struct
from pathlib import Path
from typing import Any, Literal

import aiomqtt

try:
    import fcntl
except ImportError:
    fcntl = None

from .message import DEFAULT_HOP_LIMIT, DEFAULT_RATE_COOLDOWN, DEFAULT_RATE_LIMIT
from .mcp_server import (
    FastMCP,
    _MCP_AVAILABLE,
    _MCPRuntime,
    create_managed_runtime,
    create_mcp_app,
)
from .runtime import (
    PresenceRequiredError,
    RuntimeFatalError,
    StoreUnavailable,
    TransportUnavailable,
)

SCHEMA_VERSION = 1
MAX_FRAME_BYTES = 1024 * 1024
_FRAME_HEADER = struct.Struct("!I")
_PEER_CREDENTIALS = struct.Struct("3i")
_READ_TIMEOUT_SECONDS = 30.0
_RESPONSE_MARGIN_SECONDS = 5.0


class LocalRPCError(RuntimeError):
    """Base error for the private runtime transport."""


class LocalRPCUnavailable(LocalRPCError, TransportUnavailable):
    """The local runtime daemon cannot be reached."""


class LocalRPCProtocolError(LocalRPCError):
    """A local peer sent an invalid protocol frame."""


class LocalRPCOwnershipError(LocalRPCError):
    """The socket or lock path is not safe for this daemon."""


_REMOTE_ERRORS: dict[str, type[Exception]] = {
    "PresenceRequiredError": PresenceRequiredError,
    "RuntimeFatalError": RuntimeFatalError,
    "StoreUnavailable": StoreUnavailable,
    "TransportUnavailable": TransportUnavailable,
    "ValueError": ValueError,
}


def _encode_frame(payload: dict[str, Any]) -> bytes:
    try:
        body = json.dumps(
            payload,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise LocalRPCProtocolError("RPC payload is not JSON serializable") from exc
    if not body or len(body) > MAX_FRAME_BYTES:
        raise LocalRPCProtocolError(
            f"RPC frame must contain 1..{MAX_FRAME_BYTES} bytes"
        )
    return _FRAME_HEADER.pack(len(body)) + body


async def _read_frame(reader: asyncio.StreamReader) -> dict[str, Any]:
    try:
        header = await reader.readexactly(_FRAME_HEADER.size)
    except asyncio.IncompleteReadError as exc:
        raise LocalRPCProtocolError("truncated RPC frame header") from exc
    (length,) = _FRAME_HEADER.unpack(header)
    if length == 0 or length > MAX_FRAME_BYTES:
        raise LocalRPCProtocolError(
            f"RPC frame length {length} is outside 1..{MAX_FRAME_BYTES}"
        )
    try:
        body = await reader.readexactly(length)
    except asyncio.IncompleteReadError as exc:
        raise LocalRPCProtocolError("truncated RPC frame body") from exc
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise LocalRPCProtocolError("RPC frame is not valid UTF-8 JSON") from exc
    if not isinstance(payload, dict):
        raise LocalRPCProtocolError("RPC payload must be a JSON object")
    return payload


async def _write_frame(
    writer: asyncio.StreamWriter,
    payload: dict[str, Any],
) -> None:
    writer.write(_encode_frame(payload))
    await writer.drain()


def _request(operation: str, params: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "operation": operation,
        "params": params,
    }


def _success(result: Any) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "ok": True,
        "result": result,
    }


def _failure(code: str, message: str) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "ok": False,
        "error": {"code": code, "message": message},
    }


def _validate_request(payload: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    if payload.get("schema_version") != SCHEMA_VERSION:
        raise LocalRPCProtocolError("unsupported RPC schema_version")
    operation = payload.get("operation")
    params = payload.get("params")
    if not isinstance(operation, str) or not operation:
        raise LocalRPCProtocolError("RPC operation must be a non-empty string")
    if not isinstance(params, dict):
        raise LocalRPCProtocolError("RPC params must be an object")
    return operation, params


def _remote_error(error: Any) -> Exception:
    if not isinstance(error, dict):
        return LocalRPCProtocolError("RPC error response is malformed")
    code = error.get("code")
    message = error.get("message")
    if not isinstance(code, str) or not isinstance(message, str):
        return LocalRPCProtocolError("RPC error response is malformed")
    error_type = _REMOTE_ERRORS.get(code)
    if error_type is not None:
        return error_type(message)
    return LocalRPCError(f"{code}: {message}")


class LocalRuntimeProxy(_MCPRuntime):
    """Typed client for one request per private socket connection."""

    broker = "local-runtime"
    port = 0

    def __init__(self, socket_path: str | Path) -> None:
        self.socket_path = Path(socket_path)

    async def _call(
        self,
        operation: str,
        params: dict[str, Any],
        *,
        response_timeout: float = _READ_TIMEOUT_SECONDS,
    ) -> Any:
        try:
            reader, writer = await asyncio.open_unix_connection(
                str(self.socket_path)
            )
        except (FileNotFoundError, ConnectionError, OSError) as exc:
            raise LocalRPCUnavailable(
                f"local runtime unavailable at {self.socket_path}: {exc}"
            ) from exc
        try:
            await _write_frame(writer, _request(operation, params))
            response = await asyncio.wait_for(
                _read_frame(reader),
                timeout=response_timeout,
            )
        except (ConnectionError, OSError, asyncio.TimeoutError) as exc:
            raise LocalRPCUnavailable(
                f"local runtime call {operation!r} failed: {exc}"
            ) from exc
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except OSError:
                pass

        if response.get("schema_version") != SCHEMA_VERSION:
            raise LocalRPCProtocolError("unsupported RPC response schema_version")
        if response.get("ok") is True:
            return response.get("result")
        if response.get("ok") is False:
            raise _remote_error(response.get("error"))
        raise LocalRPCProtocolError("RPC response is missing boolean ok")

    async def runtime_status(self) -> dict[str, Any]:
        result = await self._call("ping", {})
        if not isinstance(result, dict):
            raise LocalRPCProtocolError("ping result must be an object")
        return result

    async def ping(self) -> dict[str, Any]:
        return await self.runtime_status()

    async def send_message(
        self,
        *,
        to: str,
        subject: str,
        body: str,
        content_type: str,
        priority: str = "normal",
        in_reply_to: str | None = None,
    ) -> None:
        params = {
            "to": to,
            "subject": subject,
            "body": body,
            "content_type": content_type,
            "in_reply_to": in_reply_to,
        }
        if priority != "normal":
            params["priority"] = priority
        await self._call("send_message", params)

    async def read_inbox(
        self,
        *,
        ack_ids: list[str] | None,
        max_messages: int,
        wait_seconds: float,
    ) -> list[dict]:
        result = await self._call(
            "read_inbox",
            {
                "ack_ids": ack_ids,
                "max_messages": max_messages,
                "wait_seconds": wait_seconds,
            },
            response_timeout=max(
                _READ_TIMEOUT_SECONDS,
                wait_seconds + _RESPONSE_MARGIN_SECONDS,
            ),
        )
        if not isinstance(result, list):
            raise LocalRPCProtocolError("read_inbox result must be an array")
        return result

    async def inbox_state(
        self,
        *,
        cursor: dict[str, Any] | None = None,
        wait_seconds: float = 0.0,
        max_messages: int = 1000,
    ) -> dict[str, Any]:
        result = await self._call(
            "inbox_state",
            {
                "cursor": cursor,
                "wait_seconds": wait_seconds,
                "max_messages": max_messages,
            },
            response_timeout=max(
                _READ_TIMEOUT_SECONDS,
                wait_seconds + _RESPONSE_MARGIN_SECONDS,
            ),
        )
        if not isinstance(result, dict):
            raise LocalRPCProtocolError("inbox_state result must be an object")
        return result

    async def list_states(
        self,
        *,
        include_offline: bool,
        lifecycle: Literal["named", "transient"] | None,
    ) -> list[dict]:
        result = await self._call(
            "list_states",
            {
                "include_offline": include_offline,
                "lifecycle": lifecycle,
            },
        )
        if not isinstance(result, list):
            raise LocalRPCProtocolError("list_states result must be an array")
        return result

    async def get_state(self, agent_id: str) -> dict:
        result = await self._call("get_state", {"agent_id": agent_id})
        if not isinstance(result, dict):
            raise LocalRPCProtocolError("get_state result must be an object")
        return result

    async def update_state(
        self,
        *,
        status: str | None,
        working_set: list[str] | None,
        capabilities: list[str] | None,
    ) -> dict:
        result = await self._call(
            "update_state",
            {
                "status": status,
                "working_set": working_set,
                "capabilities": capabilities,
            },
        )
        if not isinstance(result, dict):
            raise LocalRPCProtocolError("update_state result must be an object")
        return result


class LocalRuntimeDaemon:
    """Own one managed runtime and serve its typed operations locally."""

    def __init__(self, runtime: _MCPRuntime, socket_path: str | Path) -> None:
        self.runtime = runtime
        self.socket_path = Path(socket_path)
        self.lock_path = Path(f"{self.socket_path}.lock")
        self._lock_fd: int | None = None
        self._server: asyncio.AbstractServer | None = None
        self._socket_identity: tuple[int, int] | None = None
        self._runtime_started = False

    def _validate_parent(self) -> None:
        try:
            parent_stat = self.socket_path.parent.stat()
        except FileNotFoundError as exc:
            raise LocalRPCOwnershipError(
                f"socket parent does not exist: {self.socket_path.parent}"
            ) from exc
        if not stat.S_ISDIR(parent_stat.st_mode):
            raise LocalRPCOwnershipError("socket parent is not a directory")
        if parent_stat.st_uid != os.getuid():
            raise LocalRPCOwnershipError("socket parent is owned by another uid")
        if parent_stat.st_mode & 0o077:
            raise LocalRPCOwnershipError(
                "socket parent must not be accessible by group or other"
            )

    def _acquire_lock(self) -> None:
        flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        try:
            lock_fd = os.open(self.lock_path, flags, 0o600)
        except OSError as exc:
            raise LocalRPCOwnershipError(
                f"cannot open runtime lock {self.lock_path}: {exc}"
            ) from exc
        try:
            lock_stat = os.fstat(lock_fd)
            if (
                not stat.S_ISREG(lock_stat.st_mode)
                or lock_stat.st_uid != os.getuid()
                or lock_stat.st_nlink != 1
                or lock_stat.st_mode & 0o077
            ):
                raise LocalRPCOwnershipError("runtime lock is not a private file")
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise LocalRPCOwnershipError(
                    f"runtime socket already has an owner: {self.socket_path}"
                ) from exc
        except BaseException:
            os.close(lock_fd)
            raise
        self._lock_fd = lock_fd

    def _remove_stale_socket(self) -> None:
        try:
            socket_stat = os.lstat(self.socket_path)
        except FileNotFoundError:
            return
        if not stat.S_ISSOCK(socket_stat.st_mode):
            raise LocalRPCOwnershipError(
                f"refusing to unlink non-socket path: {self.socket_path}"
            )
        if socket_stat.st_uid != os.getuid():
            raise LocalRPCOwnershipError(
                f"refusing to unlink socket owned by another uid: {self.socket_path}"
            )

        probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        probe.settimeout(0.1)
        try:
            probe.connect(str(self.socket_path))
        except OSError as exc:
            if exc.errno not in {errno.ECONNREFUSED, errno.ENOENT}:
                raise LocalRPCOwnershipError(
                    f"cannot establish whether socket is stale: {exc}"
                ) from exc
        else:
            raise LocalRPCOwnershipError(
                f"refusing to unlink active socket: {self.socket_path}"
            )
        finally:
            probe.close()
        try:
            os.unlink(self.socket_path)
        except FileNotFoundError:
            pass

    def _peer_uid(self, writer: asyncio.StreamWriter) -> int | None:
        if not hasattr(socket, "SO_PEERCRED"):
            return None
        peer_socket = writer.get_extra_info("socket")
        if peer_socket is None:
            return None
        credentials = peer_socket.getsockopt(
            socket.SOL_SOCKET,
            socket.SO_PEERCRED,
            _PEER_CREDENTIALS.size,
        )
        _, uid, _ = _PEER_CREDENTIALS.unpack(credentials)
        return uid

    async def _dispatch(self, operation: str, params: dict[str, Any]) -> Any:
        if operation == "ping":
            if params:
                raise ValueError("ping does not accept parameters")
            return await self.runtime.runtime_status()
        if operation == "send_message":
            return await self.runtime.send_message(**params)
        if operation == "read_inbox":
            return await self.runtime.read_inbox(**params)
        if operation == "inbox_state":
            return await self.runtime.inbox_state(**params)
        if operation == "list_states":
            return await self.runtime.list_states(**params)
        if operation == "get_state":
            return await self.runtime.get_state(**params)
        if operation == "update_state":
            return await self.runtime.update_state(**params)
        raise LocalRPCProtocolError(f"unknown RPC operation {operation!r}")

    async def _handle(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        try:
            peer_uid = self._peer_uid(writer)
            if peer_uid is not None and peer_uid != os.getuid():
                response = _failure("permission_denied", "peer uid does not match")
            else:
                request = await asyncio.wait_for(
                    _read_frame(reader),
                    timeout=_READ_TIMEOUT_SECONDS,
                )
                operation, params = _validate_request(request)
                try:
                    result = await self._dispatch(operation, params)
                except aiomqtt.MqttError as exc:
                    response = _failure("TransportUnavailable", str(exc))
                except tuple(_REMOTE_ERRORS.values()) as exc:
                    response = _failure(type(exc).__name__, str(exc))
                except TypeError as exc:
                    response = _failure("ValueError", str(exc))
                else:
                    response = _success(result)
        except (LocalRPCProtocolError, asyncio.TimeoutError) as exc:
            response = _failure("protocol_error", str(exc))
        except Exception:
            response = _failure("internal_error", "runtime operation failed")

        try:
            await _write_frame(writer, response)
        except (ConnectionError, OSError):
            pass
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except OSError:
                pass

    async def start(self) -> None:
        if fcntl is None or not hasattr(os, "getuid"):
            raise LocalRPCOwnershipError(
                "the shared local runtime requires a Unix platform"
            )
        if self._server is not None:
            raise RuntimeError("local runtime daemon is already started")
        self._validate_parent()
        self._acquire_lock()
        try:
            self._remove_stale_socket()
            await self.runtime.start()
            self._runtime_started = True
            self._server = await asyncio.start_unix_server(
                self._handle,
                path=str(self.socket_path),
            )
            socket_stat = os.lstat(self.socket_path)
            if (
                not stat.S_ISSOCK(socket_stat.st_mode)
                or socket_stat.st_uid != os.getuid()
            ):
                raise LocalRPCOwnershipError("created socket has unsafe ownership")
            self._socket_identity = (socket_stat.st_dev, socket_stat.st_ino)
            os.chmod(self.socket_path, 0o600)
        except BaseException:
            await self.stop()
            raise

    async def stop(self) -> None:
        stop_error: BaseException | None = None
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None
        if self._runtime_started:
            self._runtime_started = False
            try:
                await self.runtime.stop()
            except BaseException as exc:
                stop_error = exc
        try:
            if self._socket_identity is not None:
                try:
                    socket_stat = os.lstat(self.socket_path)
                except FileNotFoundError:
                    pass
                else:
                    identity = (socket_stat.st_dev, socket_stat.st_ino)
                    if (
                        identity == self._socket_identity
                        and stat.S_ISSOCK(socket_stat.st_mode)
                        and socket_stat.st_uid == os.getuid()
                    ):
                        os.unlink(self.socket_path)
                self._socket_identity = None
        finally:
            if self._lock_fd is not None:
                fcntl.flock(self._lock_fd, fcntl.LOCK_UN)
                os.close(self._lock_fd)
                self._lock_fd = None
        if stop_error is not None:
            raise stop_error

    async def __aenter__(self) -> "LocalRuntimeDaemon":
        await self.start()
        return self

    async def __aexit__(self, exc_type, exc, traceback) -> None:
        await self.stop()


def run_mcp_proxy(socket_path: str) -> None:
    """Run one official stdio MCP session over the local runtime daemon."""
    if not _MCP_AVAILABLE:
        raise RuntimeError(
            "mcp package not installed. Run: uv pip install 'swarmbus-py[mcp]'"
        )
    runtime = LocalRuntimeProxy(socket_path)
    mcp = FastMCP("swarmbus")
    app = create_mcp_app(runtime)
    for name, fn in app._tool_fns.items():
        mcp.tool(name=name)(fn)
    mcp.run(transport="stdio")


def run_mcp_daemon(
    socket_path: str,
    agent_id: str,
    broker: str = "localhost",
    port: int = 1883,
    *,
    topic_address: str | None = None,
    persistent: bool = False,
    presence: bool = False,
    lifecycle: Literal["named", "transient"] = "named",
    client_id: str | None = None,
    capabilities: tuple[str, ...] = (),
    state_dir: str = "~/.local/state/swarmbus",
    topic_root: str = "",
    registry_heartbeat_seconds: float = 60,
    registry_stale_after_seconds: float = 180,
    username: str | None = None,
    password: str | None = None,
    tls: bool = False,
    ca_cert: str | None = None,
    client_cert: str | None = None,
    client_key: str | None = None,
    hop_limit: int = DEFAULT_HOP_LIMIT,
    rate_limit: int = DEFAULT_RATE_LIMIT,
    rate_cooldown: float = DEFAULT_RATE_COOLDOWN,
    on_trip: list[str] | None = None,
) -> None:
    """Run one managed runtime until SIGINT or SIGTERM."""
    runtime = create_managed_runtime(
        agent_id=agent_id,
        topic_address=topic_address,
        broker=broker,
        port=port,
        persistent=persistent,
        presence=presence,
        lifecycle=lifecycle,
        client_id=client_id,
        capabilities=capabilities,
        state_dir=state_dir,
        topic_root=topic_root,
        registry_heartbeat_seconds=registry_heartbeat_seconds,
        registry_stale_after_seconds=registry_stale_after_seconds,
        username=username,
        password=password,
        tls=tls,
        ca_cert=ca_cert,
        client_cert=client_cert,
        client_key=client_key,
        hop_limit=hop_limit,
        rate_limit=rate_limit,
        rate_cooldown=rate_cooldown,
        on_trip=on_trip,
        trip_listener="mcp-daemon",
    )

    async def serve() -> None:
        stopped = asyncio.Event()
        loop = asyncio.get_running_loop()
        installed: list[signal.Signals] = []
        for signum in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(signum, stopped.set)
            except (NotImplementedError, RuntimeError):
                continue
            installed.append(signum)
        try:
            async with LocalRuntimeDaemon(runtime, socket_path):
                await stopped.wait()
        finally:
            for signum in installed:
                loop.remove_signal_handler(signum)

    asyncio.run(serve())
