#!/bin/bash

# Tesla Fleet Telemetry Setup Script for Termux (Android) - V4 (Path Fix)
# Author: Manus AI (for doyaulchoi)

echo "🚀 Starting Tesla Telemetry System on Mi Pad..."

# 1. Basic Environment Setup
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
    wget -q -O tesla_telemetry_handler.py https://raw.githubusercontent.com/doyaulchoi/doyaulchoi.github.io/main/tesla_telemetry_handler.py
    
    # Clean up and Re-clone if directory structure is messy
    if [ ! -d "fleet-telemetry" ]; then
        echo "📂 Cloning Tesla Fleet Telemetry repository..."
        git clone https://github.com/teslamotors/fleet-telemetry.git
    fi
    
    cd $HOME/fleet-telemetry
    
    # Ensure binary exists
    if [ ! -f "./tesla-telemetry" ]; then
        echo "🔨 Building Telemetry Server..."
        go build -o tesla-telemetry ./cmd/telemetry
    fi

    # Create config if not exists
    if [ ! -f "config.json" ]; then
        echo "⚙️ Creating configuration..."
        cat <<EOF > config.json
{
  "host": "0.0.0.0",
  "port": 8080,
  "log_level": "info",
  "storage": {
    "type": "file",
    "path": "$HOME/tesla_data_logs"
  }
}
EOF
        mkdir -p $HOME/tesla_data_logs
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
    rm -f $HOME/update_trigger
    
    # Run server and pipe to handler (using absolute paths to be safe)
    ./tesla-telemetry -config ./config.json | python $HOME/tesla_telemetry_handler.py
    
    # If the process exits, check if it was an update request
    cd $HOME
    if [ -f "$HOME/update_trigger" ]; then
        echo "🔄 Update trigger detected. Restarting system..."
        sleep 2
    else
        echo "⚠️ System exited unexpectedly. Restarting in 10 seconds..."
        sleep 10
    fi
done
