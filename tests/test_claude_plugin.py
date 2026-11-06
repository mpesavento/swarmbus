"""Pin the Claude Code plugin (plugins/swarmbus) and its marketplace (.claude-plugin/marketplace.json)."""
import json
import re
import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MARKETPLACE = ROOT / ".claude-plugin" / "marketplace.json"
PLUGIN_DIR = ROOT / "plugins" / "swarmbus"
MANIFEST = PLUGIN_DIR / ".claude-plugin" / "plugin.json"
MCP_CONFIG = PLUGIN_DIR / ".mcp.json"
SKILL_SOURCE = ROOT / "src" / "swarmbus" / "skills" / "using-swarmbus" / "SKILL.md"
SKILL_COPY = PLUGIN_DIR / "skills" / "using-swarmbus" / "SKILL.md"
CREDENTIALS = {
    "broker_username": "SWARMBUS_BROKER_USERNAME",
    "broker_password": "SWARMBUS_BROKER_PASSWORD",
}
INSTALL = 'uv tool install "swarmbus-py[mcp]"'
LAUNCHER = (
    "command -v swarmbus >/dev/null 2>&1 || { echo 'swarmbus is not installed (no swarmbus command on PATH). "
    "Install it with: uv tool install \"swarmbus-py[mcp]\" and restart Claude Code.' >&2; exit 127; }; "
    'exec swarmbus "$@"'
)


def _json(path):
    return json.loads(path.read_text())


def _sidecar_argv(server):
    # .mcp.json is an operator hand edit, so until it lands the plugin runs swarmbus directly (#85)
    if server["command"] == "swarmbus":
        return [server["command"], *server["args"]]
    assert server["command"] == "sh"
    assert server["args"][:2] == ["-c", LAUNCHER]
    return server["args"][2:]


def test_marketplace_lists_the_plugin_under_its_manifest_name():
    market = _json(MARKETPLACE)
    manifest = _json(MANIFEST)
    [entry] = market["plugins"]
    assert entry["name"] == manifest["name"] == "swarmbus"
    assert "version" not in entry  # plugin.json carries it; both would conflict


def test_marketplace_serves_the_plugin_from_main():
    # The catalog is read from the default branch (dev, the working branch);
    # the ref keeps users on released code.
    [entry] = _json(MARKETPLACE)["plugins"]
    source = entry["source"]
    assert source["source"] == "git-subdir"
    assert source["url"] == "https://github.com/mpesavento/swarmbus.git"
    assert source["ref"] == "main"
    assert (ROOT / source["path"]).resolve() == PLUGIN_DIR


def test_plugin_version_matches_the_package():
    pyproject = (ROOT / "pyproject.toml").read_text()
    version = re.search(r'^version\s*=\s*"([^"]+)"', pyproject, re.M).group(1)
    assert _json(MANIFEST)["version"] == version


def test_plugin_skill_is_a_copy_of_the_packaged_skill():
    # The directory refuses symlinks, so the plugin carries a real copy. `make plugin-skill` refreshes it.
    assert not SKILL_COPY.is_symlink()
    assert SKILL_COPY.read_text() == SKILL_SOURCE.read_text()


def test_mcp_server_uses_only_declared_user_config():
    declared = set(_json(MANIFEST)["userConfig"])
    [server] = _json(MCP_CONFIG)["mcpServers"].values()
    argv = _sidecar_argv(server)
    assert argv[0] == "swarmbus"
    assert argv[1] == "mcp-server"
    used = set(re.findall(r"\$\{user_config\.(\w+)\}", json.dumps(server)))
    assert used == declared
    for key, var in CREDENTIALS.items():
        assert server["env"][var] == "${user_config.%s}" % key


def test_plugin_settings_and_readme_name_the_install_command():
    assert INSTALL in _json(MANIFEST)["userConfig"]["agent_id"]["description"]
    assert INSTALL in (PLUGIN_DIR / "README.md").read_text()


def test_launcher_names_the_install_command_when_swarmbus_is_missing(tmp_path):
    assert "${" not in LAUNCHER  # Claude Code expands ${...} in .mcp.json
    sh = shutil.which("sh")
    empty = tmp_path / "empty"
    empty.mkdir()
    args = ["swarmbus", "mcp-server", "--agent-id", "planner"]
    proc = subprocess.run([sh, "-c", LAUNCHER, *args], env={"PATH": str(empty)}, capture_output=True, text=True)
    assert proc.returncode == 127
    assert INSTALL in proc.stderr
    assert proc.stdout == ""  # stdout is the MCP channel

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    calls = tmp_path / "calls"
    stub = bin_dir / "swarmbus"
    stub.write_text(f"#!/bin/sh\nprintf '%s\\n' \"$@\" > {calls}\n")
    stub.chmod(0o755)
    args = ["swarmbus", "mcp-server", "--agent-id", "planner", "--broker", "", "--presence"]
    proc = subprocess.run([sh, "-c", LAUNCHER, *args], env={"PATH": str(bin_dir)}, capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
    assert calls.read_text().split("\n")[:-1] == ["mcp-server", "--agent-id", "planner", "--broker", "", "--presence"]


def test_plugin_broker_settings_default_to_empty():
    config = _json(MANIFEST)["userConfig"]
    for key in ("broker", "port"):
        assert config[key]["type"] == "string", key
        assert config[key]["default"] == "", key
        assert not config[key].get("required", False), key
    [server] = _json(MCP_CONFIG)["mcpServers"].values()
    args = server["args"]
    assert args[args.index("--broker") + 1] == "${user_config.broker}"
    assert args[args.index("--port") + 1] == "${user_config.port}"


def test_plugin_takes_optional_sensitive_broker_credentials():
    config = _json(MANIFEST)["userConfig"]
    for key in CREDENTIALS:
        assert config[key]["type"] == "string", key
        assert config[key]["sensitive"] is True, key
        assert config[key]["default"] == "", key
        assert not config[key].get("required", False), key


def test_plugin_starts_the_sidecar_with_presence_but_not_persistent():
    # agent_state refuses without --presence. --persistent gives every session on an agent id the
    # same MQTT client id, so two sessions would evict each other; it waits for #88.
    [server] = _json(MCP_CONFIG)["mcpServers"].values()
    assert "--presence" in server["args"]
    assert "--persistent" not in server["args"]


def test_plugin_advertises_agent_state_not_list_agents():
    for path in (MANIFEST, MARKETPLACE, PLUGIN_DIR / "README.md"):
        text = path.read_text()
        assert "list_agents" not in text, path
        assert "agent_state" in text, path


def test_plugin_folder_holds_only_text_files_and_no_symlinks():
    for path in PLUGIN_DIR.rglob("*"):
        assert not path.is_symlink(), path
        if path.is_file():
            assert path.suffix in {".md", ".json"}, path


def test_plugin_readme_is_long_enough_for_the_directory():
    readme = (PLUGIN_DIR / "README.md").read_text()
    prose = re.sub(r"```.*?```", "", readme, flags=re.S)
    assert len(prose.split()) >= 40
