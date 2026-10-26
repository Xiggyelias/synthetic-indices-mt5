#!/bin/sh
set -e

# Ensure data and log directories exist
mkdir -p /app/data /app/logs /app/config

# Auto-start 24/7 autonomous surveillance engine if enabled (default true)
if [ "${AUTONOMOUS_AUTOSTART:-true}" = "true" ]; then
    echo "[$(date -u +'%Y-%m-%dT%H:%M:%SZ')] Starting 24/7 QUANTA-SYNTH Autonomous Engine..."
    python synthetic_bot.py --run > /app/logs/synthetic_bot.log 2>&1 &
    BOT_PID=$!
    echo $BOT_PID > /app/data/synthetic_bot.pid
    echo "[$(date -u +'%Y-%m-%dT%H:%M:%SZ')] Autonomous Engine active (PID $BOT_PID)."
fi

# Launch Streamlit frontend in foreground
echo "[$(date -u +'%Y-%m-%dT%H:%M:%SZ')] Starting QUANTA-SYNTH Institutional Dashboard on port ${PORT:-8501}..."
exec streamlit run dashboard.py --server.port=${PORT:-8501} --server.address=0.0.0.0 --server.headless=true --browser.gatherUsageStats=false
