#!/usr/bin/env bash
# scripts/setup-mosquitto.sh
# Install mosquitto and configure as a systemd service.
#
# Modes:
#   (default)         Install and start with the default Debian config
#                     (binds to 127.0.0.1 only — local-only access).
#   --tailscale       Add /etc/mosquitto/conf.d/tailscale.conf binding an
#                     additional listener to this host's Tailscale IP, so
#                     agents on other tailnet nodes can reach the broker.
#                     Keeps the default 127.0.0.1 listener intact for local.
#   --tailscale-only  Replace the default listener with a Tailscale-only bind
#                     — nothing on the physical LAN can reach mosquitto.
#                     Belt-and-suspenders mode.
#
# For multi-machine deployments we recommend --tailscale: gives you Tailscale
# reachability without losing local-only ergonomics for daemons on the broker
# host itself.

set -euo pipefail

MODE="${1:-default}"

echo "[swarmbus] Installing mosquitto..."
sudo apt-get update -qq
sudo apt-get install -y mosquitto mosquitto-clients

_require_tailscale() {
  if ! command -v tailscale >/dev/null 2>&1; then
    echo "[swarmbus] ERROR: tailscale CLI not found. Install tailscale first:"
    echo "           https://tailscale.com/download/linux"
    exit 1
  fi
  TS_IP=$(tailscale ip -4 2>/dev/null | head -1)
  if [ -z "${TS_IP}" ]; then
    echo "[swarmbus] ERROR: could not read Tailscale IPv4 address."
    echo "           Run 'tailscale up' and try again."
    exit 1
  fi
  echo "[swarmbus] Tailscale IPv4: $TS_IP"
}

case "$MODE" in
  default)
    echo "[swarmbus] Using default config (localhost-only)."
    ;;
  --tailscale|--tailscale-only)
    _require_tailscale
    CONF_PATH="/etc/mosquitto/conf.d/tailscale.conf"
    if [ "$MODE" = "--tailscale-only" ]; then
      MAIN_CFG="# Tailscale-only mode: replace default localhost listener.
listener 1883 ${TS_IP}
allow_anonymous true
# Within the Tailscale WireGuard mesh, traffic is already end-to-end
# encrypted and peer-authenticated. Anonymous is safe HERE; it would not
# be on the public internet."
    else
      MAIN_CFG="# Additional Tailscale-bound listener. The default
# listener (127.0.0.1:1883 from mosquitto.conf) is preserved for local
# daemons on this host.
listener 1883 ${TS_IP}
allow_anonymous true"
    fi
    echo "[swarmbus] Writing $CONF_PATH (requires sudo)..."
    echo "$MAIN_CFG" | sudo tee "$CONF_PATH" >/dev/null
    ;;
  *)
    echo "[swarmbus] Unknown mode: $MODE"
    echo "           Usage: $0 [--tailscale | --tailscale-only]"
    exit 1
    ;;
esac

echo "[swarmbus] Enabling mosquitto systemd service..."
sudo systemctl enable mosquitto
sudo systemctl restart mosquitto
sudo systemctl status mosquitto --no-pager

echo
case "$MODE" in
  --tailscale)
    TS_HOSTNAME=$(tailscale status --json 2>/dev/null | python3 -c 'import json,sys; d=json.load(sys.stdin); print(d.get("Self",{}).get("DNSName","").rstrip("."))' 2>/dev/null || true)
    echo "[swarmbus] mosquitto listening on 127.0.0.1:1883 AND ${TS_IP}:1883"
    echo "[swarmbus] Remote agents point at:"
    echo "             --broker ${TS_IP}             # or:"
    [ -n "$TS_HOSTNAME" ] && echo "             --broker ${TS_HOSTNAME}   # MagicDNS"
    ;;
  --tailscale-only)
    echo "[swarmbus] mosquitto listening on ${TS_IP}:1883 ONLY. Local 127.0.0.1 access is off."
    echo "[swarmbus] Local daemons on this host must point at --broker ${TS_IP}."
    ;;
  *)
    echo "[swarmbus] mosquitto broker running on 127.0.0.1:1883 (localhost only)"
    echo "[swarmbus] For cross-machine access, re-run with --tailscale."
    ;;
esac
echo "[swarmbus] Test local: mosquitto_pub -t test -m hello & mosquitto_sub -t test"
