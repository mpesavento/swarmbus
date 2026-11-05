#!/usr/bin/env bash
# scripts/setup-cc-plugin.sh
# Register the swarmbus MCP sidecar with Claude Code (`claude mcp add --scope user`, stored in ~/.claude.json) and install the behavioral skill that teaches Claude when/how to use the MCP tools.
# The Claude Code plugin (plugins/swarmbus; `claude plugin install swarmbus --marketplace mpesavento/swarmbus`) is the preferred route and this script stays for now. Using both registers swarmbus twice, so the script refuses to run while the plugin is installed, and `swarmbus doctor` warns when it finds both.
set -euo pipefail

AGENT_ID="${1:-}"
BROKER="${2:-localhost}"

if [ -z "$AGENT_ID" ]; then
  echo "Usage: $0 <agent-id> [broker-host]"
  echo "  Example: $0 planner localhost"
  exit 1
fi

CLAUDE_DIR="${CLAUDE_CONFIG_DIR:-$HOME/.claude}"
PLUGINS_JSON="$CLAUDE_DIR/plugins/installed_plugins.json"
if [ -f "$PLUGINS_JSON" ] && grep -q '"swarmbus@' "$PLUGINS_JSON"; then
  echo "ERROR: the swarmbus Claude Code plugin is installed ($PLUGINS_JSON). It already registers the swarmbus MCP sidecar and skill; running this script too would register swarmbus twice. Use the plugin (the preferred route), or uninstall it and re-run. From swarmbus init, pass --skip-plugin." >&2
  exit 1
fi

if ! command -v claude >/dev/null 2>&1; then
  echo "ERROR: 'claude' not on PATH. Register by hand: claude mcp add --scope user swarmbus -- swarmbus mcp-server --agent-id $AGENT_ID --broker $BROKER" >&2
  exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
SKILL_SRC="$REPO_ROOT/src/swarmbus/skills/using-swarmbus"
SKILL_DST="$CLAUDE_DIR/skills/using-swarmbus"

# `claude mcp add` fails when the name already exists at this scope; a first run has nothing to remove.
claude mcp remove --scope user swarmbus >/dev/null 2>&1 || true
claude mcp add --scope user swarmbus -- swarmbus mcp-server --agent-id "$AGENT_ID" --broker "$BROKER"
echo "[swarmbus] Registered MCP sidecar for agent-id $AGENT_ID (broker $BROKER) at user scope (~/.claude.json)"

# Install the behavioral skill so Claude Code knows when/how to use the tools.
if [ -d "$SKILL_SRC" ]; then
  mkdir -p "$(dirname "$SKILL_DST")"
  rm -rf "$SKILL_DST"
  cp -r "$SKILL_SRC" "$SKILL_DST"
  echo "[swarmbus] Installed skill at $SKILL_DST"
else
  echo "[swarmbus] WARNING: skill source not found at $SKILL_SRC — MCP tools will work but Claude won't have usage guidance"
fi

echo "[swarmbus] Restart Claude Code to pick up the new MCP server + skill."
echo "[swarmbus] Note: the Claude Code plugin is the preferred route (claude plugin install swarmbus --marketplace mpesavento/swarmbus). Do not install it as well: that registers swarmbus twice."
