#!/usr/bin/env bash
# One-shot installer for running Project Oracle directly on the VPS (no Docker):
#   - creates a venv and installs requirements
#   - creates <repo>/.env from the example (if missing)
#   - installs + starts a systemd service for the Streamlit web app
#   - installs the current user's crontab for the scheduled jobs (IST)
#
# Run as your normal login user (NOT root):   bash deploy/setup.sh
# It uses sudo only for the systemd steps.
set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RUN_USER="$(id -un)"
PYTHON="${PYTHON:-python3}"
PORT="${ORACLE_PORT:-8501}"
WRAP="$APP_DIR/deploy/run-job.sh"

echo "==> Repo:   $APP_DIR"
echo "==> User:   $RUN_USER"
echo "==> Python: $($PYTHON --version 2>&1)"

# 1) venv + dependencies ------------------------------------------------------
if [ ! -d "$APP_DIR/venv" ]; then
    echo "==> Creating venv..."
    "$PYTHON" -m venv "$APP_DIR/venv"
fi
echo "==> Installing dependencies..."
"$APP_DIR/venv/bin/pip" install --quiet --upgrade pip
"$APP_DIR/venv/bin/pip" install --quiet -r "$APP_DIR/requirements.txt"

# 2) env file -----------------------------------------------------------------
if [ ! -f "$APP_DIR/.env" ]; then
    cp "$APP_DIR/deploy/oracle.env.example" "$APP_DIR/.env"
    chmod 600 "$APP_DIR/.env"
    echo "==> Created $APP_DIR/.env  —  EDIT it (GMAIL_USER / GMAIL_PASS), then re-run this script."
    exit 0
fi
chmod 600 "$APP_DIR/.env"

# 3) initialise the database (idempotent) ------------------------------------
echo "==> Initialising database..."
( cd "$APP_DIR" && "$APP_DIR/venv/bin/python" -m common.database )

# 4) systemd service for the web app -----------------------------------------
echo "==> Installing systemd service (sudo)..."
sudo tee /etc/systemd/system/oracle-web.service >/dev/null <<UNIT
[Unit]
Description=Project Oracle (Streamlit web app)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$RUN_USER
WorkingDirectory=$APP_DIR
EnvironmentFile=$APP_DIR/.env
ExecStart=$APP_DIR/venv/bin/streamlit run app.py --server.port $PORT --server.address 0.0.0.0
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
UNIT
sudo systemctl daemon-reload
sudo systemctl enable --now oracle-web.service

# 5) cron for the scheduled jobs (current user, IST) --------------------------
echo "==> Installing crontab for $RUN_USER..."
chmod +x "$WRAP"
mkdir -p "$APP_DIR/logs"
CRON_BLOCK=$(cat <<CRON
# >>> oracle jobs (managed by deploy/setup.sh) >>>
CRON_TZ=Asia/Kolkata
35 15 * * * $WRAP signals  >> $APP_DIR/logs/cron.log 2>&1
40 15 * * * $WRAP download >> $APP_DIR/logs/cron.log 2>&1
0 16 * * *  $WRAP backup   >> $APP_DIR/logs/cron.log 2>&1
30 8 * * *  $WRAP summary   >> $APP_DIR/logs/cron.log 2>&1
# <<< oracle jobs <<<
CRON
)
{ crontab -l 2>/dev/null | sed '/# >>> oracle jobs/,/# <<< oracle jobs/d' || true; echo "$CRON_BLOCK"; } | crontab -

echo ""
echo "==> Done."
echo "    Web app : http://<vps-ip>:$PORT   (systemctl status oracle-web)"
echo "    Web logs: journalctl -u oracle-web -f"
echo "    Jobs    : crontab -l        | job logs: $APP_DIR/logs/cron.log"
