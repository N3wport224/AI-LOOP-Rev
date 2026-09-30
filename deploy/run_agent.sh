#!/bin/bash
# Entry point for launchd/systemd: load .env, then exec the supervisor (engine + webhook daemon).
# exec keeps the PID the same, so launchd's SIGTERM reaches the supervisor directly.
set -euo pipefail
cd "$(dirname "$0")/.."
ENV_FILE="${AUTOMONETIZE_ENV_FILE:-$PWD/.env}"
if [[ -f "$ENV_FILE" ]]; then
  set -a
  # shellcheck disable=SC1090
  source "$ENV_FILE"
  set +a
fi
if [[ -x .venv/bin/automonetize ]]; then
  exec .venv/bin/automonetize supervise --headless
fi
exec python3 cli.py supervise --headless
