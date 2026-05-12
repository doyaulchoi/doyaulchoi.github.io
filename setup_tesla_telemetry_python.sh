#!/bin/bash

# Tesla Fleet Telemetry Setup - Python Version (No Go Compilation)
# Author: Manus AI

set -e

WORK_DIR="$HOME/tesla_telemetry_work"
SERVER_SCRIPT="$WORK_DIR/telemetry_server.py"

echo "🚀 Tesla Telemetry System (Python) Starting..."

# First-time setup only
if [ ! -d "$WORK_DIR" ]; then
    echo "📦 First-time initialization..."
    mkdir -p "$WORK_DIR"
    pkg update -y
    pkg install -y python cloudflared wget
    pip install flask requests
fi

# Infinite loop for auto-restart
while true; do
    echo "=================================================="
    echo "🔄 Cycle Start: $(date '+%Y-%m-%d %H:%M:%S')"
    echo "=================================================="
    
    # Download server
    echo "📥 Downloading server..."
    wget -q -O "$SERVER_SCRIPT" https://raw.githubusercontent.com/doyaulchoi/doyaulchoi.github.io/main/telemetry_server.py
    
    # Download handler
    echo "🤖 Downloading handler..."
    wget -q -O "$WORK_DIR/handler.py" https://raw.githubusercontent.com/doyaulchoi/doyaulchoi.github.io/main/tesla_telemetry_handler.py
    
    # Start Cloudflare tunnel if needed
    if ! pgrep -x "cloudflared" > /dev/null; then
        echo "🌐 Starting Cloudflare Tunnel..."
        nohup cloudflared tunnel --url http://localhost:8080 > "$WORK_DIR/tunnel.log" 2>&1 &
        sleep 5
    fi
    
    TUNNEL_URL=$(grep -o 'https://[-a-z0-9.]*\.trycloudflare\.com' "$WORK_DIR/tunnel.log" 2>/dev/null | head -n 1)
    if [ -z "$TUNNEL_URL" ]; then
        TUNNEL_URL="https://pending.trycloudflare.com"
    fi
    echo "✅ Active Public Endpoint: $TUNNEL_URL"
    
    # Run system
    echo "🚀 Launching Telemetry Server..."
    rm -f "$WORK_DIR/update_trigger"
    
    # Execute with absolute paths
    python "$SERVER_SCRIPT" "$WORK_DIR/handler.py"
    
    # Check if update was triggered
    if [ -f "$WORK_DIR/update_trigger" ]; then
        echo "🔄 Update triggered. Restarting..."
        sleep 2
    else
        echo "⚠️ System exited. Restarting in 10 seconds..."
        sleep 10
    fi
done
