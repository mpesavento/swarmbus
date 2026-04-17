#!/usr/bin/env bash
# scripts/install-systemd.sh
# Render systemd/swarmbus-agent.service.template for <agent-id> and
# install it at ~/.config/systemd/user/swarmbus-<agent-id>.service.
#
# Usage:
#   install-systemd.sh <agent-id> [--invoke <path>] [--broker <host>] [--inbox <path>]
#
# Defaults:
#   --broker localhost
#   --inbox  ~/sync/<agent-id>-inbox.md
#   --invoke (none — bare file-bridge daemon; no reactive wake)
#
# After install, the script runs `systemctl --user daemon-reload`, enables
# the unit, starts it, and runs `swarmbus doctor` to verify.

set -euo pipefail

AGENT_ID="${1:?Usage: install-systemd.sh <agent-id> [--invoke <wake-wrapper> [args]] [--broker <h>] [--inbox <p>]}"
shift

BROKER="localhost"
INBOX="$HOME/sync/${AGENT_ID}-inbox.md"
INVOKE=""

while [ $# -gt 0 ]; do
  case "$1" in
    --broker) BROKER="$2"; shift 2 ;;
    --inbox) INBOX="$2"; shift 2 ;;
    --invoke) INVOKE="$2"; shift 2 ;;
    *) echo "Unknown arg: $1" >&2; exit 2 ;;
  esac
done

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
TEMPLATE="$REPO_ROOT/systemd/swarmbus-agent.service.template"
UNIT_DIR="$HOME/.config/systemd/user"
UNIT_PATH="$UNIT_DIR/swarmbus-${AGENT_ID}.service"

if ! command -v swarmbus >/dev/null 2>&1; then
  echo "ERROR: 'swarmbus' not on PATH. Install it first:"
  echo "  pip install swarmbus          # or"
  echo "  pip install -e ${REPO_ROOT}   # editable install"
  exit 1
fi
SWARMBUS_PATH=$(command -v swarmbus)

mkdir -p "$UNIT_DIR" "$HOME/logs" "$(dirname "$INBOX")"

AGENT_ID_UPPER=$(printf '%s' "$AGENT_ID" | tr 'a-z-' 'A-Z_')

# If invoke is empty, strip the --invoke line entirely from the template.
# Otherwise substitute.
if [ -z "$INVOKE" ]; then
  sed -e "s|@AGENT_ID@|$AGENT_ID|g" \
      -e "s|@AGENT_ID_UPPER@|$AGENT_ID_UPPER|g" \
      -e "s|@HOME@|$HOME|g" \
      -e "s|@BROKER@|$BROKER|g" \
      -e "s|@INBOX@|$INBOX|g" \
      -e "s|@SWARMBUS_PATH@|$SWARMBUS_PATH|g" \
      -e '/--invoke/d' \
      -e 's| \\$||' \
      "$TEMPLATE" > "$UNIT_PATH"
else
  sed -e "s|@AGENT_ID@|$AGENT_ID|g" \
      -e "s|@AGENT_ID_UPPER@|$AGENT_ID_UPPER|g" \
      -e "s|@HOME@|$HOME|g" \
      -e "s|@BROKER@|$BROKER|g" \
      -e "s|@INBOX@|$INBOX|g" \
      -e "s|@INVOKE@|$INVOKE|g" \
      -e "s|@SWARMBUS_PATH@|$SWARMBUS_PATH|g" \
      "$TEMPLATE" > "$UNIT_PATH"
fi

chmod 644 "$UNIT_PATH"
echo "[install-systemd] wrote $UNIT_PATH"

# Bring up systemd user bus if not already accessible (running from a
# non-interactive session without a dbus session).
export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"
export DBUS_SESSION_BUS_ADDRESS="${DBUS_SESSION_BUS_ADDRESS:-unix:path=/run/user/$(id -u)/bus}"

systemctl --user daemon-reload
systemctl --user enable "swarmbus-${AGENT_ID}.service"
systemctl --user restart "swarmbus-${AGENT_ID}.service"

sleep 2
echo ""
echo "[install-systemd] status:"
systemctl --user --no-pager --lines=3 status "swarmbus-${AGENT_ID}.service" | head -8
echo ""
echo "[install-systemd] running doctor:"
swarmbus doctor --agent-id "$AGENT_ID" --broker "$BROKER" || true

echo ""
echo "[install-systemd] next step: loginctl enable-linger \$(whoami)"
echo "                 so the service survives logout / reboot."
