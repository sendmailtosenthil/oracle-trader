"""Database backup to Google Drive — keeps the last N dated snapshots.

Takes a consistent SQLite snapshot (via the online backup API, safe even while
the app holds the DB open) and uploads it to a ``db-backups`` folder under the
Drive root, then prunes to the most recent ``keep`` versions. Guards against
data loss without manual intervention.
"""
import datetime
import os
import sqlite3
import tempfile

from common.timez import today_ist

DEFAULT_DB_PATH = os.environ.get("ORACLE_DB_PATH", "oracle.db")
DRIVE_ROOT_FOLDER = os.environ.get("DRIVE_ROOT_FOLDER", "QuantData")
BACKUP_FOLDER = os.environ.get("DRIVE_DB_BACKUP_FOLDER", "db-backups")


def backup_db_to_drive(db_path=None, keep=3, date_str=None):
    """Snapshot the DB and upload to Drive, keeping only the latest ``keep``.

    Returns a result dict: ``{status, file, kept, deleted, error}``.
    ``status`` is ``uploaded`` / ``skipped`` / ``failed``.
    """
    db_path = db_path or DEFAULT_DB_PATH
    date_str = date_str or today_ist().isoformat()
    result = {"status": "skipped", "file": None, "kept": [], "deleted": [], "error": ""}

    if not os.path.exists(db_path):
        result["error"] = f"DB not found at {db_path}"
        return result

    tmp_fd, tmp_path = tempfile.mkstemp(suffix=".db")
    os.close(tmp_fd)
    try:
        _snapshot_sqlite(db_path, tmp_path)

        from downloader.services.drive import GoogleDriveUploader
        uploader = GoogleDriveUploader()
        root_id = uploader.ensure_folder(DRIVE_ROOT_FOLDER, "root")
        folder_id = uploader.ensure_folder(BACKUP_FOLDER, root_id)

        file_name = f"oracle-{date_str}.db"
        uploader.upload_file(tmp_path, file_name, folder_id,
                             mime_type="application/x-sqlite3")
        result["file"] = file_name
        result["status"] = "uploaded"

        # Prune to the most recent `keep` snapshots.
        existing = uploader.list_files(folder_id, name_contains="oracle-")  # newest first
        result["kept"] = [f["name"] for f in existing[:keep]]
        for stale in existing[keep:]:
            uploader.delete_file(stale["id"])
            result["deleted"].append(stale["name"])
    except Exception as exc:  # noqa: BLE001
        result["status"] = "failed"
        result["error"] = str(exc)
    finally:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)

    return result


def _snapshot_sqlite(src_path, dst_path):
    """Consistent copy of a SQLite DB using the online backup API."""
    src = sqlite3.connect(src_path)
    try:
        dst = sqlite3.connect(dst_path)
        try:
            src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()
