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

# Start the background bot daemon
python -m bees.bot &

# Start the Streamlit Web Application (top-level entry dispatches all modules)
streamlit run app.py --server.port 8501 --server.address 0.0.0.0
