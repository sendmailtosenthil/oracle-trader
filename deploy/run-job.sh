#!/usr/bin/env bash
# Wrapper for scheduled jobs on bare metal (called by cron). Loads the shared
# env file, runs the requested one-shot job from the repo's venv, then exits —
# so memory is reclaimed after each run. Self-locating: works wherever the repo
# is cloned.
set -uo pipefail
APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="${ORACLE_ENV_FILE:-$APP_DIR/.env}"

set -a
[ -f "$ENV_FILE" ] && . "$ENV_FILE"
set +a

cd "$APP_DIR" || exit 1
echo "[job] $(date '+%F %T %Z') $*"
exec "$APP_DIR/venv/bin/python" -m bees.bot "$@"
