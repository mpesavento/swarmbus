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


@pytest.fixture(scope="session")
def mosquitto_broker():
    """Start a real mosquitto broker on a free port. Yields (host, port)."""
    port = _free_port()
    proc = subprocess.Popen(
        ["/usr/sbin/mosquitto", "-p", str(port)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    time.sleep(0.3)  # let it start
    assert proc.poll() is None, "mosquitto failed to start"
    yield ("localhost", port)
    proc.terminate()
    proc.wait()


@pytest.fixture(autouse=True)
def _isolated_state_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("SWARMBUS_STATE_DIR", str(tmp_path / "swarmbus-state"))
