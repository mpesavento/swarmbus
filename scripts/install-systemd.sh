#!/usr/bin/env bash
# scripts/install-systemd.sh
# Render systemd/swarmbus-agent.service.template for <agent-id> and install
# it at ~/.config/systemd/user/swarmbus-<agent-id>.service, or, under a
# topic root, swarmbus-<agent-id>:<root>.service — one unit per agent id per
# root, so two swarms can run the same agent id on one host.
#
# Usage:
#   install-systemd.sh <agent-id> [--invoke <path>] [--broker <host>] [--inbox <path>]
#                      [--topic-root <root>]
#
# Defaults:
#   --broker localhost
#   --inbox  ~/sync/<agent-id>-inbox.md, or ~/sync/<agent-id>:<root>-inbox.md
#            under a topic root — one inbox per agent id per root, so two
#            daemons never write one file. An --inbox given here is used as
#            given. The unit's log is split the same way.
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

case "$AGENT_ID" in
  ""|*[!a-z0-9_-]*|broadcast|system)
    echo "ERROR: agent id '$AGENT_ID' must match [a-z0-9_-]{1,64} and may not be broadcast or system." >&2
    exit 2
    ;;
esac
if [ "${#AGENT_ID}" -gt 64 ]; then
  echo "ERROR: agent id '$AGENT_ID' must match [a-z0-9_-]{1,64}." >&2
  exit 2
fi

BROKER="localhost"
# Defaulted after the root is normalised, because the default carries the
# root. An --inbox the operator passes is used exactly as given.
INBOX=""
INBOX_GIVEN=""
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
    --inbox) INBOX="$2"; INBOX_GIVEN="1"; shift 2 ;;
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
    echo "       Remove the repeated slash; nothing was changed." >&2
    exit 2
    ;;
esac

TOPIC_ROOT_ARG=""
if [ -n "$TOPIC_ROOT_GIVEN" ]; then
  TOPIC_ROOT_ARG=" --topic-root=$NORMALIZED_ROOT"
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
TEMPLATE="$REPO_ROOT/systemd/swarmbus-agent.service.template"
UNIT_DIR="$HOME/.config/systemd/user"

# One root-specific token keys the unit, log and default inbox. With the
# installer charset, only '/' needs escaping.
UNIT_SCOPE="$AGENT_ID"
if [ -n "$NORMALIZED_ROOT" ]; then
  UNIT_SCOPE="${AGENT_ID}:${NORMALIZED_ROOT//\//:2f}"
fi
UNIT_WITHOUT_ROOT="swarmbus-${AGENT_ID}.service"
UNIT="swarmbus-${UNIT_SCOPE}.service"
UNIT_PATH="$UNIT_DIR/$UNIT"
PATH_WITHOUT_ROOT="$UNIT_DIR/$UNIT_WITHOUT_ROOT"

# Refuse overlong names before creating directories; this charset is ASCII.
if [ "${#UNIT}" -gt 255 ]; then
  echo "ERROR: unit name '$UNIT' is ${#UNIT} bytes, past systemd's maximum unit name length of 255 bytes." >&2
  echo "       Shorten the agent id or the topic root. Nothing was changed." >&2
  exit 2
fi

if [ -z "$INBOX_GIVEN" ]; then
  INBOX="$HOME/sync/${UNIT_SCOPE}-inbox.md"
fi

PIN_GIVEN=""
PIN_ROOT=""
read_pin() {
  PIN_GIVEN=""
  PIN_ROOT=""
  EXEC_LINE=$(grep -m1 '^ExecStart=' "$1" || true)
  case "$EXEC_LINE" in
    *" --topic-root="*)
      PIN_GIVEN="1"
      # The root charset stops at a space, so the pin's value is the text
      # after the token up to the next space or the end of the line.
      PIN_ROOT="${EXEC_LINE#* --topic-root=}"
      PIN_ROOT="${PIN_ROOT%% *}"
      PIN_ROOT="$(normalize_root "$PIN_ROOT")"
      ;;
  esac
}

# Refuse a rooted unit beside an older unit that may serve the same root.
if [ "$UNIT" != "$UNIT_WITHOUT_ROOT" ] && [ -f "$PATH_WITHOUT_ROOT" ]; then
  read_pin "$PATH_WITHOUT_ROOT"
  if [ -z "$PIN_GIVEN" ] || [ "$PIN_ROOT" = "$NORMALIZED_ROOT" ]; then
    if [ -n "$PIN_GIVEN" ]; then
      echo "ERROR: $PATH_WITHOUT_ROOT is already installed for topic root '$PIN_ROOT'." >&2
    else
      echo "ERROR: $PATH_WITHOUT_ROOT carries no --topic-root, so its daemon takes its" >&2
      echo "       root from the config file and may already be on '$NORMALIZED_ROOT'." >&2
    fi
    echo "       Installing $UNIT beside it would leave two daemons for agent id" >&2
    echo "       ${AGENT_ID} on one root, sharing one broker session and disconnecting" >&2
    echo "       each other. Nothing was changed." >&2
    echo "       Retire the older unit, then re-run this command:" >&2
    echo "         systemctl --user disable --now $UNIT_WITHOUT_ROOT" >&2
    echo "         rm $PATH_WITHOUT_ROOT" >&2
    if [ -z "$PIN_GIVEN" ]; then
      echo "       Or pin that unit to the root it really serves first, and this one can" >&2
      echo "       then run beside it: $0 ${AGENT_ID} --topic-root <its-root>" >&2
    fi
    exit 2
  fi
fi

# Never rewrite an existing unit's root pin in place.
if [ -f "$UNIT_PATH" ]; then
  read_pin "$UNIT_PATH"
  if [ -n "$PIN_GIVEN" ] && { [ -z "$TOPIC_ROOT_GIVEN" ] || [ "$PIN_ROOT" != "$NORMALIZED_ROOT" ]; }; then
    if [ -n "$PIN_ROOT" ]; then
      EXISTING_DISPLAY="'$PIN_ROOT'"
      KEEP_ARG="--topic-root $PIN_ROOT"
    else
      EXISTING_DISPLAY="(unrooted)"
      KEEP_ARG="--topic-root ''"
    fi
    if [ -z "$TOPIC_ROOT_GIVEN" ]; then
      ASKED="no --topic-root, so the config file's root"
    elif [ -n "$NORMALIZED_ROOT" ]; then
      ASKED="topic root '$NORMALIZED_ROOT'"
    else
      ASKED="(unrooted)"
    fi
    echo "ERROR: $UNIT_PATH is installed for topic root $EXISTING_DISPLAY." >&2
    echo "       This run asked for: $ASKED." >&2
    echo "       This unit's name cannot carry a root, so writing it would move the" >&2
    echo "       swarmbus-${AGENT_ID} daemon to a different swarm. Nothing was changed." >&2
    echo "       To keep the unit as it is, pass $KEEP_ARG." >&2
    echo "       To replace it, remove the existing unit first:" >&2
    echo "         systemctl --user disable --now $UNIT" >&2
    echo "         rm $UNIT_PATH" >&2
    echo "       To run this agent id in a second swarm, install that one with" >&2
    echo "       --topic-root <root>: a rooted unit has a name of its own." >&2
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
      -e "s|@UNIT_SCOPE@|$UNIT_SCOPE|g" \
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
      -e "s|@UNIT_SCOPE@|$UNIT_SCOPE|g" \
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
# The directory name must match the unit name exactly or systemd ignores
# it, and the daemon then starts without the credentials.
DROPIN_DIR="$UNIT_DIR/${UNIT}.d"
DROPIN_PATH="$DROPIN_DIR/auth.conf"

if [ -n "$BROKER_USERNAME$BROKER_PASSWORD$BROKER_CA_CERT$BROKER_CLIENT_CERT$BROKER_CLIENT_KEY$BROKER_TLS" ]; then
  mkdir -p "$DROPIN_DIR"
  {
    echo "# Generated by install-systemd.sh — broker credentials for $AGENT_ID."
    echo "# Hand-edit with \`systemctl --user edit ${UNIT}\`"
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
systemctl --user enable "$UNIT"
systemctl --user restart "$UNIT"

sleep 2
echo ""
echo "[install-systemd] status:"
systemctl --user --no-pager --lines=3 status "$UNIT" | head -8
echo ""
echo "[install-systemd] running doctor:"
DOCTOR_ARGS=(--agent-id "$AGENT_ID" --broker "$BROKER")
if [ -n "$TOPIC_ROOT_GIVEN" ]; then
  DOCTOR_ARGS+=("--topic-root=$NORMALIZED_ROOT")
fi
swarmbus doctor "${DOCTOR_ARGS[@]}" || true

echo ""
echo "[install-systemd] next step: loginctl enable-linger \$(whoami)"
echo "                 so the service survives logout / reboot."
