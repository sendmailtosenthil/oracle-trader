"""One-time migration: give `users` per-page permissions and an admin flag.

Usage (from the project root):

    python -m migrations.add_user_permissions [path-to-db]

Defaults to ``oracle.db`` in the current directory. Safe to re-run: every step
checks before it acts, so a second run reports "already applied" and changes
nothing.

What it does:
  1. ALTER TABLE users ADD COLUMN is_admin / permissions / created_at (if missing).
  2. Marks every login that predates this change as an administrator — they had
     unrestricted access before, so nobody is locked out by the upgrade.
  3. Stamps a created_at on rows that have none.

Logins created from here on are managed in the app (Setup > User Management) or
with ``scripts/manage_users.py``; ORACLE_ADMIN_PASSWORD only bootstraps the very
first administrator on an empty database.

The running app applies the same column changes itself on startup (see
``common.database._ensure_columns``), so this script is for migrating a database
ahead of a deploy, or for seeing exactly what changed.
"""
import datetime
import sqlite3
import sys


def _column_exists(conn, table, col):
    cur = conn.execute(f"PRAGMA table_info({table})")
    return any(r[1] == col for r in cur.fetchall())


def _table_exists(conn, table):
    cur = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,)
    )
    return cur.fetchone() is not None


def ensure_columns(conn):
    """Add the new columns (SQLAlchemy's create_all won't ALTER an existing table).

    ``is_admin`` is added with DEFAULT 1 deliberately: the rows already in the
    table are the pre-permissions logins, and they had full access.
    """
    added = []
    if not _column_exists(conn, 'users', 'is_admin'):
        conn.execute("ALTER TABLE users ADD COLUMN is_admin BOOLEAN DEFAULT 1")
        added.append("users.is_admin")
    if not _column_exists(conn, 'users', 'permissions'):
        conn.execute("ALTER TABLE users ADD COLUMN permissions VARCHAR")
        added.append("users.permissions")
    if not _column_exists(conn, 'users', 'created_at'):
        conn.execute("ALTER TABLE users ADD COLUMN created_at DATETIME")
        added.append("users.created_at")
    conn.commit()

    for col in added:
        print(f"Added column {col}")
    if not added:
        print("Columns already present — nothing to add.")
    return added


def backfill(conn):
    """Existing logins become administrators; every row gets a created_at."""
    promoted = conn.execute(
        "UPDATE users SET is_admin = 1 WHERE is_admin IS NULL"
    ).rowcount
    # Written as text in SQLAlchemy's own DATETIME format, so the ORM reads it
    # back as a datetime. Timestamps in this database are naive UTC.
    now = datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S.%f")
    stamped = conn.execute(
        "UPDATE users SET created_at = ? WHERE created_at IS NULL", (now,)
    ).rowcount
    conn.commit()

    # Rows that were already in the table are promoted by the ALTER's DEFAULT 1;
    # this UPDATE only catches rows the column was added to as NULL. Report the
    # resulting total either way, so the outcome is unambiguous.
    admins = conn.execute("SELECT COUNT(*) FROM users WHERE is_admin = 1").fetchone()[0]
    if promoted:
        print(f"Promoted {promoted} login(s) with no flag set.")
    print(f"{admins} login(s) now have administrator access.")
    print(f"Stamped created_at on {stamped} row(s)." if stamped
          else "Every row already had a created_at.")


def report(conn):
    rows = conn.execute(
        "SELECT username, is_admin, permissions FROM users ORDER BY username"
    ).fetchall()
    if not rows:
        print("\nNo users yet — the first administrator is created on app startup "
              "from ORACLE_ADMIN_USER / ORACLE_ADMIN_PASSWORD.")
        return
    print(f"\n{len(rows)} login(s):")
    for username, is_admin, perms in rows:
        if is_admin:
            print(f"  {username:<16} administrator — full access")
        else:
            granted = [f"{k}={v}" for k, v in _granted(perms)]
            print(f"  {username:<16} {', '.join(granted) or 'no pages granted'}")


def _granted(perms_json):
    """The non-``none`` entries of a stored permission map."""
    from common import permissions as P
    return [(k, v) for k, v in sorted(P.loads(perms_json).items()) if v != P.NONE]


def main():
    path = sys.argv[1] if len(sys.argv) > 1 else 'oracle.db'
    print(f"== User permissions migration on {path} ==")

    conn = sqlite3.connect(path)
    try:
        if not _table_exists(conn, 'users'):
            print("No `users` table — this database has never run the app. "
                  "Start the app once and it will be created.")
            return
        ensure_columns(conn)
        backfill(conn)
        report(conn)
    finally:
        conn.close()
    print("\nDone.")


if __name__ == '__main__':
    main()
