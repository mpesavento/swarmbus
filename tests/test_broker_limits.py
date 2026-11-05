"""Pin the broker quota defaults that setup-mosquitto.sh installs (#66)."""
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
LIMITS_FILE = ROOT / "scripts" / "mosquitto-limits.conf"
LIMITS_DEST = "/etc/mosquitto/conf.d/swarmbus-limits.conf"
EXPECTED = {
    "max_packet_size": "524288",
    "max_queued_messages": "1000",
    "max_queued_bytes": "33554432",
    "max_inflight_messages": "20",
}


def _settings(text):
    out = {}
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        parts = stripped.split()
        assert len(parts) == 2, f"not a 'key value' line: {line!r}"
        key, value = parts
        assert key not in out, f"duplicate setting: {key}"
        out[key] = value
    return out


def test_limits_file_holds_the_four_defaults_each_under_a_comment():
    text = LIMITS_FILE.read_text()

    assert _settings(text) == EXPECTED

    lines = [line.strip() for line in text.splitlines() if line.strip()]
    for i, line in enumerate(lines):
        if not line.startswith("#"):
            assert i > 0 and lines[i - 1].startswith("#"), f"no comment above {line!r}"


def test_largest_envelope_fits_under_max_packet_size():
    from swarmbus.message import DEFAULT_HOP_LIMIT, _MAX_BODY_BYTES, AgentMessage
    from swarmbus.topics import DEFAULT_TOPICS

    # json.dumps escapes this to a 12-byte surrogate pair, and "\x01" to the
    # 6-byte "\u0001", so every field is at its worst case.
    wide = "\U0001F600"
    msg = AgentMessage.create(
        from_="a" * 64,
        to="b" * 64,
        subject=wide * 1024,
        body="\x01" * _MAX_BODY_BYTES,
        content_type=wide * 64,
        priority=wide * 64,
        reply_to=wide * 128,
        in_reply_to=wide * 128,
        hops=DEFAULT_HOP_LIMIT,
        origin=wide * 128,
    )
    payload = msg.to_json().encode("utf-8")
    topic = DEFAULT_TOPICS.inbox("b" * 64).encode("utf-8")

    assert len(payload) > 6 * _MAX_BODY_BYTES
    # 1024 covers the MQTT fixed header, packet id and any v5 properties.
    limit = int(_settings(LIMITS_FILE.read_text())["max_packet_size"])
    assert len(payload) + len(topic) + 1024 <= limit


def test_readme_limits_block_matches_limits_file():
    readme = (ROOT / "README.md").read_text()
    section = readme.split("### Broker limits", 1)[1]
    section = re.split(r"\n## |\n### ", section, maxsplit=1)[0]
    match = re.search(r"```[^\n]*\n(.*?)```", section, re.DOTALL)

    assert match, "no fenced block in the Broker limits section"
    assert _settings(match.group(1)) == _settings(LIMITS_FILE.read_text())
    assert "scripts/mosquitto-limits.conf" in section
    assert LIMITS_DEST in section


def _run_setup(tmp_path, mode):
    """Run the real setup script with fake sudo and tailscale first on PATH and the conf
    directory in tmp_path; return the result and the sudo log lines."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    sudo = bin_dir / "sudo"
    sudo.write_text("#!/bin/sh\nprintf '%s\\n' \"$*\" >> \"$SUDO_LOG\"\ncat > /dev/null\n")
    sudo.chmod(0o755)
    ts = bin_dir / "tailscale"
    ts.write_text("#!/bin/sh\ncase \"$1\" in ip) echo 100.64.0.1 ;; status) echo '{}' ;; esac\n")
    ts.chmod(0o755)

    log = tmp_path / "sudo.log"
    env = {
        **os.environ,
        "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        "SUDO_LOG": str(log),
        "SWARMBUS_MOSQUITTO_CONF_D": str(tmp_path / "conf.d"),
    }
    assert shutil.which("sudo", path=env["PATH"]) == str(sudo)

    result = subprocess.run(
        ["bash", str(ROOT / "scripts" / "setup-mosquitto.sh"), *mode],
        env=env,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    return result, (log.read_text().splitlines() if log.exists() else [])


@pytest.mark.parametrize("mode", [[], ["--tailscale"], ["--tailscale-only"]], ids=["default", "tailscale", "tailscale-only"])
def test_setup_script_installs_limits_before_restart(tmp_path, mode):
    _, lines = _run_setup(tmp_path, mode)
    dest = str(tmp_path / "conf.d" / "swarmbus-limits.conf")
    installs = [i for i, line in enumerate(lines) if "mosquitto-limits.conf" in line and dest in line]
    assert installs, lines
    assert installs[0] < lines.index("systemctl restart mosquitto")


def test_setup_script_leaves_an_existing_limits_file_alone(tmp_path):
    existing = tmp_path / "conf.d" / "swarmbus-limits.conf"
    existing.parent.mkdir()
    existing.write_text("# mine\n")
    result, lines = _run_setup(tmp_path, [])
    assert not [line for line in lines if "mosquitto-limits.conf" in line], lines
    assert "already exists" in result.stdout
    assert existing.read_text() == "# mine\n"
