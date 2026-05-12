#!/bin/bash

# Tesla Fleet Telemetry Setup Script for Termux (Android) - V5 (Complete Rewrite)
# Author: Manus AI

set -e

WORK_DIR="$HOME/tesla_telemetry_work"
TELEMETRY_DIR="$WORK_DIR/telemetry_server"
HANDLER_SCRIPT="$WORK_DIR/handler.py"

echo "🚀 Tesla Telemetry System V5 Starting..."

# 1. First-time setup only
if [ ! -d "$WORK_DIR" ]; then
    echo "📦 First-time initialization..."
    mkdir -p "$WORK_DIR"
    pkg update -y
    pkg install -y golang git cloudflared wget python python-pip
    pip install requests
fi

# 2. Infinite loop for auto-restart
while true; do
    echo "=================================================="
    echo "🔄 Cycle Start: $(date '+%Y-%m-%d %H:%M:%S')"
    echo "=================================================="
    
    # Clean and prepare working directory
    rm -rf "$TELEMETRY_DIR"
    mkdir -p "$TELEMETRY_DIR"
    
    # Clone fresh telemetry server
    echo "📂 Cloning Tesla Fleet Telemetry..."
    git clone https://github.com/teslamotors/fleet-telemetry.git "$TELEMETRY_DIR"
    
    # Build binary
    echo "🔨 Building telemetry server..."
    cd "$TELEMETRY_DIR"
    go build -o telemetry_binary ./cmd/telemetry
    
    # Create config
    cat <<EOF > config.json
{
  "host": "0.0.0.0",
  "port": 8080,
  "log_level": "info",
  "storage": {
    "type": "file",
    "path": "$WORK_DIR/logs"
  }
}
EOF
    mkdir -p "$WORK_DIR/logs"
    
    # Download handler
    echo "🤖 Downloading handler..."
    wget -q -O "$HANDLER_SCRIPT" https://raw.githubusercontent.com/doyaulchoi/doyaulchoi.github.io/main/tesla_telemetry_handler.py
    
    # Start Cloudflare tunnel if needed
    if ! pgrep -x "cloudflared" > /dev/null; then
        echo "🌐 Starting Cloudflare Tunnel..."
        nohup cloudflared tunnel --url http://localhost:8080 > "$WORK_DIR/tunnel.log" 2>&1 &
        sleep 5
    fi
    
    TUNNEL_URL=$(grep -o 'https://[-a-z0-9.]*\.trycloudflare\.com' "$WORK_DIR/tunnel.log" 2>/dev/null | head -n 1)
    echo "✅ Endpoint: $TUNNEL_URL"
    
    # Run system
    echo "🚀 Launching system..."
    rm -f "$WORK_DIR/update_trigger"
    
    # Execute with absolute paths
    "$TELEMETRY_DIR/telemetry_binary" -config "$TELEMETRY_DIR/config.json" | python "$HANDLER_SCRIPT"
    
    # Check if update was triggered
    if [ -f "$WORK_DIR/update_trigger" ]; then
        echo "🔄 Update triggered. Restarting..."
        sleep 2
    else
        echo "⚠️ System exited. Restarting in 10 seconds..."
        sleep 10
    fi
done
