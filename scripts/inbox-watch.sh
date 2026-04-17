#!/usr/bin/env bash
# scripts/inbox-watch.sh
#
# Cron-driven watcher for an swarmbus inbox. When new entries have appeared
# since the last run, push a short summary to the human operator via
# Telegram. Does NOT wake the agent itself — that's what the reactive
# wake wrappers (examples/claude-code-wake.sh, examples/openclaw-wake.sh)
# are for when wired via the listener daemon's --invoke.
#
# Purpose: give the operator visibility into cross-agent traffic while
# agents are dormant, without paying the cost of reactive wake on every
# message. Complementary to the 4-tier notification protocol in
# docs/notification-patterns.md (this implements a "poll and summarize"
# surface that agents can share).
#
# Runs under cron (typical cadence: every 5 minutes). Idempotent: cursor
# file tracks the last byte offset seen. Inode change / truncation
# resets to 0.
#
# Required env vars:
#   TELEGRAM_BOT_TOKEN       — operator's Telegram bot token, or read
#                              from --token-file (default: ~/.secrets/TELEGRAM_BOT_TOKEN)
#   TELEGRAM_CHAT_ID         — operator's chat id
#
# Required flags (or env vars):
#   --agent-id <name>        — the agent whose inbox is being watched
#   --inbox <path>           — defaults to ~/sync/<agent-id>-inbox.md
#   --state-dir <path>       — defaults to ~/.local/state/<agent-id>
#   --chat-id <id>           — overrides TELEGRAM_CHAT_ID
#   --token-file <path>      — overrides default token file
#   --dry-run                — print what would be sent; no push
#   --reset-cursor           — reset to 0 before running
#
# Crontab example (every 5 minutes, off-minute to avoid synchronized fleets):
#   2,7,12,17,22,27,32,37,42,47,52,57 * * * * \
#     TELEGRAM_BOT_TOKEN=<your-bot-token> TELEGRAM_CHAT_ID=12345 \
#     bash /path/to/inbox-watch.sh --agent-id myagent \
#       >> ~/logs/inbox-watch.log 2>&1
#
# TELEGRAM_BOT_TOKEN can be omitted from the inline cron env **only** if
# ~/.secrets/TELEGRAM_BOT_TOKEN exists — the script falls back to reading
# that file. Without either source the push is silently skipped (logged
# to the agent's state dir only).

set -euo pipefail

AGENT_ID=""
INBOX=""
STATE_DIR=""
CHAT_ID="${TELEGRAM_CHAT_ID:-}"
TOKEN_FILE="$HOME/.secrets/TELEGRAM_BOT_TOKEN"
DRY_RUN=0
RESET_CURSOR=0

while [ $# -gt 0 ]; do
  case "$1" in
    --agent-id) AGENT_ID="$2"; shift 2 ;;
    --inbox) INBOX="$2"; shift 2 ;;
    --state-dir) STATE_DIR="$2"; shift 2 ;;
    --chat-id) CHAT_ID="$2"; shift 2 ;;
    --token-file) TOKEN_FILE="$2"; shift 2 ;;
    --dry-run) DRY_RUN=1; shift ;;
    --reset-cursor) RESET_CURSOR=1; shift ;;
    -h|--help) sed -n '2,40p' "$0"; exit 0 ;;
    *) echo "Unknown arg: $1" >&2; exit 2 ;;
  esac
done

if [ -z "$AGENT_ID" ]; then
  echo "ERROR: --agent-id required" >&2
  exit 2
fi

[ -z "$INBOX" ] && INBOX="$HOME/sync/${AGENT_ID}-inbox.md"
[ -z "$STATE_DIR" ] && STATE_DIR="$HOME/.local/state/${AGENT_ID}"
CURSOR_FILE="$STATE_DIR/inbox-watch.cursor"
LOG="$STATE_DIR/inbox-watch.log"

mkdir -p "$STATE_DIR"

_log() {
  printf '[%s] %s\n' "$(date -Iseconds)" "$*" >> "$LOG"
}

if [ "$RESET_CURSOR" = 1 ]; then
  rm -f "$CURSOR_FILE"
fi

if [ ! -f "$INBOX" ]; then
  _log "inbox missing: $INBOX; nothing to do"
  exit 0
fi

# Cursor format: "<offset> <inode>" — shared with swarmbus tail.
cursor=0
stored_inode=""
if [ -f "$CURSOR_FILE" ]; then
  read -r cursor stored_inode < "$CURSOR_FILE" || true
  cursor="${cursor:-0}"
fi

current_inode=$(stat -c '%i' "$INBOX")
current_size=$(stat -c '%s' "$INBOX")

if [ -n "$stored_inode" ] && [ "$stored_inode" != "$current_inode" ]; then
  _log "inode changed ($stored_inode -> $current_inode); resetting"
  cursor=0
fi
if [ "$current_size" -lt "$cursor" ]; then
  _log "size shrank ($current_size < $cursor); resetting"
  cursor=0
fi

if [ "$current_size" = "$cursor" ]; then
  if [ "$stored_inode" != "$current_inode" ]; then
    printf '%s %s\n' "$cursor" "$current_inode" > "$CURSOR_FILE"
  fi
  exit 0
fi

new_content=$(tail -c +"$((cursor + 1))" "$INBOX")
headers=$(printf '%s' "$new_content" | grep -E '^## \[' || true)

if [ -z "$headers" ]; then
  _log "new bytes but no complete header; skipping"
  exit 0
fi

n_messages=$(printf '%s\n' "$headers" | wc -l | tr -d ' ')

head_block=$(printf '%s\n' "$headers" | head -5)
more=""
if [ "$n_messages" -gt 5 ]; then
  more=$(printf '\n... and %d more' "$((n_messages - 5))")
fi
msg=$(cat <<END
🪶 ${AGENT_ID} inbox: ${n_messages} new from swarmbus peers since last check

${head_block}${more}

Prompt ${AGENT_ID} to process them.
END
)

if [ "$DRY_RUN" = 1 ]; then
  printf '=== DRY RUN — would send ===\n%s\n' "$msg"
  exit 0
fi

if [ -z "$CHAT_ID" ]; then
  _log "no chat id (TELEGRAM_CHAT_ID or --chat-id); skipping push"
  exit 0
fi

if [ -n "${TELEGRAM_BOT_TOKEN:-}" ]; then
  token="$TELEGRAM_BOT_TOKEN"
elif [ -f "$TOKEN_FILE" ]; then
  token=$(cat "$TOKEN_FILE")
else
  # Visible on stderr so cron's redirect captures it — the silent-in-log
  # behavior was a real bug (operator follows minimal doc example, no
  # TELEGRAM_BOT_TOKEN in env and no ~/.secrets file, every tick silently
  # no-ops with the failure only in the agent's state dir).
  msg="[inbox-watch] no TELEGRAM_BOT_TOKEN env var or token file at $TOKEN_FILE; skipping push"
  _log "$msg"
  echo "$msg" >&2
  exit 0
fi

response=$(curl -s -o /tmp/inbox-watch-curl.out -w '%{http_code}' \
  "https://api.telegram.org/bot${token}/sendMessage" \
  --data-urlencode "chat_id=${CHAT_ID}" \
  --data-urlencode "text=${msg}" \
  --data-urlencode "parse_mode=")

if [ "$response" = "200" ]; then
  printf '%s %s\n' "$current_size" "$current_inode" > "$CURSOR_FILE"
  _log "pushed summary (${n_messages} msgs); cursor advanced to $current_size"
else
  _log "push failed (http $response); cursor NOT advanced. body=$(cat /tmp/inbox-watch-curl.out)"
  exit 1
fi
