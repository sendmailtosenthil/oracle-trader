"""One-time migration: remove the BANK vs IT (BANKBEES / ITBEES) strategy.

Usage (from the project root):

    python -m migrations.drop_bank_it [path-to-db] [--dry-run]

Defaults to ``oracle.db``. Safe to re-run.

Why: the BANK vs IT pair has been taken out of the app — it is no longer seeded,
so once its rows are gone it stays gone. NIFTY vs GOLD is untouched. This
deletes the strategy and everything hanging off it:

  pending_switches, trades, cash_flows, portfolio   (rows for that strategy)
  strategies                                         (the strategy itself)

Only the app's record goes; units actually held at the broker are not touched.
The delete is irreversible; copy the database first if the history matters.
"""
import argparse
import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

NAME = 'BANK vs IT'
# Children first: they point at strategies.id.
CHILDREN = ['pending_switches', 'trades', 'cash_flows', 'portfolio']


def drop_strategy(conn, dry_run=False):
    row = conn.execute("SELECT id FROM strategies WHERE name = ?", (NAME,)).fetchone()
    if row is None:
        print(f"  No '{NAME}' strategy left.")
        return False
    strategy_id = row[0]
    for table in CHILDREN:
        n = conn.execute(f"SELECT COUNT(*) FROM {table} WHERE strategy_id = ?",
                         (strategy_id,)).fetchone()[0]
        print(f"  Deleting {n} row(s) from {table}")
        if not dry_run:
            conn.execute(f"DELETE FROM {table} WHERE strategy_id = ?", (strategy_id,))
    print(f"  Deleting strategy '{NAME}' (id {strategy_id})")
    if not dry_run:
        conn.execute("DELETE FROM strategies WHERE id = ?", (strategy_id,))
        conn.commit()
    return True


def main():
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("db", nargs="?", default="oracle.db")
    parser.add_argument("--dry-run", action="store_true",
                        help="report what would change, write nothing")
    args = parser.parse_args()

    print(f"== Drop BANK vs IT migration on {args.db} ==")
    if args.dry_run:
        print("   (dry run — nothing will be written)\n")

    conn = sqlite3.connect(args.db)
    try:
        drop_strategy(conn, args.dry_run)
        left = [r[0] for r in conn.execute("SELECT name FROM strategies ORDER BY id")]
        print(f"\nStrategies: {', '.join(left) or 'none'}")
    finally:
        conn.close()
    print("\nDone.")


if __name__ == "__main__":
    main()
