import pytest
from unittest.mock import AsyncMock, patch

from click.testing import CliRunner

from swarmbus.cli import main
from swarmbus.maintenance import RegistryListReport

SEND = ["send", "--to", "wren", "--subject", "x", "--body", "y"]


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for key in (
        "SWARMBUS_BROKER_USERNAME", "SWARMBUS_BROKER_PASSWORD", "SWARMBUS_BROKER_TLS",
        "SWARMBUS_OUTBOX", "SWARMBUS_OUTBOX_SPARROW", "SWARMBUS_ID", "SWARMBUS_HOPS",
        "SWARMBUS_MAX_OUTBOX_BYTES", "SWARMBUS_KEEP_OUTBOX_FILES",
    ):
        monkeypatch.delenv(key, raising=False)


def _write(path, text, mode=0o600):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    path.chmod(mode)
    return path


def _send(args, env=None):
    with patch("swarmbus.cli.AgentBus") as MockBus:
        MockBus.return_value.send = AsyncMock()
        result = CliRunner().invoke(main, args, env=env)
    return result, MockBus


def test_config_file_sets_defaults_below_env_vars_and_flags(tmp_path):
    cfg = _write(tmp_path / "c.ini", """
[broker]
host = cfg.example
port = 1884
username = sparrow
tls = yes

[agent]
id = sparrow
""")
    result, bus = _send(["--config", str(cfg), *SEND])
    assert result.exit_code == 0, result.output
    kw = bus.call_args.kwargs
    assert (kw["agent_id"], kw["broker"], kw["port"], kw["username"], kw["tls"]) == (
        "sparrow", "cfg.example", 1884, "sparrow", True)

    result, bus = _send(
        ["--config", str(cfg), *SEND, "--broker", "flag.example", "--agent-id", "robin"],
        env={"SWARMBUS_BROKER_USERNAME": "env-user", "SWARMBUS_BROKER_TLS": "0"},
    )
    assert result.exit_code == 0, result.output
    kw = bus.call_args.kwargs
    assert (kw["agent_id"], kw["broker"], kw["port"], kw["username"], kw["tls"]) == (
        "robin", "flag.example", 1884, "env-user", False)

    result, bus = _send(SEND, env={"SWARMBUS_CONFIG": str(cfg)})
    assert result.exit_code == 0, result.output
    assert bus.call_args.kwargs["broker"] == "cfg.example"


def test_config_reaches_registry_commands(tmp_path):
    cfg = _write(tmp_path / "registry.ini", """
[broker]
host = maintenance.example
port = 2883
username = maintenance
password = secret
tls = yes
""")
    report = RegistryListReport(scanned=0)
    with patch("swarmbus.cli.RegistryMaintenance") as maintenance:
        maintenance.return_value.list_registry = AsyncMock(return_value=report)
        result = CliRunner().invoke(
            main, ["--config", str(cfg), "registry-list"]
        )

    assert result.exit_code == 0, result.output
    assert maintenance.call_args.kwargs == {
        "broker": "maintenance.example",
        "port": 2883,
        "username": "maintenance",
        "password": "secret",
        "tls": True,
        "ca_cert": None,
        "client_cert": None,
        "client_key": None,
        "snapshot_seconds": 0.5,
    }


def test_config_values_convert_by_type(tmp_path):
    for spelling, expected in (("1", True), ("YES", True), ("on", True),
                               ("0", False), ("No", False), ("off", False)):
        cfg = _write(tmp_path / f"tls-{spelling}.ini", f"[agent]\nid = sparrow\n[broker]\ntls = {spelling}\n")
        result, bus = _send(["--config", str(cfg), *SEND])
        assert result.exit_code == 0, (spelling, result.output)
        assert bus.call_args.kwargs["tls"] is expected, spelling

    cfg = _write(tmp_path / "paths.ini", """
[agent]
id = sparrow
state_dir = ~/sb-state
[broker]
ca_cert = ~/ca.pem
port =
""")
    result, bus = _send(["--config", str(cfg), *SEND])
    assert result.exit_code == 0, result.output
    kw = bus.call_args.kwargs
    assert not kw["ca_cert"].startswith("~") and kw["ca_cert"].endswith("ca.pem")
    assert kw["port"] == 1883          # an empty value counts as not set


def test_config_reaches_the_listeners_and_tail(tmp_path):
    inbox = tmp_path / "sparrow-inbox.md"
    inbox.write_text("\n## [2026-10-17 10:00] From: wren | hi\n| hello\n")
    cfg = _write(tmp_path / "c.ini", f"""
[agent]
id = sparrow
inbox = {inbox}

[limits]
hop_limit = 5
rate_limit = 7
rate_cooldown = 30
""")
    runner = CliRunner()
    with patch("swarmbus.cli.AgentBus") as MockBus, \
         patch("swarmbus.cli.SQLiteMessageStore") as MockStore:
        MockBus.return_value.run = lambda: None
        result = runner.invoke(main, ["--config", str(cfg), "start"])
        assert result.exit_code == 0, result.output
        assert MockBus.call_args.kwargs["agent_id"] == "sparrow"
        kw = MockStore.call_args.kwargs
        assert (kw["hop_limit"], kw["rate_limit"], kw["rate_cooldown"]) == (5, 7, 30)
        assert f"  config:      {cfg}" in result.output
        assert f"  inbox:       {inbox}" in result.output

        result = runner.invoke(main, ["--config", str(cfg), "start"],
                               env={"SWARMBUS_HOP_LIMIT": "6"})
        assert result.exit_code == 0, result.output
        assert MockStore.call_args.kwargs["hop_limit"] == 6

        result = runner.invoke(main, ["start", "--agent-id", "sparrow"])
        assert "  config:      (none)" in result.output

    with patch("swarmbus.mcp_server.run_mcp_server") as run_server:
        result = runner.invoke(main, ["--config", str(cfg), "mcp-server"])
    assert result.exit_code == 0, result.output
    kw = run_server.call_args.kwargs
    assert (kw["agent_id"], kw["hop_limit"], kw["rate_limit"]) == ("sparrow", 5, 7)

    result = runner.invoke(main, ["--config", str(cfg), "tail",
                                  "--cursor-dir", str(tmp_path / "cursors")])
    assert result.exit_code == 0, result.output
    assert "From: wren | hi" in result.output


def test_config_outbox_ranks_below_both_outbox_env_vars(tmp_path):
    cfg = _write(tmp_path / "c.ini", """
[agent]
id = sparrow

[outbox]
path = /tmp/{agent_id}-cfg.md
max_bytes = 500
keep = 3
""")
    base = ["--config", str(cfg), *SEND]

    result, bus = _send(base)
    assert result.exit_code == 0, result.output
    kw = bus.return_value.send.call_args.kwargs
    assert (kw["outbox_path"], kw["outbox_max_bytes"], kw["outbox_keep"]) == (
        "/tmp/{agent_id}-cfg.md", 500, 3)

    _, bus = _send(base, env={"SWARMBUS_OUTBOX": "/tmp/env.md"})
    assert bus.return_value.send.call_args.kwargs["outbox_path"] == "/tmp/env.md"
    _, bus = _send(base, env={"SWARMBUS_OUTBOX_SPARROW": "/tmp/scoped.md"})
    assert bus.return_value.send.call_args.kwargs["outbox_path"] == "/tmp/scoped.md"
    _, bus = _send([*base, "--outbox", "/tmp/flag.md"], env={"SWARMBUS_OUTBOX": "/tmp/env.md"})
    assert bus.return_value.send.call_args.kwargs["outbox_path"] == "/tmp/flag.md"
    _, bus = _send(base, env={"SWARMBUS_KEEP_OUTBOX_FILES": "4"})
    assert bus.return_value.send.call_args.kwargs["outbox_keep"] == 4


def test_config_errors_fail_loud_with_the_path_and_never_the_value(tmp_path):
    cases = {
        "no-header.ini": ("host = SECRET1\n", "line 1"),
        "no-equals.ini": ("[broker]\npassword SECRET1\n", "line 2"),
        "dup-key.ini": ("[broker]\nport = 1\nport = 2\n", "line 3"),
        "dup-section.ini": ("[broker]\n[broker]\n", "line 2"),
        "word-port.ini": ("[broker]\nport = SECRET1\n", "[broker] port"),
        "range-port.ini": ("[broker]\nport = 70000\n", "[broker] port"),
        "float-hops.ini": ("[limits]\nhop_limit = 8.5\n", "[limits] hop_limit"),
        "negative.ini": ("[limits]\nhop_limit = -1\n", "[limits] hop_limit"),
        "inf-cooldown.ini": ("[limits]\nrate_cooldown = inf\n", "[limits] rate_cooldown"),
        "bad-bool.ini": ("[broker]\ntls = SECRET1\n", "[broker] tls"),
    }
    for name, (text, needle) in cases.items():
        cfg = _write(tmp_path / name, text)
        result, bus = _send(["--config", str(cfg), *SEND, "--agent-id", "sparrow"])
        assert result.exit_code == 2, (name, result.output)
        assert str(cfg) in result.stderr and needle in result.stderr, (name, result.stderr)
        assert "SECRET1" not in result.output, name
        bus.assert_not_called()

    missing = tmp_path / "missing.ini"
    result, bus = _send(["--config", str(missing), *SEND, "--agent-id", "sparrow"])
    assert result.exit_code == 2 and str(missing) in result.stderr
    bus.assert_not_called()
    result, bus = _send([*SEND, "--agent-id", "sparrow"], env={"SWARMBUS_CONFIG": str(missing)})
    assert result.exit_code == 2
    bus.assert_not_called()


def test_unknown_config_sections_and_keys_warn_on_stderr_and_do_not_fail(tmp_path):
    cfg = _write(tmp_path / "c.ini", """
[DEFAULT]
port = 1999

[agent]
id = sparrow
colour = blue

[future]
x = 1
""")
    result, bus = _send(["--config", str(cfg), *SEND])
    assert result.exit_code == 0, result.output
    assert "[agent] colour" in result.stderr
    assert "[future]" in result.stderr and "[DEFAULT]" in result.stderr
    assert "colour" not in result.stdout
    kw = bus.call_args.kwargs
    assert (kw["agent_id"], kw["port"]) == ("sparrow", 1883)   # [DEFAULT] is not special


def test_default_config_path_is_read_when_present_and_ignored_when_missing(tmp_path, monkeypatch):
    from swarmbus import config as sb_config

    home_cfg = tmp_path / "home" / ".swarmbus" / "config.ini"
    monkeypatch.setattr(sb_config, "default_config_path", lambda: home_cfg)

    result, bus = _send([*SEND, "--agent-id", "sparrow"])
    assert result.exit_code == 0, result.output
    assert result.stderr == ""
    assert bus.call_args.kwargs["broker"] == "localhost"

    _write(home_cfg, "[broker]\nhost = home.example\n", mode=0o644)
    result, bus = _send([*SEND, "--agent-id", "sparrow"])
    assert result.exit_code == 0, result.output
    assert bus.call_args.kwargs["broker"] == "home.example"

    _write(home_cfg, "host = home.example\n")
    result, bus = _send([*SEND, "--agent-id", "sparrow"])
    assert result.exit_code == 2 and str(home_cfg) in result.stderr
    bus.assert_not_called()


def test_broker_password_needs_a_private_file_and_is_read_verbatim(tmp_path):
    text = "[agent]\nid = sparrow\n\n[broker]\nusername = sparrow\npassword = pa%ss#1;x\n"
    loose = _write(tmp_path / "loose.ini", text, mode=0o644)
    result, bus = _send(["--config", str(loose), *SEND])
    assert result.exit_code == 2
    assert str(loose) in result.stderr and "chmod 600" in result.stderr
    assert "pa%ss" not in result.output
    bus.assert_not_called()

    result, bus = _send(["--config", str(loose), *SEND],
                        env={"SWARMBUS_BROKER_PASSWORD": "from-env"})
    assert result.exit_code == 2

    tight = _write(tmp_path / "tight.ini", text, mode=0o600)
    result, bus = _send(["--config", str(tight), *SEND])
    assert result.exit_code == 0, result.output
    assert bus.call_args.kwargs["password"] == "pa%ss#1;x"   # no interpolation, no inline comment
    result, bus = _send(["--config", str(tight), *SEND],
                        env={"SWARMBUS_BROKER_PASSWORD": "from-env"})
    assert bus.call_args.kwargs["password"] == "from-env"

    no_pw = _write(tmp_path / "no-pw.ini", "[agent]\nid = sparrow\n", mode=0o644)
    result, _ = _send(["--config", str(no_pw), *SEND])
    assert result.exit_code == 0, result.output
