"""Scheduled-job orchestration for the downloader module.

These tie the download/backup services to shared infra (DB, broker config,
email). The bot daemon imports and schedules them; keeping them here keeps the
daemon thin and the cross-cutting wiring inside the module that owns it.
"""
import datetime

import pytz

from common.database import BrokerConfig, DownloadJob, get_db
from common.notifications import send_email
from downloader.services import core
from downloader.services.backup import backup_db_to_drive

IST = pytz.timezone('Asia/Kolkata')


def run_daily_download():
    """Auto-download today's market data, upload to Drive and email a summary."""
    now_ist = datetime.datetime.now(IST)
    if now_ist.weekday() >= 5:
        return

    print(f"[{now_ist.strftime('%H:%M:%S')}] Running daily market-data download...")
    db = next(get_db())
    broker_config = db.query(BrokerConfig).filter(BrokerConfig.broker_name == 'ZERODHA').first()
    if not broker_config or not broker_config.enctoken:
        print("Daily download skipped: no Zerodha enctoken configured.")
        db.close()
        return

    today = now_ist.date()
    job = DownloadJob(
        job_type="auto", status="running",
        start_date=today.isoformat(), end_date=today.isoformat(),
        symbols="NIFTY,BANKNIFTY",
    )
    db.add(job)
    db.commit()

    report = core.run_download(
        enctoken=broker_config.enctoken,
        user_id=broker_config.user_id,
        start_date=today,
        end_date=today,
        symbols=["NIFTY", "BANKNIFTY"],
        skip_upload_today=False,  # bot runs after market close, so upload today's data
        upload=True,
        db=db,
    )

    job.status = "failed" if (report.fatal or report.errors) else "completed"
    job.message = report.fatal or (report.errors[0] if report.errors else
                                   f"{len(report.trading_days)} day(s), {report.total_files} files")
    db.commit()
    db.close()

    subject = ("📥 Oracle Download " + ("FAILED" if report.fatal else "Report")
               + f" — {report.start_date}")
    send_email(core.report_html(report), subject)


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
