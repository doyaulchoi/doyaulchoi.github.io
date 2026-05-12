#!/bin/bash

# Tesla Fleet Telemetry Setup Script for Termux (Android) - V3 (Remote Update Support)
# Author: Manus AI (for doyaulchoi)

echo "🚀 Starting Tesla Telemetry System on Mi Pad..."

# 1. Basic Environment Setup (First time only)
if ! command -v go &> /dev/null; then
    echo "📦 Initializing environment..."
    pkg update -y
    pkg install -y golang git cloudflared wget python python-pip
    pip install requests
fi

cd $HOME

# 2. Infinite Loop for Auto-Update Support
while true; do
    echo "🔄 Fetching/Updating latest scripts from GitHub..."
    wget -O tesla_telemetry_handler.py https://raw.githubusercontent.com/doyaulchoi/doyaulchoi.github.io/main/tesla_telemetry_handler.py
    
    # Check if telemetry server exists, if not build it
    if [ ! -d "fleet-telemetry" ]; then
        git clone https://github.com/teslamotors/fleet-telemetry.git
        cd fleet-telemetry && go build -o tesla-telemetry ./cmd/telemetry && cd ..
    fi

    # Start Cloudflare Tunnel if not running
    if ! pgrep -x "cloudflared" > /dev/null; then
        echo "🌐 Starting Cloudflare Tunnel..."
        nohup cloudflared tunnel --url http://localhost:8080 > $HOME/tunnel.log 2>&1 &
        sleep 5
    fi

    TUNNEL_URL=$(grep -o 'https://[-a-z0-9.]*\.trycloudflare\.com' $HOME/tunnel.log | head -n 1)
    echo "--------------------------------------------------------"
    echo "✅ Active Public Endpoint: $TUNNEL_URL"
    echo "--------------------------------------------------------"

    # 3. Run the system
    echo "🚀 Launching Telemetry Monitor..."
    # If update_trigger exists, remove it
    rm -f $HOME/update_trigger
    
    # Run server and pipe to handler
    ./fleet-telemetry/tesla-telemetry -config ./fleet-telemetry/config.json | python $HOME/tesla_telemetry_handler.py
    
    # If the process exits, check if it was an update request
    if [ -f "$HOME/update_trigger" ]; then
        echo "🔄 Update trigger detected. Restarting system..."
        sleep 2
    else
        echo "⚠️ System exited unexpectedly. Restarting in 10 seconds..."
        sleep 10
    fi
done
