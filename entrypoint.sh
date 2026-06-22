#!/bin/bash

# Best Practice: Always take a backup of the DB before launching
if [ -f "oracle.db" ]; then
    cp oracle.db "oracle.db.$(date +%Y%m%d_%H%M%S).bak"
    echo "Database backup created securely."
fi

# Start the background bot daemon
python bot.py &

# Start the Streamlit Web Application
streamlit run app.py --server.port 8501 --server.address 0.0.0.0
