#!/bin/bash
# Install (or reinstall) the AutoMonetize launchd jobs.
#
#   deploy/install_launchd.sh                       agent (+ tunnel if configured), per-user, start at login
#   sudo deploy/install_launchd.sh --system         same, as LaunchDaemons: start at boot, no login needed
#   deploy/install_launchd.sh --service tunnel      one job only (agent | tunnel | all)
#   deploy/install_launchd.sh --render-only DIR     write the rendered plists to DIR, touch nothing else
#   deploy/install_launchd.sh uninstall [--system]  stop + remove
#
# Tunnel settings come from the environment or the defaults setup_tunnel.sh uses:
#   TUNNEL_NAME (automonetize), TUNNEL_CONFIG (~/.cloudflared/automonetize.yml), CLOUDFLARED (on PATH)
set -euo pipefail

WORKDIR="$(cd "$(dirname "$0")/.." && pwd)"
SERVICE="all"
SYSTEM=0
RENDER_DIR=""
ACTION="install"

while [[ $# -gt 0 ]]; do
  case "$1" in
    uninstall) ACTION="uninstall" ;;
    --system) SYSTEM=1 ;;
    --service) SERVICE="$2"; shift ;;
    --render-only) RENDER_DIR="$2"; shift ;;
    -h|--help) sed -n '2,13p' "$0"; exit 0 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
  shift
done
case "$SERVICE" in agent|tunnel|all) ;; *) echo "--service must be agent, tunnel or all" >&2; exit 2 ;; esac

# Under sudo, run the jobs as (and log for) the invoking user, not root.
RUN_USER="${SUDO_USER:-$(id -un)}"
if [[ -n "${SUDO_USER:-}" ]]; then
  RUN_HOME="$(eval echo "~$SUDO_USER")"
else
  RUN_HOME="$HOME"
fi
TUNNEL_NAME="${TUNNEL_NAME:-automonetize}"
TUNNEL_CONFIG="${TUNNEL_CONFIG:-$RUN_HOME/.cloudflared/automonetize.yml}"
CLOUDFLARED="${CLOUDFLARED:-$(command -v cloudflared || echo /opt/homebrew/bin/cloudflared)}"

if [[ $SYSTEM -eq 1 ]]; then
  TARGET_DIR="/Library/LaunchDaemons"
  DOMAIN="system"
  if [[ -z "$RENDER_DIR" && $EUID -ne 0 ]]; then
    echo "--system needs sudo: sudo $0 --system" >&2; exit 1
  fi
else
  TARGET_DIR="$RUN_HOME/Library/LaunchAgents"
  DOMAIN="gui/$(id -u "$RUN_USER")"
fi
[[ -n "$RENDER_DIR" ]] && TARGET_DIR="$RENDER_DIR"

services() {
  if [[ "$SERVICE" == "all" ]]; then
    echo agent
    if [[ -f "$TUNNEL_CONFIG" || -n "$RENDER_DIR" ]]; then
      echo tunnel
    else
      echo "note: no tunnel config at $TUNNEL_CONFIG (run deploy/tunnel/setup_tunnel.sh); installing the agent only" >&2
    fi
  else
    echo "$SERVICE"
  fi
}

template() {
  case "$1" in
    agent) echo "$WORKDIR/deploy/com.automonetize.agent.plist" ;;
    tunnel) echo "$WORKDIR/deploy/tunnel/com.automonetize.tunnel.plist" ;;
  esac
}

render() {  # render <service> <target>
  sed -e "s#__WORKDIR__#$WORKDIR#g" -e "s#__HOME__#$RUN_HOME#g" \
      -e "s#__CLOUDFLARED__#$CLOUDFLARED#g" -e "s#__CONFIG__#$TUNNEL_CONFIG#g" -e "s#__TUNNEL__#$TUNNEL_NAME#g" \
      "$(template "$1")" > "$2"
  if [[ $SYSTEM -eq 1 ]]; then
    # A LaunchDaemon runs as root unless told otherwise: run it as the owner of the checkout.
    if command -v plutil >/dev/null; then
      plutil -insert UserName -string "$RUN_USER" "$2"
    else
      python3 - "$2" "$RUN_USER" <<'PY'
import plistlib, sys
path, user = sys.argv[1], sys.argv[2]
with open(path, "rb") as fh:
    data = plistlib.load(fh)
data["UserName"] = user
with open(path, "wb") as fh:
    plistlib.dump(data, fh)
PY
    fi
  fi
  if grep -Eq "__[A-Z]+__" "$2"; then echo "unrendered placeholder in $2" >&2; exit 1; fi
  if command -v plutil >/dev/null; then plutil -lint "$2" >/dev/null; fi
}

if [[ "$ACTION" == "uninstall" ]]; then
  if [[ "$SERVICE" == "all" ]]; then targets="agent tunnel"; else targets="$SERVICE"; fi
  for svc in $targets; do
    label="com.automonetize.$svc"
    launchctl bootout "$DOMAIN/$label" 2>/dev/null || true
    rm -f "$TARGET_DIR/$label.plist"
    echo "removed $label"
  done
  exit 0
fi

mkdir -p "$TARGET_DIR"
if [[ -z "$RENDER_DIR" ]]; then
  mkdir -p "$RUN_HOME/Library/Logs/automonetize"
  if [[ -n "${SUDO_USER:-}" ]]; then chown "$RUN_USER" "$RUN_HOME/Library/Logs/automonetize"; fi
  chmod +x "$WORKDIR/deploy/run_agent.sh"
fi

for svc in $(services); do
  label="com.automonetize.$svc"
  target="$TARGET_DIR/$label.plist"
  if [[ "$svc" == "tunnel" && -z "$RENDER_DIR" && ! -x "$CLOUDFLARED" ]]; then
    echo "cloudflared not found at $CLOUDFLARED; run deploy/tunnel/setup_tunnel.sh first" >&2; exit 1
  fi
  render "$svc" "$target"
  if [[ -n "$RENDER_DIR" ]]; then
    echo "rendered $target"
    continue
  fi
  if [[ $SYSTEM -eq 1 ]]; then chown root:wheel "$target"; chmod 644 "$target"; fi
  launchctl bootout "$DOMAIN/$label" 2>/dev/null || true
  launchctl bootstrap "$DOMAIN" "$target"
  launchctl enable "$DOMAIN/$label"
  launchctl kickstart -k "$DOMAIN/$label"
  echo "installed $target"
done

if [[ -z "$RENDER_DIR" ]]; then
  echo "logs:   tail -f ~/Library/Logs/automonetize/*.log"
  echo "status: launchctl print $DOMAIN/com.automonetize.agent | head -30"
fi
