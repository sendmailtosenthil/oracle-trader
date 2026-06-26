#!/bin/bash

# Best Practice: Always take a backup of the DB before launching
if [ -f "oracle.db" ]; then
    cp oracle.db "oracle.db.$(date +%Y%m%d_%H%M%S).bak"
    echo "Database backup created securely."
    # Keep only the 5 most recent local backups (the 4 PM job also pushes to Drive).
    ls -1t oracle.db.*.bak 2>/dev/null | tail -n +6 | xargs -r rm -f
fi

# Ensure the project root is importable so the `common`, `bees` and
# `downloader` packages resolve (Streamlit only adds the script's own directory
# to sys.path by default).
export PYTHONPATH="$(pwd):${PYTHONPATH}"

# Scheduled jobs run via cron (see /etc/cron.d/oracle), not a resident daemon —
# each fires a short-lived `python -m bees.bot <job>` so memory is freed after.
# Cron doesn't inherit our environment, so snapshot it (shell-quoted) to a file
# the job wrapper sources. Written to /etc (not the bind-mounted /app).
printenv \
    | grep -E '^(GMAIL_USER|GMAIL_PASS|DRIVE_|DOWNLOADER_|ZERODHA_|TZ|PATH|PYTHONPATH|LANG)=' \
    | while IFS='=' read -r k v; do printf '%s=%q\n' "$k" "$v"; done > /etc/oracle.env

# Start the cron daemon (background)
cron

# Start the Streamlit Web Application in the foreground (keeps the container
# alive; top-level app.py dispatches all modules).
streamlit run app.py --server.port 8501 --server.address 0.0.0.0
