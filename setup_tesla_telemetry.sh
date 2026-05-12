#!/bin/bash

# Tesla Fleet Telemetry Setup Script for Termux (Android) - V2 (Telegram Included)
# Author: Manus AI (for doyaulchoi)

echo "🚀 Starting Tesla Telemetry Server Setup on Mi Pad..."

# 1. Update and Install Dependencies
echo "📦 Updating packages and installing dependencies..."
pkg update -y
pkg install -y golang git cloudflared wget python python-pip

# Install Python requests for Telegram
pip install requests

# 2. Clone Tesla Fleet Telemetry Repository
echo "📂 Cloning Tesla Fleet Telemetry repository..."
cd $HOME
rm -rf fleet-telemetry
git clone https://github.com/teslamotors/fleet-telemetry.git
cd fleet-telemetry

# 3. Build Telemetry Server
echo "🔨 Building Telemetry Server..."
go build -o tesla-telemetry ./cmd/telemetry

# 4. Create a basic configuration
echo "⚙️ Creating basic configuration..."
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

# 5. Download Telegram Handler
echo "🤖 Downloading Telegram Handler..."
wget -O $HOME/tesla_telemetry_handler.py https://raw.githubusercontent.com/doyaulchoi/doyaulchoi.github.io/main/tesla_telemetry_handler.py

# 6. Setup Cloudflare Tunnel
echo "🌐 Starting Cloudflare Tunnel..."
nohup cloudflared tunnel --url http://localhost:8080 > $HOME/tunnel.log 2>&1 &

sleep 5
TUNNEL_URL=$(grep -o 'https://[-a-z0-9.]*\.trycloudflare\.com' $HOME/tunnel.log | head -n 1)

echo "--------------------------------------------------------"
if [ -z "$TUNNEL_URL" ]; then
    echo "❌ Failed to get Cloudflare Tunnel URL automatically."
else
    echo "✅ Your Public HTTPS Endpoint: $TUNNEL_URL"
    echo "Please share this URL with Manus."
fi
echo "--------------------------------------------------------"

# 7. Run everything
echo "🚀 Starting System..."
# Run telemetry server and pipe logs to python handler for real-time alerts
./tesla-telemetry -config config.json | python $HOME/tesla_telemetry_handler.py
