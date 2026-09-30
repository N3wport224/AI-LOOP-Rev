#!/bin/bash
# One-time setup of a permanent Cloudflare named tunnel for the Stripe webhook listener.
#
#   deploy/tunnel/setup_tunnel.sh hooks.example.com            configure, save the URL, start the launchd job
#   deploy/tunnel/setup_tunnel.sh hooks.example.com --no-load  configure only
#
# Requirements (not avoidable, stated plainly):
#   * a domain whose DNS is on your Cloudflare account (the free plan is fine). Quick tunnels
#     (*.trycloudflare.com) get a new URL on every restart, so they can't back a Stripe endpoint.
#   * one interactive `cloudflared tunnel login` (a browser window) the first time. Everything
#     after that, including restarts and reboots, is unattended.
#
# What it does, idempotently:
#   1. installs cloudflared with Homebrew if it's missing
#   2. logs in once (skipped if ~/.cloudflared/cert.pem exists)
#   3. creates the tunnel (or reuses the existing one with the same name)
#   4. routes DNS: HOSTNAME CNAME -> <tunnel id>.cfargotunnel.com
#   5. writes ~/.cloudflared/automonetize.yml: only /webhook and /healthz are forwarded to
#      127.0.0.1:<webhook_port>; everything else gets a 404 at Cloudflare's edge
#   6. saves PUBLIC_WEBHOOK_URL=https://HOSTNAME/webhook to .env and data/tunnel.json
#   7. installs and starts the com.automonetize.tunnel launchd job
set -euo pipefail

WORKDIR="$(cd "$(dirname "$0")/../.." && pwd)"
HOSTNAME_ARG="${1:-${TUNNEL_HOSTNAME:-}}"
LOAD=1
for arg in "$@"; do [[ "$arg" == "--no-load" ]] && LOAD=0; done
TUNNEL_NAME="${TUNNEL_NAME:-automonetize}"
PORT="${AUTOMONETIZE_WEBHOOK_PORT:-8443}"
ENV_FILE="${AUTOMONETIZE_ENV_FILE:-$WORKDIR/.env}"
DATA_DIR="${AUTOMONETIZE_DATA_DIR:-$WORKDIR/data}"
CF_DIR="$HOME/.cloudflared"
TUNNEL_CONFIG="${TUNNEL_CONFIG:-$CF_DIR/automonetize.yml}"
PY="${PYTHON:-$( [[ -x "$WORKDIR/.venv/bin/python" ]] && echo "$WORKDIR/.venv/bin/python" || echo python3)}"
helper() { (cd "$WORKDIR" && "$PY" -m agent.tunnel "$@"); }

if [[ -z "$HOSTNAME_ARG" || "$HOSTNAME_ARG" == --* ]]; then
  sed -n '2,6p' "$0"; exit 2
fi
HOST="$(helper validate-hostname "$HOSTNAME_ARG")"

# 1. cloudflared
if ! command -v cloudflared >/dev/null; then
  if command -v brew >/dev/null; then
    echo "==> installing cloudflared (brew)"
    brew install cloudflared
  else
    echo "cloudflared is missing and Homebrew isn't installed. Install one of them: https://brew.sh" >&2
    exit 1
  fi
fi
CLOUDFLARED="$(command -v cloudflared)"
echo "==> $("$CLOUDFLARED" --version 2>&1 | head -1)"

# 2. one-time login
mkdir -p "$CF_DIR"
if [[ ! -f "$CF_DIR/cert.pem" ]]; then
  echo "==> one-time Cloudflare login: pick the zone that owns $HOST in the browser window"
  "$CLOUDFLARED" tunnel login
fi

# 3. tunnel
TUNNEL_ID="$("$CLOUDFLARED" tunnel list -o json 2>/dev/null | helper find-id "$TUNNEL_NAME" || true)"
if [[ -z "$TUNNEL_ID" ]]; then
  echo "==> creating tunnel $TUNNEL_NAME"
  CREATE_OUT="$("$CLOUDFLARED" tunnel create "$TUNNEL_NAME" 2>&1)"
  echo "$CREATE_OUT"
  TUNNEL_ID="$(echo "$CREATE_OUT" | helper parse-create | head -1)"
else
  echo "==> reusing tunnel $TUNNEL_NAME ($TUNNEL_ID)"
fi
CRED_FILE="$CF_DIR/$TUNNEL_ID.json"
if [[ ! -f "$CRED_FILE" ]]; then
  echo "credentials $CRED_FILE are missing (tunnel created on another machine?)." >&2
  echo "Delete it with: cloudflared tunnel delete $TUNNEL_NAME   then re-run this script." >&2
  exit 1
fi
chmod 600 "$CRED_FILE"

# 4. DNS (fails harmlessly if the record already points at this tunnel)
if ! ROUTE_OUT="$("$CLOUDFLARED" tunnel route dns "$TUNNEL_NAME" "$HOST" 2>&1)"; then
  if echo "$ROUTE_OUT" | grep -qi "already exists"; then
    echo "==> DNS record for $HOST already exists; make sure it's a CNAME to $TUNNEL_ID.cfargotunnel.com"
  else
    echo "$ROUTE_OUT" >&2; exit 1
  fi
else
  echo "==> routed $HOST -> tunnel $TUNNEL_NAME"
fi

# 5. config with restricted ingress
helper render-config --tunnel-id "$TUNNEL_ID" --credentials-file "$CRED_FILE" --hostname "$HOST" \
  --service "http://127.0.0.1:$PORT" --out "$TUNNEL_CONFIG" >/dev/null
helper check-config "$TUNNEL_CONFIG" >/dev/null
"$CLOUDFLARED" tunnel --config "$TUNNEL_CONFIG" ingress validate
echo "==> wrote $TUNNEL_CONFIG"

# 6. persist the public URL
URL="$(helper write-env --env-file "$ENV_FILE" --hostname "$HOST" --record "$DATA_DIR/tunnel.json" \
  --tunnel-id "$TUNNEL_ID" --tunnel-name "$TUNNEL_NAME" --config-file "$TUNNEL_CONFIG")"
echo "==> saved PUBLIC_WEBHOOK_URL=$URL to $ENV_FILE and $DATA_DIR/tunnel.json"

# 7. launchd companion job
if [[ $LOAD -eq 1 ]]; then
  TUNNEL_NAME="$TUNNEL_NAME" TUNNEL_CONFIG="$TUNNEL_CONFIG" CLOUDFLARED="$CLOUDFLARED" \
    "$WORKDIR/deploy/install_launchd.sh" --service tunnel
fi

echo
echo "Tunnel ready. Next: automonetize setup-autonomous   (registers $URL with Stripe and verifies it end to end)"
