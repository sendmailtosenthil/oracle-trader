"""One-time migration: a group remembers the range it was armed against.

Usage (from the project root):

    python -m migrations.add_group_baseline [path-to-db] [--dry-run]

Defaults to ``oracle.db``. Safe to re-run.

Why: the payoff chart draws ±1SD and ±2SD around the underlying's current
price, and those bands move constantly — mostly because ``sigma`` scales with
the square root of the time left, so they narrow every day whether or not the
market has repriced anything. A band on its own therefore says little about
whether the trade has gone the way you expected.

So a group now freezes, at the moment it is deployed:

  baseline_spot    where the underlying stood
  baseline_sigma   1SD in points, over the distance to the front expiry
  baseline_iv      the at-the-money implied vol those points came from
  baseline_at      when it was taken

The chart draws that frozen range in light grey behind the live one, so the
distance between the two centres is how far the underlying has drifted since
you armed the group, and the difference in width is how the expected range has
changed.

Nothing is backfilled. A group deployed before this migration has no honest
baseline available — the spot and vol of that moment are gone, and inventing
them from today's would draw a reference line that never existed. Those groups
show only the live bands until they are next undeployed and redeployed, which
is exactly when a fresh baseline is taken.
"""
import argparse
import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TABLE = 'ztrade_groups'
COLUMNS = [('baseline_spot', 'FLOAT'),
           ('baseline_sigma', 'FLOAT'),
           ('baseline_iv', 'FLOAT'),
           ('baseline_at', 'DATETIME')]


def _column_exists(conn, table, col):
    return any(r[1] == col for r in conn.execute(f"PRAGMA table_info({table})"))


def _table_exists(conn, table):
    cur = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,))
    return cur.fetchone() is not None


def ensure_columns(conn, dry_run=False):
    added = []
    for name, decl in COLUMNS:
        if _column_exists(conn, TABLE, name):
            continue
        added.append(name)
        if not dry_run:
            conn.execute(f"ALTER TABLE {TABLE} ADD COLUMN {name} {decl}")
    if not dry_run:
        conn.commit()
    for name in added:
        print(f"  Added column {TABLE}.{name}")
    if not added:
        print("  Columns already present.")
    return added


def report(conn):
    if not _column_exists(conn, TABLE, 'baseline_spot'):
        print("\n(Nothing to report yet — run without --dry-run to apply.)")
        return
    rows = list(conn.execute(
        f"SELECT name, status, baseline_spot, baseline_sigma, baseline_iv, baseline_at "
        f"FROM {TABLE} ORDER BY name"))
    if not rows:
        print("\nNo groups yet.")
        return
    print(f"\nGroups ({len(rows)}):")
    for name, status, spot, sigma, iv, at in rows:
        if spot is None:
            note = ("no baseline — redeploy to take one"
                    if status in ('deployed', 'triggered') else "not deployed")
        else:
            note = (f"spot {spot:,.2f} ±{sigma or 0:,.0f} "
                    f"(IV {100 * (iv or 0):.1f}%) at {at}")
        print(f"  {name[:24]:<24} {status:<10} {note}")


def main():
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("db", nargs="?", default="oracle.db")
    parser.add_argument("--dry-run", action="store_true",
                        help="report what would change, write nothing")
    args = parser.parse_args()

    print(f"== Group baseline migration on {args.db} ==")
    if args.dry_run:
        print("   (dry run — nothing will be written)\n")

    conn = sqlite3.connect(args.db)
    try:
        if not _table_exists(conn, TABLE):
            print(f"No `{TABLE}` table — start the app once first.")
            return
        print("Schema:")
        ensure_columns(conn, args.dry_run)
        report(conn)
    finally:
        conn.close()
    print("\nDone. Existing deployed groups take a baseline on their next redeploy.")


if __name__ == "__main__":
    main()
