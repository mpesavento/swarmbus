# tests/conftest.py
import os
import shutil
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


MOSQUITTO_FALLBACKS = (
    "/usr/sbin/mosquitto",
    "/opt/homebrew/sbin/mosquitto",
    "/usr/local/sbin/mosquitto",
)


def _mosquitto_bin():
    """Path to a mosquitto binary: PATH first, then the usual sbin locations."""
    found = shutil.which("mosquitto")
    if found:
        return found
    for path in MOSQUITTO_FALLBACKS:
        if os.access(path, os.X_OK):
            return path
    return None


@pytest.fixture(scope="session")
def mosquitto_broker():
    """Start a real mosquitto broker on a free port. Yields (host, port)."""
    mosquitto = _mosquitto_bin()
    if mosquitto is None:
        pytest.fail(
            "mosquitto not found on PATH or in /usr/sbin, /opt/homebrew/sbin, /usr/local/sbin: "
            "install it (apt install mosquitto / brew install mosquitto) or put it on PATH"
        )
    port = _free_port()
    proc = subprocess.Popen(
        [mosquitto, "-p", str(port)],
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
    for key in ("SWARMBUS_HOP_LIMIT", "SWARMBUS_RATE_LIMIT", "SWARMBUS_RATE_COOLDOWN", "SWARMBUS_ON_TRIP"):
        monkeypatch.delenv(key, raising=False)
