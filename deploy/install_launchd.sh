#!/bin/bash
# Install (or reinstall) the AutoMonetize launchd agent for the current user.
#   deploy/install_launchd.sh            install + start
#   deploy/install_launchd.sh uninstall  stop + remove
set -euo pipefail
LABEL="com.automonetize.agent"
WORKDIR="$(cd "$(dirname "$0")/.." && pwd)"
TARGET="$HOME/Library/LaunchAgents/$LABEL.plist"
DOMAIN="gui/$(id -u)"

if [[ "${1:-}" == "uninstall" ]]; then
  launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null || true
  rm -f "$TARGET"
  echo "removed $LABEL"
  exit 0
fi

mkdir -p "$HOME/Library/LaunchAgents" "$HOME/Library/Logs"
chmod +x "$WORKDIR/deploy/run_agent.sh"
sed -e "s#__WORKDIR__#$WORKDIR#g" -e "s#__HOME__#$HOME#g" "$WORKDIR/deploy/com.automonetize.agent.plist" > "$TARGET"
plutil -lint "$TARGET"
launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null || true
launchctl bootstrap "$DOMAIN" "$TARGET"
launchctl enable "$DOMAIN/$LABEL"
launchctl kickstart -k "$DOMAIN/$LABEL"
echo "installed $TARGET"
echo "logs: tail -f ~/Library/Logs/automonetize.stdout.log ~/Library/Logs/automonetize.stderr.log"
echo "status: launchctl print $DOMAIN/$LABEL | head -30"
