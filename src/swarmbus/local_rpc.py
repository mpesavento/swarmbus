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
import subprocess
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any, Literal, NamedTuple, Protocol

import aiomqtt

try:
    import fcntl
except ImportError:
    fcntl = None

from . import __version__
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
# Linux SO_PEERCRED returns struct ucred {pid, uid, gid}.
_LINUX_UCRED = struct.Struct("3i")
# Darwin LOCAL_PEERCRED returns struct xucred, 76 bytes, whose first two fields
# are cr_version and cr_uid (both u_int32). A short getsockopt read of just
# those is legal. FreeBSD's struct of the same name is 88 bytes and carries a
# pid; do not reuse a layout written for it.
_DARWIN_XUCRED_PREFIX = struct.Struct("II")
_DARWIN_XUCRED_VERSION = 0
# CPython does not expose socket.SOL_LOCAL; on Darwin it is 0.
_SOL_LOCAL = 0
_READ_TIMEOUT_SECONDS = 30.0
_RESPONSE_MARGIN_SECONDS = 5.0
DAEMON_START_TIMEOUT_SECONDS = 15.0
_PROBE_TIMEOUT_SECONDS = 2.0
_START_POLL_SECONDS = 0.1


class LocalRPCError(RuntimeError):
    """Base error for the private runtime transport."""


class LocalRPCUnavailable(LocalRPCError, TransportUnavailable):
    """The local runtime daemon cannot be reached."""


class LocalRPCProtocolError(LocalRPCError):
    """A local peer sent an invalid protocol frame."""


class LocalRPCOwnershipError(LocalRPCError):
    """The socket or lock path is not safe for this daemon."""


class LocalRPCDaemonMismatch(LocalRPCError):
    """A running daemon's settings or version differ from the caller's."""


class LocalRPCStartTimeout(LocalRPCError):
    """A started daemon never answered."""


class _PeerCredAPI(NamedTuple):
    """How to ask this platform for a Unix socket peer's uid."""

    level: int
    option: int
    nbytes: int
    decode: Callable[[bytes], int]


def _decode_linux_ucred(raw: bytes) -> int:
    _, uid, _ = _LINUX_UCRED.unpack(raw)
    return uid


def _decode_darwin_xucred(raw: bytes) -> int:
    version, uid = _DARWIN_XUCRED_PREFIX.unpack(raw)
    if version != _DARWIN_XUCRED_VERSION:
        raise LocalRPCOwnershipError(
            f"unsupported xucred version {version}; refusing to trust peer uid"
        )
    return uid


def _resolve_peer_cred_api() -> _PeerCredAPI | None:
    if hasattr(socket, "SO_PEERCRED"):
        return _PeerCredAPI(
            socket.SOL_SOCKET,
            socket.SO_PEERCRED,
            _LINUX_UCRED.size,
            _decode_linux_ucred,
        )
    if hasattr(socket, "LOCAL_PEERCRED"):
        return _PeerCredAPI(
            _SOL_LOCAL,
            socket.LOCAL_PEERCRED,
            _DARWIN_XUCRED_PREFIX.size,
            _decode_darwin_xucred,
        )
    return None


# Resolved once so start()'s platform refusal and _peer_uid()'s decode can never
# disagree. If they could, the daemon would start on a platform whose peer uid it
# cannot read -- and the peer uid is the only thing authenticating a caller.
_PEER_CRED_API = _resolve_peer_cred_api()

# The accepted parameter names per RPC operation. Pinned explicitly rather than
# derived from the runtime signatures, so a new runtime parameter does not widen
# the wire contract until someone adds it here on purpose.
_DISPATCH_PARAMS: dict[str, frozenset[str]] = {
    "shutdown": frozenset(),
    "ping": frozenset(),
    "send_message": frozenset(
        {"to", "subject", "body", "content_type", "in_reply_to"}
    ),
    "read_inbox": frozenset({"ack_ids", "max_messages", "wait_seconds"}),
    "list_states": frozenset({"include_offline", "lifecycle"}),
    "get_state": frozenset({"agent_id"}),
    "update_state": frozenset({"status", "working_set", "capabilities"}),
}

# The smallest sun_path across supported platforms: 104 bytes on Darwin, 108 on
# Linux. Used only to sanity-check a diagnosis, never to predict a limit.
_SMALLEST_SUN_PATH = 104


def _over_long_socket_path(exc: OSError, socket_path: Path) -> str | None:
    """Explain an AF_UNIX length refusal, or None if that is not the cause.

    CPython rejects a path that will not fit `sun_path` before issuing the
    syscall and sets no errno, which is what separates this from a kernel bind
    or connect failure. The limit is platform-dependent, so a path that works
    on Linux can be refused on macOS.
    """
    if exc.errno is not None:
        return None
    encoded = len(os.fsencode(str(socket_path)))
    if encoded < _SMALLEST_SUN_PATH:
        return None
    return (
        f"socket path is {encoded} bytes, past this platform's AF_UNIX limit "
        f"({_SMALLEST_SUN_PATH} on macOS, 108 on Linux): {socket_path}"
    )


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


class _LocalRuntime(_MCPRuntime, Protocol):
    async def runtime_status(self) -> dict[str, Any]: ...


class LocalRuntimeProxy:
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
            # Without this the message reads "unavailable", sending a reader
            # after a daemon that may be running fine.
            detail = _over_long_socket_path(exc, self.socket_path)
            raise LocalRPCUnavailable(
                detail
                if detail is not None
                else f"local runtime unavailable at {self.socket_path}: {exc}"
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
        in_reply_to: str | None = None,
    ) -> None:
        params = {
            "to": to,
            "subject": subject,
            "body": body,
            "content_type": content_type,
            "in_reply_to": in_reply_to,
        }
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

    def __init__(
        self,
        runtime: _LocalRuntime,
        socket_path: str | Path,
        *,
        settings: dict[str, Any] | None = None,
    ) -> None:
        self.runtime = runtime
        self.settings = settings
        self.socket_path = Path(socket_path)
        self.lock_path = Path(f"{self.socket_path}.lock")
        self._lock_fd: int | None = None
        self._server: asyncio.AbstractServer | None = None
        self._socket_identity: tuple[int, int] | None = None
        self._runtime_started = False
        self._shutdown_requested = asyncio.Event()

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

        keep_open = False
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
            self._lock_fd = lock_fd
            keep_open = True
        finally:
            if not keep_open:
                os.close(lock_fd)

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

    def _peer_uid(self, writer: asyncio.StreamWriter) -> int:
        """The connected peer's effective uid.

        Raises rather than returning None: a caller that cannot read the peer
        uid must refuse the connection, never serve it unauthenticated.
        """
        if _PEER_CRED_API is None:
            raise LocalRPCOwnershipError(
                "no peer-credential API on this platform"
            )
        peer_socket = writer.get_extra_info("socket")
        if peer_socket is None:
            raise LocalRPCOwnershipError(
                "connection exposes no socket to read credentials from"
            )
        credentials = peer_socket.getsockopt(
            _PEER_CRED_API.level,
            _PEER_CRED_API.option,
            _PEER_CRED_API.nbytes,
        )
        return _PEER_CRED_API.decode(credentials)

    def _peer_denial(
        self,
        writer: asyncio.StreamWriter,
    ) -> dict[str, Any] | None:
        """A failure response when the peer is not this uid, else None."""
        try:
            peer_uid = self._peer_uid(writer)
        except (LocalRPCOwnershipError, OSError, struct.error) as exc:
            return _failure(
                "permission_denied",
                f"cannot verify peer uid: {exc}",
            )
        if peer_uid != os.getuid():
            return _failure("permission_denied", "peer uid does not match")
        return None

    async def _dispatch(self, operation: str, params: dict[str, Any]) -> Any:
        allowed = _DISPATCH_PARAMS.get(operation)
        if allowed is None:
            raise LocalRPCProtocolError(f"unknown RPC operation {operation!r}")
        rejected = sorted(set(params) - allowed)
        if rejected:
            raise ValueError(
                f"{operation} does not accept {', '.join(rejected)}"
            )
        if operation == "shutdown":
            return {"accepted": True}
        if operation == "ping":
            return {
                **(await self.runtime.runtime_status()),
                "daemon": {
                    "pid": os.getpid(),
                    "version": __version__,
                    "settings": self.settings,
                },
            }
        if operation == "send_message":
            return await self.runtime.send_message(**params)
        if operation == "read_inbox":
            return await self.runtime.read_inbox(**params)
        if operation == "list_states":
            return await self.runtime.list_states(**params)
        if operation == "get_state":
            return await self.runtime.get_state(**params)
        if operation == "update_state":
            return await self.runtime.update_state(**params)
        raise LocalRPCProtocolError(
            f"operation {operation!r} is allow-listed but not dispatched"
        )

    async def _handle(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        shutdown_requested = False
        try:
            try:
                denial = self._peer_denial(writer)
                if denial is not None:
                    response = denial
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
                        shutdown_requested = operation == "shutdown"
            except (LocalRPCProtocolError, asyncio.TimeoutError) as exc:
                response = _failure("protocol_error", str(exc))

            try:
                await _write_frame(writer, response)
            except (ConnectionError, OSError):
                pass
            if shutdown_requested:
                self.request_shutdown()
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except OSError:
                pass

    def request_shutdown(self) -> None:
        self._shutdown_requested.set()

    async def wait_for_shutdown(self) -> None:
        await self._shutdown_requested.wait()

    async def start(self) -> None:
        if fcntl is None or _PEER_CRED_API is None:
            raise LocalRPCOwnershipError(
                "local runtime sharing requires a Unix platform with peer "
                "credentials: Linux SO_PEERCRED or Darwin LOCAL_PEERCRED"
            )
        if self._server is not None:
            raise RuntimeError("local runtime daemon is already started")
        self._validate_parent()
        self._acquire_lock()
        started = False
        try:
            self._remove_stale_socket()
            await self.runtime.start()
            self._runtime_started = True
            try:
                self._server = await asyncio.start_unix_server(
                    self._handle,
                    path=str(self.socket_path),
                )
            except OSError as exc:
                detail = _over_long_socket_path(exc, self.socket_path)
                if detail is None:
                    raise
                raise LocalRPCOwnershipError(detail) from exc
            socket_stat = os.lstat(self.socket_path)
            if (
                not stat.S_ISSOCK(socket_stat.st_mode)
                or socket_stat.st_uid != os.getuid()
            ):
                raise LocalRPCOwnershipError("created socket has unsafe ownership")
            self._socket_identity = (socket_stat.st_dev, socket_stat.st_ino)
            os.chmod(self.socket_path, 0o600)
            started = True
        finally:
            if not started:
                await self.stop()

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None
        try:
            if self._runtime_started:
                self._runtime_started = False
                await self.runtime.stop()
        finally:
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

    async def __aenter__(self) -> "LocalRuntimeDaemon":
        await self.start()
        return self

    async def __aexit__(self, exc_type, exc, traceback) -> None:
        await self.stop()


def _normalized_path(path: str | None) -> str | None:
    if path is None:
        return None
    return os.path.abspath(os.path.expanduser(path))


def daemon_settings(
    *,
    agent_id: str,
    broker: str,
    port: int,
    username: str | None,
    password: str | None,
    tls: bool,
    ca_cert: str | None,
    client_cert: str | None,
    client_key: str | None,
    persistent: bool,
    presence: bool,
    lifecycle: str,
) -> dict[str, Any]:
    """The settings fingerprint a daemon reports and an attaching session compares.

    It holds only whether a password is set. There is no password key and no
    hash or other derivation of it, because the fingerprint crosses the socket
    in every ping reply and appears in error messages.
    """
    return {
        "agent_id": agent_id,
        "broker": broker,
        "port": port,
        "username": username,
        "password_set": password is not None,
        "tls": tls,
        "ca_cert": _normalized_path(ca_cert),
        "client_cert": _normalized_path(client_cert),
        "client_key": _normalized_path(client_key),
        "persistent": persistent,
        "presence": presence,
        "lifecycle": lifecycle,
    }


def probe_daemon(
    socket_path: str | Path,
    *,
    timeout: float = _PROBE_TIMEOUT_SECONDS,
) -> dict[str, Any] | None:
    """Ask the daemon at `socket_path` to ping; None when nothing answers."""
    path = Path(socket_path)
    try:
        result = asyncio.run(
            LocalRuntimeProxy(path)._call("ping", {}, response_timeout=timeout)
        )
    except LocalRPCUnavailable as exc:
        cause = exc.__cause__
        if isinstance(cause, OSError):
            detail = _over_long_socket_path(cause, path)
            if detail is not None:
                raise LocalRPCOwnershipError(detail) from exc
        return None
    if not isinstance(result, dict):
        raise LocalRPCProtocolError("ping result must be an object")
    return result


def _private_dir(path: Path) -> None:
    """Create `path` private to this user, or insist an existing one already is.

    Two sessions can attach at once on a first run, so creation must not be a
    check-then-create: whoever loses the mkdir leaves the winner's directory,
    or one that already existed, exactly as it is.
    """
    try:
        path.mkdir(mode=0o700, parents=True)
        # The umask may have narrowed the mode mkdir was given.
        os.chmod(path, 0o700)
    except FileExistsError:
        pass
    info = path.stat()
    if info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise LocalRPCOwnershipError(
            f"{path} must be private to this user: chmod 700 {path}"
        )


def _check_fingerprint(
    reply: dict[str, Any],
    settings: dict[str, Any],
    socket_path: str | Path,
) -> dict[str, Any]:
    daemon = reply.get("daemon")
    if not isinstance(daemon, dict) or not isinstance(daemon.get("settings"), dict):
        raise LocalRPCDaemonMismatch(
            f"the swarmbus daemon at {socket_path} reports no pid, version or "
            "settings because it predates mcp-attach. Stop that mcp-daemon, "
            "then start this session again."
        )
    running = daemon["settings"]
    fields: list[str] = []
    if daemon.get("version") != __version__:
        fields.append("version")
    fields.extend(
        key
        for key in sorted(set(running) | set(settings))
        if running.get(key) != settings.get(key)
    )
    if fields:
        pid = daemon.get("pid")
        raise LocalRPCDaemonMismatch(
            f"the swarmbus daemon for {settings['agent_id']!r} at {socket_path} "
            f"(pid {pid}, swarmbus {daemon.get('version')}) differs from this "
            f"session in: {', '.join(fields)}. Stop it with `kill {pid}` (it "
            "shuts down cleanly on SIGTERM), then start this session again."
        )
    return reply


def ensure_daemon(
    socket_path: str | Path,
    *,
    settings: dict[str, Any],
    daemon_argv: list[str],
    env: dict[str, str],
    log_path: str | Path,
    start_timeout: float = DAEMON_START_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    """Return the ping reply of the daemon on `socket_path`, starting one if needed.

    A daemon that is already running must carry this session's settings and
    swarmbus version, or LocalRPCDaemonMismatch names the differing fields.
    """
    socket_path = Path(socket_path)
    log_path = Path(log_path)
    _private_dir(socket_path.parent)
    _private_dir(log_path.parent)

    reply = probe_daemon(socket_path)
    if reply is not None:
        return _check_fingerprint(reply, settings, socket_path)

    flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND
    flags |= getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    log_fd = os.open(log_path, flags, 0o600)
    try:
        # The child must never inherit this process's stdin or stdout: they are
        # the MCP channel to the session, and a daemon holding them would corrupt
        # it and outlive it.
        proc = subprocess.Popen(
            daemon_argv,
            stdin=subprocess.DEVNULL,
            stdout=log_fd,
            stderr=subprocess.STDOUT,
            env=env,
            cwd=str(socket_path.parent),
            start_new_session=True,
            close_fds=True,
        )
    finally:
        os.close(log_fd)

    deadline = time.monotonic() + start_timeout
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        # Reap a child that exited. It is not a failure by itself: under a
        # concurrent attach the loser exits while the winner comes up.
        proc.poll()
        reply = probe_daemon(
            socket_path, timeout=min(_PROBE_TIMEOUT_SECONDS, remaining)
        )
        if reply is not None:
            return _check_fingerprint(reply, settings, socket_path)
        time.sleep(_START_POLL_SECONDS)

    code = proc.poll()
    exited = f" (it exited with status {code})" if code is not None else ""
    raise LocalRPCStartTimeout(
        f"swarmbus mcp-daemon did not answer at {socket_path} within "
        f"{start_timeout:g}s{exited}; see {log_path}"
    )


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
    persistent: bool = False,
    presence: bool = False,
    lifecycle: Literal["named", "transient"] = "named",
    capabilities: tuple[str, ...] = (),
    state_dir: str = "~/.local/state/swarmbus",
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
    """Run one managed runtime until a signal or local shutdown request."""
    runtime = create_managed_runtime(
        agent_id=agent_id,
        broker=broker,
        port=port,
        persistent=persistent,
        presence=presence,
        lifecycle=lifecycle,
        capabilities=capabilities,
        state_dir=state_dir,
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

    settings = daemon_settings(
        agent_id=agent_id,
        broker=broker,
        port=port,
        username=username,
        password=password,
        tls=tls,
        ca_cert=ca_cert,
        client_cert=client_cert,
        client_key=client_key,
        persistent=persistent,
        presence=presence,
        lifecycle=lifecycle,
    )

    async def serve() -> None:
        daemon = LocalRuntimeDaemon(runtime, socket_path, settings=settings)
        loop = asyncio.get_running_loop()
        installed: list[signal.Signals] = []
        for signum in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(signum, daemon.request_shutdown)
            except (NotImplementedError, RuntimeError):
                continue
            installed.append(signum)
        try:
            async with daemon:
                await daemon.wait_for_shutdown()
        finally:
            for signum in installed:
                loop.remove_signal_handler(signum)

    asyncio.run(serve())
