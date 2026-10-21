#!/bin/bash
# stop.sh - Cleanly stops the Streamlit server

echo "Attempting to stop the Streamlit demo server..."

PID_FILE="streamlit.pid"

if [ -f "$PID_FILE" ]; then
    PID=$(cat "$PID_FILE")
    echo "Found PID file. Killing process: $PID"
    kill -9 "$PID"
    rm "$PID_FILE"
else
    echo "PID file not found."
fi

echo "Performing fallback cleanup on ports 8501 and 8585..."
lsof -ti :8501,8585 | xargs kill -9 2>/dev/null || true

rm streamlit.log 2>/dev/null

echo "Shutdown complete."

