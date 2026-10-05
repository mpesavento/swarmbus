# tests/test_cli_tls.py
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
from typing import get_args
from unittest.mock import patch, AsyncMock

import pytest
from click.testing import CliRunner

from swarmbus.cli import main
from swarmbus.registry import AgentLifecycle


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


def test_mcp_server_durable_flag_threads_through():
    runner = CliRunner()
    with patch("swarmbus.mcp_server.run_mcp_server") as mock_run:
        result = runner.invoke(
            main,
            ["mcp-server", "--agent-id", "sb", "--durable", "--presence"],
        )
    assert result.exit_code == 0, result.output
    assert mock_run.call_args.kwargs["durable"] is True


def test_mcp_server_no_durable_is_default():
    runner = CliRunner()
    with patch("swarmbus.mcp_server.run_mcp_server") as mock_run:
        result = runner.invoke(
            main,
            ["mcp-server", "--agent-id", "sb"],
        )
    assert result.exit_code == 0, result.output
    assert mock_run.call_args.kwargs["durable"] is False


def test_mcp_server_registry_timing_env_vars():
    runner = CliRunner()
    with patch("swarmbus.mcp_server.run_mcp_server") as mock_run:
        result = runner.invoke(
            main,
            ["mcp-server", "--agent-id", "sb"],
            env={
                "SWARMBUS_REGISTRY_HEARTBEAT_SECONDS": "20",
                "SWARMBUS_REGISTRY_STALE_AFTER_SECONDS": "90",
            },
        )

    assert result.exit_code == 0, result.output
    assert mock_run.call_args.kwargs["registry_heartbeat_seconds"] == 20
    assert mock_run.call_args.kwargs["registry_stale_after_seconds"] == 90


def test_mcp_server_threads_registry_lifecycle_options():
    runner = CliRunner()
    with patch("swarmbus.mcp_server.run_mcp_server") as mock_run:
        result = runner.invoke(
            main,
            [
                "mcp-server",
                "--agent-id",
                "sb",
                "--lifecycle",
                "transient",
                "--presence",
                "--client-id",
                "sb-session-7",
                "--capability",
                "development.files.write",
                "--capability",
                "web.read",
                "--registry-heartbeat-seconds",
                "20",
                "--registry-stale-after-seconds",
                "90",
            ],
        )

    assert result.exit_code == 0, result.output
    assert mock_run.call_args.kwargs["lifecycle"] == "transient"
    assert mock_run.call_args.kwargs["presence"] is True
    assert mock_run.call_args.kwargs["client_id"] == "sb-session-7"
    assert mock_run.call_args.kwargs["capabilities"] == (
        "development.files.write",
        "web.read",
    )
    assert mock_run.call_args.kwargs["registry_heartbeat_seconds"] == 20
    assert mock_run.call_args.kwargs["registry_stale_after_seconds"] == 90


def _lifecycle_choices():
    """Read the --lifecycle vocabulary off the command that declares it."""
    command = main.commands["mcp-server"]
    option = next(
        param for param in command.params if param.name == "lifecycle"
    )
    return tuple(option.type.choices)


def test_mcp_server_lifecycle_choices_match_registry_type():
    """Keep CLI lifecycle choices aligned with AgentLifecycle."""
    assert set(_lifecycle_choices()) == set(get_args(AgentLifecycle))


def test_mcp_server_rejects_transient_without_presence():
    """Transient lifecycle without presence is a CLI usage error."""
    runner = CliRunner()
    with patch("swarmbus.mcp_server.run_mcp_server") as mock_run:
        result = runner.invoke(
            main,
            ["mcp-server", "--agent-id", "sb", "--lifecycle", "transient"],
        )

    assert result.exit_code == 2, result.output
    assert "--lifecycle transient" in result.output
    assert "--presence" in result.output
    assert mock_run.call_count == 0


_ACCEPTED_DURABLE_PRESENCE_FLAGS = [
    ([], False, False),
    (["--presence"], False, True),
    (["--durable", "--presence"], True, True),
]
_REJECTED_DURABLE_PRESENCE_FLAGS = [
    (["--durable"], True, False),
]


def test_mcp_server_durable_presence_flag_matrix_is_exhaustive():
    """Cover the lifecycle/presence flag cross product."""
    covered = {
        (durable, presence)
        for _, durable, presence in (
            _ACCEPTED_DURABLE_PRESENCE_FLAGS
            + _REJECTED_DURABLE_PRESENCE_FLAGS
        )
    }

    assert covered == {
        (durable, presence)
        for durable in (False, True)
        for presence in (False, True)
    }


@pytest.mark.parametrize(
    ("flags", "expected_durable", "expected_presence"),
    _ACCEPTED_DURABLE_PRESENCE_FLAGS,
)
def test_mcp_server_accepts_valid_durable_presence_combinations(
    flags,
    expected_durable,
    expected_presence,
):
    """Only durable sidecars without presence are rejected."""
    runner = CliRunner()
    with patch("swarmbus.mcp_server.run_mcp_server") as mock_run:
        result = runner.invoke(
            main,
            ["mcp-server", "--agent-id", "sb", *flags],
        )

    assert result.exit_code == 0, result.output
    assert mock_run.call_args.kwargs["durable"] is expected_durable
    assert mock_run.call_args.kwargs["presence"] is expected_presence


@pytest.mark.parametrize(
    ("flags", "expected_durable", "expected_presence"),
    _REJECTED_DURABLE_PRESENCE_FLAGS,
)
def test_mcp_server_rejects_durable_without_presence(
    flags,
    expected_durable,
    expected_presence,
):
    """Reject durable sidecars absent from the directory."""
    runner = CliRunner()
    with patch("swarmbus.mcp_server.run_mcp_server") as mock_run:
        result = runner.invoke(
            main,
            ["mcp-server", "--agent-id", "sb", *flags],
        )

    assert result.exit_code == 2, result.output
    assert "--durable" in result.output
    assert "--presence" in result.output
    assert mock_run.call_count == 0


def test_mcp_server_rejects_the_retired_persistent_flag():
    """Reject the removed --persistent option."""
    runner = CliRunner()
    with patch("swarmbus.mcp_server.run_mcp_server") as mock_run:
        result = runner.invoke(
            main,
            ["mcp-server", "--agent-id", "sb", "--persistent", "--presence"],
        )

    assert result.exit_code == 2, result.output
    assert "--persistent" in result.output
    assert mock_run.call_count == 0


def _durable_default(command_name):
    """Read the --durable default off the command that declares it."""
    command = main.commands[command_name]
    option = next(
        param for param in command.params if param.name == "durable"
    )
    return option.default


def test_durable_defaults_differ_between_start_and_mcp_server():
    """Preserve distinct start and mcp-server durability defaults."""
    assert _durable_default("start") is True
    assert _durable_default("mcp-server") is False


@pytest.mark.parametrize(
    ("lifecycle", "presence_flags", "expected_presence"),
    [
        ("transient", ["--presence"], True),
        ("persistent", [], False),
        ("persistent", ["--presence"], True),
    ],
)
def test_mcp_server_accepts_valid_lifecycle_presence_combinations(
    lifecycle,
    presence_flags,
    expected_presence,
):
    """Only transient lifecycle without presence is rejected."""
    runner = CliRunner()
    with patch("swarmbus.mcp_server.run_mcp_server") as mock_run:
        result = runner.invoke(
            main,
            [
                "mcp-server",
                "--agent-id",
                "sb",
                "--lifecycle",
                lifecycle,
                *presence_flags,
            ],
        )

    assert result.exit_code == 0, result.output
    assert mock_run.call_args.kwargs["lifecycle"] == lifecycle
    assert mock_run.call_args.kwargs["presence"] is expected_presence


def test_mcp_server_default_lifecycle_presence_pair_is_accepted():
    """The no-flags invocation must stay valid and unchanged."""
    runner = CliRunner()
    with patch("swarmbus.mcp_server.run_mcp_server") as mock_run:
        result = runner.invoke(main, ["mcp-server", "--agent-id", "sb"])

    assert result.exit_code == 0, result.output
    assert mock_run.call_args.kwargs["lifecycle"] == "persistent"
    assert mock_run.call_args.kwargs["presence"] is False


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
