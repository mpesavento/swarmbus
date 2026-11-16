#!/usr/bin/env bash
# scripts/install-systemd.sh
# Render systemd/swarmbus-agent.service.template for <agent-id> and
# install it at ~/.config/systemd/user/swarmbus-<agent-id>.service.
#
# Usage:
#   install-systemd.sh <agent-id> [--invoke <path>] [--broker <host>] [--inbox <path>]
#                      [--topic-root <root>]
#
# Defaults:
#   --broker localhost
#   --inbox  ~/sync/<agent-id>-inbox.md
#   --invoke (none — bare file-bridge daemon; no reactive wake)
#   --topic-root (not given — the daemon reads [broker] topic_root from its
#                config file; given, including given empty, pins the unit to
#                that root)
#
# After install, the script runs `systemctl --user daemon-reload`, enables
# the unit, starts it, and runs `swarmbus doctor` to verify.

set -euo pipefail

AGENT_ID="${1:?Usage: install-systemd.sh <agent-id> [--invoke <wake-wrapper> [args]] [--broker <h>] [--inbox <p>] [--topic-root <root>] [--username <u>] [--password <p>] [--ca-cert <path>] [--client-cert <path>] [--client-key <path>] [--tls]}"
shift

BROKER="localhost"
INBOX="$HOME/sync/${AGENT_ID}-inbox.md"
INVOKE=""
TOPIC_ROOT=""
TOPIC_ROOT_GIVEN=""
BROKER_USERNAME=""
BROKER_PASSWORD=""
BROKER_CA_CERT=""
BROKER_CLIENT_CERT=""
BROKER_CLIENT_KEY=""
BROKER_TLS=""

while [ $# -gt 0 ]; do
  case "$1" in
    --broker) BROKER="$2"; shift 2 ;;
    --inbox) INBOX="$2"; shift 2 ;;
    --invoke) INVOKE="$2"; shift 2 ;;
    --topic-root) TOPIC_ROOT="$2"; TOPIC_ROOT_GIVEN="1"; shift 2 ;;
    --username) BROKER_USERNAME="$2"; shift 2 ;;
    --password) BROKER_PASSWORD="$2"; shift 2 ;;
    --ca-cert) BROKER_CA_CERT="$2"; shift 2 ;;
    --client-cert) BROKER_CLIENT_CERT="$2"; shift 2 ;;
    --client-key) BROKER_CLIENT_KEY="$2"; shift 2 ;;
    --tls) BROKER_TLS="1"; shift 1 ;;
    *) echo "Unknown arg: $1" >&2; exit 2 ;;
  esac
done

# The root is rendered into a sed replacement and into a systemd ExecStart
# line, so refuse one that would corrupt either rather than writing a broken
# unit. Checked before anything is created.
if [ -n "$TOPIC_ROOT_GIVEN" ]; then
  case "$TOPIC_ROOT" in
    *[!A-Za-z0-9._/-]*)
      echo "ERROR: topic root '$TOPIC_ROOT' has characters this script cannot render safely into a systemd unit." >&2
      echo "       Use letters, digits, '.', '_', '/' and '-', or drop --topic-root and set" >&2
      echo "       [broker] topic_root in ~/.swarmbus/config.ini instead." >&2
      exit 2
      ;;
  esac
fi

TOPIC_ROOT_ARG=""
if [ -n "$TOPIC_ROOT_GIVEN" ]; then
  TOPIC_ROOT_ARG=" --topic-root=$TOPIC_ROOT"
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
TEMPLATE="$REPO_ROOT/systemd/swarmbus-agent.service.template"
UNIT_DIR="$HOME/.config/systemd/user"
UNIT_PATH="$UNIT_DIR/swarmbus-${AGENT_ID}.service"

# A host runs one unit per agent id, so an existing unit's --topic-root pin
# is what decides which swarm its daemon belongs to. Read the pin before
# anything is created: replacing it, or dropping it so the config file
# decides, would move a running daemon to another swarm unannounced.
if [ -f "$UNIT_PATH" ]; then
  EXEC_LINE=$(grep -m1 '^ExecStart=' "$UNIT_PATH" || true)
  EXISTING_PIN_GIVEN=""
  EXISTING_ROOT=""
  case "$EXEC_LINE" in
    *" --topic-root="*)
      EXISTING_PIN_GIVEN="1"
      # The root charset stops at a space, so the pin's value is the text
      # after the token up to the next space or the end of the line.
      EXISTING_ROOT="${EXEC_LINE#* --topic-root=}"
      EXISTING_ROOT="${EXISTING_ROOT%% *}"
      ;;
  esac

  if [ -n "$EXISTING_PIN_GIVEN" ] && { [ -z "$TOPIC_ROOT_GIVEN" ] || [ "$EXISTING_ROOT" != "$TOPIC_ROOT" ]; }; then
    if [ -n "$EXISTING_ROOT" ]; then
      EXISTING_DISPLAY="'$EXISTING_ROOT'"
      KEEP_ARG="--topic-root $EXISTING_ROOT"
    else
      EXISTING_DISPLAY="(unrooted)"
      KEEP_ARG="--topic-root ''"
    fi
    if [ -z "$TOPIC_ROOT_GIVEN" ]; then
      ASKED="no --topic-root, so the config file's root"
    elif [ -n "$TOPIC_ROOT" ]; then
      ASKED="topic root '$TOPIC_ROOT'"
    else
      ASKED="(unrooted)"
    fi
    echo "ERROR: $UNIT_PATH is installed for topic root $EXISTING_DISPLAY." >&2
    echo "       This run asked for: $ASKED." >&2
    echo "       A host runs one systemd unit per agent id, so writing this unit would move" >&2
    echo "       the swarmbus-${AGENT_ID} daemon to a different swarm. Nothing was changed." >&2
    echo "       To keep the unit as it is, pass $KEEP_ARG." >&2
    echo "       To replace it, remove the existing unit first:" >&2
    echo "         systemctl --user disable --now swarmbus-${AGENT_ID}.service" >&2
    echo "         rm $UNIT_PATH" >&2
    echo "       To run both swarms here, give the second swarm's agent a different agent id." >&2
    exit 2
  fi
fi

if ! command -v swarmbus >/dev/null 2>&1; then
  echo "ERROR: 'swarmbus' not on PATH. Install it first:"
  echo "  pip install swarmbus          # or"
  echo "  pip install -e ${REPO_ROOT}   # editable install"
  exit 1
fi
SWARMBUS_PATH=$(command -v swarmbus)

mkdir -p "$UNIT_DIR" "$HOME/logs" "$(dirname "$INBOX")"

AGENT_ID_UPPER=$(printf '%s' "$AGENT_ID" | tr 'a-z-' 'A-Z_')

# If invoke is empty, drop the --invoke argument from the one-line ExecStart. Otherwise substitute.
if [ -z "$INVOKE" ]; then
  sed -e "s|@AGENT_ID@|$AGENT_ID|g" \
      -e "s|@AGENT_ID_UPPER@|$AGENT_ID_UPPER|g" \
      -e "s|@HOME@|$HOME|g" \
      -e "s|@BROKER@|$BROKER|g" \
      -e "s|@INBOX@|$INBOX|g" \
      -e "s|@TOPIC_ROOT_ARG@|$TOPIC_ROOT_ARG|g" \
      -e "s|@SWARMBUS_PATH@|$SWARMBUS_PATH|g" \
      -e 's| --invoke "@INVOKE@"||' \
      "$TEMPLATE" > "$UNIT_PATH"
else
  sed -e "s|@AGENT_ID@|$AGENT_ID|g" \
      -e "s|@AGENT_ID_UPPER@|$AGENT_ID_UPPER|g" \
      -e "s|@HOME@|$HOME|g" \
      -e "s|@BROKER@|$BROKER|g" \
      -e "s|@INBOX@|$INBOX|g" \
      -e "s|@TOPIC_ROOT_ARG@|$TOPIC_ROOT_ARG|g" \
      -e "s|@INVOKE@|$INVOKE|g" \
      -e "s|@SWARMBUS_PATH@|$SWARMBUS_PATH|g" \
      "$TEMPLATE" > "$UNIT_PATH"
fi

chmod 644 "$UNIT_PATH"
echo "[install-systemd] wrote $UNIT_PATH"

# Write a systemd drop-in for SWARMBUS_BROKER_* credentials when any auth
# arg was supplied. Drop-ins live alongside the main unit and are merged
# at daemon-reload time, so the main template stays clean and a later
# re-render of the template doesn't clobber the credentials. The file is
# written 0600 because it contains plaintext secrets.
DROPIN_DIR="$UNIT_DIR/swarmbus-${AGENT_ID}.service.d"
DROPIN_PATH="$DROPIN_DIR/auth.conf"

if [ -n "$BROKER_USERNAME$BROKER_PASSWORD$BROKER_CA_CERT$BROKER_CLIENT_CERT$BROKER_CLIENT_KEY$BROKER_TLS" ]; then
  mkdir -p "$DROPIN_DIR"
  {
    echo "# Generated by install-systemd.sh — broker credentials for $AGENT_ID."
    echo "# Hand-edit with \`systemctl --user edit swarmbus-${AGENT_ID}.service\`"
    echo "# rather than this file directly; that prompt-edits an override.conf"
    echo "# in the same directory and survives re-runs of install-systemd.sh."
    echo "[Service]"
    [ -n "$BROKER_USERNAME"    ] && echo "Environment=SWARMBUS_BROKER_USERNAME=$BROKER_USERNAME"
    [ -n "$BROKER_PASSWORD"    ] && echo "Environment=SWARMBUS_BROKER_PASSWORD=$BROKER_PASSWORD"
    [ -n "$BROKER_CA_CERT"     ] && echo "Environment=SWARMBUS_BROKER_CA_CERT=$BROKER_CA_CERT"
    [ -n "$BROKER_CLIENT_CERT" ] && echo "Environment=SWARMBUS_BROKER_CLIENT_CERT=$BROKER_CLIENT_CERT"
    [ -n "$BROKER_CLIENT_KEY"  ] && echo "Environment=SWARMBUS_BROKER_CLIENT_KEY=$BROKER_CLIENT_KEY"
    [ -n "$BROKER_TLS"         ] && echo "Environment=SWARMBUS_BROKER_TLS=$BROKER_TLS"
  } > "$DROPIN_PATH"
  chmod 600 "$DROPIN_PATH"
  echo "[install-systemd] wrote $DROPIN_PATH (mode 600)"
elif [ -f "$DROPIN_PATH" ]; then
  # Re-run with no auth args: surface the stale drop-in instead of silently
  # leaving previous secrets in place.
  echo "[install-systemd] note: existing $DROPIN_PATH not modified."
  echo "                 delete it if you intend to drop credentials."
fi

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
DOCTOR_ARGS=(--agent-id "$AGENT_ID" --broker "$BROKER")
if [ -n "$TOPIC_ROOT_GIVEN" ]; then
  DOCTOR_ARGS+=("--topic-root=$TOPIC_ROOT")
fi
swarmbus doctor "${DOCTOR_ARGS[@]}" || true

echo ""
echo "[install-systemd] next step: loginctl enable-linger \$(whoami)"
echo "                 so the service survives logout / reboot."
