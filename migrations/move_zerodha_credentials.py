"""One-time migration: Zerodha accounts get an owner, and credentials leave .env.

Usage (from the project root):

    python -m migrations.move_zerodha_credentials [path-to-db] [--env PATH] [--owner USER]
    python -m migrations.move_zerodha_credentials --dry-run

Defaults to ``oracle.db`` and ``.env`` in the current directory, and to the
first administrator as the owner. Safe to re-run: each step checks before it
acts.

What it does:
  1. ALTER TABLE broker_config ADD COLUMN owner / password_enc / totp_enc /
     created_at, and ztrade_groups ADD COLUMN owner / shared (if missing).
  2. Reads ZERODHA_USER_ID / ZERODHA_PASSWORD / ZERODHA_TOTP_SECRET from ``.env``
     (or the environment) and stores them, encrypted, on the matching account.
  3. Claims every unowned Zerodha account and trade group for ``--owner``, so
     the person who has been running the system keeps working exactly as before.
     Groups stay unshared: sharing is opt-in, per group.

Afterwards, delete ZERODHA_PASSWORD and ZERODHA_TOTP_SECRET from ``.env`` — the
app no longer reads them once they are stored, and the point of the move is that
the only copy is the encrypted one. The script prints a reminder.

The encryption key is created at ``data/secret.key`` on first use and is NOT in
the database (which is uploaded to Google Drive nightly). Back it up separately;
without it the stored credentials can only be re-entered, not recovered.
"""
import argparse
import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from common import secrets as sec


def _column_exists(conn, table, col):
    return any(r[1] == col for r in conn.execute(f"PRAGMA table_info({table})"))


def _table_exists(conn, table):
    cur = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,))
    return cur.fetchone() is not None


WANTED = {
    'broker_config': [('owner', 'VARCHAR'),
                      ('password_enc', 'VARCHAR'),
                      ('totp_enc', 'VARCHAR'),
                      ('created_at', 'DATETIME')],
    'ztrade_groups': [('owner', 'VARCHAR'),
                      ('shared', 'BOOLEAN DEFAULT 0')],
}


def ensure_columns(conn, dry_run=False):
    added = []
    for table, cols in WANTED.items():
        if not _table_exists(conn, table):
            print(f"  (no {table} table yet — the app creates it on first run)")
            continue
        for name, decl in cols:
            if _column_exists(conn, table, name):
                continue
            added.append(f"{table}.{name}")
            if not dry_run:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")
    if not dry_run:
        conn.commit()
    for col in added:
        print(f"  {'would add' if dry_run else 'Added'} column {col}")
    if not added:
        print("  Columns already present.")
    return added


def read_env_file(path):
    """Parse a KEY=VALUE ``.env`` into a dict. Missing file → empty."""
    values = {}
    if not os.path.exists(path):
        return values
    with open(path) as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            values[key.strip()] = val.strip().strip('"').strip("'")
    return values


def pick_owner(conn, requested):
    """The username to hand unowned records to."""
    if requested:
        row = conn.execute("SELECT username FROM users WHERE username = ?",
                           (requested.strip().lower(),)).fetchone()
        if not row:
            raise SystemExit(f"error: no such user '{requested}' — run "
                             "`python scripts/manage_users.py list`")
        return row[0]
    if not _table_exists(conn, 'users'):
        return None
    row = conn.execute(
        "SELECT username FROM users WHERE is_admin = 1 ORDER BY id LIMIT 1").fetchone()
    return row[0] if row else None


def import_credentials(conn, env, owner, dry_run=False):
    """Move ZERODHA_PASSWORD / ZERODHA_TOTP_SECRET onto their account row."""
    user_id = (env.get("ZERODHA_USER_ID") or os.environ.get("ZERODHA_USER_ID") or "").strip()
    password = (env.get("ZERODHA_PASSWORD") or os.environ.get("ZERODHA_PASSWORD") or "").strip()
    totp = (env.get("ZERODHA_TOTP_SECRET") or os.environ.get("ZERODHA_TOTP_SECRET") or "").strip()

    if not (password or totp):
        print("  No ZERODHA_PASSWORD / ZERODHA_TOTP_SECRET found — nothing to move.")
        print("  (Set them on Setup > Zerodha Accounts instead.)")
        return False

    user_id = (user_id or "PC8006").upper()
    if not _column_exists(conn, 'broker_config', 'password_enc'):
        print(f"  would encrypt and store the credentials for {user_id} "
              "(once the columns exist)")
        return True
    row = conn.execute(
        "SELECT id, user_id, password_enc, totp_enc FROM broker_config "
        "WHERE UPPER(user_id) = ?", (user_id,)).fetchone()
    if row is None:
        print(f"  ⚠️  No broker_config row for {user_id} — add the account on "
              "Setup > Zerodha Accounts, then re-run.")
        return False

    if row[2] or row[3]:
        print(f"  {user_id} already has stored credentials — leaving them alone.")
        return False

    if not sec.available():
        raise SystemExit("error: " + sec.install_hint())

    what = " + ".join(n for n, v in (("password", password), ("TOTP secret", totp)) if v)
    if dry_run:
        print(f"  would encrypt and store {what} for {user_id}")
        return True

    conn.execute(
        "UPDATE broker_config SET password_enc = ?, totp_enc = ? WHERE id = ?",
        (sec.encrypt(password), sec.encrypt(totp), row[0]))
    conn.commit()
    print(f"  Stored {what} for {user_id}, encrypted.")
    return True


def claim(conn, owner, dry_run=False):
    """Give every unowned account and group to ``owner``."""
    if not owner:
        print("  No administrator found — skipping. Create one first "
              "(`python scripts/manage_users.py list`), then re-run.")
        return

    for table, what in (('broker_config', 'Zerodha account'),
                        ('ztrade_groups', 'trade group')):
        if not _table_exists(conn, table):
            continue
        if not _column_exists(conn, table, 'owner'):
            # Only reachable on a dry run, where the ALTER above was skipped.
            print(f"  would assign every unowned {what} to '{owner}' "
                  "(once the columns exist)")
            continue
        n = conn.execute(
            f"SELECT COUNT(*) FROM {table} WHERE owner IS NULL OR TRIM(owner) = ''"
        ).fetchone()[0]
        if not n:
            print(f"  Every {what} already has an owner.")
            continue
        if dry_run:
            print(f"  would assign {n} {what}(s) to '{owner}'")
            continue
        conn.execute(
            f"UPDATE {table} SET owner = ? WHERE owner IS NULL OR TRIM(owner) = ''",
            (owner,))
        print(f"  Assigned {n} {what}(s) to '{owner}'.")
    if not dry_run:
        conn.commit()


def report(conn):
    if not _column_exists(conn, 'broker_config', 'owner'):
        print("\n(Nothing to report yet — run without --dry-run to apply.)")
        return
    print("\nZerodha accounts:")
    for uid, owner, pw, totp, enc in conn.execute(
            "SELECT user_id, owner, password_enc, totp_enc, enctoken "
            "FROM broker_config ORDER BY user_id"):
        creds = "auto-login stored" if (pw and totp) else "no stored credentials"
        token = "token set" if enc else "no token"
        print(f"  {uid:<10} owner={owner or '—':<12} {creds}, {token}")

    if _table_exists(conn, 'ztrade_groups'):
        rows = list(conn.execute(
            "SELECT name, user_id, owner, shared FROM ztrade_groups ORDER BY id"))
        print(f"\nTrade groups ({len(rows)}):")
        for name, uid, owner, shared in rows:
            flag = " · shared" if shared else ""
            print(f"  {name:<24} {uid:<10} owner={owner or '—'}{flag}")


def main():
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("db", nargs="?", default="oracle.db")
    parser.add_argument("--env", default=".env", help="path to the .env to read")
    parser.add_argument("--owner", help="app username to claim unowned records "
                                        "(default: the first administrator)")
    parser.add_argument("--dry-run", action="store_true",
                        help="report what would change, write nothing")
    args = parser.parse_args()

    print(f"== Zerodha ownership + credentials migration on {args.db} ==")
    if args.dry_run:
        print("   (dry run — nothing will be written)\n")

    conn = sqlite3.connect(args.db)
    try:
        if not _table_exists(conn, 'broker_config'):
            print("No `broker_config` table — start the app once first.")
            return

        print("Schema:")
        ensure_columns(conn, args.dry_run)

        owner = pick_owner(conn, args.owner)
        print(f"\nOwnership (claiming for '{owner or 'nobody'}'):")
        claim(conn, owner, args.dry_run)

        print(f"\nCredentials (from {args.env}):")
        moved = import_credentials(conn, read_env_file(args.env), owner, args.dry_run)

        report(conn)

        if moved and not args.dry_run:
            print("\n⚠️  Now delete ZERODHA_PASSWORD and ZERODHA_TOTP_SECRET from "
                  f"{args.env} and restart the app — the encrypted copy in the "
                  "database is the one that counts.")
            print("⚠️  Back up data/secret.key somewhere safe. It is not in the "
                  "database (nor in the nightly Drive backup); without it the "
                  "stored credentials must be re-entered.")
    finally:
        conn.close()
    print("\nDone.")


if __name__ == "__main__":
    main()
