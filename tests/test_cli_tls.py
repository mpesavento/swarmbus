"""CLI surface tests for the broker auth + TLS flags added in issue #7.

These verify two contracts:

1. Each of ``send``, ``start``, ``read``, ``watch``, ``list``,
   ``mcp-server``, ``doctor`` accepts ``--username`` / ``--password`` /
   ``--ca-cert`` / ``--client-cert`` / ``--client-key`` / ``--tls`` and
   threads them into ``AgentBus`` (or ``run_mcp_server``).
2. The ``SWARMBUS_BROKER_*`` env vars are picked up as fallbacks per
   issue #7's spec.

Mocks ``AgentBus`` / ``run_mcp_server`` so no broker is required.
"""
from unittest.mock import patch, AsyncMock
from click.testing import CliRunner

from swarmbus.cli import main


# ---------------------------------------------------------------------------
# send
# ---------------------------------------------------------------------------


def test_send_passes_username_password_via_flags():
    runner = CliRunner()
    with patch("swarmbus.cli.AgentBus") as MockBus:
        MockBus.return_value.send = AsyncMock()
        result = runner.invoke(main, [
            "send", "--agent-id", "tx", "--to", "rx",
            "--subject", "s", "--body", "b",
            "--username", "alice", "--password", "secret",
        ])
    assert result.exit_code == 0, result.output
    init_kwargs = MockBus.call_args.kwargs
    assert init_kwargs["username"] == "alice"
    assert init_kwargs["password"] == "secret"


def test_send_passes_tls_and_ca_cert():
    runner = CliRunner()
    with patch("swarmbus.cli.AgentBus") as MockBus:
        MockBus.return_value.send = AsyncMock()
        result = runner.invoke(main, [
            "send", "--agent-id", "tx", "--to", "rx",
            "--subject", "s", "--body", "b",
            "--tls", "--ca-cert", "/etc/ssl/certs/ca.crt",
        ])
    assert result.exit_code == 0, result.output
    init_kwargs = MockBus.call_args.kwargs
    assert init_kwargs["tls"] is True
    assert init_kwargs["ca_cert"] == "/etc/ssl/certs/ca.crt"


def test_send_picks_up_swarmbus_broker_env_vars():
    """The exact env-var prefix defined by upstream issue #7."""
    runner = CliRunner()
    with patch("swarmbus.cli.AgentBus") as MockBus:
        MockBus.return_value.send = AsyncMock()
        result = runner.invoke(
            main,
            ["send", "--agent-id", "tx", "--to", "rx",
             "--subject", "s", "--body", "b"],
            env={
                "SWARMBUS_BROKER_USERNAME": "env-alice",
                "SWARMBUS_BROKER_PASSWORD": "env-secret",
                "SWARMBUS_BROKER_CA_CERT": "/env/ca.crt",
                "SWARMBUS_BROKER_CLIENT_CERT": "/env/client.crt",
                "SWARMBUS_BROKER_CLIENT_KEY": "/env/client.key",
                "SWARMBUS_BROKER_TLS": "1",
            },
        )
    assert result.exit_code == 0, result.output
    kw = MockBus.call_args.kwargs
    assert kw["username"] == "env-alice"
    assert kw["password"] == "env-secret"
    assert kw["ca_cert"] == "/env/ca.crt"
    assert kw["client_cert"] == "/env/client.crt"
    assert kw["client_key"] == "/env/client.key"
    assert kw["tls"] is True


def test_send_explicit_flag_overrides_env_var():
    """Click contract: explicit CLI flag wins over envvar."""
    runner = CliRunner()
    with patch("swarmbus.cli.AgentBus") as MockBus:
        MockBus.return_value.send = AsyncMock()
        result = runner.invoke(
            main,
            ["send", "--agent-id", "tx", "--to", "rx",
             "--subject", "s", "--body", "b",
             "--username", "flag-alice"],
            env={"SWARMBUS_BROKER_USERNAME": "env-alice"},
        )
    assert result.exit_code == 0, result.output
    assert MockBus.call_args.kwargs["username"] == "flag-alice"


# ---------------------------------------------------------------------------
# read / watch / list
# ---------------------------------------------------------------------------


def test_read_passes_auth_through():
    runner = CliRunner()
    with patch("swarmbus.cli.AgentBus") as MockBus:
        MockBus.return_value.read_inbox = AsyncMock(return_value=[])
        result = runner.invoke(
            main,
            ["read", "--agent-id", "rx"],
            env={
                "SWARMBUS_BROKER_USERNAME": "alice",
                "SWARMBUS_BROKER_PASSWORD": "secret",
            },
        )
    assert result.exit_code == 0, result.output
    assert MockBus.call_args.kwargs["username"] == "alice"
    assert MockBus.call_args.kwargs["password"] == "secret"


def test_watch_passes_auth_through():
    runner = CliRunner()
    with patch("swarmbus.cli.AgentBus") as MockBus:
        MockBus.return_value.watch_inbox = AsyncMock(return_value=None)
        result = runner.invoke(
            main,
            ["watch", "--agent-id", "rx", "--timeout", "0.1"],
            env={"SWARMBUS_BROKER_USERNAME": "alice"},
        )
    # watch returns exit 1 on timeout (None) but auth still threaded through.
    assert MockBus.call_args.kwargs["username"] == "alice"


def test_list_passes_auth_through():
    runner = CliRunner()
    with patch("swarmbus.cli.AgentBus") as MockBusClass:
        # list_agents_cmd uses AgentBus.probe — patch that too.
        MockBusClass.probe.return_value.list_agents = AsyncMock(return_value=[])
        result = runner.invoke(
            main,
            ["list"],
            env={
                "SWARMBUS_BROKER_USERNAME": "alice",
                "SWARMBUS_BROKER_TLS": "1",
            },
        )
    assert result.exit_code == 0, result.output
    probe_kw = MockBusClass.probe.call_args.kwargs
    assert probe_kw["username"] == "alice"
    assert probe_kw["tls"] is True


# ---------------------------------------------------------------------------
# mcp-server — kwargs flow into run_mcp_server
# ---------------------------------------------------------------------------


def test_mcp_server_passes_auth_to_run_mcp_server():
    runner = CliRunner()
    with patch("swarmbus.mcp_server.run_mcp_server") as mock_run:
        result = runner.invoke(
            main,
            ["mcp-server", "--agent-id", "sb",
             "--username", "alice", "--password", "secret",
             "--tls", "--ca-cert", "/ca.crt"],
        )
    assert result.exit_code == 0, result.output
    kw = mock_run.call_args.kwargs
    assert kw["username"] == "alice"
    assert kw["password"] == "secret"
    assert kw["tls"] is True
    assert kw["ca_cert"] == "/ca.crt"


def test_mcp_server_picks_up_env_vars():
    runner = CliRunner()
    with patch("swarmbus.mcp_server.run_mcp_server") as mock_run:
        result = runner.invoke(
            main,
            ["mcp-server", "--agent-id", "sb"],
            env={
                "SWARMBUS_BROKER_USERNAME": "env-alice",
                "SWARMBUS_BROKER_TLS": "1",
            },
        )
    assert result.exit_code == 0, result.output
    kw = mock_run.call_args.kwargs
    assert kw["username"] == "env-alice"
    assert kw["tls"] is True


# ---------------------------------------------------------------------------
# start
# ---------------------------------------------------------------------------


def test_start_passes_auth_through():
    runner = CliRunner()
    with patch("swarmbus.cli.AgentBus") as MockBus:
        MockBus.return_value.run = lambda: None
        MockBus.return_value.register_handler = lambda h: None
        result = runner.invoke(
            main,
            ["start", "--agent-id", "rx", "--broker", "localhost",
             "--username", "alice", "--password", "secret", "--tls"],
        )
    kw = MockBus.call_args.kwargs
    assert kw["username"] == "alice"
    assert kw["password"] == "secret"
    assert kw["tls"] is True


# ---------------------------------------------------------------------------
# doctor
# ---------------------------------------------------------------------------


def test_doctor_passes_auth_to_probe():
    """doctor threads auth kwargs into AgentBus.probe() for peer discovery."""
    runner = CliRunner()
    with patch("swarmbus.cli.AgentBus") as MockBus, \
         patch("swarmbus.cli.aiomqtt"):
        MockBus.probe.return_value._aiomqtt_kwargs = lambda: {}
        MockBus.probe.return_value.list_agents = AsyncMock(return_value=[])
        result = runner.invoke(
            main,
            ["doctor", "--agent-id", "test-agent",
             "--username", "alice", "--password", "secret", "--tls"],
        )
    # probe() is called at least once (step 7); after the refactor, also step 2.
    assert MockBus.probe.call_count >= 1
    # Check the last call (step 7 peer discovery) has auth kwargs.
    probe_kw = MockBus.probe.call_args.kwargs
    assert probe_kw["username"] == "alice"
    assert probe_kw["password"] == "secret"
    assert probe_kw["tls"] is True


# ---------------------------------------------------------------------------
# init
# ---------------------------------------------------------------------------


def test_init_passes_auth_to_step_systemd():
    """init threads auth kwargs through to _step_systemd."""
    runner = CliRunner()
    with patch("swarmbus.cli._step_broker", return_value=True), \
         patch("swarmbus.cli._step_package", return_value=True), \
         patch("swarmbus.cli._step_systemd", return_value=True) as mock_sys, \
         patch("swarmbus.cli._step_wake_wrapper", return_value=True), \
         patch("swarmbus.cli._step_plugin", return_value=True), \
         patch("swarmbus.cli._step_doctor", return_value=True), \
         patch("swarmbus.cli.find_repo_root", return_value="/fake/repo"), \
         patch("swarmbus.cli.detect_platform", return_value="linux"), \
         patch("swarmbus.cli.resolve_broker_addr", return_value="localhost"):
        result = runner.invoke(
            main,
            ["init", "--agent-id", "rx", "--host-type", "cc",
             "--yes",
             "--username", "alice", "--password", "secret", "--tls"],
        )
    kw = mock_sys.call_args.kwargs
    assert kw["username"] == "alice"
    assert kw["password"] == "secret"
    assert kw["tls"] is True
