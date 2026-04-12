#!/bin/bash
# Setup script for CVTailor Pipeline systemd service
# Run as: sudo bash setup_service.sh

set -e

SERVICE_NAME="cv-pipeline"
USER=$(whoami)
INSTALL_DIR="/root/projects/cv-pipeline"
PORT=3001
VENV_PYTHON="$INSTALL_DIR/venv/bin/python3"

# Load .env if it exists (for MINIMAX_API_KEY)
if [ -f "$INSTALL_DIR/.env" ]; then
    set -a
    source "$INSTALL_DIR/.env"
    set +a
fi

echo "=== CVTailor Pipeline Service Setup ==="
echo "MINIMAX_API_KEY: ${MINIMAX_API_KEY:0:10}..." # Print first 10 chars for confirmation

# 1. Create virtual environment
echo "[1/5] Creating virtual environment..."
python3 -m venv "$INSTALL_DIR/venv"

# 2. Install dependencies
echo "[2/5] Installing dependencies..."
"$VENV_PYTHON" -m pip install --upgrade pip
"$VENV_PYTHON" -m pip install -r "$INSTALL_DIR/requirements.txt"

# 3. Create systemd unit file
echo "[3/5] Creating systemd service..."
cat > /etc/systemd/system/${SERVICE_NAME}.service << EOF
[Unit]
Description=CVTailor AI Pipeline
After=network.target

[Service]
Type=simple
User=root
WorkingDirectory=$INSTALL_DIR
Environment="MINIMAX_API_KEY=${MINIMAX_API_KEY}"
Environment="SUPABASE_URL=${SUPABASE_URL}"
Environment="SUPABASE_SERVICE_ROLE_KEY=${SUPABASE_SERVICE_ROLE_KEY}"
Environment="PORT=$PORT"
ExecStart=$VENV_PYTHON $INSTALL_DIR/pipeline.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

# 4. Reload systemd and start service
echo "[4/5] Starting service..."
systemctl daemon-reload
systemctl enable ${SERVICE_NAME}
systemctl restart ${SERVICE_NAME}

# 5. Verify
echo "[5/5] Verifying..."
sleep 2
systemctl status ${SERVICE_NAME} --no-pager || true
curl -s http://localhost:${PORT}/health

echo ""
echo "=== Done ==="
echo "Service: ${SERVICE_NAME}"
echo "Port: ${PORT}"
echo "Logs: journalctl -u ${SERVICE_NAME} -f"
echo "Restart: systemctl restart ${SERVICE_NAME}"
