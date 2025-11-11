FROM python:3.11-alpine

LABEL "language"="python"

# Install system dependencies including OpenVPN
RUN apk add --no-cache \
    ffmpeg \
    sqlite \
    gcc \
    musl-dev \
    linux-headers \
    openvpn \
    openssl \
    ca-certificates \
    iproute2 \
    bash \
    && rm -rf /var/cache/apk/*

WORKDIR /app

# Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application
COPY . .

# Create temp directories
RUN mkdir -p /tmp/audio_uploads /tmp/segments /tmp/yt_downloads /etc/openvpn/config /var/log

# Create startup script
RUN cat > /app/start.sh << 'EOF'
#!/bin/bash
set -e

# Check if VPN credentials are provided
if [ -z "$VPN_USERNAME" ] || [ -z "$VPN_PASSWORD" ] || [ -z "$VPN_CONFIG" ]; then
    echo "ERROR: VPN_USERNAME, VPN_PASSWORD, and VPN_CONFIG environment variables are required"
    exit 1
fi

# Create OpenVPN credentials file
cat > /etc/openvpn/credentials.txt << CREDS
$VPN_USERNAME
$VPN_PASSWORD
CREDS
chmod 600 /etc/openvpn/credentials.txt

# Create OpenVPN config file from environment variable
echo "$VPN_CONFIG" > /etc/openvpn/config/client.conf
chmod 600 /etc/openvpn/config/client.conf

# Ensure TUN device exists
if [ ! -c /dev/net/tun ]; then
    echo "ERROR: /dev/net/tun device not found. This container needs --device /dev/net/tun"
    exit 1
fi

# Start OpenVPN in background
echo "Starting OpenVPN connection..."
openvpn --config /etc/openvpn/config/client.conf --daemon --log /var/log/openvpn.log --writepid /var/run/openvpn.pid

# Wait for VPN to establish (check for tun0 interface)
echo "Waiting for VPN connection to establish..."
for i in $(seq 1 60); do
    if ip link show tun0 > /dev/null 2>&1; then
        echo "✅ VPN connection established!"
        sleep 2  # Give it a moment to fully stabilize
        break
    fi
    echo "⏳ Waiting for VPN... ($i/60)"
    sleep 1
done

# Verify VPN is connected
if ! ip link show tun0 > /dev/null 2>&1; then
    echo "❌ ERROR: VPN connection failed to establish"
    echo "OpenVPN log:"
    cat /var/log/openvpn.log
    exit 1
fi

# Test VPN connectivity
echo "Testing VPN connectivity..."
if ! ping -c 1 8.8.8.8 > /dev/null 2>&1; then
    echo "⚠️  Warning: Cannot ping 8.8.8.8, but VPN interface exists"
fi

# Start the FastAPI application
echo "🚀 Starting FastAPI application..."
exec uvicorn app.main:app \
    --host 0.0.0.0 \
    --port 8080 \
    --workers 1 \
    --limit-concurrency 10 \
    --timeout-keep-alive 30
EOF

# ✅ FIXED: Use RUN before chmod
RUN chmod +x /app/start.sh

EXPOSE 8080

CMD ["/app/start.sh"]