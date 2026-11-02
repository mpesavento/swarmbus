"""Pin the Claude Code plugin (plugins/swarmbus) and its marketplace (.claude-plugin/marketplace.json)."""
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MARKETPLACE = ROOT / ".claude-plugin" / "marketplace.json"
PLUGIN_DIR = ROOT / "plugins" / "swarmbus"
MANIFEST = PLUGIN_DIR / ".claude-plugin" / "plugin.json"
MCP_CONFIG = PLUGIN_DIR / ".mcp.json"
SKILL_SOURCE = ROOT / "src" / "swarmbus" / "skills" / "using-swarmbus" / "SKILL.md"
SKILL_COPY = PLUGIN_DIR / "skills" / "using-swarmbus" / "SKILL.md"


def _json(path):
    return json.loads(path.read_text())


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
    assert server["command"] == "swarmbus"
    assert server["args"][0] == "mcp-server"
    used = set(re.findall(r"\$\{user_config\.(\w+)\}", json.dumps(server)))
    assert used == declared


def test_plugin_folder_holds_only_text_files_and_no_symlinks():
    for path in PLUGIN_DIR.rglob("*"):
        assert not path.is_symlink(), path
        if path.is_file():
            assert path.suffix in {".md", ".json"}, path


def test_plugin_readme_is_long_enough_for_the_directory():
    readme = (PLUGIN_DIR / "README.md").read_text()
    prose = re.sub(r"```.*?```", "", readme, flags=re.S)
    assert len(prose.split()) >= 40
