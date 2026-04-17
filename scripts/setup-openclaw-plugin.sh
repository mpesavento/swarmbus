#!/usr/bin/env bash
# scripts/setup-openclaw-plugin.sh
# Install the using-swarmbus skill into an OpenClaw install and print the
# listener-daemon command. OpenClaw doesn't natively register MCP servers,
# so CLI mode is the supported path.
set -euo pipefail

AGENT_ID="${1:-}"
BROKER="${2:-localhost}"

if [ -z "$AGENT_ID" ]; then
  echo "Usage: $0 <agent-id> [broker-host]"
  echo "  Example: $0 coder localhost"
  exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
SKILL_SRC="$REPO_ROOT/skills/using-swarmbus 
SKILL_DST="$HOME/.openclaw/skills/using-swarmbus 
INBOX="$HOME/sync/${AGENT_ID}-inbox.md"

# 1. Verify OpenClaw is present. The skills directory is the canonical marker.
# Bail here rather than silently creating a fake install — installing the skill
# into a non-existent OpenClaw is worse than erroring out.
if [ ! -d "$HOME/.openclaw/skills" ]; then
  echo "[swarmbus] ERROR: $HOME/.openclaw/skills does not exist."
  echo "[swarmbus] OpenClaw does not appear to be installed for this user."
  echo "[swarmbus] Install OpenClaw first, then re-run this script."
  exit 1
fi

# 2. Verify the swarmbus CLI resolves
if ! command -v swarmbus >/dev/null 2>&1; then
  echo "[swarmbus] WARNING: 'swarmbus' not on PATH."
  echo "[swarmbus] Install with: pip install swarmbus   (or 'pip install -e .' from the repo)"
fi

# 3. Install the skill
if [ -d "$SKILL_DST" ]; then
  echo "[swarmbus] Skill already at $SKILL_DST — overwriting"
  rm -rf "$SKILL_DST"
fi
cp -r "$SKILL_SRC" "$SKILL_DST"
echo "[swarmbus] Installed skill at $SKILL_DST"

# 4. Ensure the inbox dir exists so the daemon can write there
mkdir -p "$(dirname "$INBOX")"

# 5. Print the daemon instructions
cat <<EOF

────────────────────────────────────────────────────────────
Skill installed. OpenClaw runs the swarmbus CLI directly —
no MCP sidecar needed. The agent can now use:

  swarmbus send   --agent-id $AGENT_ID --to <peer> --subject ... --body ...
  swarmbus read   --agent-id $AGENT_ID
  swarmbus watch  --agent-id $AGENT_ID --timeout 60
  swarmbus list

To *receive* messages reactively, run the listener daemon in a
persistent session (byobu, tmux, or systemd-user):

  swarmbus start --agent-id $AGENT_ID --broker $BROKER --inbox $INBOX

The daemon will:
  • announce $AGENT_ID online to the broker (retained presence)
  • subscribe to agents/$AGENT_ID/inbox and agents/broadcast
  • append every received message to $INBOX

Quick byobu start:
  byobu new-session -d -s swarmbus-$AGENT_ID \\
    "swarmbus start --agent-id $AGENT_ID --broker $BROKER --inbox $INBOX"

Or persist across reboots with systemd-user:
  systemctl --user enable --now swarmbus-$AGENT_ID.service
  (service file not installed by this script; see docs/)
────────────────────────────────────────────────────────────
EOF
