"""`--topic-root` reaches every command that talks to the broker.

A root that one command misses fails silently: that command publishes or
subscribes in a namespace nobody else is in, and MQTT reports nothing. So
most tests here replace only the broker client, with one that records the
topic strings it is handed, and read what the real bus resolved. Expected
topics are built with ``TopicMap``, so a layout change moves them too;
``tests/test_topics.py`` pins the literals.
"""
from __future__ import annotations

import subprocess
from contextlib import contextmanager
from unittest.mock import AsyncMock, patch

import pytest
from click.testing import CliRunner

from swarmbus.bus import AgentBus
from swarmbus.cli import _topic_root_option, main
from swarmbus.maintenance import (
    RegistryForgetReport,
    RegistryGCReport,
    RegistryListReport,
)
from swarmbus.mcp_server import create_managed_runtime
from swarmbus.topics import TopicMap

ROOT = "fleet"
FLEET = TopicMap(root=ROOT)
PLAIN = TopicMap()
SEND = ["send", "--agent-id", "tx", "--to", "rx", "--subject", "s", "--body", "b"]

# What a developer shell may carry that would change which topics these
# commands use, or make `send` append to a real outbox file.
_AMBIENT = (
    "SWARMBUS_TOPIC_ROOT",
    "SWARMBUS_OUTBOX",
    "SWARMBUS_OUTBOX_TX",
    "SWARMBUS_ID",
    "SWARMBUS_HOPS",
    "SWARMBUS_ORIGIN",
    "SWARMBUS_BROKER_USERNAME",
    "SWARMBUS_BROKER_PASSWORD",
    "SWARMBUS_BROKER_TLS",
    "SWARMBUS_BROKER_CA_CERT",
    "SWARMBUS_BROKER_CLIENT_CERT",
    "SWARMBUS_BROKER_CLIENT_KEY",
)

ROOTED_AND_NOT = pytest.mark.parametrize(
    "root, topics", [(ROOT, FLEET), (None, PLAIN)], ids=["rooted", "unrooted"]
)


@pytest.fixture(autouse=True)
def _no_ambient_environment(monkeypatch):
    for key in _AMBIENT:
        monkeypatch.delenv(key, raising=False)


class _BrokerLog:
    def __init__(self) -> None:
        self.published: list[str] = []
        self.subscribed: list[str] = []
        self.wills: list[str] = []


class _RecordingClient:
    """Stands in for ``aiomqtt.Client`` and records the topics it is given.

    It delivers no messages, so the drain loops in read, watch, list and
    start end at once instead of waiting out a timeout.
    """

    def __init__(self, log: _BrokerLog, *_args, **kwargs) -> None:
        self._log = log
        will = kwargs.get("will")
        if will is not None:
            log.wills.append(will.topic)

    async def __aenter__(self) -> "_RecordingClient":
        return self

    async def __aexit__(self, *_exc) -> bool:
        return False

    async def publish(self, topic, *_args, **_kwargs) -> None:
        self._log.published.append(topic)

    async def subscribe(self, topic, *_args, **_kwargs) -> None:
        self._log.subscribed.append(topic)

    @property
    def messages(self):
        async def _none():
            for message in ():
                yield message

        return _none()


@contextmanager
def _fake_broker():
    # bus.py and cli.py both reach the client through the aiomqtt module
    # object, so one patch also covers doctor's own reachability probe.
    log = _BrokerLog()

    def _client(*args, **kwargs):
        return _RecordingClient(log, *args, **kwargs)

    with patch("swarmbus.bus.aiomqtt.Client", side_effect=_client):
        yield log


def _run(args, env=None):
    with _fake_broker() as log:
        result = CliRunner().invoke(main, args, env=env)
    return result, log


def _root_args(root):
    return [] if root is None else ["--topic-root", root]


def _config(tmp_path, root):
    path = tmp_path / "config.ini"
    path.write_text(f"[broker]\ntopic_root = {root}\n")
    return ["--config", str(path)]


# ---------------------------------------------------------------------------
# where the root comes from
# ---------------------------------------------------------------------------


def test_send_publishes_under_the_root():
    result, log = _run([*SEND, "--topic-root", ROOT])

    assert result.exit_code == 0, result.output
    assert log.published == [FLEET.inbox("rx")]


def test_send_without_a_root_keeps_the_unrooted_topic():
    result, log = _run(SEND)

    assert result.exit_code == 0, result.output
    # The literal: an unrooted command must keep the topic it used before
    # the option existed.
    assert log.published == ["agents/rx/inbox"]


def test_broadcast_stays_unrooted_under_a_root():
    result, log = _run([
        "send", "--agent-id", "tx", "--to", "broadcast",
        "--subject", "s", "--body", "b", "--topic-root", ROOT,
    ])

    assert result.exit_code == 0, result.output
    assert log.published == [PLAIN.broadcast]


def test_the_root_is_normalized_whichever_way_it_arrives():
    result, log = _run([*SEND, "--topic-root", " /fleet/ "])
    assert result.exit_code == 0, result.output
    assert log.published == [FLEET.inbox("rx")]

    result, log = _run(SEND, env={"SWARMBUS_TOPIC_ROOT": " /fleet/ "})
    assert result.exit_code == 0, result.output
    assert log.published == [FLEET.inbox("rx")]


def test_root_precedence_is_flag_then_env_then_config(tmp_path):
    config = _config(tmp_path, "from-config")

    result, log = _run([*config, *SEND])
    assert result.exit_code == 0, result.output
    assert log.published == [TopicMap(root="from-config").inbox("rx")]

    env = {"SWARMBUS_TOPIC_ROOT": "from-env"}
    result, log = _run([*config, *SEND], env=env)
    assert result.exit_code == 0, result.output
    assert log.published == [TopicMap(root="from-env").inbox("rx")]

    result, log = _run([*config, *SEND, "--topic-root", ROOT], env=env)
    assert result.exit_code == 0, result.output
    assert log.published == [FLEET.inbox("rx")]


def test_an_explicit_empty_root_overrides_a_configured_one(tmp_path):
    """Reaching the unrooted tree must stay possible on a rooted host."""
    result, log = _run([*_config(tmp_path, ROOT), *SEND, "--topic-root", ""])

    assert result.exit_code == 0, result.output
    assert log.published == [PLAIN.inbox("rx")]


def test_an_empty_root_env_var_counts_as_unset(tmp_path):
    result, log = _run(
        [*_config(tmp_path, ROOT), *SEND], env={"SWARMBUS_TOPIC_ROOT": ""}
    )

    assert result.exit_code == 0, result.output
    assert log.published == [FLEET.inbox("rx")]


def test_an_unusable_root_is_refused_before_anything_is_sent(tmp_path):
    wildcard = "fleet/+"

    result, log = _run([*SEND, "--topic-root", wildcard])
    assert result.exit_code == 2, result.output
    assert "--topic-root" in result.output
    assert log.published == []

    result, log = _run([*_config(tmp_path, wildcard), *SEND])
    assert result.exit_code == 2, result.output
    assert log.published == []


# ---------------------------------------------------------------------------
# listeners and discovery
# ---------------------------------------------------------------------------


@ROOTED_AND_NOT
def test_start_listens_and_announces_on_its_root(root, topics):
    result, log = _run(
        ["start", "--agent-id", "rx", "--no-persistent", *_root_args(root)]
    )

    assert result.exit_code == 0, result.output
    assert log.subscribed == [topics.inbox("rx"), topics.broadcast]
    assert set(log.published) == {topics.registry("rx"), topics.presence("rx")}
    # A last will on another root would leave a crashed agent showing
    # online to everyone who can see it.
    assert log.wills == [topics.presence("rx")]
    assert f"topic root:  {topics.root or '(none)'}" in result.output


@ROOTED_AND_NOT
def test_read_drains_the_inbox_on_its_root(root, topics):
    result, log = _run(["read", "--agent-id", "rx", *_root_args(root)])

    assert result.exit_code == 0, result.output
    assert log.subscribed == [topics.inbox("rx")]


@ROOTED_AND_NOT
def test_watch_waits_on_the_inbox_on_its_root(root, topics):
    result, log = _run(
        ["watch", "--agent-id", "rx", "--timeout", "0.1", *_root_args(root)]
    )

    # Exit 1 is the timeout path: the recording broker delivers nothing.
    assert result.exit_code == 1, result.output
    assert log.subscribed == [topics.inbox("rx")]


@ROOTED_AND_NOT
def test_list_reads_presence_on_its_root(root, topics):
    result, log = _run(["list", *_root_args(root)])

    assert result.exit_code == 0, result.output
    assert log.subscribed == [topics.any_presence_filter()]


def test_doctor_roots_both_of_its_probes(tmp_path):
    """doctor builds two probes, a long way apart.

    An unrooted peer-discovery probe reports zero peers, which reads as a
    daemon that is not announcing rather than as doctor looking in the
    wrong namespace.
    """
    real_probe = AgentBus.probe
    built: list[AgentBus] = []

    def _record(**kwargs):
        bus = real_probe(**kwargs)
        built.append(bus)
        return bus

    no_unit = subprocess.CompletedProcess(
        args=[], returncode=1, stdout="", stderr=""
    )
    with _fake_broker() as log, \
            patch.object(AgentBus, "probe", side_effect=_record), \
            patch("subprocess.run", return_value=no_unit):
        result = CliRunner().invoke(
            main,
            ["doctor", "--agent-id", "rx", "--topic-root", ROOT],
            env={"CLAUDE_CONFIG_DIR": str(tmp_path / "claude")},
        )

    # 2 would mean doctor gave up before running its checks.
    assert result.exit_code in (0, 1), result.output
    assert [bus.topics for bus in built] == [FLEET, FLEET]
    assert log.subscribed == [FLEET.any_presence_filter()]


# ---------------------------------------------------------------------------
# registry maintenance
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "args, method, report",
    [
        (["registry-list"], "list_registry", RegistryListReport(scanned=0)),
        (
            ["registry-gc"],
            "gc_transient",
            RegistryGCReport(
                dry_run=True,
                scanned=2,
                eligible=1,
                selected=1,
                would_delete=1,
                would_delete_agent_ids=["worker"],
            ),
        ),
        (
            ["registry-forget", "--agent-id", "old", "--yes"],
            "forget",
            RegistryForgetReport(
                agent_id="old",
                deleted=True,
                session_destroyed=True,
                dry_run=False,
            ),
        ),
    ],
    ids=["list", "gc", "forget"],
)
def test_registry_commands_work_on_the_root_they_are_given(
    tmp_path, args, method, report
):
    def topics_for(extra, config=()):
        with patch("swarmbus.cli.RegistryMaintenance") as maintenance:
            setattr(
                maintenance.return_value, method, AsyncMock(return_value=report)
            )
            result = CliRunner().invoke(main, [*config, *args, *extra])
        assert result.exit_code == 0, result.output
        return maintenance.call_args.kwargs["topics"]

    rooted_host = _config(tmp_path, ROOT)

    assert topics_for(["--topic-root", ROOT]) == FLEET
    assert topics_for([]) == PLAIN
    assert topics_for([], config=rooted_host) == FLEET
    # Retiring a record left on the unrooted tree, from a host whose config
    # file sets a root, must not be redirected to the rooted tree.
    assert topics_for(["--topic-root", ""], config=rooted_host) == PLAIN


# ---------------------------------------------------------------------------
# MCP runtimes
# ---------------------------------------------------------------------------


def test_mcp_server_and_daemon_hand_the_root_to_the_runtime(tmp_path):
    with patch("swarmbus.mcp_server.run_mcp_server") as run_server:
        result = CliRunner().invoke(
            main, ["mcp-server", "--agent-id", "rx", "--topic-root", ROOT]
        )
    assert result.exit_code == 0, result.output
    assert run_server.call_args.kwargs["topics"] == FLEET

    with patch("swarmbus.cli.run_mcp_daemon") as run_daemon:
        result = CliRunner().invoke(
            main,
            ["mcp-daemon", "--socket", str(tmp_path / "rx.sock"),
             "--agent-id", "rx"],
            env={"SWARMBUS_TOPIC_ROOT": ROOT},
        )
    assert result.exit_code == 0, result.output
    assert run_daemon.call_args.kwargs["topics"] == FLEET


def test_managed_runtime_announces_on_the_topics_it_is_given(tmp_path):
    runtime = create_managed_runtime(
        agent_id="rx", state_dir=str(tmp_path), presence=True, topics=FLEET
    )

    assert runtime.topics is FLEET
    assert runtime.registry.topics is FLEET
    assert runtime._client_kwargs()["will"].topic == FLEET.presence("rx")


@ROOTED_AND_NOT
def test_mcp_attach_starts_and_fingerprints_the_daemon_on_its_root(
    root, topics
):
    with patch("swarmbus.cli.ensure_daemon") as ensure, \
            patch("swarmbus.cli.run_mcp_proxy"):
        result = CliRunner().invoke(
            main, ["mcp-attach", "--agent-id", "rx", *_root_args(root)]
        )

    assert result.exit_code == 0, result.output
    kwargs = ensure.call_args.kwargs
    assert kwargs["settings"]["topic_root"] == topics.root

    # The argv attach builds is what mcp-daemon parses: run it through the
    # real command and read the root the daemon would start on. A root set
    # in the daemon's own environment must not win over it.
    daemon_args = kwargs["daemon_argv"][3:]
    with patch("swarmbus.cli.run_mcp_daemon") as run_daemon:
        result = CliRunner().invoke(
            main, daemon_args, env={"SWARMBUS_TOPIC_ROOT": "elsewhere"}
        )
    assert result.exit_code == 0, result.output
    assert run_daemon.call_args.kwargs["topics"] == topics


# ---------------------------------------------------------------------------
# the next command
# ---------------------------------------------------------------------------

# `init` takes --broker to set a host up, but opens no broker connection of
# its own; the daemon it installs reads the root from the config file.
_BROKER_OPTION_WITHOUT_A_BUS = {"init"}


def test_every_broker_facing_command_carries_the_shared_root_option():
    shared = _topic_root_option(lambda: None).__click_params__[0]
    facing = {
        name
        for name, command in main.commands.items()
        if any(param.name == "broker" for param in command.params)
    } - _BROKER_OPTION_WITHOUT_A_BUS

    # A floor, so a detector that matched nothing could not pass.
    assert facing >= {
        "send", "start", "read", "watch", "list", "doctor",
        "registry-list", "registry-gc", "registry-forget",
        "mcp-server", "mcp-daemon", "mcp-attach",
    }
    for name in sorted(facing):
        matching = [
            param
            for param in main.commands[name].params
            if param.name == shared.name
        ]
        assert matching, f"{name} talks to the broker but has no --topic-root"
        option = matching[0]
        assert (option.opts, option.envvar, option.default) == (
            shared.opts, shared.envvar, shared.default,
        ), name
