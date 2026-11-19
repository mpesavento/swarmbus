"""Tests for swarmbus init command and helpers."""
from __future__ import annotations

import os
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from swarmbus.cli import _derive_invoke, _run_step, main
from swarmbus.topics import TopicMap


# ---------------------------------------------------------------------------
# _run_step unit tests
# ---------------------------------------------------------------------------

class TestRunStep:
    def test_dry_run_no_subprocess(self):
        with patch("subprocess.run") as mock_run:
            result = _run_step("Test step", ["echo", "hello"], dry_run=True)
        assert result is True
        mock_run.assert_not_called()

    def test_dry_run_prints_command(self, capsys):
        _run_step("Test step", ["echo", "hello"], dry_run=True)
        captured = capsys.readouterr()
        assert "would run: echo hello" in captured.out

    def test_real_success(self):
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = ""
        mock_result.stderr = ""
        with patch("subprocess.run", return_value=mock_result):
            result = _run_step("Test step", ["true"], dry_run=False)
        assert result is True

    def test_real_failure_returns_false(self):
        mock_result = MagicMock()
        mock_result.returncode = 1
        mock_result.stdout = "some stdout output"
        mock_result.stderr = "some stderr output"
        with patch("subprocess.run", return_value=mock_result):
            result = _run_step("Test step", ["false"], dry_run=False)
        assert result is False

    def test_real_failure_prints_stdout_and_stderr(self, capsys):
        mock_result = MagicMock()
        mock_result.returncode = 1
        mock_result.stdout = "script printed this"
        mock_result.stderr = "error detail"
        with patch("subprocess.run", return_value=mock_result):
            _run_step("Test step", ["false"], dry_run=False)
        captured = capsys.readouterr()
        assert "script printed this" in captured.out
        assert "error detail" in captured.out


# ---------------------------------------------------------------------------
# _derive_invoke
# ---------------------------------------------------------------------------

class TestDeriveInvoke:
    def test_cc_with_repo_root(self):
        result = _derive_invoke("cc", "sparrow", "/home/user/swarmbus")
        assert result == "/home/user/swarmbus/examples/claude-code-wake.sh sparrow"

    def test_openclaw_with_repo_root(self):
        result = _derive_invoke("openclaw", "wren", "/home/user/swarmbus")
        assert result == "/home/user/swarmbus/examples/openclaw-wake.sh wren"

    def test_none_host_type_returns_none(self):
        result = _derive_invoke("none", "test", "/home/user/swarmbus")
        assert result is None

    def test_no_repo_root_returns_none(self):
        result = _derive_invoke("cc", "sparrow", None)
        assert result is None

    def test_no_repo_root_openclaw_returns_none(self):
        result = _derive_invoke("openclaw", "wren", None)
        assert result is None


# ---------------------------------------------------------------------------
# CLI flag validation
# ---------------------------------------------------------------------------

class TestAgentIdValidation:
    def _invoke(self, agent_id: str) -> int:
        runner = CliRunner()
        result = runner.invoke(main, [
            "init", "--agent-id", agent_id, "--dry-run", "--skip-broker",
        ])
        return result.exit_code

    def test_valid_lowercase(self):
        with patch("swarmbus.cli.resolve_broker_addr", return_value="localhost"), \
             patch("swarmbus.cli.detect_platform", return_value="debian"), \
             patch("swarmbus.cli.find_repo_root", return_value=None):
            assert self._invoke("sparrow") == 0

    def test_valid_with_hyphen(self):
        with patch("swarmbus.cli.resolve_broker_addr", return_value="localhost"), \
             patch("swarmbus.cli.detect_platform", return_value="debian"), \
             patch("swarmbus.cli.find_repo_root", return_value=None):
            assert self._invoke("wren-beta") == 0

    def test_valid_with_underscore(self):
        with patch("swarmbus.cli.resolve_broker_addr", return_value="localhost"), \
             patch("swarmbus.cli.detect_platform", return_value="debian"), \
             patch("swarmbus.cli.find_repo_root", return_value=None):
            assert self._invoke("agent_01") == 0

    def test_valid_at_the_wire_format_limit(self):
        with patch("swarmbus.cli.resolve_broker_addr", return_value="localhost"), \
             patch("swarmbus.cli.detect_platform", return_value="debian"), \
             patch("swarmbus.cli.find_repo_root", return_value=None):
            assert self._invoke("a" * 64) == 0

    def test_valid_with_leading_separator(self):
        with patch("swarmbus.cli.resolve_broker_addr", return_value="localhost"), \
             patch("swarmbus.cli.detect_platform", return_value="debian"), \
             patch("swarmbus.cli.find_repo_root", return_value=None):
            assert self._invoke("_agent") == 0

    def test_invalid_uppercase(self):
        assert self._invoke("Sparrow") == 2

    def test_invalid_special_chars(self):
        assert self._invoke("sparrow!") == 2

    def test_invalid_with_space(self):
        assert self._invoke("my agent") == 2

    def test_invalid_with_slash(self):
        assert self._invoke("foo/bar") == 2

    def test_reserved_broadcast(self):
        assert self._invoke("broadcast") == 2

    def test_reserved_system(self):
        assert self._invoke("system") == 2


class TestHostTypeChoices:
    def test_cc_accepted(self):
        runner = CliRunner()
        with patch("swarmbus.cli.resolve_broker_addr", return_value="localhost"), \
             patch("swarmbus.cli.detect_platform", return_value="debian"), \
             patch("swarmbus.cli.find_repo_root", return_value=None):
            result = runner.invoke(main, ["init", "--agent-id", "test", "--host-type", "cc", "--dry-run", "--skip-broker"])
        assert result.exit_code == 0

    def test_openclaw_accepted(self):
        runner = CliRunner()
        with patch("swarmbus.cli.resolve_broker_addr", return_value="localhost"), \
             patch("swarmbus.cli.detect_platform", return_value="debian"), \
             patch("swarmbus.cli.find_repo_root", return_value=None):
            result = runner.invoke(main, ["init", "--agent-id", "test", "--host-type", "openclaw", "--dry-run", "--skip-broker"])
        assert result.exit_code == 0

    def test_none_accepted(self):
        runner = CliRunner()
        with patch("swarmbus.cli.resolve_broker_addr", return_value="localhost"), \
             patch("swarmbus.cli.detect_platform", return_value="debian"), \
             patch("swarmbus.cli.find_repo_root", return_value=None):
            result = runner.invoke(main, ["init", "--agent-id", "test", "--host-type", "none", "--dry-run", "--skip-broker"])
        assert result.exit_code == 0

    def test_invalid_choice_rejected(self):
        runner = CliRunner()
        result = runner.invoke(main, ["init", "--agent-id", "test", "--host-type", "docker", "--dry-run"])
        assert result.exit_code != 0
        assert "Invalid value" in result.output


class TestHelpShowsAllFlags:
    def test_help_exit_0(self):
        runner = CliRunner()
        result = runner.invoke(main, ["init", "--help"])
        assert result.exit_code == 0

    def test_help_shows_flags(self):
        runner = CliRunner()
        result = runner.invoke(main, ["init", "--help"])
        for flag in ["--agent-id", "--host-type", "--broker", "--invoke",
                     "--inbox", "--skip-broker", "--skip-plugin", "--dry-run", "--yes",
                     "--topic-root"]:
            assert flag in result.output, f"Missing {flag} in --help output"


# ---------------------------------------------------------------------------
# Dry-run smoke tests
# ---------------------------------------------------------------------------

def _patch_platform(repo_root="/repo"):
    """Return a context stack that patches platform detection."""
    from contextlib import ExitStack
    stack = ExitStack()
    stack.enter_context(patch("swarmbus.cli.resolve_broker_addr", return_value="localhost"))
    stack.enter_context(patch("swarmbus.cli.detect_platform", return_value="debian"))
    stack.enter_context(patch("swarmbus.cli.find_repo_root", return_value=repo_root))
    return stack


class TestDryRunSmoke:
    def test_dry_run_cc_exits_0(self):
        runner = CliRunner()
        with _patch_platform():
            result = runner.invoke(main, [
                "init", "--agent-id", "test", "--host-type", "cc", "--dry-run",
            ])
        assert result.exit_code == 0, result.output

    def test_dry_run_cc_prints_six_steps(self):
        runner = CliRunner()
        with _patch_platform():
            result = runner.invoke(main, [
                "init", "--agent-id", "test", "--host-type", "cc", "--dry-run",
            ])
        for step in ["Broker", "Package", "Systemd unit", "Wake wrapper", "Host plugin", "Doctor"]:
            assert step in result.output, f"Missing step '{step}' in output"

    def test_dry_run_none_host_exits_0(self):
        runner = CliRunner()
        with _patch_platform():
            result = runner.invoke(main, [
                "init", "--agent-id", "test", "--host-type", "none", "--dry-run",
            ])
        assert result.exit_code == 0, result.output

    def test_dry_run_none_host_plugin_skipped(self):
        runner = CliRunner()
        with _patch_platform():
            result = runner.invoke(main, [
                "init", "--agent-id", "test", "--host-type", "none", "--dry-run",
            ])
        assert "skipped" in result.output

    def test_dry_run_skip_broker(self):
        runner = CliRunner()
        with _patch_platform():
            result = runner.invoke(main, [
                "init", "--agent-id", "test", "--dry-run", "--skip-broker",
            ])
        assert result.exit_code == 0, result.output
        assert "--skip-broker" in result.output or "skipped" in result.output

    def test_dry_run_skip_plugin(self):
        runner = CliRunner()
        with _patch_platform():
            result = runner.invoke(main, [
                "init", "--agent-id", "test", "--host-type", "cc", "--dry-run", "--skip-plugin",
            ])
        assert result.exit_code == 0, result.output

    def test_dry_run_shows_success_banner(self):
        runner = CliRunner()
        with _patch_platform():
            result = runner.invoke(main, [
                "init", "--agent-id", "sparrow", "--dry-run",
            ])
        assert "sparrow is ready" in result.output


# ---------------------------------------------------------------------------
# Wake wrapper fallback (no repo root)
# ---------------------------------------------------------------------------

class TestWakeWrapperFallback:
    def test_no_repo_root_warns_not_fails(self):
        runner = CliRunner()
        with _patch_platform(repo_root=None):
            result = runner.invoke(main, [
                "init", "--agent-id", "test", "--host-type", "cc", "--dry-run",
            ])
        assert result.exit_code == 0, result.output
        assert "⚠" in result.output or "not wired" in result.output

    def test_invoke_path_missing_warns_not_fails(self, tmp_path):
        runner = CliRunner()
        nonexistent = str(tmp_path / "nonexistent-wake.sh")
        with _patch_platform():
            result = runner.invoke(main, [
                "init", "--agent-id", "test", "--host-type", "cc",
                "--invoke", f"{nonexistent} test",
                "--dry-run",
            ])
        assert result.exit_code == 0, result.output

    def test_invoke_override_used_when_supplied(self):
        runner = CliRunner()
        with _patch_platform():
            result = runner.invoke(main, [
                "init", "--agent-id", "test", "--host-type", "none",
                "--invoke", "/custom/wake.sh test",
                "--dry-run",
            ])
        assert "/custom/wake.sh test" in result.output


# ---------------------------------------------------------------------------
# Plugin step positional args
# ---------------------------------------------------------------------------

class TestPluginStepArgs:
    def test_cc_plugin_uses_positional_args(self):
        runner = CliRunner()
        captured_cmds = []

        def fake_run_step(label, cmd, dry_run):
            captured_cmds.append(cmd)
            return True

        with _patch_platform(), \
             patch("swarmbus.cli._run_step", side_effect=fake_run_step):
            runner.invoke(main, [
                "init", "--agent-id", "sparrow", "--host-type", "cc",
                "--skip-broker",
            ])

        plugin_calls = [cmd for cmd in captured_cmds if "setup-cc-plugin" in " ".join(cmd)]
        assert len(plugin_calls) == 1
        cmd = plugin_calls[0]
        # Positional args: <agent-id> <broker> — NOT flags
        assert "sparrow" in cmd
        assert "localhost" in cmd
        assert "--agent-id" not in cmd  # not using flags

    def test_none_host_type_no_plugin_call(self):
        runner = CliRunner()
        captured_cmds = []

        def fake_run_step(label, cmd, dry_run):
            captured_cmds.append(cmd)
            return True

        with _patch_platform(), \
             patch("swarmbus.cli._run_step", side_effect=fake_run_step):
            runner.invoke(main, [
                "init", "--agent-id", "test", "--host-type", "none",
                "--skip-broker",
            ])

        plugin_calls = [cmd for cmd in captured_cmds if "setup-cc-plugin" in " ".join(cmd)
                        or "setup-openclaw-plugin" in " ".join(cmd)]
        assert len(plugin_calls) == 0


# ---------------------------------------------------------------------------
# Exit code tests
# ---------------------------------------------------------------------------

class TestExitCodes:
    def test_all_steps_pass_exits_0(self):
        runner = CliRunner()
        with _patch_platform(), \
             patch("swarmbus.cli._run_step", return_value=True):
            result = runner.invoke(main, [
                "init", "--agent-id", "test", "--host-type", "none", "--skip-broker",
            ])
        assert result.exit_code == 0, result.output

    def test_broker_failure_exits_1(self):
        runner = CliRunner()
        call_count = [0]

        def fake_run_step(label, cmd, dry_run):
            call_count[0] += 1
            # broker is first real step call
            if call_count[0] == 1:
                return False
            return True

        with _patch_platform(), \
             patch("swarmbus.cli._run_step", side_effect=fake_run_step):
            result = runner.invoke(main, [
                "init", "--agent-id", "test", "--host-type", "none",
            ])
        assert result.exit_code == 1

    def test_partial_failure_exits_1(self):
        runner = CliRunner()

        def fake_run_step(label, cmd, dry_run):
            # doctor step fails
            if "doctor" in " ".join(cmd):
                return False
            return True

        with _patch_platform(), \
             patch("swarmbus.cli._run_step", side_effect=fake_run_step):
            result = runner.invoke(main, [
                "init", "--agent-id", "test", "--skip-broker",
            ])
        assert result.exit_code == 1

    def test_broker_resolve_error_exits_1(self):
        runner = CliRunner()
        with patch("swarmbus.cli.resolve_broker_addr",
                   side_effect=RuntimeError("tailscale not found")):
            result = runner.invoke(main, [
                "init", "--agent-id", "test", "--broker", "tailscale",
            ])
        assert result.exit_code == 1


class TestStepDoctor:
    """Regression: _step_doctor must build a valid subprocess list even when
    swarmbus is not on PATH (the old fallback produced a single string like
    'python3 -m swarmbus' as a list element, which subprocess.run passes
    verbatim as the executable name and raises FileNotFoundError)."""

    def test_doctor_uses_swarmbus_on_path(self):
        from swarmbus.cli import _step_doctor
        captured = []

        def fake_run_step(label, cmd, dry_run):
            captured.append(cmd)
            return True

        with patch("swarmbus.cli.shutil.which", return_value="/usr/bin/swarmbus"), \
             patch("swarmbus.cli._run_step", side_effect=fake_run_step):
            _step_doctor("sparrow", "fleet", dry_run=True)

        assert captured[0][0] == "/usr/bin/swarmbus"
        assert "--agent-id" in captured[0]
        assert "--topic-root=fleet" in captured[0]

    def test_doctor_fallback_is_list_not_string(self):
        """When swarmbus not on PATH, cmd must be a proper list of strings."""
        from swarmbus.cli import _step_doctor
        captured = []

        def fake_run_step(label, cmd, dry_run):
            captured.append(cmd)
            return True

        with patch("swarmbus.cli.shutil.which", return_value=None), \
             patch("swarmbus.cli._run_step", side_effect=fake_run_step):
            _step_doctor("sparrow", "", dry_run=True)

        cmd = captured[0]
        # Must be a proper list — no element should contain spaces
        for element in cmd:
            assert " " not in element, f"Element {element!r} contains space — subprocess would fail"
        assert "doctor" in cmd


SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"


def _stub_bin(home, names):
    """Stub executables that append their argv (space-joined) to <name>.calls."""
    bin_dir = home / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    for name in names:
        stub = bin_dir / name
        stub.write_text(
            "#!/bin/sh\n"
            f'printf "%s\\n" "$*" >> "$(dirname "$0")/{name}.calls"\n'
        )
        stub.chmod(0o755)
    return bin_dir


def _run_script(home, script, *args):
    bin_dir = home / "bin"
    env = {"PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}", "HOME": str(home)}
    return subprocess.run(
        ["bash", str(SCRIPTS / script), *args],
        env=env, capture_output=True, text=True, timeout=60,
    )


UNIT_DIR = ".config/systemd/user"


def _root_of(extra):
    """The ``--topic-root`` value in `extra`, or None when it is not given."""
    args = list(extra)
    return args[args.index("--topic-root") + 1] if "--topic-root" in args else None


def _unit_path(home, root=None):
    """Where install-systemd.sh writes planner's unit for `root`.

    Derived from TopicMap so the script and the CLI are held to one name:
    doctor, systemctl and the drop-in directory are all keyed by it.
    """
    topics = TopicMap() if root is None else TopicMap(root=root)
    return home / UNIT_DIR / topics.systemd_unit("planner")


def _render_unit(home, *extra):
    home.mkdir(parents=True)
    _stub_bin(home, ("swarmbus", "systemctl", "sleep"))
    result = _run_script(home, "install-systemd.sh", "planner", *extra)
    assert result.returncode == 0, result.stdout + result.stderr
    return _unit_path(home, _root_of(extra)).read_text()


def test_install_systemd_renders_one_line_execstart(tmp_path):
    bare = _render_unit(tmp_path / "bare")
    lines = bare.splitlines()
    execs = [line for line in lines if line.startswith("ExecStart=")]
    assert len(execs) == 1, bare
    assert " start --agent-id planner --broker localhost --inbox " in execs[0]
    assert "--invoke" not in execs[0]
    assert not [line for line in lines if line.lstrip().startswith("--")]
    assert not [line for line in lines if line.rstrip().endswith("\\")]

    wake = tmp_path / "wake.sh"
    woke = _render_unit(tmp_path / "woke", "--invoke", f"{wake} planner")
    execs = [line for line in woke.splitlines() if line.startswith("ExecStart=")]
    assert len(execs) == 1, woke
    assert " start --agent-id planner --broker localhost --inbox " in execs[0]
    assert execs[0].endswith(f' --invoke "{wake} planner"')


def test_install_systemd_unit_orders_after_network_online(tmp_path):
    unit = _render_unit(tmp_path / "home")
    after = [line for line in unit.splitlines() if line.startswith("After=")]
    assert after and "network-online.target" in after[0].split("=", 1)[1].split()
    assert "Wants=network-online.target" in unit.splitlines()


def test_setup_cc_plugin_uses_claude_mcp_add_and_rerun_does_not_nest(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    bin_dir = _stub_bin(home, ("claude",))
    for _ in range(2):
        result = _run_script(home, "setup-cc-plugin.sh", "planner", "localhost")
        assert result.returncode == 0, result.stdout + result.stderr

    skill = home / ".claude/skills/using-swarmbus"
    assert (skill / "SKILL.md").is_file()
    assert not (skill / "using-swarmbus").exists()
    assert not (home / ".claude/settings.json").exists()
    remove = "mcp remove --scope user swarmbus"
    add = "mcp add --scope user swarmbus -- swarmbus mcp-server --agent-id planner --broker localhost"
    assert (bin_dir / "claude.calls").read_text().splitlines() == [remove, add, remove, add]


@pytest.mark.parametrize(
    "script", ["install-systemd.sh", "setup-openclaw-plugin.sh"]
)
def test_setup_scripts_reject_a_rooted_scope_as_an_agent_id(tmp_path, script):
    home = tmp_path / script
    home.mkdir()
    rooted_scope = TopicMap(root="fleet").unit_scope("planner")

    result = _run_script(home, script, rooted_scope)

    assert result.returncode == 2, result.stdout + result.stderr
    assert "agent id" in result.stderr
    assert not (home / ".config").exists()
    assert not (home / ".openclaw").exists()


def test_setup_cc_plugin_refuses_while_the_plugin_is_installed(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    bin_dir = _stub_bin(home, ("claude",))
    registry = home / ".claude/plugins/installed_plugins.json"
    registry.parent.mkdir(parents=True)
    registry.write_text('{"version": 2, "plugins": {"swarmbus@swarmbus": [{"scope": "user"}]}}')

    result = _run_script(home, "setup-cc-plugin.sh", "planner", "localhost")
    assert result.returncode == 1, result.stdout + result.stderr
    assert "plugin is installed" in result.stderr
    assert "twice" in result.stderr
    assert not (bin_dir / "claude.calls").exists()
    assert not (home / ".claude/skills/using-swarmbus").exists()

    registry.write_text('{"version": 2, "plugins": {"other@market": []}}')
    result = _run_script(home, "setup-cc-plugin.sh", "planner", "localhost")
    assert result.returncode == 0, result.stdout + result.stderr
    calls = (bin_dir / "claude.calls").read_text()
    assert "mcp add --scope user swarmbus -- swarmbus mcp-server --agent-id planner --broker localhost" in calls


# ---------------------------------------------------------------------------
# topic root: the unit, the plugin registration and the init steps
# ---------------------------------------------------------------------------

UNIT = ".config/systemd/user/swarmbus-planner.service"


def test_install_systemd_pins_the_unit_to_a_given_topic_root(tmp_path):
    home = tmp_path / "rooted"
    unit = _render_unit(home, "--topic-root", "fleet")
    execs = [line for line in unit.splitlines() if line.startswith("ExecStart=")]
    assert len(execs) == 1, unit
    assert execs[0].split().count("--topic-root=fleet") == 1, execs[0]
    doctor = (home / "bin/swarmbus.calls").read_text()
    assert "doctor --agent-id planner --broker localhost --topic-root=fleet" in doctor


def test_install_systemd_renders_an_explicitly_empty_topic_root(tmp_path):
    unit = _render_unit(tmp_path / "unrooted", "--topic-root", "")
    [exec_line] = [line for line in unit.splitlines() if line.startswith("ExecStart=")]
    assert exec_line.split()[-1] == "--topic-root=", exec_line


def test_install_systemd_leaves_the_root_to_the_config_file_when_not_given(tmp_path):
    unit = _render_unit(tmp_path / "default")
    assert "--topic-root" not in unit


def test_install_systemd_writes_the_unit_name_the_cli_computes(tmp_path):
    """The script and TopicMap.systemd_unit must agree byte for byte.

    ``/`` is legal in a root and in neither a unit name nor a file name,
    so a multi-level root is the case that catches a second spelling of
    the escape.
    """
    home = tmp_path / "rooted"
    _render_unit(home, "--topic-root", "fleet/prod")

    unit = TopicMap(root="fleet/prod").systemd_unit("planner")
    assert unit == "swarmbus-planner:fleet:2fprod.service"
    assert (home / UNIT_DIR / unit).is_file()
    calls = (home / "bin/systemctl.calls").read_text()
    assert f"--user enable {unit}" in calls
    assert f"--user restart {unit}" in calls
    assert not (home / UNIT).exists()


def test_install_systemd_keeps_the_unrooted_unit_path_byte_identical(tmp_path):
    """An install that predates root-aware unit names must not be renamed."""
    for name, extra in (("default", ()), ("explicit-empty", ("--topic-root", ""))):
        home = tmp_path / name
        _render_unit(home, *extra)

        assert (home / UNIT).is_file()
        installed = sorted(p.name for p in (home / UNIT_DIR).iterdir())
        assert installed == ["swarmbus-planner.service"], installed
        calls = (home / "bin/systemctl.calls").read_text()
        assert "--user enable swarmbus-planner.service" in calls


def test_install_systemd_keys_the_auth_dropin_to_the_rooted_unit_name(tmp_path):
    """systemd ignores a drop-in directory that does not match the unit.

    A mismatch here does not fail: the daemon starts without the
    credentials and cannot authenticate to the broker.
    """
    home = tmp_path / "creds"
    _render_unit(home, "--topic-root", "fleet", "--username", "u", "--password", "p")

    unit = TopicMap(root="fleet").systemd_unit("planner")
    dropin = home / UNIT_DIR / f"{unit}.d" / "auth.conf"
    assert dropin.is_file(), sorted(p.name for p in (home / UNIT_DIR).iterdir())
    assert "Environment=SWARMBUS_BROKER_USERNAME=u" in dropin.read_text()


@pytest.mark.parametrize("root", ["two words", "fleet|pipe"])
def test_install_systemd_refuses_a_root_it_cannot_render(tmp_path, root):
    home = tmp_path / "unsafe"
    home.mkdir(parents=True)
    _stub_bin(home, ("swarmbus", "systemctl", "sleep"))

    result = _run_script(home, "install-systemd.sh", "planner", "--topic-root", root)

    assert result.returncode == 2, result.stdout + result.stderr
    assert root in result.stderr
    assert "config.ini" in result.stderr
    assert not (home / UNIT).exists()


@pytest.mark.parametrize(
    ("script", "args"),
    [
        ("install-systemd.sh", ("planner", "--topic-root", "fleet//prod")),
        ("setup-openclaw-plugin.sh", ("planner", "localhost", "fleet//prod")),
    ],
)
def test_setup_scripts_refuse_an_empty_topic_segment(tmp_path, script, args):
    home = tmp_path / script
    home.mkdir(parents=True)

    result = _run_script(home, script, *args)

    assert result.returncode == 2, result.stdout + result.stderr
    assert "empty segment" in result.stderr
    assert not (home / ".config").exists()
    assert not (home / ".openclaw").exists()


def _exec_start(unit_text):
    [exec_line] = [
        line for line in unit_text.splitlines() if line.startswith("ExecStart=")
    ]
    return exec_line


def _rendered_inbox(unit_text):
    """The ``--inbox`` value on the rendered ExecStart line."""
    argv = _exec_start(unit_text).split()
    return argv[argv.index("--inbox") + 1]


def _rendered_logs(unit_text):
    """Every path the unit appends its stdout and stderr to."""
    return {
        line.split("append:", 1)[1]
        for line in unit_text.splitlines()
        if line.startswith(("StandardOutput=", "StandardError="))
    }


def test_install_systemd_keeps_the_unrooted_log_and_inbox_byte_identical(tmp_path):
    """The literals an existing install already has: no migration."""
    for name, extra in (("default", ()), ("explicit-empty", ("--topic-root", ""))):
        home = tmp_path / name
        unit = _render_unit(home, *extra)

        assert _rendered_logs(unit) == {f"{home}/logs/swarmbus-planner.log"}, unit
        assert _rendered_inbox(unit) == f"{home}/sync/planner-inbox.md"


def test_install_systemd_splits_the_log_and_the_default_inbox_per_root(tmp_path):
    """Both carry the same escaped token the unit name does."""
    home = tmp_path / "rooted"
    unit = _render_unit(home, "--topic-root", "fleet/prod")
    topics = TopicMap(root="fleet/prod")

    assert _rendered_logs(unit) == {
        f"{home}/logs/{topics.unit_log_name('planner')}"
    }, unit
    assert _rendered_inbox(unit) == (
        f"{home}/sync/{topics.default_inbox_name('planner')}"
    )
    assert topics.unit_log_name("planner") == "swarmbus-planner:fleet:2fprod.log"


def test_install_systemd_gives_two_roots_their_own_log_and_inbox(tmp_path):
    """The contention this split exists to remove: one log interleaving two
    daemons' output, one inbox file written by both."""
    home = tmp_path / "two-roots"
    _render_unit(home, "--topic-root", "fleet")
    result = _run_script(home, "install-systemd.sh", "planner", "--topic-root", "lab")
    assert result.returncode == 0, result.stdout + result.stderr

    fleet = _unit_path(home, "fleet").read_text()
    lab = _unit_path(home, "lab").read_text()

    assert _rendered_logs(fleet).isdisjoint(_rendered_logs(lab))
    assert _rendered_inbox(fleet) != _rendered_inbox(lab)


def test_install_systemd_leaves_an_explicit_inbox_untouched(tmp_path):
    """``--inbox`` is operator-supplied; only the DEFAULT carries the root."""
    home = tmp_path / "explicit-inbox"
    chosen = tmp_path / "chosen" / "mail.md"
    unit = _render_unit(home, "--topic-root", "fleet", "--inbox", str(chosen))

    assert _rendered_inbox(unit) == str(chosen)
    assert TopicMap(root="fleet").default_inbox_name("planner") not in unit


def test_install_systemd_refuses_a_unit_name_past_systemds_byte_limit(tmp_path):
    """255 bytes is systemd's maximum unit name length.

    The script is what writes the file, so it refuses before anything is
    created rather than failing inside sed with ENAMETOOLONG, and it names
    the real byte count beside the limit.
    """
    home = tmp_path / "too-long"
    home.mkdir(parents=True)
    _stub_bin(home, ("swarmbus", "systemctl", "sleep"))
    # swarmbus- (9) + planner (7) + : (1) + 240 (root, no escape needed)
    # + .service (8) = 265.
    result = _run_script(home, "install-systemd.sh", "planner",
                         "--topic-root", "a" * 240)

    assert result.returncode == 2, result.stdout + result.stderr
    assert "265" in result.stderr, result.stderr
    assert "255" in result.stderr, result.stderr
    assert not (home / UNIT_DIR).exists(), sorted(
        p.name for p in (home / UNIT_DIR).iterdir()
    )
    assert not (home / "bin/systemctl.calls").exists()


def _unit_and_calls(home, root=None):
    """The installed unit and the systemctl stub's call log, byte for byte."""
    return (
        _unit_path(home, root).read_bytes(),
        (home / "bin/systemctl.calls").read_bytes(),
    )


def _legacy_unit(home, pin=None):
    """A unit at the name installs used before they carried the root.

    ``pin=None`` is a unit whose daemon takes its root from the config
    file; ``pin=""`` is one pinned to the unrooted tree.
    """
    home.mkdir(parents=True, exist_ok=True)
    _stub_bin(home, ("swarmbus", "systemctl", "sleep"))
    path = home / UNIT
    path.parent.mkdir(parents=True, exist_ok=True)
    root_arg = "" if pin is None else f" --topic-root={pin}"
    path.write_text(
        "[Service]\n"
        "ExecStart=/usr/bin/swarmbus start --agent-id planner "
        f"--broker localhost{root_arg}\n"
    )
    return path


def test_install_systemd_runs_two_roots_side_by_side(tmp_path):
    """Distinct roots are distinct units, so neither install can move the
    other's daemon. This REPLACES the different-root refusal."""
    home = tmp_path / "two-roots"
    fleet_unit = _render_unit(home, "--topic-root", "fleet")

    result = _run_script(home, "install-systemd.sh", "planner", "--topic-root", "lab")

    assert result.returncode == 0, result.stdout + result.stderr
    fleet, lab = _unit_path(home, "fleet"), _unit_path(home, "lab")
    assert fleet != lab
    assert fleet.read_text() == fleet_unit
    assert "--topic-root=lab" in lab.read_text()
    # The outbox variable's NAME is derived from the agent id, and an
    # environment variable name cannot hold a root, so both units export
    # the same one.
    for unit in (fleet, lab):
        [env_line] = [
            line for line in unit.read_text().splitlines()
            if "SWARMBUS_OUTBOX_" in line and line.startswith("Environment=")
        ]
        assert env_line.startswith("Environment=SWARMBUS_OUTBOX_PLANNER="), env_line


def test_install_systemd_reinstalls_a_unit_with_the_same_root(tmp_path):
    home = tmp_path / "idempotent"
    first = _render_unit(home, "--topic-root", "fleet")

    result = _run_script(home, "install-systemd.sh", "planner", "--topic-root", "fleet")

    assert result.returncode == 0, result.stdout + result.stderr
    assert _unit_path(home, "fleet").read_text() == first


def test_install_systemd_refuses_a_rooted_install_beside_the_same_roots_old_unit(
    tmp_path,
):
    """Both units would be one agent id on one root: two daemons sharing a
    persistent session, each disconnecting the other."""
    home = tmp_path / "legacy-same-root"
    legacy = _legacy_unit(home, "fleet")
    before = legacy.read_bytes()

    result = _run_script(home, "install-systemd.sh", "planner", "--topic-root", "fleet")

    assert result.returncode == 2, result.stdout + result.stderr
    assert str(legacy) in result.stderr
    assert "two daemons" in result.stderr
    assert "fleet" in result.stderr
    assert legacy.read_bytes() == before
    assert not _unit_path(home, "fleet").exists()
    # No call log at all means the refusal landed before any systemctl ran.
    assert not (home / "bin/systemctl.calls").exists()


def test_install_systemd_normalizes_a_legacy_pin_before_collision_check(tmp_path):
    home = tmp_path / "legacy-normalized-root"
    legacy = _legacy_unit(home, "/fleet/")
    before = legacy.read_bytes()

    result = _run_script(home, "install-systemd.sh", "planner", "--topic-root", "fleet")

    assert result.returncode == 2, result.stdout + result.stderr
    assert "two daemons" in result.stderr
    assert legacy.read_bytes() == before
    assert not _unit_path(home, "fleet").exists()
    assert not (home / "bin/systemctl.calls").exists()


def test_install_systemd_refuses_a_rooted_install_beside_an_unpinned_unit(tmp_path):
    """An unpinned unit's daemon reads its root from the config file, so
    whether it is this swarm cannot be answered from here."""
    home = tmp_path / "legacy-unpinned"
    legacy = _legacy_unit(home)
    before = legacy.read_bytes()

    result = _run_script(home, "install-systemd.sh", "planner", "--topic-root", "fleet")

    assert result.returncode == 2, result.stdout + result.stderr
    assert str(legacy) in result.stderr
    assert "no --topic-root" in result.stderr
    assert legacy.read_bytes() == before
    assert not _unit_path(home, "fleet").exists()


@pytest.mark.parametrize("pin", ["", "lab"], ids=["unrooted", "another-root"])
def test_install_systemd_installs_beside_a_unit_pinned_to_another_root(tmp_path, pin):
    """Two roots on one host is the point of the rename: a unit pinned to
    another swarm is left exactly as it is."""
    home = tmp_path / f"beside-{pin or 'unrooted'}"
    legacy = _legacy_unit(home, pin)
    before = legacy.read_bytes()

    result = _run_script(home, "install-systemd.sh", "planner", "--topic-root", "fleet")

    assert result.returncode == 0, result.stdout + result.stderr
    assert legacy.read_bytes() == before
    assert _unit_path(home, "fleet").is_file()


def test_install_systemd_refuses_to_unpin_the_unit_without_a_root(tmp_path):
    """Dropping a pin hands that daemon's namespace to the config file."""
    home = tmp_path / "unpin"
    legacy = _legacy_unit(home, "fleet")
    before = legacy.read_bytes()

    result = _run_script(home, "install-systemd.sh", "planner")

    assert result.returncode == 2, result.stdout + result.stderr
    assert "--topic-root fleet" in result.stderr
    assert "config file" in result.stderr
    assert legacy.read_bytes() == before


def test_install_systemd_refuses_to_move_the_unit_without_a_root_to_another_swarm(
    tmp_path,
):
    """Its name cannot carry a root, so overwriting it moves its daemon."""
    home = tmp_path / "repin"
    legacy = _legacy_unit(home, "fleet")
    before = legacy.read_bytes()

    result = _run_script(home, "install-systemd.sh", "planner", "--topic-root", "")

    assert result.returncode == 2, result.stdout + result.stderr
    assert "fleet" in result.stderr
    assert "(unrooted)" in result.stderr
    assert legacy.read_bytes() == before


def test_install_systemd_refuses_to_unpin_a_unit_pinned_unrooted(tmp_path):
    home = tmp_path / "wasunrooted"
    _render_unit(home, "--topic-root", "")
    before = _unit_and_calls(home)

    result = _run_script(home, "install-systemd.sh", "planner")

    assert result.returncode == 2, result.stdout + result.stderr
    assert "(unrooted)" in result.stderr
    assert "--topic-root ''" in result.stderr
    assert _unit_and_calls(home) == before


def test_setup_cc_plugin_registers_a_given_topic_root(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    bin_dir = _stub_bin(home, ("claude",))

    result = _run_script(home, "setup-cc-plugin.sh", "planner", "localhost", "fleet")

    assert result.returncode == 0, result.stdout + result.stderr
    assert ("mcp add --scope user swarmbus -- swarmbus mcp-server "
            "--agent-id planner --broker localhost --topic-root=fleet"
            ) in (bin_dir / "claude.calls").read_text()
    assert "fleet" in result.stdout


def test_setup_cc_plugin_omits_the_topic_root_when_not_given(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    bin_dir = _stub_bin(home, ("claude",))

    result = _run_script(home, "setup-cc-plugin.sh", "planner", "localhost")

    assert result.returncode == 0, result.stdout + result.stderr
    assert "--topic-root" not in (bin_dir / "claude.calls").read_text()


def test_setup_openclaw_uses_the_rooted_inbox_unit_and_commands(tmp_path):
    home = tmp_path / "home"
    (home / ".openclaw/skills").mkdir(parents=True)
    _stub_bin(home, ("swarmbus",))
    topics = TopicMap(root="/fleet/prod/")

    result = _run_script(
        home, "setup-openclaw-plugin.sh", "planner", "localhost", "/fleet/prod/"
    )

    assert result.returncode == 0, result.stdout + result.stderr
    inbox = home / "sync" / topics.default_inbox_name("planner")
    assert f"--inbox {inbox} --topic-root=fleet/prod" in result.stdout
    assert (
        f"systemctl --user enable --now {topics.systemd_unit('planner')}"
        in result.stdout
    )
    assert "swarmbus read   --agent-id planner --topic-root=fleet/prod" in result.stdout


def test_setup_openclaw_uses_a_distinct_byobu_session_for_each_root(tmp_path):
    sessions = []

    for root in ("fleet", "lab"):
        home = tmp_path / root
        (home / ".openclaw/skills").mkdir(parents=True)
        _stub_bin(home, ("swarmbus",))

        result = _run_script(
            home, "setup-openclaw-plugin.sh", "planner", "localhost", root
        )

        assert result.returncode == 0, result.stdout + result.stderr
        sessions.append(
            result.stdout.split("byobu new-session -d -s ", 1)[1].split()[0]
        )

    assert len(set(sessions)) == 2


def _init_steps(*args):
    """argv of every step `init` would run, with the step runner stubbed out."""
    captured: list[list[str]] = []

    def fake_run_step(label, cmd, dry_run):
        captured.append(cmd)
        return True

    with _patch_platform(), patch("swarmbus.cli._run_step", side_effect=fake_run_step):
        result = CliRunner().invoke(main, list(args))
    assert result.exit_code == 0, result.output
    return captured


def _step(commands, needle):
    [cmd] = [c for c in commands if needle in " ".join(c)]
    return cmd


def test_init_rejects_a_root_the_systemd_renderer_cannot_round_trip():
    root = TopicMap(root="x@y").root
    with _patch_platform(), patch("swarmbus.cli._run_step") as run_step:
        result = CliRunner().invoke(main, [
            "init", "--agent-id", "planner", "--dry-run", "--skip-broker",
            "--topic-root", root,
        ])

    assert result.exit_code == 2, result.output
    assert "cannot be installed as a systemd unit" in result.output
    run_step.assert_not_called()


def test_init_pins_the_unit_and_doctor_to_the_root_it_resolved():
    commands = _init_steps(
        "init", "--agent-id", "planner", "--host-type", "cc",
        "--dry-run", "--skip-broker", "--topic-root", "fleet",
    )

    systemd = _step(commands, "install-systemd.sh")
    assert systemd[systemd.index("--topic-root") + 1] == "fleet"
    assert "--topic-root=fleet" in _step(commands, "doctor")
    assert _step(commands, "setup-cc-plugin.sh")[1:] == ["planner", "localhost", "fleet"]


def test_init_dry_run_prints_the_root_it_would_install():
    with _patch_platform():
        result = CliRunner().invoke(main, [
            "init", "--agent-id", "planner", "--dry-run", "--skip-broker",
            "--topic-root", "fleet",
        ])

    assert result.exit_code == 0, result.output
    assert "broker=localhost topic-root=fleet" in result.output
    lines = result.output.splitlines()
    [install] = [line for line in lines if "install-systemd.sh" in line]
    assert "--topic-root fleet" in install
    [doctor] = [line for line in lines if " doctor " in line]
    assert "--topic-root=fleet" in doctor


def test_init_takes_the_root_from_the_config_file(tmp_path):
    config = tmp_path / "config.ini"
    config.write_text("[broker]\ntopic_root = fleet\n")

    commands = _init_steps(
        "--config", str(config),
        "init", "--agent-id", "planner", "--dry-run", "--skip-broker",
    )

    systemd = _step(commands, "install-systemd.sh")
    assert systemd[systemd.index("--topic-root") + 1] == "fleet"
    assert "--topic-root=fleet" in _step(commands, "doctor")


def test_init_pins_the_unit_to_the_unrooted_layout_by_default():
    commands = _init_steps("init", "--agent-id", "planner", "--dry-run", "--skip-broker")

    systemd = _step(commands, "install-systemd.sh")
    assert systemd[systemd.index("--topic-root") + 1] == ""
    assert "--topic-root=" in _step(commands, "doctor")


def test_init_defaults_the_inbox_to_the_root_it_resolved():
    """init always passes --inbox, so its default is the one that decides."""
    commands = _init_steps(
        "init", "--agent-id", "planner", "--dry-run", "--skip-broker",
        "--topic-root", "fleet",
    )

    systemd = _step(commands, "install-systemd.sh")
    inbox = systemd[systemd.index("--inbox") + 1]
    assert inbox == str(
        Path.home() / "sync" / TopicMap(root="fleet").default_inbox_name("planner")
    )


def test_init_keeps_the_unrooted_default_inbox_byte_identical():
    commands = _init_steps("init", "--agent-id", "planner", "--dry-run", "--skip-broker")

    systemd = _step(commands, "install-systemd.sh")
    inbox = systemd[systemd.index("--inbox") + 1]
    assert inbox == str(Path.home() / "sync" / "planner-inbox.md")


def test_init_passes_an_explicit_inbox_through_untouched():
    commands = _init_steps(
        "init", "--agent-id", "planner", "--dry-run", "--skip-broker",
        "--topic-root", "fleet", "--inbox", "/srv/mail/planner.md",
    )

    systemd = _step(commands, "install-systemd.sh")
    assert systemd[systemd.index("--inbox") + 1] == "/srv/mail/planner.md"
