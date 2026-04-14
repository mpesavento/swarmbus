#!/usr/bin/env bash
# scripts/setup-openclaw-plugin.sh
# Install the using-agentbus skill into an OpenClaw install and print the
# listener-daemon command. OpenClaw doesn't natively register MCP servers,
# so CLI mode is the supported path.
set -euo pipefail

AGENT_ID="${1:-}"
BROKER="${2:-localhost}"

if [ -z "$AGENT_ID" ]; then
  echo "Usage: $0 <agent-id> [broker-host]"
  echo "  Example: $0 wren localhost"
  exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
SKILL_SRC="$REPO_ROOT/skills/using-agentbus"
SKILL_DST="$HOME/.openclaw/skills/using-agentbus"
INBOX="$HOME/sync/${AGENT_ID}-inbox.md"

# 1. Verify OpenClaw is present (soft check — the skill dir is the canonical marker)
if [ ! -d "$HOME/.openclaw/skills" ]; then
  echo "[agentbus] WARNING: $HOME/.openclaw/skills not found — is OpenClaw installed?"
  echo "[agentbus] Creating the directory anyway and installing the skill."
  mkdir -p "$HOME/.openclaw/skills"
fi

# 2. Verify the agentbus CLI resolves
if ! command -v agentbus >/dev/null 2>&1; then
  echo "[agentbus] WARNING: 'agentbus' not on PATH."
  echo "[agentbus] Install with: pip install agentbus   (or 'pip install -e .' from the repo)"
fi

# 3. Install the skill
if [ -d "$SKILL_DST" ]; then
  echo "[agentbus] Skill already at $SKILL_DST — overwriting"
  rm -rf "$SKILL_DST"
fi
cp -r "$SKILL_SRC" "$SKILL_DST"
echo "[agentbus] Installed skill at $SKILL_DST"

# 4. Ensure the inbox dir exists so the daemon can write there
mkdir -p "$(dirname "$INBOX")"

# 5. Print the daemon instructions
cat <<EOF

────────────────────────────────────────────────────────────
Skill installed. OpenClaw runs the agentbus CLI directly —
no MCP sidecar needed. The agent can now use:

  agentbus send   --agent-id $AGENT_ID --to <peer> --subject ... --body ...
  agentbus read   --agent-id $AGENT_ID
  agentbus watch  --agent-id $AGENT_ID --timeout 60
  agentbus list

To *receive* messages reactively, run the listener daemon in a
persistent session (byobu, tmux, or systemd-user):

  agentbus start --agent-id $AGENT_ID --broker $BROKER --inbox $INBOX

The daemon will:
  • announce $AGENT_ID online to the broker (retained presence)
  • subscribe to agents/$AGENT_ID/inbox and agents/broadcast
  • append every received message to $INBOX

Quick byobu start:
  byobu new-session -d -s agentbus-$AGENT_ID \\
    "agentbus start --agent-id $AGENT_ID --broker $BROKER --inbox $INBOX"

Or persist across reboots with systemd-user:
  systemctl --user enable --now agentbus-$AGENT_ID.service
  (service file not installed by this script; see docs/)
────────────────────────────────────────────────────────────
EOF
