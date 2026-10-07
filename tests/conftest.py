# tests/conftest.py
import socket
import subprocess
import time
import pytest


def _free_port() -> int:
    s = socket.socket()
    s.bind(("", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _wait_for_broker(proc: subprocess.Popen, port: int, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            raise RuntimeError("mosquitto failed to start")
        try:
            with socket.create_connection(("localhost", port), timeout=0.1):
                return
        except OSError:
            time.sleep(0.01)
    raise TimeoutError("mosquitto did not accept connections")


@pytest.fixture(scope="session")
def mosquitto_broker():
    """Start a real mosquitto broker on a free port. Yields (host, port)."""
    port = _free_port()
    proc = subprocess.Popen(
        ["/usr/sbin/mosquitto", "-p", str(port)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    _wait_for_broker(proc, port)
    yield ("localhost", port)
    proc.terminate()
    proc.wait()
