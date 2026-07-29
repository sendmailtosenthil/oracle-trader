#!/usr/bin/env bash
# Installs + starts the enctoken ingest API as a systemd service (oracle-api).
# Mirrors deploy/setup.sh: same repo, same venv, same .env (EnvironmentFile).
# Run as your normal login user (NOT root):   bash deploy/setup-api.sh
set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RUN_USER="$(id -un)"
PORT="${ENCTOKEN_API_PORT:-8502}"

echo "==> Repo:  $APP_DIR"
echo "==> User:  $RUN_USER"
echo "==> Port:  $PORT"

if [ ! -x "$APP_DIR/venv/bin/python" ]; then
    echo "!! venv not found — run deploy/setup.sh first." >&2
    exit 1
fi
if [ ! -f "$APP_DIR/.env" ]; then
    echo "!! $APP_DIR/.env missing — run deploy/setup.sh first, then set ENCTOKEN_API_*." >&2
    exit 1
fi
if ! grep -q '^ENCTOKEN_API_USER=' "$APP_DIR/.env" || ! grep -q '^ENCTOKEN_API_PASS=' "$APP_DIR/.env"; then
    echo "!! Add ENCTOKEN_API_USER and ENCTOKEN_API_PASS to $APP_DIR/.env, then re-run." >&2
    exit 1
fi

echo "==> Installing systemd service oracle-api (sudo)..."
sudo tee /etc/systemd/system/oracle-api.service >/dev/null <<UNIT
[Unit]
Description=Project Oracle enctoken ingest API
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$RUN_USER
WorkingDirectory=$APP_DIR
EnvironmentFile=$APP_DIR/.env
Environment=PYTHONPATH=$APP_DIR
ExecStart=$APP_DIR/venv/bin/python -m api.server
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
UNIT

sudo systemctl daemon-reload
sudo systemctl enable --now oracle-api.service

echo ""
echo "==> Done."
echo "    API   : http://<vps-ip>:$PORT/api/health   (systemctl status oracle-api)"
echo "    Logs  : journalctl -u oracle-api -f"
