"""One-time migration: remove the momentum strategy's tables and grants.

Usage (from the project root):

    python -m migrations.drop_momentum [path-to-db] [--dry-run]

Defaults to ``oracle.db``. Safe to re-run.

Why: the momentum feature (Nifty 500 ranking, rebalance advisory, ledger) has
been taken out of the app. Its tables are no longer created or read, so this
drops what an existing database still carries:

  momentum_config, momentum_holdings, momentum_trades, momentum_rankings,
  momentum_delivery

and strips the ``momentum.*`` page grants from ``users.permissions``. Those
grants are already ignored on load (unknown pages are dropped), so that part is
housekeeping. The price cache under ``data/momentum`` is plain files — delete
the directory by hand.

The drop is irreversible; copy the database first if the history matters.
"""
import argparse
import json
import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TABLES = ['momentum_config', 'momentum_holdings', 'momentum_trades',
          'momentum_rankings', 'momentum_delivery']
PREFIX = 'momentum.'


def _table_exists(conn, table):
    cur = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,))
    return cur.fetchone() is not None


def drop_tables(conn, dry_run=False):
    dropped = []
    for table in TABLES:
        if not _table_exists(conn, table):
            continue
        rows = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        print(f"  Dropping {table} ({rows} rows)")
        dropped.append(table)
        if not dry_run:
            conn.execute(f"DROP TABLE {table}")
    if not dry_run:
        conn.commit()
    if not dropped:
        print("  No momentum tables left.")
    return dropped


def strip_grants(conn, dry_run=False):
    if not _table_exists(conn, 'users'):
        return 0
    changed = 0
    for user_id, username, text in list(conn.execute(
            "SELECT id, username, permissions FROM users WHERE permissions LIKE ?",
            (f'%{PREFIX}%',))):
        try:
            perms = json.loads(text)
        except (TypeError, ValueError):
            continue
        if not isinstance(perms, dict):
            continue
        kept = {k: v for k, v in perms.items() if not k.startswith(PREFIX)}
        if kept == perms:
            continue
        changed += 1
        print(f"  Stripping momentum grants from {username}")
        if not dry_run:
            conn.execute("UPDATE users SET permissions = ? WHERE id = ?",
                         (json.dumps(kept, sort_keys=True), user_id))
    if not dry_run:
        conn.commit()
    if not changed:
        print("  No momentum grants held.")
    return changed


def main():
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("db", nargs="?", default="oracle.db")
    parser.add_argument("--dry-run", action="store_true",
                        help="report what would change, write nothing")
    args = parser.parse_args()

    print(f"== Drop-momentum migration on {args.db} ==")
    if args.dry_run:
        print("   (dry run — nothing will be written)\n")

    conn = sqlite3.connect(args.db)
    try:
        print("Tables:")
        dropped = drop_tables(conn, args.dry_run)
        print("Grants:")
        strip_grants(conn, args.dry_run)
        if dropped and not args.dry_run:
            conn.execute("VACUUM")  # hand the freed pages back to the disk
    finally:
        conn.close()
    print("\nDone.")


if __name__ == "__main__":
    main()
