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
- **System timezone set to `Asia/Kolkata`** — Ubuntu's cron ignores `CRON_TZ`, so the box itself is put on IST and cron's system-local time *is* IST. Without this, jobs run in UTC (5.5h late). App logic is unaffected (it uses explicit `datetime.now(IST)`).
- **User crontab** — the four jobs in IST: `signals` 15:35, `download` 15:40, `backup` 16:00, `summary` 08:30. Each runs `python -m bees.bot <job>` from the venv and exits.

## Logins
Users live in `oracle.db` and are managed in the app under **Setup › User
Management** (administrators only). Each user gets a level per page — no
access, read only, or edit — so a viewer can be given, say, the Bees dashboard
without the ledger.

Upgrading a database that predates per-page permissions? The app applies the
schema change itself on startup, so a normal `git pull` + restart is enough.
To do it ahead of time (and see exactly what changed):
```bash
venv/bin/python -m migrations.add_user_permissions   # idempotent
```
Existing logins become administrators, so nobody is locked out by the upgrade.

`ORACLE_ADMIN_USER` / `ORACLE_ADMIN_PASSWORD` in `.env` only bootstrap the first
administrator on an empty database; after that the database is the source of
truth and those values are ignored. When nobody can sign in, use the host:
```bash
venv/bin/python scripts/manage_users.py list          # who exists, and their access
venv/bin/python scripts/manage_users.py passwd senthil
venv/bin/python scripts/manage_users.py pages         # grantable pages and levels
venv/bin/python scripts/manage_users.py add viewer --grant bees.dashboard=read
```
Changing a password or deleting a user drops that user's remembered browser
sessions immediately.

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
