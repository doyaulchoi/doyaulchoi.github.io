#!/bin/bash

# Tesla Fleet Telemetry Setup Script for Termux (Android)
# Author: Manus AI (for doyaulchoi)

echo "🚀 Starting Tesla Telemetry Server Setup on Mi Pad..."

# 1. Update and Install Dependencies
echo "📦 Updating packages and installing dependencies..."
pkg update -y
pkg install -y golang git cloudflared wget

# 2. Clone Tesla Fleet Telemetry Repository
echo "📂 Cloning Tesla Fleet Telemetry repository..."
cd $HOME
rm -rf fleet-telemetry
git clone https://github.com/teslamotors/fleet-telemetry.git
cd fleet-telemetry

# 3. Build Telemetry Server
echo "🔨 Building Telemetry Server (this may take a few minutes)..."
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

# 5. Setup Cloudflare Tunnel
echo "🌐 Starting Cloudflare Tunnel to provide HTTPS endpoint..."
echo "--------------------------------------------------------"
echo "ATTENTION: Please look for a line starting with 'https://...' below."
echo "That is your public endpoint. Please share it with Manus."
echo "--------------------------------------------------------"

# Run Cloudflare Tunnel in background and output the URL
# Note: This uses trycloudflare.com for a free, no-login tunnel
nohup cloudflared tunnel --url http://localhost:8080 > $HOME/tunnel.log 2>&1 &

sleep 5
TUNNEL_URL=$(grep -o 'https://[-a-z0-9.]*\.trycloudflare\.com' $HOME/tunnel.log | head -n 1)

if [ -z "$TUNNEL_URL" ]; then
    echo "❌ Failed to get Cloudflare Tunnel URL automatically."
    echo "Check \$HOME/tunnel.log for details."
else
    echo "✅ Your Public HTTPS Endpoint: $TUNNEL_URL"
    echo "Please tell Manus this URL."
fi

# 6. Run the Telemetry Server
echo "🚀 Starting Tesla Telemetry Server..."
./tesla-telemetry -config config.json
