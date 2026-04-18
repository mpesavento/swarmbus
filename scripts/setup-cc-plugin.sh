#!/usr/bin/env bash
# scripts/setup-cc-plugin.sh
# Register swarmbus MCP sidecar in Claude Code settings.json and install the
# behavioral skill that teaches Claude when/how to use the MCP tools.
set -euo pipefail

AGENT_ID="${1:-}"
BROKER="${2:-localhost}"

if [ -z "$AGENT_ID" ]; then
  echo "Usage: $0 <agent-id> [broker-host]"
  echo "  Example: $0 planner localhost"
  exit 1
fi

SETTINGS_FILE="${HOME}/.claude/settings.json"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
SKILL_SRC="$REPO_ROOT/src/swarmbus/skills/using-swarmbus"
SKILL_DST="$HOME/.claude/skills/using-swarmbus"

if [ ! -f "$SETTINGS_FILE" ]; then
  mkdir -p "$(dirname "$SETTINGS_FILE")"
  echo '{}' > "$SETTINGS_FILE"
fi

python3 - "$SETTINGS_FILE" "$AGENT_ID" "$BROKER" <<'EOF'
import json, sys

settings_path, agent_id, broker = sys.argv[1], sys.argv[2], sys.argv[3]

with open(settings_path) as f:
    settings = json.load(f)

settings.setdefault("mcpServers", {})
settings["mcpServers"]["swarmbus"] = {
    "command": "swarmbus",
    "args": ["mcp-server", "--agent-id", agent_id, "--broker", broker]
}

with open(settings_path, "w") as f:
    json.dump(settings, f, indent=2)

print(f"[swarmbus] Registered MCP sidecar in {settings_path}")
print(f"[swarmbus] agent-id: {agent_id}, broker: {broker}")
EOF

# Install the behavioral skill so Claude Code knows when/how to use the tools.
if [ -d "$SKILL_SRC" ]; then
  mkdir -p "$(dirname "$SKILL_DST")"
  cp -r "$SKILL_SRC" "$SKILL_DST"
  echo "[swarmbus] Installed skill at $SKILL_DST"
else
  echo "[swarmbus] WARNING: skill source not found at $SKILL_SRC — MCP tools will work but Claude won't have usage guidance"
fi

echo "[swarmbus] Restart Claude Code to pick up the new MCP server + skill."
