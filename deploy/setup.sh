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

# Chromium for the headless Zerodha login (daily enctoken refresh). --with-deps
# also pulls the OS shared libraries Chromium needs (needs sudo/apt); it's a
# one-shot ~150MB download and the browser only runs for a few seconds a day.
echo "==> Installing Playwright Chromium (headless login)..."
"$APP_DIR/venv/bin/python" -m playwright install --with-deps chromium \
    || "$APP_DIR/venv/bin/python" -m playwright install chromium \
    || echo "!! Chromium install failed — 'relogin' will be disabled until you run: venv/bin/python -m playwright install --with-deps chromium"

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

# 5) system timezone -> IST ---------------------------------------------------
# Ubuntu's stock cron does NOT honour the `CRON_TZ` setting — it silently treats
# it as a plain env var and schedules in system-local time. On a UTC box that
# runs every job 5.5h late. The market is IST-only, so put the whole box on IST;
# cron's system-local time is then IST and the crontab times below are literal.
# (App logic is unaffected — it uses explicit datetime.now(IST) regardless.)
echo "==> Setting system timezone to Asia/Kolkata (sudo)..."
sudo timedatectl set-timezone Asia/Kolkata

# 6) cron for the scheduled jobs (system-local time is now IST) ----------------
echo "==> Installing crontab for $RUN_USER..."
chmod +x "$WRAP"
mkdir -p "$APP_DIR/logs"
# CRON_TZ is kept as documentation of intent; the real guarantee is the system
# timezone set above (Ubuntu cron ignores CRON_TZ).
CRON_BLOCK=$(cat <<CRON
# >>> oracle jobs (managed by deploy/setup.sh) >>>
CRON_TZ=Asia/Kolkata
29 15 * * * $WRAP relogin  >> $APP_DIR/logs/cron.log 2>&1
35 15 * * * $WRAP signals  >> $APP_DIR/logs/cron.log 2>&1
40 15 * * * $WRAP download >> $APP_DIR/logs/cron.log 2>&1
0 16 * * *  $WRAP backup   >> $APP_DIR/logs/cron.log 2>&1
0 17 * * 1-5 $WRAP momentum >> $APP_DIR/logs/cron.log 2>&1
30 8 * * *  $WRAP summary   >> $APP_DIR/logs/cron.log 2>&1
# <<< oracle jobs <<<
CRON
)
{ crontab -l 2>/dev/null | sed '/# >>> oracle jobs/,/# <<< oracle jobs/d' || true; echo "$CRON_BLOCK"; } | crontab -

# cron caches the timezone; restart it so the IST switch takes effect now.
sudo systemctl restart cron

echo ""
echo "==> Done."
echo "    Web app : http://<vps-ip>:$PORT   (systemctl status oracle-web)"
echo "    Web logs: journalctl -u oracle-web -f"
echo "    Jobs    : crontab -l        | job logs: $APP_DIR/logs/cron.log"
echo "    Timezone: $(timedatectl show -p Timezone --value 2>/dev/null || echo '?')  (jobs run in this zone)"
