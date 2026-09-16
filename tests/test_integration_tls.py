# tests/test_integration_tls.py
"""End-to-end TLS+auth coverage against a real eclipse-mosquitto-style broker.

Spins up a mosquitto with a self-signed CA + server cert and a password
file, then exercises the same send/receive contract as the plaintext
``test_integration.py`` battery. Skipped automatically when openssl or
mosquitto_passwd is unavailable, so this fits into a CI lane that lacks
the optional system tools.

The fixture is intentionally local to this file: the cert/conf/password
generation is non-trivial and shouldn't crowd ``conftest.py`` for the
common-case plaintext fixture.
"""
from __future__ import annotations

import asyncio
import os
import shutil
import socket
import ssl
import subprocess
import time
from pathlib import Path
from typing import Iterator

import pytest

from swarmbus.bus import AgentBus
from swarmbus.handlers.base import BaseHandler
from swarmbus.message import AgentMessage


def _free_port() -> int:
    s = socket.socket()
    s.bind(("", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _have(*cmds: str) -> bool:
    return all(shutil.which(c) is not None for c in cmds)


@pytest.fixture(scope="module")
def mosquitto_tls_broker(tmp_path_factory) -> Iterator[dict]:
    """Yield ``{host, port, ca_cert, username, password}`` for a TLS broker
    that requires user/pass + TLS. Fails fast if required tooling is
    missing (rather than producing inscrutable connect errors mid-test).
    """
    if not _have("openssl", "mosquitto_passwd", "/usr/sbin/mosquitto"):
        pytest.skip("openssl, mosquitto_passwd, or /usr/sbin/mosquitto missing")

    tmp = tmp_path_factory.mktemp("mqtt-tls")
    ca_key = tmp / "ca.key"
    ca_cert = tmp / "ca.crt"
    srv_key = tmp / "srv.key"
    srv_csr = tmp / "srv.csr"
    srv_cert = tmp / "srv.crt"
    pwfile = tmp / "passwd"
    conf = tmp / "mosquitto.conf"

    # CA
    subprocess.run(["openssl", "genrsa", "-out", str(ca_key), "2048"],
                   check=True, capture_output=True)
    subprocess.run([
        "openssl", "req", "-new", "-x509", "-days", "1",
        "-key", str(ca_key), "-out", str(ca_cert),
        "-subj", "/CN=swarmbus-test-ca",
        "-addext", "keyUsage=critical,keyCertSign,cRLSign",
        "-addext", "basicConstraints=critical,CA:TRUE",
    ], check=True, capture_output=True)

    # Server cert with localhost SAN. The SAN is what aiomqtt/ssl will
    # actually verify against — without it, the handshake fails CN check.
    subprocess.run(["openssl", "genrsa", "-out", str(srv_key), "2048"],
                   check=True, capture_output=True)
    san_cnf = tmp / "san.cnf"
    san_cnf.write_text(
        "[req]\ndistinguished_name=req\nreq_extensions=v3_req\n"
        "[v3_req]\nsubjectAltName=@alt_names\n"
        "[alt_names]\nDNS.1=localhost\nIP.1=127.0.0.1\n"
    )
    subprocess.run([
        "openssl", "req", "-new", "-key", str(srv_key), "-out", str(srv_csr),
        "-subj", "/CN=localhost",
        "-config", str(san_cnf),
    ], check=True, capture_output=True)
    subprocess.run([
        "openssl", "x509", "-req", "-in", str(srv_csr),
        "-CA", str(ca_cert), "-CAkey", str(ca_key), "-CAcreateserial",
        "-out", str(srv_cert), "-days", "1",
        "-extensions", "v3_req", "-extfile", str(san_cnf),
    ], check=True, capture_output=True)

    # Password file. mosquitto_passwd -c -b creates and adds in one shot.
    subprocess.run([
        "mosquitto_passwd", "-c", "-b", str(pwfile), "alice", "secret",
    ], check=True, capture_output=True)

    port = _free_port()
    conf.write_text(
        # Mosquitto 2.0+ drops privileges to the ``mosquitto`` user by
        # default when a config file is provided. Inside a test container
        # running as root the temp-dir certs/pwfile are root-owned and
        # unreadable after the drop, so we keep root.
        "user root\n"
        "per_listener_settings true\n"
        f"listener {port} 0.0.0.0\n"
        "allow_anonymous false\n"
        f"password_file {pwfile}\n"
        f"cafile {ca_cert}\n"
        f"certfile {srv_cert}\n"
        f"keyfile {srv_key}\n"
        # Don't require client cert -- TLS+password only. mTLS (client
        # cert) is validated at the unit level in test_bus_tls.py.
        "require_certificate false\n"
    )

    proc = subprocess.Popen(
        ["/usr/sbin/mosquitto", "-c", str(conf)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    time.sleep(0.5)  # broker boot
    if proc.poll() is not None:
        pytest.fail(f"mosquitto failed to start; conf={conf.read_text()}")

    try:
        yield {
            "host": "localhost",
            "port": port,
            "ca_cert": str(ca_cert),
            "username": "alice",
            "password": "secret",
        }
    finally:
        proc.terminate()
        proc.wait()


class _Recorder(BaseHandler):
    def __init__(self):
        self.received: list[AgentMessage] = []
        self._event = asyncio.Event()

    async def handle(self, msg: AgentMessage) -> None:
        self.received.append(msg)
        self._event.set()

    async def wait(self, timeout: float = 5.0) -> None:
        await asyncio.wait_for(self._event.wait(), timeout=timeout)
        self._event.clear()


@pytest.mark.asyncio
async def test_tls_with_password_roundtrip(mosquitto_tls_broker):
    """The headline scenario: TLS-encrypted, password-authenticated send +
    receive succeeds end-to-end. This is the deployment shape Plan A
    (bucket MQTT) hands swarmbus."""
    cfg = mosquitto_tls_broker
    handler = _Recorder()
    receiver = AgentBus(
        agent_id="rx", broker=cfg["host"], port=cfg["port"],
        username=cfg["username"], password=cfg["password"],
        ca_cert=cfg["ca_cert"],
    )
    receiver.register_handler(handler)
    sender = AgentBus(
        agent_id="tx", broker=cfg["host"], port=cfg["port"],
        username=cfg["username"], password=cfg["password"],
        ca_cert=cfg["ca_cert"],
    )

    listen_task = asyncio.create_task(receiver.listen())
    await asyncio.sleep(0.3)
    try:
        await sender.send(to="rx", subject="hi", body="encrypted hello")
        await handler.wait(timeout=5.0)
        assert handler.received[0].body == "encrypted hello"
    finally:
        listen_task.cancel()
        try:
            await listen_task
        except (asyncio.CancelledError, Exception):
            pass


@pytest.mark.asyncio
async def test_tls_wrong_password_fails(mosquitto_tls_broker):
    """Wrong password must fail at MQTT auth, not silently succeed.
    Catches a regression where credentials would be quietly dropped."""
    import aiomqtt
    cfg = mosquitto_tls_broker
    bus = AgentBus(
        agent_id="probe", broker=cfg["host"], port=cfg["port"],
        username=cfg["username"], password="WRONG",
        ca_cert=cfg["ca_cert"],
    )
    with pytest.raises(aiomqtt.MqttError):
        await bus.send(to="rx", subject="x", body="x")


@pytest.mark.asyncio
async def test_tls_anonymous_fails(mosquitto_tls_broker):
    """Anonymous connect to a hardened broker must fail. If this passes,
    the broker fixture is misconfigured (allow_anonymous true) and every
    other test in this module is suspect."""
    import aiomqtt
    cfg = mosquitto_tls_broker
    bus = AgentBus(
        agent_id="probe", broker=cfg["host"], port=cfg["port"],
        ca_cert=cfg["ca_cert"],  # TLS but no creds
    )
    with pytest.raises(aiomqtt.MqttError):
        await bus.send(to="rx", subject="x", body="x")
