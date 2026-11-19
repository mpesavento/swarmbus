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


def _require_mosquitto() -> str:
    mosquitto = _mosquitto_bin()
    if mosquitto is None:
        pytest.fail(
            "mosquitto not found on PATH or in /usr/sbin, /opt/homebrew/sbin, /usr/local/sbin: "
            "install it (apt install mosquitto / brew install mosquitto) or put it on PATH"
        )
    return mosquitto


@pytest.fixture(scope="session")
def mosquitto_broker():
    """Start a real mosquitto broker on a free port. Yields (host, port)."""
    mosquitto = _require_mosquitto()
    port = _free_port()
    proc = subprocess.Popen(
        [mosquitto, "-p", str(port)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    _wait_for_broker(proc, port)
    yield ("localhost", port)
    proc.terminate()
    proc.wait()


# The per-session recipe from docs/security.md for a login named `flock`:
# %u is the login and %c the session's MQTT client id. The first line is for
# the tests' own clients, which connect without a login.
PER_SESSION_ACL = """\
topic readwrite #

pattern read agents/%c/inbox
pattern readwrite agents/%c/presence
pattern write swarmbus/registry/%c

user flock
topic write agents/+/+/inbox
topic read agents/+/+/presence
topic read swarmbus/registry/+/+
"""


@pytest.fixture(scope="session")
def mosquitto_acl_broker(tmp_path_factory):
    """Start a real mosquitto that enforces PER_SESSION_ACL. Yields (host, port).

    There is no password file, so a client is whoever its username says.
    Only the ACL is under test.
    """
    mosquitto = _require_mosquitto()
    config_dir = tmp_path_factory.mktemp("mosquitto-acl")
    acl = config_dir / "swarmbus.acl"
    acl.write_text(PER_SESSION_ACL)
    acl.chmod(0o600)
    port = _free_port()
    settings = [
        f"listener {port} 127.0.0.1",
        "allow_anonymous true",
        f"acl_file {acl}",
    ]
    if os.geteuid() == 0:
        # As root, mosquitto switches to its own user before it opens the
        # ACL file, which pytest keeps in a directory only root can enter.
        settings.append("user root")
    config = config_dir / "mosquitto.conf"
    config.write_text("\n".join(settings) + "\n")
    log_path = config_dir / "mosquitto.log"
    with log_path.open("w") as log:
        proc = subprocess.Popen(
            [mosquitto, "-c", str(config)],
            stdout=subprocess.DEVNULL,
            stderr=log,
        )
    try:
        _wait_for_broker(proc, port)
    except RuntimeError as exc:
        raise RuntimeError(f"{exc}: {log_path.read_text().strip()}") from exc
    yield ("127.0.0.1", port)
    proc.terminate()
    proc.wait()


@pytest.fixture(autouse=True)
def _isolated_state_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("SWARMBUS_STATE_DIR", str(tmp_path / "swarmbus-state"))
    for key in (
        "SWARMBUS_HOP_LIMIT",
        "SWARMBUS_RATE_LIMIT",
        "SWARMBUS_RATE_COOLDOWN",
        "SWARMBUS_ON_TRIP",
        "SWARMBUS_MAX_OUTBOX_BYTES",
        "SWARMBUS_KEEP_OUTBOX_FILES",
        "SWARMBUS_TOPIC_ROOT",
        "SWARMBUS_TOPIC_ADDRESS",
    ):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.delenv("SWARMBUS_CONFIG", raising=False)
    try:
        from swarmbus import config as sb_config
    except ImportError:  # a checkout from before the config file (#5)
        return
    monkeypatch.setattr(sb_config, "default_config_path", lambda: tmp_path / "no-swarmbus-config.ini")
