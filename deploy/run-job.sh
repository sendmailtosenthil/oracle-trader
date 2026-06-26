#!/usr/bin/env bash
# Wrapper for scheduled jobs on bare metal (called by cron). Loads the shared
# env file, runs the requested one-shot job from the repo's venv, then exits —
# so memory is reclaimed after each run. Self-locating: works wherever the repo
# is cloned.
set -uo pipefail
APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="${ORACLE_ENV_FILE:-$APP_DIR/.env}"

# Load .env the way systemd's EnvironmentFile does — KEY=VALUE, value taken
# verbatim (so passwords with spaces/specials don't get shell-evaluated). Do
# NOT `source` it: that splits on spaces and would run words as commands.
if [ -f "$ENV_FILE" ]; then
    while IFS= read -r line || [ -n "$line" ]; do
        case "$line" in ''|'#'*) continue ;; esac   # skip blanks/comments
        case "$line" in *=*) : ;; *) continue ;; esac  # require KEY=VALUE
        line="${line#export }"
        export "${line%%=*}=${line#*=}"
    done < "$ENV_FILE"
fi

cd "$APP_DIR" || exit 1
echo "[job] $(date '+%F %T %Z') $*"
exec "$APP_DIR/venv/bin/python" -m bees.bot "$@"
