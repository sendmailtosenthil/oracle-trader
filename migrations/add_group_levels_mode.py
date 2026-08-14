"""One-time migration: automatic stoploss / target for a trade group.

Usage (from the project root):

    python -m migrations.add_group_levels_mode [path-to-db] [--dry-run]

Defaults to ``oracle.db``. Safe to re-run.

Why: creating a group used to mean typing two rupee levels along with its name.
They are now a choice — ``levels_mode``:

  fixed   the original behaviour. The user types the stoploss and target and
          nothing else moves them.
  auto    the app manages them from the expected profit — the premium of the open
          legs plus what the closed ones actually made. The opening stoploss is
          that whole figure, the opening target 50% of it; the stoploss then
          trails the profit in steps of at least ``auto_threshold`` rupees and
          never widens, and reaching a target notifies, drops the stoploss to
          break-even and climbs the ladder 50% → 70% → 85%, the last rung
          advising that the trade be closed. Typing over either level moves the
          group to ``fixed`` for good.

This adds:

  ztrade_groups.levels_mode            'fixed' | 'auto'
  ztrade_groups.auto_expected_profit   premium the levels derive from, frozen on
                                       arming
  ztrade_groups.auto_threshold         smallest stoploss step worth taking (300)
  ztrade_groups.auto_anchor_pnl        P&L the stoploss was last set at
  ztrade_groups.auto_target_stage      how many targets have been taken
  ztrade_group_level_events            every level move, in order — the stoploss
                                       journey the dashboard draws

Every existing group is backfilled to ``fixed``, which is exactly what it is: its
levels were typed by hand, and the auto_* columns go unread on it. Nothing else
changes.
"""
import argparse
import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TABLE = 'ztrade_groups'
MODE = 'levels_mode'
DEFAULT = 'fixed'
COLUMNS = [(MODE, f"VARCHAR DEFAULT '{DEFAULT}'"),
           ('auto_expected_profit', 'FLOAT'),
           ('auto_threshold', 'FLOAT DEFAULT 300.0'),
           ('auto_anchor_pnl', 'FLOAT DEFAULT 0.0'),
           ('auto_target_stage', 'INTEGER DEFAULT 0')]

EVENTS = 'ztrade_group_level_events'
EVENTS_DDL = f"""
CREATE TABLE IF NOT EXISTS {EVENTS} (
    id INTEGER NOT NULL PRIMARY KEY,
    group_id INTEGER NOT NULL REFERENCES {TABLE} (id),
    at DATETIME,
    kind VARCHAR NOT NULL,
    stoploss FLOAT,
    target FLOAT,
    pnl FLOAT,
    expected_profit FLOAT,
    note VARCHAR
)
"""
EVENTS_INDEX = (f"CREATE INDEX IF NOT EXISTS ix_{EVENTS}_group_id "
                f"ON {EVENTS} (group_id)")


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


def ensure_events_table(conn, dry_run=False):
    """The level history. Empty to start with — nothing to backfill.

    A group deployed before this existed has no recorded journey, and there is
    none to reconstruct: the levels it held on the way here were never stored.
    Its history starts at its next arming.
    """
    if _table_exists(conn, EVENTS):
        print(f"  Table {EVENTS} already present.")
        return False
    if not dry_run:
        conn.execute(EVENTS_DDL)
        conn.execute(EVENTS_INDEX)
        conn.commit()
    print(f"  Created table {EVENTS}")
    return True


def backfill(conn, dry_run=False):
    """Give every pre-existing group the mode it has always had."""
    if not _column_exists(conn, TABLE, MODE):
        return 0
    cur = conn.execute(
        f"SELECT COUNT(*) FROM {TABLE} WHERE {MODE} IS NULL OR {MODE} = ''")
    n = cur.fetchone()[0]
    if n and not dry_run:
        conn.execute(f"UPDATE {TABLE} SET {MODE} = ? "
                     f"WHERE {MODE} IS NULL OR {MODE} = ''", (DEFAULT,))
        conn.commit()
    print(f"  {n} group(s) set to '{DEFAULT}'." if n
          else "  No groups needed backfilling.")
    return n


def report(conn):
    if not _column_exists(conn, TABLE, MODE):
        print("\n(Nothing to report yet — run without --dry-run to apply.)")
        return
    rows = list(conn.execute(
        f"SELECT name, status, {MODE}, stoploss, target FROM {TABLE} ORDER BY name"))
    if not rows:
        print("\nNo groups yet.")
        return
    print(f"\nGroups ({len(rows)}):")
    for name, status, mode, stoploss, target in rows:
        levels = " / ".join("—" if v is None else f"{v:,.2f}"
                            for v in (stoploss, target))
        print(f"  {name[:24]:<24} {status:<10} {mode or DEFAULT:<6} SL/TGT {levels}")


def main():
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("db", nargs="?", default="oracle.db")
    parser.add_argument("--dry-run", action="store_true",
                        help="report what would change, write nothing")
    args = parser.parse_args()

    print(f"== Group levels-mode migration on {args.db} ==")
    if args.dry_run:
        print("   (dry run — nothing will be written)\n")

    conn = sqlite3.connect(args.db)
    try:
        if not _table_exists(conn, TABLE):
            print(f"No `{TABLE}` table — start the app once first.")
            return
        print("Schema:")
        ensure_columns(conn, args.dry_run)
        ensure_events_table(conn, args.dry_run)
        backfill(conn, args.dry_run)
        report(conn)
    finally:
        conn.close()
    print("\nDone. Existing groups keep their hand-set levels; new ones can opt "
          "into automatic ones.")


if __name__ == "__main__":
    main()
