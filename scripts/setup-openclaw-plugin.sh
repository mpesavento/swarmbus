#!/usr/bin/env bash
# Install the using-swarmbus skill into OpenClaw and print CLI listener setup.
set -euo pipefail

AGENT_ID="${1:-}"
BROKER="${2:-localhost}"
TOPIC_ROOT="${3:-}"
TOPIC_ROOT_GIVEN=""
if [ "$#" -ge 3 ]; then
  TOPIC_ROOT_GIVEN="1"
fi

if [ -z "$AGENT_ID" ]; then
  echo "Usage: $0 <agent-id> [broker-host] [topic-root]"
  echo "  Example: $0 coder localhost fleet"
  exit 1
fi
case "$AGENT_ID" in
  *[!a-z0-9_-]*|broadcast|system)
    echo "ERROR: agent id '$AGENT_ID' must match [a-z0-9_-]{1,64} and may not be broadcast or system." >&2
    exit 2
    ;;
esac
if [ "${#AGENT_ID}" -gt 64 ]; then
  echo "ERROR: agent id '$AGENT_ID' must match [a-z0-9_-]{1,64}." >&2
  exit 2
fi

if [ -n "$TOPIC_ROOT_GIVEN" ]; then
  case "$TOPIC_ROOT" in
    *[!A-Za-z0-9._/-]*)
      echo "ERROR: topic root '$TOPIC_ROOT' cannot be rendered safely into a systemd unit." >&2
      echo "       Use letters, digits, '.', '_', '/' and '-'." >&2
      exit 2
      ;;
  esac
fi

normalize_root() {
  local root="$1"
  while [ "${root#/}" != "$root" ]; do
    root="${root#/}"
  done
  while [ "${root%/}" != "$root" ]; do
    root="${root%/}"
  done
  printf '%s' "$root"
}

NORMALIZED_ROOT="$(normalize_root "$TOPIC_ROOT")"
case "$NORMALIZED_ROOT" in
  *//*)
    echo "ERROR: topic root '$TOPIC_ROOT' contains an empty segment ('//')." >&2
    echo "       Remove the repeated slash." >&2
    exit 2
    ;;
esac

ROOT_ARG=""
if [ -n "$TOPIC_ROOT_GIVEN" ]; then
  ROOT_ARG=" --topic-root=$NORMALIZED_ROOT"
fi

UNIT_SCOPE="$AGENT_ID"
if [ -n "$NORMALIZED_ROOT" ]; then
  UNIT_SCOPE="${AGENT_ID}:${NORMALIZED_ROOT//\//:2f}"
fi
UNIT="swarmbus-${UNIT_SCOPE}.service"
BYOBU_SESSION="swarmbus-${AGENT_ID}"
if [ -n "$NORMALIZED_ROOT" ]; then
  ROOT_HEX="$(printf '%s' "$NORMALIZED_ROOT" | od -An -tx1 | tr -d ' \n')"
  BYOBU_SESSION="${BYOBU_SESSION}-root-${ROOT_HEX}"
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
SKILL_SRC="$REPO_ROOT/src/swarmbus/skills/using-swarmbus"
SKILL_DST="$HOME/.openclaw/skills/using-swarmbus"
INBOX="$HOME/sync/${UNIT_SCOPE}-inbox.md"

if [ ! -d "$HOME/.openclaw/skills" ]; then
  echo "[swarmbus] ERROR: $HOME/.openclaw/skills does not exist."
  echo "[swarmbus] OpenClaw does not appear to be installed for this user."
  echo "[swarmbus] Install OpenClaw first, then re-run this script."
  exit 1
fi

if ! command -v swarmbus >/dev/null 2>&1; then
  echo "[swarmbus] WARNING: 'swarmbus' not on PATH."
  echo "[swarmbus] Install with: pip install swarmbus   (or 'pip install -e .' from the repo)"
fi

if [ -d "$SKILL_DST" ]; then
  echo "[swarmbus] Skill already at $SKILL_DST — overwriting"
  rm -rf "$SKILL_DST"
fi
cp -r "$SKILL_SRC" "$SKILL_DST"
echo "[swarmbus] Installed skill at $SKILL_DST"

mkdir -p "$(dirname "$INBOX")"

cat <<EOF

────────────────────────────────────────────────────────────
Skill installed. OpenClaw runs the swarmbus CLI directly —
no MCP sidecar needed. The agent can now use:

  swarmbus send   --agent-id $AGENT_ID --to <peer> --subject ... --body ...$ROOT_ARG
  swarmbus read   --agent-id $AGENT_ID$ROOT_ARG
  swarmbus watch  --agent-id $AGENT_ID --timeout 60$ROOT_ARG
  swarmbus list$ROOT_ARG

To *receive* messages reactively, run the listener daemon in a
persistent session (byobu, tmux, or systemd-user):

  swarmbus start --agent-id $AGENT_ID --broker $BROKER --inbox $INBOX$ROOT_ARG

The daemon will:
  • announce $AGENT_ID online to the broker (retained presence)
  • subscribe to $AGENT_ID's inbox topic and agents/broadcast
  • append every received message to $INBOX

Quick byobu start:
  byobu new-session -d -s $BYOBU_SESSION \
    "swarmbus start --agent-id $AGENT_ID --broker $BROKER --inbox $INBOX$ROOT_ARG"

Or persist across reboots with systemd-user:
  systemctl --user enable --now $UNIT
  (service file not installed by this script; see docs/)
────────────────────────────────────────────────────────────
EOF
