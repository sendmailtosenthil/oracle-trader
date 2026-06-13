#!/bin/bash
# Start the background bot daemon
python bot.py &

# Start the Streamlit Web Application
streamlit run app.py --server.port 8501 --server.address 0.0.0.0
