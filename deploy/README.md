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
- **`MALLOC_ARENA_MAX=2` in `.env`** — glibc gives each thread its own malloc
  arena, which inflates RSS on a threaded Python process well past what is live.
  Worth real memory back on a sub-1GB host. It lives in `.env` rather than the
  units so it reaches the cron jobs too (`run-job.sh` exports that file, and the
  downloader is the hungriest process here). On an existing install add the line
  yourself — `setup.sh` only seeds `.env` when it is missing:
  ```bash
  echo 'MALLOC_ARENA_MAX=2' >> .env
  sudo systemctl restart oracle-web oracle-api
  ```
- **System timezone set to `Asia/Kolkata`** — Ubuntu's cron ignores `CRON_TZ`, so the box itself is put on IST and cron's system-local time *is* IST. Without this, jobs run in UTC (5.5h late). App logic is unaffected (it uses explicit `datetime.now(IST)`).
- **User crontab** — the four jobs in IST: `signals` 15:45, `download` 15:40, `backup` 16:00, `summary` 08:30. Each runs `python -m bees.bot <job>` from the venv and exits. `signals` sits at 15:45 (`DATA_CUTOFF` in `common/market_calendar.py`) so the day's closing candle has landed; if it hasn't, the scan falls back to the previous session rather than reading a partial bar.

## Logins
Users live in `oracle.db` and are managed in the app under **Setup › User
Management** (administrators only). Each user gets a level per page — no
access, read only, or edit — so a viewer can be given, say, the Bees dashboard
without the ledger.

## Zerodha accounts
Kite logins live in the database too, on **Setup › Zerodha Accounts**, with the
password and TOTP secret that let the 8:10am job refresh the enctoken by itself.

An account belongs to the user who added it. Everyone who can open the Setup
pages sees that it exists — its Kite id, owner, and whether its token is live —
but only the owner (and administrators) can see or change its password, TOTP
secret and enctoken, or remove it. Zerodha Trades follows the same line: your
account tabs are the accounts you added, and a trade group is editable only by
whoever created it. A group can be shared for others to *view* with a per-group
toggle, off by default.

The two secrets are encrypted at rest with a key held **outside** the database,
because the nightly backup uploads the database to Google Drive. The key is
`ORACLE_SECRET_KEY` if set, otherwise `data/secret.key`, generated 0600 on first
use and excluded from both git and the backup:
```bash
cp data/secret.key /somewhere/safe/          # do this once
```
Lose it and the stored credentials can only be re-entered (nothing else is
affected). Restoring a backup onto a host with a different key shows a
"can't be decrypted" warning on the account, with a field to re-enter them.

Migrating credentials out of `.env` (see what it would do first):
```bash
venv/bin/python -m migrations.move_zerodha_credentials --dry-run
venv/bin/python -m migrations.move_zerodha_credentials
```
It adds the ownership columns, hands existing accounts and groups to the first
administrator, and moves `ZERODHA_PASSWORD` / `ZERODHA_TOTP_SECRET` into the
database encrypted. Delete those two lines from `.env` afterwards — the script
reminds you. Use `--owner USER` to assign to somebody other than the first
administrator.

## Upgrades
The app applies its own additive schema changes on startup, so a normal
`git pull` + restart is enough. Each migration can also be run ahead of time to
see exactly what it would change — all are idempotent and take `--dry-run`
except the first:
```bash
venv/bin/python -m migrations.add_user_permissions        # logins -> per-page permissions
venv/bin/python -m migrations.move_zerodha_credentials    # accounts -> owners + encrypted creds
venv/bin/python -m migrations.add_settled_pnl             # leg P&L -> banked + live
venv/bin/python -m migrations.add_group_baseline          # groups -> deploy-time SD reference
venv/bin/python -m migrations.add_group_levels_mode       # groups -> fixed or automatic levels
```
Existing logins become administrators, so nobody is locked out by the upgrade.

`add_group_baseline` records, when a group is deployed, where its underlying
stood and how far the market implied it could travel before the front expiry.
The payoff chart draws that frozen range in light grey behind the live one, so
the gap between their centres is the drift since arming. Nothing is backfilled:
a group deployed before the upgrade has no honest baseline available, and picks
one up on its next redeploy.

`add_group_levels_mode` makes the stoploss and target a choice rather than two
boxes you must fill in. A group is either `fixed` — you type them, nothing else
moves them — or `auto`, where creating it asks only for a name and the app manages
the levels from the **expected profit**: the premium the open legs were sold for
plus whatever the closed ones actually made. The opening stoploss is that whole
figure and the opening target 50% of it; the stoploss then trails the profit in
steps of at least ₹300 (per group) and never widens, and reaching a target
notifies every channel, drops the stoploss to break-even and climbs the ladder
50% → 70% → 85%, the last rung advising that the trade be closed. Closing a leg or
adding one re-prices all of it. Typing over either level hands the group to you
permanently — it becomes a fixed-levels group. Every move is recorded in the new
`ztrade_group_level_events` table and drawn as the stoploss journey on the
dashboard, and deleting a group deletes its history with it. Every existing group
is backfilled to `fixed`, which is what it already was; groups deployed before the
upgrade have no recorded journey to reconstruct and start one at their next
arming.

`add_settled_pnl` splits a trade leg's P&L into what closed cycles already made
and what the position currently held is doing. That is what lets a contract be
closed and re-opened without losing the first cycle: the two figures add up, the
banked one survives Zerodha dropping the position from the book, and it can be
corrected by hand once nothing is running against it. A correction applies to the
total as it stood when typed, so later cycles still add on top of it.

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
