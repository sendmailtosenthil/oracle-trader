#!/usr/bin/env bash
# Wrapper invoked by cron (see /etc/cron.d/oracle). Cron jobs do NOT inherit the
# container's environment, so we source the snapshot captured by entrypoint.sh,
# then run the one-shot job and exit (its memory is reclaimed afterwards).
set -a
[ -f /etc/oracle.env ] && . /etc/oracle.env
set +a
cd /app || exit 1
echo "[cron] $(date '+%F %T %Z') job: ${1:-?}"
exec python -m bees.bot "$1"
