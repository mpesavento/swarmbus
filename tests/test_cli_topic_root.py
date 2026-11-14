"""`--topic-root` reaches every command that talks to the broker.

A root that one command misses fails silently: that command publishes or
subscribes in a namespace nobody else is in, and MQTT reports nothing. So
most tests here replace only the broker client, with one that records the
topic strings it is handed, and read what the real bus resolved. Expected
topics are built with ``TopicMap``, so a layout change moves them too;
``tests/test_topics.py`` pins the literals.
"""
from __future__ import annotations

import asyncio
import json
import subprocess
from contextlib import contextmanager
from unittest.mock import AsyncMock, patch

import pytest
from click.testing import CliRunner

from swarmbus import __version__
from swarmbus.archive import SQLiteMessageStore, state_db_path
from swarmbus.bus import AgentBus
from swarmbus.cli import _doctor_local_daemons, _topic_root_option, main
from swarmbus.local_rpc import daemon_settings
from swarmbus.maintenance import (
    RegistryForgetReport,
    RegistryGCReport,
    RegistryListReport,
)
from swarmbus.mcp_server import create_managed_runtime
from swarmbus.message import AgentMessage
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


def test_an_empty_root_env_var_selects_the_unrooted_layout_over_config(tmp_path):
    """Exporting an empty root is how a wrapper asks for the unrooted tree.

    On a host whose config file sets a root, that is the only way to reach
    the unrooted tree without editing every command line.
    """
    config = _config(tmp_path, ROOT)

    result, log = _run([*config, *SEND], env={"SWARMBUS_TOPIC_ROOT": ""})
    assert result.exit_code == 0, result.output
    assert log.published == [PLAIN.inbox("rx")]

    result, log = _run(
        [*config, *SEND, "--topic-root", ROOT], env={"SWARMBUS_TOPIC_ROOT": ""}
    )
    assert result.exit_code == 0, result.output
    assert log.published == [FLEET.inbox("rx")]


def test_an_unusable_root_is_refused_before_anything_is_sent(tmp_path):
    wildcard = "fleet/+"

    result, log = _run([*SEND, "--topic-root", wildcard])
    assert result.exit_code == 2, result.output
    assert "--topic-root" in result.output
    assert log.published == []

    config = _config(tmp_path, wildcard)
    result, log = _run([*config, *SEND])
    assert result.exit_code == 2, result.output
    assert log.published == []
    # A root from the file is a config error: naming a flag nobody passed
    # sends the reader looking in the wrong place, and a config error never
    # echoes a value from the file.
    assert config[1] in result.output
    assert "[broker] topic_root" in result.output
    assert "--topic-root" not in result.output
    assert wildcard not in result.output

    result, log = _run(SEND, env={"SWARMBUS_TOPIC_ROOT": wildcard})
    assert result.exit_code == 2, result.output
    assert log.published == []
    assert "SWARMBUS_TOPIC_ROOT" in result.output
    assert "--topic-root" not in result.output


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
    assert log.subscribed == list(topics.presence_filters())


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
    assert log.subscribed == list(FLEET.presence_filters())


def _doctor_with_systemctl(tmp_path, extra, systemctl):
    """doctor with `systemctl` answered by `systemctl(cmd)`, broker faked."""
    asked: list[str] = []

    def fake_run(cmd, **_kwargs):
        asked.extend(arg for arg in cmd if arg.endswith(".service"))
        return systemctl(cmd)

    with _fake_broker(), patch("subprocess.run", side_effect=fake_run):
        result = CliRunner().invoke(
            main,
            ["doctor", "--agent-id", "rx", "--state-dir", str(tmp_path / "state"),
             *extra],
            env={"CLAUDE_CONFIG_DIR": str(tmp_path / "claude")},
        )
    assert result.exit_code in (0, 1), result.output
    return result, asked


def _no_unit(cmd):
    return subprocess.CompletedProcess(args=cmd, returncode=1, stdout="", stderr="")


def test_doctor_probes_the_unit_name_of_its_root(tmp_path):
    """The unit a rooted install writes is the unit doctor must ask about.

    Probing the unrooted name on a rooted host reports a running daemon
    as missing and sends the operator to install one that is already
    there.
    """
    result, asked = _doctor_with_systemctl(
        tmp_path, ["--topic-root", ROOT], _no_unit
    )

    unit = FLEET.systemd_unit("rx")
    assert unit == "swarmbus-rx:fleet.service"
    assert unit in asked
    assert f"{unit} NOT FOUND" in result.output
    assert f"install-systemd.sh rx --topic-root {ROOT}" in result.output


def test_doctor_keeps_probing_the_unrooted_unit_name(tmp_path):
    """The literal: an unrooted host keeps the unit name it has."""
    result, asked = _doctor_with_systemctl(tmp_path, [], _no_unit)

    assert "swarmbus-rx.service" in asked
    assert "swarmbus-rx.service NOT FOUND" in result.output
    assert "swarmbus-rx:" not in result.output


def test_doctor_finds_a_rooted_daemon_under_the_unit_name_without_a_root(tmp_path):
    """An install from before unit names carried the root still runs.

    Calling it NOT FOUND sends the operator to install a second unit for
    the same agent id and root, which is two daemons on one persistent
    session, each disconnecting the other.
    """
    legacy = PLAIN.systemd_unit("rx")
    props = (
        "ActiveState=active\nSubState=running\nMainPID=0\n"
        "ExecMainStartTimestamp=Mon 2026-11-16 09:00:00 PST\n"
        "ExecStart=/usr/bin/swarmbus start --agent-id rx --topic-root=fleet\n"
    )

    def systemctl(cmd):
        if legacy in cmd:
            return subprocess.CompletedProcess(args=cmd, returncode=0, stdout=props)
        return _no_unit(cmd)

    result, _ = _doctor_with_systemctl(tmp_path, ["--topic-root", ROOT], systemctl)

    assert "systemd user unit....... active" in result.output
    assert legacy in result.output
    assert "before unit names carried the root" in result.output
    assert f"install-systemd.sh rx --topic-root {ROOT}" in result.output


def test_doctor_does_not_claim_a_unit_pinned_to_another_root(tmp_path):
    """Without a root in its name, only the pin says which swarm it serves."""
    legacy = PLAIN.systemd_unit("rx")
    props = (
        "ActiveState=active\nSubState=running\nMainPID=0\n"
        "ExecStart=/usr/bin/swarmbus start --agent-id rx --topic-root=lab\n"
    )

    def systemctl(cmd):
        if legacy in cmd:
            return subprocess.CompletedProcess(args=cmd, returncode=0, stdout=props)
        return _no_unit(cmd)

    result, _ = _doctor_with_systemctl(tmp_path, ["--topic-root", ROOT], systemctl)

    assert f"{FLEET.systemd_unit('rx')} NOT FOUND" in result.output
    assert "systemd user unit....... active" not in result.output


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
    tmp_path, root, topics
):
    state = tmp_path / "state"
    with patch("swarmbus.cli.ensure_daemon") as ensure, \
            patch("swarmbus.cli.run_mcp_proxy") as proxy:
        result = CliRunner().invoke(
            main,
            ["mcp-attach", "--agent-id", "rx", "--state-dir", str(state),
             *_root_args(root)],
        )

    assert result.exit_code == 0, result.output
    kwargs = ensure.call_args.kwargs
    assert kwargs["settings"]["topic_root"] == topics.root

    # Per root, or a second attach finds the first root's daemon on the one
    # socket and is refused instead of starting its own.
    socket = ensure.call_args.args[0]
    assert socket.parent == state and kwargs["log_path"].parent == state
    # The literals: an unrooted deployment keeps the names it has.
    assert (socket.name, kwargs["log_path"].name) == {
        "": ("rx.sock", "rx.daemon.log"),
        ROOT: ("rx@fleet.sock", "rx@fleet.daemon.log"),
    }[topics.root]
    proxy.assert_called_once_with(str(socket))

    daemon_argv = kwargs["daemon_argv"]
    # One token: a separate empty argument is the kind of thing a process
    # supervisor or shell wrapper drops on the way to execve.
    assert f"--topic-root={topics.root}" in daemon_argv
    assert "--topic-root" not in daemon_argv

    # The argv attach builds is what mcp-daemon parses: run it through the
    # real command and read the root the daemon would start on. A root set
    # in the daemon's own environment must not win over it.
    daemon_args = daemon_argv[3:]
    with patch("swarmbus.cli.run_mcp_daemon") as run_daemon:
        result = CliRunner().invoke(
            main, daemon_args, env={"SWARMBUS_TOPIC_ROOT": "elsewhere"}
        )
    assert result.exit_code == 0, result.output
    assert run_daemon.call_args.kwargs["topics"] == topics


# ---------------------------------------------------------------------------
# per-host files
#
# Every file keyed per agent is named for the agent scope, so one agent id
# under two roots on one host does not share an inbox, holds, rate-limit
# state or a daemon. Unrooted names are pinned as literals: a deployment
# without a root must keep exactly the files it has.
# ---------------------------------------------------------------------------

INBOX = "agents/rx/inbox"


def _other_root(root):
    """The root args of the run that must NOT see what `root` recorded."""
    return [] if root is not None else ["--topic-root", ROOT]


def _trip(db, sender="wren"):
    """Trip `sender` in `db` the way a receiver past its hop limit does."""
    store = SQLiteMessageStore(db, hop_limit=4)
    message = AgentMessage.create(
        from_=sender, to="rx", subject="deep", body="b", hops=5
    )
    assert asyncio.run(store.admit(message, source_topic=INBOX)) is False
    return message


def _seed_inbox(db, count, *, acknowledge=False):
    """Put `count` delivered messages in `db`'s inbox, optionally acked."""
    store = SQLiteMessageStore(db)

    async def fill():
        delivered = []
        for index in range(count):
            message = AgentMessage.create(
                from_="tx", to="rx", subject=f"s{index}", body="b"
            )
            assert await store.store(message, source_topic=INBOX) is True
            delivered.append(message.id)
        if acknowledge:
            assert await store.ack(delivered) == count

    asyncio.run(fill())


def _doctor(state, extra, claude_dir):
    no_unit = subprocess.CompletedProcess(
        args=[], returncode=1, stdout="", stderr=""
    )
    with _fake_broker(), patch("subprocess.run", return_value=no_unit):
        return CliRunner().invoke(
            main,
            ["doctor", "--agent-id", "rx", "--state-dir", str(state), *extra],
            env={"CLAUDE_CONFIG_DIR": str(claude_dir)},
        )


@ROOTED_AND_NOT
@pytest.mark.parametrize(
    "args, exit_code",
    [
        (["read", "--agent-id", "rx"], 0),
        (["watch", "--agent-id", "rx", "--timeout", "0.1"], 1),
        (["start", "--agent-id", "rx", "--no-persistent"], 0),
    ],
    ids=["read", "watch", "start"],
)
def test_commands_open_the_state_db_of_their_root(
    tmp_path, root, topics, args, exit_code
):
    state = tmp_path / "state"

    result, _ = _run([*args, "--state-dir", str(state), *_root_args(root)])

    assert result.exit_code == exit_code, result.output
    assert (state / f"{topics.agent_scope('rx')}.sqlite3").exists()
    unused = {"rx.sqlite3", "rx@fleet.sqlite3"} - {
        f"{topics.agent_scope('rx')}.sqlite3"
    }
    for name in unused:
        assert not (state / name).exists()


@ROOTED_AND_NOT
def test_held_reads_the_state_db_of_its_root(tmp_path, root, topics):
    state = tmp_path / "state"
    base = ["held", "--agent-id", "rx", "--state-dir", str(state), "--json"]
    tripped = _trip(state_db_path(state, "rx", topics=topics))

    on_root = CliRunner().invoke(main, [*base, *_root_args(root)])
    assert on_root.exit_code == 0, on_root.output
    listed = json.loads(on_root.stdout)
    assert [t["sender"] for t in listed["tripped"]] == ["wren"]
    assert [h["id"] for h in listed["held"]] == [tripped.id]

    elsewhere = CliRunner().invoke(main, [*base, *_other_root(root)])
    assert elsewhere.exit_code == 0, elsewhere.output
    assert json.loads(elsewhere.stdout)["tripped"] == []


@ROOTED_AND_NOT
def test_send_counts_hops_from_the_parent_recorded_on_its_root(
    tmp_path, root, topics
):
    state = tmp_path / "state"
    store = SQLiteMessageStore(state_db_path(state, "rx", topics=topics))
    parent = AgentMessage.create(
        from_="tx", to="rx", subject="p", body="b", hops=3, origin="planner"
    )
    assert asyncio.run(store.admit(parent, source_topic=INBOX)) is True

    def reply(extra):
        with patch("swarmbus.cli.AgentBus") as bus:
            bus.return_value.send = AsyncMock()
            result = CliRunner().invoke(main, [
                "send", "--agent-id", "rx", "--to", "tx", "--subject", "s",
                "--body", "b", "--state-dir", str(state),
                "--in-reply-to", parent.id, *extra,
            ])
            return result, bus.return_value.send.call_args.kwargs

    result, chain = reply(_root_args(root))
    assert result.exit_code == 0, result.output
    assert (chain["hops"], chain["origin"]) == (4, "planner")

    result, chain = reply(_other_root(root))
    assert result.exit_code == 0, result.output
    # No parent record on the other root: one hop, and no inherited origin.
    assert (chain["hops"], chain["origin"]) == (1, "rx")


def test_mcp_attach_uses_an_explicit_socket_as_given(tmp_path):
    state = tmp_path / "state"
    chosen = tmp_path / "chosen.sock"

    with patch("swarmbus.cli.ensure_daemon") as ensure, \
            patch("swarmbus.cli.run_mcp_proxy") as proxy:
        result = CliRunner().invoke(main, [
            "mcp-attach", "--agent-id", "rx", "--state-dir", str(state),
            "--socket", str(chosen), "--topic-root", ROOT,
        ])

    assert result.exit_code == 0, result.output
    assert ensure.call_args.args == (chosen,)
    assert ensure.call_args.kwargs["log_path"] == state / "rx@fleet.daemon.log"
    proxy.assert_called_once_with(str(chosen))


@ROOTED_AND_NOT
def test_tail_reads_the_default_inbox_of_its_root(tmp_path, monkeypatch, root, topics):
    """`tail` reads the file the daemon writes, so their defaults must agree.

    A rooted tail must not read the unrooted deployment's inbox, and an
    unrooted one must keep reading exactly the file it always has.
    """
    monkeypatch.setenv("HOME", str(tmp_path))
    sync = tmp_path / "sync"
    sync.mkdir()
    (sync / PLAIN.default_inbox_name("rx")).write_text("unrooted entry\n")
    (sync / FLEET.default_inbox_name("rx")).write_text("fleet entry\n")

    result = CliRunner().invoke(main, [
        "tail", "--agent-id", "rx",
        "--cursor-dir", str(tmp_path / "cursors"),
        *_root_args(root),
    ])

    assert result.exit_code == 0, result.output
    mine, theirs = ("fleet entry", "unrooted entry") if root else (
        "unrooted entry", "fleet entry"
    )
    assert mine in result.output, result.output
    assert theirs not in result.output, result.output
    assert (sync / topics.default_inbox_name("rx")).read_text() == f"{mine}\n"


@ROOTED_AND_NOT
def test_tail_keeps_a_cursor_per_root(tmp_path, monkeypatch, root, topics):
    """Two roots are two inbox files. One cursor over both re-emits one of
    them on every switch, because the stored inode never matches."""
    monkeypatch.setenv("HOME", str(tmp_path))
    sync = tmp_path / "sync"
    sync.mkdir()
    (sync / PLAIN.default_inbox_name("rx")).write_text("unrooted entry\n")
    (sync / FLEET.default_inbox_name("rx")).write_text("fleet entry\n")
    cursors = tmp_path / "cursors"

    result = CliRunner().invoke(main, [
        "tail", "--agent-id", "rx", "--cursor-dir", str(cursors),
        *_root_args(root),
    ])

    assert result.exit_code == 0, result.output
    assert (cursors / f"{topics.agent_scope('rx')}--default.cursor").exists(), (
        sorted(p.name for p in cursors.iterdir())
    )
    unused = {"rx--default.cursor", "rx@fleet--default.cursor"} - {
        f"{topics.agent_scope('rx')}--default.cursor"
    }
    for name in unused:
        assert not (cursors / name).exists()


def test_managed_runtime_opens_the_state_db_of_its_root(tmp_path):
    rooted = create_managed_runtime(
        agent_id="rx", state_dir=str(tmp_path), topics=FLEET
    )
    plain = create_managed_runtime(agent_id="rx", state_dir=str(tmp_path))

    assert rooted.store.db_path == tmp_path / "rx@fleet.sqlite3"
    assert plain.store.db_path == tmp_path / "rx.sqlite3"


def test_doctor_reads_the_state_db_of_its_root(tmp_path):
    state = tmp_path / "state"
    _trip(state_db_path(state, "rx", topics=FLEET))

    rooted = _doctor(state, ["--topic-root", ROOT], tmp_path / "claude")
    assert "1 tripped: wren (hop-limit)" in rooted.output
    assert "; 1 message(s) held" in rooted.output
    assert "held: 1 hop-limit" in rooted.output

    plain = _doctor(state, [], tmp_path / "claude")
    assert "8. tripped senders......... (no state db at" in plain.output
    assert "9. channel traffic......... (no state db at" in plain.output
    assert not (state / "rx.sqlite3").exists()


def _daemon_reply(agent_id, topic_root, *, pid=4242, version=__version__):
    return {
        "ready": True,
        "daemon": {
            "pid": pid,
            "version": version,
            "settings": daemon_settings(
                agent_id=agent_id,
                broker="localhost",
                port=1883,
                username=None,
                password=None,
                tls=False,
                ca_cert=None,
                client_cert=None,
                client_key=None,
                persistent=True,
                presence=True,
                lifecycle="named",
                topic_root=topic_root,
            ),
        },
    }


def _daemon_rows(state, daemons, topics):
    """`_doctor_local_daemons` over a state dir with one socket per daemon."""
    state.mkdir(mode=0o700, parents=True)
    replies = {}
    for name, reply in daemons.items():
        (state / name).touch()
        replies[state / name] = reply

    with patch("swarmbus.cli.probe_daemon", side_effect=replies.__getitem__):
        return _doctor_local_daemons(str(state), "rx", topics)


def test_doctor_warns_about_a_daemon_for_this_agent_on_another_root(tmp_path):
    rows = _daemon_rows(
        tmp_path / "state", {"rx.sock": _daemon_reply("rx", "")}, FLEET
    )

    (label, status, hint), = rows
    assert status == "warn"
    assert "agent rx, root (none)" in label
    assert f"root {ROOT}" in label
    assert "--topic-root ''" in hint
    assert "kill 4242" in hint


def test_doctor_shows_the_root_of_a_daemon_it_does_not_warn_about(tmp_path):
    same_root = _daemon_rows(
        tmp_path / "same", {"rx@fleet.sock": _daemon_reply("rx", ROOT)}, FLEET
    )
    assert [status for _, status, _ in same_root] == ["ok"]
    assert f"agent rx, root {ROOT}" in same_root[0][0]

    # Another agent id is another agent: its root is reported, not judged.
    other_agent = _daemon_rows(
        tmp_path / "other",
        {"tx@elsewhere.sock": _daemon_reply("tx", "elsewhere")},
        FLEET,
    )
    assert [status for _, status, _ in other_agent] == ["ok"]
    assert "agent tx, root elsewhere" in other_agent[0][0]


def test_doctor_reports_a_version_and_a_root_mismatch_together(tmp_path):
    rows = _daemon_rows(
        tmp_path / "state",
        {"rx.sock": _daemon_reply("rx", "", version="0.0.1")},
        FLEET,
    )

    (label, status, hint), = rows
    assert status == "warn"
    assert f"this CLI is {__version__}" in label
    assert "agent rx, root (none)" in label
    assert "kill 4242" in hint
    assert "--topic-root ''" in hint


def test_doctor_warns_about_messages_left_in_the_unrooted_state_db(tmp_path):
    state = tmp_path / "state"
    _seed_inbox(state_db_path(state, "rx"), 2)

    result = _doctor(state, ["--topic-root", ROOT], tmp_path / "claude")

    # The numbered probes keep 1-10 and the daemon rows stay above this one.
    assert "11. local daemons" in result.output
    assert "12. unrooted state db" in result.output
    assert "2 unacknowledged message(s)" in result.output
    assert str(state / "rx.sqlite3") in result.output


def test_doctor_says_nothing_about_an_unrooted_state_db_with_nothing_in_it(
    tmp_path,
):
    unrooted_run = tmp_path / "unrooted-run"
    _seed_inbox(state_db_path(unrooted_run, "rx"), 1)
    result = _doctor(unrooted_run, [], tmp_path / "claude")
    assert "unrooted state db" not in result.output

    acknowledged = tmp_path / "acknowledged"
    _seed_inbox(state_db_path(acknowledged, "rx"), 1, acknowledge=True)
    result = _doctor(acknowledged, ["--topic-root", ROOT], tmp_path / "claude")
    assert "unrooted state db" not in result.output


def test_a_rooted_doctor_never_creates_the_unrooted_state_db(tmp_path):
    state = tmp_path / "state"

    result = _doctor(state, ["--topic-root", ROOT], tmp_path / "claude")

    assert "unrooted state db" not in result.output
    assert not state.exists()


# ---------------------------------------------------------------------------
# the next command
# ---------------------------------------------------------------------------

def test_every_broker_facing_command_carries_the_shared_root_option():
    shared = _topic_root_option(lambda: None).__click_params__[0]
    facing = {
        name
        for name, command in main.commands.items()
        if any(param.name == "broker" for param in command.params)
    }

    # A floor, so a detector that matched nothing could not pass.
    assert facing >= {
        "send", "start", "read", "watch", "list", "doctor", "init",
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


def test_the_persistent_session_help_scopes_the_session_to_the_root():
    for name in ("watch", "mcp-server", "mcp-daemon", "mcp-attach"):
        [option] = [
            param for param in main.commands[name].params
            if param.name == "persistent"
        ]
        assert "per agent id on a topic root" in option.help, name
        assert "per agent-id" not in option.help, name
