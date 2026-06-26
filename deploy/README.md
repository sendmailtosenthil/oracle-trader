# Bare-metal deploy (venv + systemd + cron)

Run Project Oracle directly on the VPS without Docker. Lighter on RAM (no
docker/containerd daemons) and simpler to iterate (`git pull` + restart).

## First-time setup
```bash
cd ~/oracle-trader            # the repo
bash deploy/setup.sh          # creates venv, .env, then exits asking you to edit .env
nano .env                     # set GMAIL_USER / GMAIL_PASS (+ optional overrides)
bash deploy/setup.sh          # run again: installs service + cron, starts the app
```
`credentials.json` and `token.json` (Google Drive) must be in the repo root, as before.

## What it installs
- **systemd service `oracle-web`** — the Streamlit app on port 8501, `Restart=always`, starts on boot.
- **User crontab** — the four jobs in IST: `signals` 15:35, `download` 15:40, `backup` 16:00, `summary` 08:30. Each runs `python -m bees.bot <job>` from the venv and exits.

## Day-to-day
```bash
# update
git pull && venv/bin/pip install -r requirements.txt   # pip only if deps changed
sudo systemctl restart oracle-web

# status / logs
systemctl status oracle-web
journalctl -u oracle-web -f
tail -f logs/cron.log

# run a job by hand
deploy/run-job.sh backup
```

## Uninstall / fall back to Docker
```bash
sudo systemctl disable --now oracle-web && sudo rm /etc/systemd/system/oracle-web.service && sudo systemctl daemon-reload
crontab -l | sed '/# >>> oracle jobs/,/# <<< oracle jobs/d' | crontab -
# then bring Docker back up: docker compose up -d
```
