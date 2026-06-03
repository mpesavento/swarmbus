# tests/test_bus_tls.py
"""Unit tests for the broker auth + TLS surface added in issue #7.

These tests verify that the constructor surface, the
``_aiomqtt_kwargs`` helper, and every ``aiomqtt.Client`` call site in
``AgentBus`` propagate ``username`` / ``password`` / ``tls_context``
faithfully. They use mocks for ``aiomqtt.Client`` — no broker is
required. End-to-end TLS+auth coverage against a real broker lives in
``tests/test_integration_tls.py``.
"""
from __future__ import annotations

import ssl
from unittest.mock import patch

import pytest

from swarmbus.bus import AgentBus, _build_tls_context


# ---------------------------------------------------------------------------
# _build_tls_context
# ---------------------------------------------------------------------------


def test_build_tls_context_returns_none_when_all_unset():
    assert _build_tls_context(
        tls=False, ca_cert=None, client_cert=None, client_key=None
    ) is None


def test_build_tls_context_with_tls_flag_uses_system_trust():
    ctx = _build_tls_context(
        tls=True, ca_cert=None, client_cert=None, client_key=None
    )
    assert isinstance(ctx, ssl.SSLContext)


def test_build_tls_context_rejects_partial_mtls():
    with pytest.raises(ValueError, match="must be set together"):
        _build_tls_context(
            tls=False, ca_cert=None, client_cert="/x.crt", client_key=None
        )
    with pytest.raises(ValueError, match="must be set together"):
        _build_tls_context(
            tls=False, ca_cert=None, client_cert=None, client_key="/x.key"
        )


# ---------------------------------------------------------------------------
# _aiomqtt_kwargs
# ---------------------------------------------------------------------------


def test_aiomqtt_kwargs_empty_for_anonymous_plaintext():
    bus = AgentBus(agent_id="anon", broker="localhost")
    assert bus._aiomqtt_kwargs() == {}


def test_aiomqtt_kwargs_with_user_pass_only():
    bus = AgentBus(
        agent_id="auth", broker="localhost",
        username="alice", password="secret",
    )
    assert bus._aiomqtt_kwargs() == {"username": "alice", "password": "secret"}


def test_aiomqtt_kwargs_username_only_no_password():
    bus = AgentBus(agent_id="anon", broker="localhost", username="alice")
    assert bus._aiomqtt_kwargs() == {"username": "alice"}


def test_aiomqtt_kwargs_includes_tls_context_when_tls_enabled():
    bus = AgentBus(agent_id="tlsonly", broker="localhost", tls=True)
    kwargs = bus._aiomqtt_kwargs()
    assert "tls_context" in kwargs
    assert isinstance(kwargs["tls_context"], ssl.SSLContext)


def test_aiomqtt_kwargs_combined_user_pass_and_tls():
    bus = AgentBus(
        agent_id="full", broker="localhost",
        username="alice", password="secret", tls=True,
    )
    kwargs = bus._aiomqtt_kwargs()
    assert kwargs["username"] == "alice"
    assert kwargs["password"] == "secret"
    assert isinstance(kwargs["tls_context"], ssl.SSLContext)


def test_partial_mtls_fails_at_construction_not_first_call():
    """Cert misconfiguration should surface at AgentBus(), not on the
    first network call. This is a usability win for systemd units —
    ``swarmbus start`` either prints a useful traceback at boot or runs
    cleanly. It never silently survives until the first peer message
    arrives."""
    with pytest.raises(ValueError, match="must be set together"):
        AgentBus(
            agent_id="bad", broker="localhost",
            client_cert="/some/cert.pem",  # missing client_key
        )


# ---------------------------------------------------------------------------
# probe() exposes the same surface
# ---------------------------------------------------------------------------


def test_probe_default_anonymous_plaintext():
    bus = AgentBus.probe(broker="localhost")
    assert bus._aiomqtt_kwargs() == {}


def test_probe_with_auth_and_tls():
    bus = AgentBus.probe(
        broker="localhost",
        username="alice", password="secret", tls=True,
    )
    kwargs = bus._aiomqtt_kwargs()
    assert kwargs["username"] == "alice"
    assert kwargs["password"] == "secret"
    assert isinstance(kwargs["tls_context"], ssl.SSLContext)


# ---------------------------------------------------------------------------
# aiomqtt.Client call sites — verify kwargs flow through
# ---------------------------------------------------------------------------


class _FakeClient:
    """Minimal aiomqtt.Client stand-in that records construction kwargs."""
    def __init__(self, *args, **kwargs):
        self.init_args = args
        self.init_kwargs = kwargs
        _FakeClient.last = self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        pass

    async def publish(self, *args, **kwargs):
        pass

    async def subscribe(self, *args, **kwargs):
        pass

    @property
    def messages(self):
        async def _gen():
            if False:
                yield  # pragma: no cover
        return _gen()


@pytest.mark.asyncio
async def test_send_oneshot_passes_username_password_tls():
    bus = AgentBus(
        agent_id="sender", broker="b", port=8883,
        username="alice", password="secret", tls=True,
    )
    with patch("swarmbus.bus.aiomqtt.Client", side_effect=_FakeClient):
        await bus.send(to="rx", subject="s", body="b")
    assert _FakeClient.last.init_kwargs["username"] == "alice"
    assert _FakeClient.last.init_kwargs["password"] == "secret"
    assert isinstance(_FakeClient.last.init_kwargs["tls_context"], ssl.SSLContext)


@pytest.mark.asyncio
async def test_listen_passes_auth_alongside_will_and_persistent():
    bus = AgentBus(
        agent_id="listener", broker="b", port=8883,
        username="alice", password="secret", tls=True,
        persistent=True,
    )
    with patch("swarmbus.bus.aiomqtt.Client", side_effect=_FakeClient):
        await bus.listen()
    kw = _FakeClient.last.init_kwargs
    assert kw["username"] == "alice"
    assert kw["password"] == "secret"
    assert isinstance(kw["tls_context"], ssl.SSLContext)
    # Auth kwargs must coexist with the will + persistent-session kwargs;
    # regression here would mean one merge clobbered the other.
    assert "will" in kw
    assert kw["identifier"] == "swarmbus-listener"
    assert kw["clean_session"] is False


@pytest.mark.asyncio
async def test_read_inbox_passes_auth():
    bus = AgentBus(
        agent_id="rx", broker="b", port=8883,
        username="alice", password="secret",
    )
    with patch("swarmbus.bus.aiomqtt.Client", side_effect=_FakeClient):
        await bus.read_inbox()
    assert _FakeClient.last.init_kwargs["username"] == "alice"
    assert _FakeClient.last.init_kwargs["password"] == "secret"


@pytest.mark.asyncio
async def test_watch_inbox_passes_auth():
    bus = AgentBus(
        agent_id="rx", broker="b", port=8883,
        username="alice", password="secret",
    )
    with patch("swarmbus.bus.aiomqtt.Client", side_effect=_FakeClient):
        await bus.watch_inbox(timeout=0.05)
    assert _FakeClient.last.init_kwargs["username"] == "alice"


@pytest.mark.asyncio
async def test_list_agents_passes_auth():
    bus = AgentBus.probe(
        broker="b", port=8883,
        username="alice", password="secret",
    )
    with patch("swarmbus.bus.aiomqtt.Client", side_effect=_FakeClient):
        await bus.list_agents(collect_window=0.05)
    assert _FakeClient.last.init_kwargs["username"] == "alice"


@pytest.mark.asyncio
async def test_disconnect_passes_auth():
    bus = AgentBus(
        agent_id="rx", broker="b", port=8883,
        username="alice", password="secret",
    )
    with patch("swarmbus.bus.aiomqtt.Client", side_effect=_FakeClient):
        await bus.disconnect()
    assert _FakeClient.last.init_kwargs["username"] == "alice"


@pytest.mark.asyncio
async def test_connect_passes_auth():
    bus = AgentBus(
        agent_id="rx", broker="b", port=8883,
        username="alice", password="secret", tls=True,
    )
    with patch("swarmbus.bus.aiomqtt.Client", side_effect=_FakeClient):
        await bus.connect()
        await bus.close()
    assert _FakeClient.last.init_kwargs["username"] == "alice"
    assert _FakeClient.last.init_kwargs["password"] == "secret"
    assert isinstance(_FakeClient.last.init_kwargs["tls_context"], ssl.SSLContext)


# ---------------------------------------------------------------------------
# Backward compatibility — anonymous remains the default and must not
# silently start sending TLS or username/password kwargs through.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_anonymous_send_passes_no_auth_kwargs():
    """A bare AgentBus must construct aiomqtt.Client with no auth/TLS
    kwargs at all — this preserves the existing v0.1.x behaviour."""
    bus = AgentBus(agent_id="anon", broker="b", port=1883)
    with patch("swarmbus.bus.aiomqtt.Client", side_effect=_FakeClient):
        await bus.send(to="rx", subject="s", body="b")
    kw = _FakeClient.last.init_kwargs
    assert "username" not in kw
    assert "password" not in kw
    assert "tls_context" not in kw
