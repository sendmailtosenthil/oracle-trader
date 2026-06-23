"""Scheduled-job orchestration for the downloader module.

Only the lightweight DB backup is scheduled here. Market-data downloads are
deliberately NOT scheduled — they are memory-heavy (full option chains) and the
VPS can't run them unattended — so downloads happen only on user click via the
Options Download page (``downloader.views.page`` -> ``core.run_download``).
"""
import datetime

import pytz

from common.database import DownloadJob, get_db
from common.notifications import send_email
from downloader.services.backup import backup_db_to_drive

IST = pytz.timezone('Asia/Kolkata')


def run_db_backup():
    """Back up the DB to Drive (keep last 3) — only when there is no activity.

    Skips if a download job is still ``running`` so we never snapshot mid-write.
    """
    now_ist = datetime.datetime.now(IST)
    db = next(get_db())
    active = (
        db.query(DownloadJob)
        .filter(DownloadJob.status == 'running')
        .first()
    )
    db.close()
    if active:
        print("DB backup skipped: a download job is still running (activity in progress).")
        return

    print(f"[{now_ist.strftime('%H:%M:%S')}] Backing up database to Drive...")
    result = backup_db_to_drive(keep=3, date_str=now_ist.date().isoformat())

    if result["status"] == "uploaded":
        html = f"""
        <h2>🗄️ Oracle DB Backup</h2>
        <p>Backed up <b>{result['file']}</b> to Google Drive.</p>
        <p>Versions retained: {', '.join(result['kept']) or '—'}</p>
        <p>Old versions deleted: {', '.join(result['deleted']) or 'none'}</p>
        """
        send_email(html, f"🗄️ Oracle DB Backup — {result['file']}")
    elif result["status"] == "failed":
        send_email(
            f"<h2>⚠️ Oracle DB Backup FAILED</h2><p>{result['error']}</p>",
            "⚠️ Oracle DB Backup FAILED",
        )
    else:
        print(f"DB backup skipped: {result['error']}")
