import os
import subprocess
from pathlib import Path

EXAMPLES = Path(__file__).resolve().parents[1] / "examples"
CLAUDE_WAKE = EXAMPLES / "claude-code-wake.sh"
OPENCLAW_WAKE = EXAMPLES / "openclaw-wake.sh"
STUBS = ("claude", "node", "openclaw", "curl")
LABEL = "[RECEIVER PROTOCOL"


def _run(home, script, agent_id, *, to, priority, extra_env=None, body="hello from alice"):
    """Run a wake script with stub binaries on PATH and HOME at ``home``.

    Each stub records its argv to ``<name>.args`` and its stdin to
    ``<name>.stdin`` in ``home/bin``; a missing file means it never ran.
    """
    bin_dir = home / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    for name in STUBS:
        stub = bin_dir / name
        stub.write_text(
            "#!/bin/sh\n"
            f'printf "%s\\n" "$@" > "$(dirname "$0")/{name}.args"\n'
            f'cat > "$(dirname "$0")/{name}.stdin"\n'
        )
        stub.chmod(0o755)
    env = {
        "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        "HOME": str(home),
        "SWARMBUS_FROM": "alice",
        "SWARMBUS_TO": to,
        "SWARMBUS_ID": "m-1",
        "SWARMBUS_SUBJECT": "status",
        "SWARMBUS_CONTENT_TYPE": "text/plain",
        "SWARMBUS_PRIORITY": priority,
        "SWARMBUS_TS": "2026-10-15T00:00:00+00:00",
        **(extra_env or {}),
    }
    result = subprocess.run(
        ["bash", str(script), agent_id],
        input=body.encode(),
        env=env,
        capture_output=True,
        timeout=60,
        check=False,
    )
    return result, bin_dir


def _telegram_secrets(home):
    secrets = home / ".secrets"
    secrets.mkdir(parents=True, exist_ok=True)
    (secrets / "TELEGRAM_BOT_TOKEN").write_text("t")
    (secrets / "TELEGRAM_CHAT_ID").write_text("c")


def test_openclaw_wake_gates_on_priority_like_claude_code(tmp_path):
    result, bin_dir = _run(tmp_path / "normal", OPENCLAW_WAKE, "main", to="bob", priority="normal")
    assert result.returncode == 0
    assert not (bin_dir / "node.stdin").exists()

    result, bin_dir = _run(tmp_path / "high", OPENCLAW_WAKE, "main", to="bob", priority="high")
    assert result.returncode == 0
    assert "hello from alice" in (bin_dir / "node.stdin").read_text()

    result, bin_dir = _run(
        tmp_path / "all", OPENCLAW_WAKE, "main", to="bob", priority="normal",
        extra_env={"SWARMBUS_WAKE_POLICY": "all"},
    )
    assert result.returncode == 0
    assert (bin_dir / "node.stdin").exists()


def test_claude_wake_never_wakes_on_broadcast(tmp_path):
    for case, extra in (("default", {}), ("all", {"SWARMBUS_WAKE_POLICY": "all"})):
        home = tmp_path / case
        _telegram_secrets(home)
        result, bin_dir = _run(home, CLAUDE_WAKE, "bob", to="broadcast", priority="high", extra_env=extra)
        assert result.returncode == 0
        assert not (bin_dir / "claude.stdin").exists()
        assert not (bin_dir / "curl.args").exists()
        log = home / ".local/state/swarmbus-wake/bob.log"
        assert "broadcast" in log.read_text()


def test_openclaw_wake_never_wakes_on_broadcast(tmp_path):
    cases = (
        ("bridge", {}),
        ("all", {"SWARMBUS_WAKE_POLICY": "all"}),
        ("cli", {"SWARMBUS_WAKE_POLICY": "all", "OPENCLAW_WAKE_USE_CLI": "1"}),
    )
    for case, extra in cases:
        home = tmp_path / case
        _telegram_secrets(home)
        result, bin_dir = _run(home, OPENCLAW_WAKE, "main", to="broadcast", priority="high", extra_env=extra)
        assert result.returncode == 0
        assert not (bin_dir / "node.stdin").exists()
        assert not (bin_dir / "openclaw.args").exists()
        assert not (bin_dir / "curl.args").exists()
        log = home / ".local/state/swarmbus-wake/main-wake.log"
        assert "broadcast" in log.read_text()


def _assert_protocol_first(prompt, rule):
    assert prompt.count(LABEL) == 1
    assert rule in prompt
    assert prompt.index(LABEL) < prompt.index(rule) < prompt.index("[UNTRUSTED PEER METADATA")
    assert prompt.index("[UNTRUSTED PEER METADATA") < prompt.index("hello from alice")


def _check_receive_protocol(tmp_path, script, agent_id, stub):
    # 1. Default file ~/.swarmbus/receive-protocol-<agent-id>.md
    home = tmp_path / "file"
    (home / ".swarmbus").mkdir(parents=True)
    (home / ".swarmbus" / f"receive-protocol-{agent_id}.md").write_text("RULE-FROM-FILE: clear your inbox.\n")
    _, bin_dir = _run(home, script, agent_id, to="bob", priority="high")
    _assert_protocol_first((bin_dir / f"{stub}.stdin").read_text(), "RULE-FROM-FILE")

    # 2. SWARMBUS_RECEIVE_PROTOCOL wins over the default file.
    other = home / "elsewhere.md"
    other.write_text("RULE-FROM-ENV: ack the operator.\n")
    _, bin_dir = _run(home, script, agent_id, to="bob", priority="high",
                      extra_env={"SWARMBUS_RECEIVE_PROTOCOL": str(other)})
    prompt = (bin_dir / f"{stub}.stdin").read_text()
    _assert_protocol_first(prompt, "RULE-FROM-ENV")
    assert "RULE-FROM-FILE" not in prompt

    # 3. No file, no env: no protocol section; the wake still happens.
    _, bin_dir = _run(tmp_path / "none", script, agent_id, to="bob", priority="high")
    prompt = (bin_dir / f"{stub}.stdin").read_text()
    assert LABEL not in prompt
    assert "hello from alice" in prompt

    # 4. Env names a missing file: logged, wake proceeds without the section.
    home = tmp_path / "missing"
    result, bin_dir = _run(home, script, agent_id, to="bob", priority="high",
                           extra_env={"SWARMBUS_RECEIVE_PROTOCOL": str(home / "nope.md")})
    assert result.returncode == 0
    prompt = (bin_dir / f"{stub}.stdin").read_text()
    assert LABEL not in prompt
    log_name = f"{agent_id}.log" if script == CLAUDE_WAKE else f"{agent_id}-wake.log"
    assert "nope.md" in (home / ".local/state/swarmbus-wake" / log_name).read_text()


def test_claude_wake_puts_receive_protocol_before_peer_sections(tmp_path):
    _check_receive_protocol(tmp_path, CLAUDE_WAKE, "bob", "claude")


def test_openclaw_wake_puts_receive_protocol_before_peer_sections(tmp_path):
    _check_receive_protocol(tmp_path, OPENCLAW_WAKE, "main", "node")


REPO = EXAMPLES.parent


def test_docs_describe_wake_policy_broadcast_and_receive_protocol():
    openclaw_doc = (REPO / "docs" / "openclaw-wake.md").read_text()
    readme = (REPO / "README.md").read_text()
    changelog = (REPO / "CHANGELOG.md").read_text()

    for var in ("SWARMBUS_WAKE_POLICY", "SWARMBUS_RECEIVE_PROTOCOL"):
        assert var in openclaw_doc
        assert var in readme
        assert var in changelog
    assert "every\nswarmbus message wakes" not in openclaw_doc
    assert "broadcast" in openclaw_doc
    assert "receive-protocol-" in openclaw_doc
    assert "receive-protocol-example.md" in readme
    assert "never wakes" in readme
    unreleased = changelog.index("## [Unreleased]")
    assert unreleased < changelog.index("## [0.2.0]")
