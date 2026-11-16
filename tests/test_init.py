"""Tests for swarmbus init command and helpers."""
from __future__ import annotations

import os
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from swarmbus.cli import _derive_invoke, _run_step, main


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


def _render_unit(home, *extra):
    home.mkdir(parents=True)
    _stub_bin(home, ("swarmbus", "systemctl", "sleep"))
    result = _run_script(home, "install-systemd.sh", "planner", *extra)
    assert result.returncode == 0, result.stdout + result.stderr
    return (home / ".config/systemd/user/swarmbus-planner.service").read_text()


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


def _unit_and_calls(home):
    """The installed unit and the systemctl stub's call log, byte for byte."""
    return (
        (home / UNIT).read_bytes(),
        (home / "bin/systemctl.calls").read_bytes(),
    )


def test_install_systemd_refuses_to_repin_a_unit_to_another_root(tmp_path):
    home = tmp_path / "repin"
    _render_unit(home, "--topic-root", "fleet")
    before = _unit_and_calls(home)

    result = _run_script(home, "install-systemd.sh", "planner", "--topic-root", "lab")

    assert result.returncode == 2, result.stdout + result.stderr
    assert "fleet" in result.stderr
    assert "lab" in result.stderr
    assert str(home / UNIT) in result.stderr
    assert "one systemd unit per agent id" in result.stderr
    # Unchanged call log means the refusal landed before any systemctl ran.
    assert _unit_and_calls(home) == before


def test_install_systemd_refuses_to_unpin_a_pinned_unit(tmp_path):
    home = tmp_path / "unpin"
    _render_unit(home, "--topic-root", "fleet")
    before = _unit_and_calls(home)

    result = _run_script(home, "install-systemd.sh", "planner")

    assert result.returncode == 2, result.stdout + result.stderr
    assert "--topic-root fleet" in result.stderr
    assert "config file" in result.stderr
    assert _unit_and_calls(home) == before


def test_install_systemd_refuses_to_root_a_unit_pinned_unrooted(tmp_path):
    home = tmp_path / "wasunrooted"
    _render_unit(home, "--topic-root", "")
    before = _unit_and_calls(home)

    result = _run_script(home, "install-systemd.sh", "planner", "--topic-root", "fleet")

    assert result.returncode == 2, result.stdout + result.stderr
    assert "(unrooted)" in result.stderr
    assert _unit_and_calls(home) == before


def test_install_systemd_pins_a_unit_that_carries_no_root(tmp_path):
    # A unit rendered without a pin carries no --topic-root token at all;
    # pinning it is the supported upgrade, not a root change to refuse.
    home = tmp_path / "upgrade"
    _render_unit(home)

    result = _run_script(home, "install-systemd.sh", "planner", "--topic-root", "fleet")

    assert result.returncode == 0, result.stdout + result.stderr
    [exec_line] = [
        line for line in (home / UNIT).read_text().splitlines()
        if line.startswith("ExecStart=")
    ]
    assert exec_line.split().count("--topic-root=fleet") == 1, exec_line


def test_install_systemd_reinstalls_a_unit_with_the_same_root(tmp_path):
    home = tmp_path / "idempotent"
    first = _render_unit(home, "--topic-root", "fleet")

    result = _run_script(home, "install-systemd.sh", "planner", "--topic-root", "fleet")

    assert result.returncode == 0, result.stdout + result.stderr
    assert (home / UNIT).read_text() == first


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
